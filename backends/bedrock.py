"""Bedrock server backend: tmux/screen transport, console.log, save hold/query.

Differences from Java handled here:
  - No RCON: commands are injected via a tmux/screen session (see mux.py) and
    are fire-and-forget. Command *output* is read back from the captured-stdout
    console.log, which the shared LogWatcher tails.
  - Terse log: only join/leave are available (parsed in bot.py); deaths and
    achievements are not emitted by BDS, so they are absent from CAPABILITIES.
  - Save flow: `save hold` -> poll `save query` until the snapshot is ready ->
    copy each listed file truncated to its reported length -> `save resume`.
  - Player data lives in the world LevelDB, not per-player files, so per-player
    restore is unsupported (CAP_PLAYER_RESTORE absent).

Note: BDS console wording and `save query` path formatting vary slightly across
versions. The parsing here is best-effort and tolerant; verify against the
target server's actual output (see the design doc's open questions).
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from backup_utils import RE_FULL, RE_INCR
from .base import (
    ServerBackend, BackendUnavailable, EVENT_JOIN, EVENT_LEAVE,
    CAP_PLAYER_RESTORE,
)
from .mux import detect

# Learned, account-stable xuid -> {name, identities:[MsaId, SelfSignedId]}.
# The data key is per-server random, but identities are stable, so we resolve a
# player by trying their identities against each backup's sidecar mappings.
_PLAYERS_PATH = Path(__file__).resolve().parent.parent / "bedrock_players.json"


def _load_players() -> dict:
    try:
        return json.loads(_PLAYERS_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logging.getLogger("mcnotifier").exception("Failed to read bedrock_players.json")
        return {}


def _save_players(data: dict) -> None:
    try:
        _PLAYERS_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        logging.getLogger("mcnotifier").exception("Failed to write bedrock_players.json")

logger = logging.getLogger("mcnotifier")

# `list` response: "There are 2/10 players online:" followed by a names line.
_RE_LIST_HEADER = re.compile(r'There are (\d+)/\d+ players online:?')
# `save query` readiness marker. The "path:bytes, ..." list follows on the
# next (unprefixed) line(s).
_SAVE_READY = "Files are now ready to be copied"
# BDS log line prefix, e.g. "[2026-06-24 02:03:56:398 INFO] ".
_RE_LOG_PREFIX = re.compile(r'^\[[^\]]*\]\s*')


def _strip_prefix(line: str) -> str:
    return _RE_LOG_PREFIX.sub("", line.strip())


class BedrockBackend(ServerBackend):
    CAPABILITIES = {EVENT_JOIN, EVENT_LEAVE, CAP_PLAYER_RESTORE}

    def __init__(self, config):
        super().__init__(config)
        self._mux = detect(config.mux_session)
        if self._mux is None:
            looked = f" '{config.mux_session}'" if config.mux_session else ""
            raise BackendUnavailable(
                f"no tmux/screen session{looked} is hosting the Bedrock server. "
                "Start the server inside byobu/tmux/screen and set MUX_SESSION.")

    # --- availability / readiness ---
    def is_available(self, log_warnings: bool = False) -> bool:
        # Re-detect so a session that died after startup is noticed.
        self._mux = detect(self.config.mux_session) or self._mux
        ok = self._mux is not None
        if not ok and log_warnings:
            logger.warning("Bedrock: no tmux/screen session available for commands")
        return ok

    def wait_for_ready(self, timeout: float = 120) -> bool:
        """Wait for BDS to report it has started (in console.log)."""
        logger.info("Waiting for Bedrock server to be ready (monitoring %s)...",
                    self.config.log_path.name)
        waiter = self._watcher.expect_line("Server started")
        return waiter.wait(timeout=timeout)

    # --- command transport (fire-and-forget) ---
    def send_command(self, cmd: str) -> str:
        self._mux.send(cmd)
        return ""

    def _send_confirmed(self, cmd: str, success_phrase: str, timeout: float = 30,
                        retries: int = 3, log=None) -> bool:
        """Send ``cmd`` and wait for ``success_phrase`` in the console.

        BDS injection over tmux/screen can occasionally drop characters (we have
        seen ``save hold`` arrive as ``ave``), which BDS reports as
        ``Unknown command``. If we see that — or nothing within ``timeout`` —
        resend, up to ``retries`` times. Returns True once confirmed.
        """
        for attempt in range(1, retries + 1):
            ok = self._watcher.expect_line(success_phrase)
            err = self._watcher.expect_line("Unknown command")
            self.send_command(cmd)
            deadline = time.time() + timeout
            confirmed = False
            while time.time() < deadline:
                if ok.wait(0.25):
                    confirmed = True
                    break
                if err.triggered():  # garbled command echoed back by BDS
                    break
            self._watcher.cancel(ok)
            self._watcher.cancel(err)
            if confirmed:
                return True
            if attempt < retries and log:
                log(f"'{cmd}' not confirmed (attempt {attempt}/{retries}), retrying")
        return False

    # --- backup freeze/flush ---
    def begin_save(self, log_fn=None) -> None:
        def log(msg):
            if log_fn:
                log_fn(msg)
        log("Holding save...")
        if self._send_confirmed("save hold", "Saving...", retries=2, log=log):
            log("Save held")
            return
        # Not confirmed — a previous backup may have left a stale hold. Clear it
        # with a resume, then try once more.
        log("Clearing any stale save-hold, retrying...")
        self.send_command("save resume")
        time.sleep(1)
        if self._send_confirmed("save hold", "Saving...", retries=2, log=log):
            log("Save held")
        else:
            log("Warning: 'save hold' not confirmed after retries, proceeding anyway")

    def files_ready(self, log_fn=None):
        """Poll `save query` until the snapshot is ready; return [(path, bytes)].

        Each file must be copied truncated to its reported byte length — BDS
        keeps writing past the snapshot point, and only the first N bytes of
        each file are part of the consistent snapshot.
        """
        def log(msg):
            if log_fn:
                log_fn(msg)
        log("Querying save state...")
        for _ in range(60):  # ~60 s budget
            self.send_command("save query")
            time.sleep(1)
            files = self._scan_query_output()
            if files is not None:
                log(f"Snapshot ready: {len(files)} file(s)")
                return files
        raise RuntimeError("'save query' did not report ready within 60 s")

    def end_save(self, log_fn=None) -> None:
        def log(msg):
            if log_fn:
                log_fn(msg)
        if self._send_confirmed("save resume", "are resumed", retries=3, log=log):
            log("Save resumed")
        else:
            log("Warning: 'save resume' not confirmed after retries")

    # --- server lifecycle (per-player restore: stop -> edit db -> relaunch) ---
    def stop_server(self, log_fn=None) -> bool:
        """Request a stop and confirm BDS acknowledged it. Actual shutdown (lock
        release) is confirmed separately by wait_for_db_unlock()."""
        def log(msg):
            if log_fn:
                log_fn(msg)
        ok = self._send_confirmed("stop", "Server stop requested", retries=3, log=log)
        log("Stop requested" if ok else "Warning: 'stop' not confirmed")
        return ok

    def wait_for_db_unlock(self, timeout: float = 120) -> bool:
        """Poll until the world db LevelDB lock is released (server fully down)."""
        import bedrock_player
        db_path = bedrock_player.world_db_path(self.config.minecraft_dir)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not bedrock_player.is_db_locked(db_path):
                return True
            time.sleep(1)
        return False

    def relaunch(self, log_fn=None) -> bool:
        """Send MUX_START_CMD to the mux window and wait for 'Server started'."""
        def log(msg):
            if log_fn:
                log_fn(msg)
        cmd = self.config.mux_start_cmd
        if not cmd:
            log("No MUX_START_CMD configured; cannot relaunch the server")
            return False
        for attempt in range(1, 4):
            waiter = self._watcher.expect_line("Server started")
            self.send_command(cmd)
            if waiter.wait(timeout=120):
                log("Server relaunched")
                return True
            self._watcher.cancel(waiter)
            if attempt < 3:
                log(f"Relaunch not confirmed (attempt {attempt}/3), retrying")
        return False

    # --- server-side queries ---
    def query_online_players(self) -> list[str]:
        self.send_command("list")
        time.sleep(1)
        return self._scan_list_output()

    def is_player_online(self, username: str) -> bool:
        target = username.lower()
        return any(n.lower() == target for n in self.query_online_players())

    # --- per-player restore (stop -> edit world LevelDB -> relaunch) ---
    def learn_player(self, name: str, xuid: str, idents: list) -> bool:
        """Persist a learned xuid -> identities binding (account-stable). Returns
        True if newly learned/changed."""
        players = _load_players()
        if players.get(xuid, {}).get("identities") == idents:
            return False
        players[xuid] = {"name": name, "identities": idents}
        _save_players(players)
        return True

    def player_identities(self, xuid: str) -> list:
        return _load_players().get(xuid, {}).get("identities", [])

    def list_player_versions(self, player_id: str, chain: tuple) -> list:
        """Restore points for a player from each backup zip's sidecar (deduped
        by construction — a player only appears in zips where they changed)."""
        idents = self.player_identities(player_id)
        if not idents:
            return []
        try:
            import bedrock_player
        except Exception:
            return []
        chain_id, base_full = chain
        versions = []
        if not self.config.backup_dir.exists():
            return []
        for f in self.config.backup_dir.iterdir():
            if not f.is_file() or f.suffix != ".zip":
                continue
            ts_str = label = None
            m_incr = RE_INCR.match(f.name)
            if m_incr:
                ts_str = m_incr.group(3)
                cur = chain_id and m_incr.group(2) == chain_id
                label = "incremental backup" if cur else "incremental backup (old chain)"
            else:
                m_full = RE_FULL.match(f.name)
                if m_full:
                    ts_str = m_full.group(2)
                    cur = chain_id and f.name == base_full
                    label = "full backup" if cur else "full backup (old chain)"
            if ts_str is None:
                continue
            sidecar = bedrock_player.read_sidecar(f)
            if not any(bedrock_player.resolve_from_sidecar(sidecar, i) for i in idents):
                continue
            try:
                ts_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            versions.append({
                "timestamp": ts_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "sort_key": ts_dt.timestamp(), "source": label,
                "path": f, "idents": idents,
            })
        versions.sort(key=lambda v: v["sort_key"], reverse=True)
        return versions

    def restore_player(self, username: str, player_id: str, version: dict,
                       status_cb=None) -> None:
        """Stop the server, overwrite the player's value in the world LevelDB
        from the chosen backup, relaunch. Fail-safe: everything that can fail is
        done before the stop, and a relaunch failure alerts loudly."""
        import bedrock_player

        def status(msg):
            logger.info("RestorePlayer(BR): %s", msg)
            if status_cb:
                status_cb(msg)

        idents = version["idents"]
        stopped = db_unlocked = False
        try:
            # 1. Read the historical value (server untouched if this fails).
            sidecar = bedrock_player.read_sidecar(version["path"])
            resolved = None
            for ident in idents:
                resolved = bedrock_player.resolve_from_sidecar(sidecar, ident)
                if resolved:
                    break
            if not resolved:
                status("Could not read player data from the selected backup.")
                return
            backup_key, value = resolved

            # 2. Player must be offline (server still up).
            try:
                if self.is_player_online(username):
                    status(f"{username} is online — log them out first.")
                    return
                status(f"{username} is offline")
            except Exception as e:
                status(f"Online check failed: {e}")
                return

            # 3. Stop the server and wait for the db lock to release.
            status("Stopping server...")
            self.stop_server(status)
            stopped = True
            if not self.wait_for_db_unlock(timeout=120):
                status("⚠️ Server did not release the world db in time. "
                       "Aborting; check the server manually.")
                return
            db_unlocked = True
            status("Server stopped; world db unlocked")

            # 4. Resolve the live data key (per-server), back it up, overwrite.
            db_path = bedrock_player.world_db_path(self.config.minecraft_dir)
            live_key = None
            db = bedrock_player.open_db(db_path)
            try:
                for ident in idents:
                    live_key = bedrock_player.lookup_server_key(db, ident)
                    if live_key:
                        break
            finally:
                db.close()
            live_key = live_key or backup_key
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            undo = self.config.backup_dir / f"{live_key}.pre-restore-{ts}.nbt"
            bedrock_player.backup_player_value(db_path, live_key, undo)
            status(f"Saved current data as {undo.name}")
            bedrock_player.write_player_value(db_path, live_key, value)
            status(f"Restored {username} to {version['timestamp']} "
                   f"({len(value)} bytes)")
        except Exception as e:
            logger.exception("Bedrock player restore failed")
            status(f"Restore failed: {e}")
        finally:
            if db_unlocked:
                status("Relaunching server...")
                if self.relaunch(status):
                    status("✅ Server is back up.")
                else:
                    status("⚠️ Server FAILED to relaunch — start it "
                           f"manually:\n  {self.config.mux_start_cmd}")
            elif stopped:
                status("⚠️ Sent stop but couldn't confirm shutdown — NOT "
                       "relaunching (avoids a double start). Check the server.")

    # --- console.log parsing helpers ---
    def _tail(self, max_bytes: int = 65536) -> list[str]:
        """Return the last lines of console.log (empty if missing)."""
        try:
            with open(self.config.log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                data = f.read()
            return data.decode("utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            logger.exception("Bedrock: failed to read %s", self.config.log_path)
            return []

    def _scan_query_output(self):
        """Find the most recent `save query` result. Returns [(Path, bytes)] when
        the snapshot is ready, else None.

        Console layout (the list is on its own unprefixed line after the marker):
            [.. INFO] Data saved. Files are now ready to be copied.
            Bedrock level/db/003678.ldb:2117709, Bedrock level/level.dat:3042, ...
        Paths can contain spaces and slashes, so each comma-separated token is
        split on its LAST colon (path : byte-length). Paths are relative to the
        world's parent (the `worlds/` directory).
        """
        lines = self._tail()
        idx = None
        for i in range(len(lines) - 1, -1, -1):
            if _SAVE_READY in lines[i]:
                idx = i
                break
        if idx is None:
            return None
        # The listing is the unprefixed line(s) after the marker; a new log
        # message (starts with "[") or a blank line ends it.
        result = []
        for line in lines[idx + 1:]:
            s = line.strip()
            if not s or s.startswith("["):
                break
            for token in s.split(","):
                token = token.strip()
                if not token:
                    continue
                rel, sep, length = token.rpartition(":")
                if not sep or not length.strip().isdigit():
                    continue
                path = self._resolve_listed_path(rel.strip())
                if path is not None:
                    result.append((path, int(length.strip())))
        return result or None

    def _resolve_listed_path(self, rel: str):
        """Resolve a `save query` path (e.g. "Bedrock level/db/003678.ldb",
        relative to the worlds/ directory) to an absolute path under
        minecraft_dir."""
        mc = self.config.minecraft_dir
        for base in (mc / "worlds", mc):
            p = base / rel
            if p.exists():
                return p
        logger.warning("Bedrock: save query listed missing file: %s", rel)
        return None

    def _scan_list_output(self) -> list[str]:
        """Parse the most recent `list` response from console.log."""
        lines = self._tail()
        for i in range(len(lines) - 1, -1, -1):
            stripped = _strip_prefix(lines[i])
            m = _RE_LIST_HEADER.search(stripped)
            if m:
                if int(m.group(1)) == 0:
                    return []
                # Names follow the ':' on this line, or on the next line.
                after = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                if not after and i + 1 < len(lines):
                    after = _strip_prefix(lines[i + 1])
                return [n.strip() for n in after.split(", ") if n.strip()]
        return []
