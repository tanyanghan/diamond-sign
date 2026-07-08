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
import threading
import time
from datetime import datetime

from utils.backup_utils import RE_FULL, RE_INCR
from .base import (
    ServerBackend, BackendUnavailable, EVENT_JOIN, EVENT_LEAVE, EVENT_DEATH,
    CAP_PLAYER_RESTORE, CAP_STATS,
)
from .mux import detect

logger = logging.getLogger("diamondsign")

# Portable player list / name registry. xuid -> {name, identities:[MsaId,
# SelfSignedId], first_seen, last_seen}. Identities are account-stable (the data
# key is per-server random), so this file can be copied to another instance.
# bedrock_players.json and statistics.json now live under data/<server-name>/
# (paths set per-instance in __init__), so two Bedrock servers in one process
# can't clobber each other's player list / stats. The player list is still
# portable (identities are account-stable; the data key is per-server random).
_players_lock = threading.Lock()
_stats_lock = threading.Lock()

# Serialises response-capturing commands so two concurrent captures can't read
# each other's console.log output.
_capture_lock = threading.Lock()


def _load_players(path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed to read bedrock_players.json")
        return {}


def _save_players(path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        logger.exception("Failed to write bedrock_players.json")


def _load_stats(path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed to read statistics.json")
        return {}


def _save_stats(path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        logger.exception("Failed to write statistics.json")

# `list` response: "There are 2/10 players online:" followed by a names line.
_RE_LIST_HEADER = re.compile(r'There are (\d+)/\d+ players online:?')
# `save query` readiness marker. The "path:bytes, ..." list follows on the
# next (unprefixed) line(s).
_SAVE_READY = "Files are now ready to be copied"
# BDS log line prefix, e.g. "[2026-06-24 02:03:56:398 INFO] ".
_RE_LOG_PREFIX = re.compile(r'^\[[^\]]*\]\s*')


def _strip_prefix(line: str) -> str:
    return _RE_LOG_PREFIX.sub("", line.strip())


# BDS emits structured command output wrapped in ###* {json} *### markers (e.g.
# `allowlist list`), where the closing *### sits on its own unprefixed line.
_RE_STRUCTURED = re.compile(r'###\*\s*(.*?)\s*\*###', re.DOTALL)


def _format_structured(payload: str):
    """Render a BDS structured-JSON payload into readable text, or None if it
    isn't a shape we special-case."""
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    if obj.get("command") == "allowlist" and isinstance(obj.get("result"), list):
        names = [e.get("name", "?") for e in obj["result"]]
        if not names:
            return "The allowlist is empty."
        noun = "entry" if len(names) == 1 else "entries"
        return f"There are {len(names)} allowlist {noun}: " + ", ".join(names)
    return None


def _format_console_response(text: str) -> str:
    """Turn raw captured console output into a clean reply.

    Handles the ###* {json} *### structured block (e.g. `allowlist list`),
    otherwise keeps the prefixed server lines and drops the echoed command and
    bare marker lines.
    """
    m = _RE_STRUCTURED.search(text)
    if m:
        payload = " ".join(_strip_prefix(ln) for ln in m.group(1).splitlines()
                           if ln.strip())
        formatted = _format_structured(payload)
        if formatted is not None:
            return formatted
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or not _RE_LOG_PREFIX.match(ln):
            continue  # echoed command / blank / bare marker line
        stripped = _strip_prefix(ln)
        if stripped in ("###*", "*###") or not stripped:
            continue
        if "DIAMONDSIGN " in stripped:
            continue  # behavior-pack event marker (async; not command output)
        lines.append(stripped)
    return "\n".join(lines)


class BedrockBackend(ServerBackend):
    CAPABILITIES = {EVENT_JOIN, EVENT_LEAVE, CAP_PLAYER_RESTORE, CAP_STATS}
    ALLOWLIST_VERB = "allowlist"  # Bedrock's name for Java's "whitelist"

    def __init__(self, config):
        super().__init__(config)
        self.players_path = self._data_path("bedrock_players.json")
        self.stats_path = self._data_path("statistics.json")
        self._mux = detect(config.mux_session)
        if self._mux is None:
            looked = f" '{config.mux_session}'" if config.mux_session else ""
            raise BackendUnavailable(
                f"no tmux/screen session{looked} is hosting the Bedrock server. "
                "Start the server inside byobu/tmux/screen and set MUX_SESSION.")
        # The bedrock_pack behavior pack supplies death events, so /deaths and
        # death announcements become available when it's enabled.
        if config.bedrock_script_events:
            self.CAPABILITIES = self.CAPABILITIES | {EVENT_DEATH}

    # --- availability / readiness ---
    def is_available(self, log_warnings: bool = False) -> bool:
        # Re-detect so a session that died after startup is noticed.
        self._mux = detect(self.config.mux_session) or self._mux
        ok = self._mux is not None
        if not ok and log_warnings:
            logger.warning("Bedrock: no tmux/screen session available for commands")
        return ok

    def is_online(self) -> bool:
        """True if the server is running and responding on the console.

        Sends ``list`` and checks whether BDS echoed anything into console.log —
        a running server always responds (even with zero players); a stopped one
        produces nothing. This is authoritative and side-effect-free, unlike
        probing the world LevelDB lock (whose behaviour is BDS-version-dependent,
        depends on the level-name path resolving to the *running* world, and is
        unsafe to open read-write against a live server)."""
        try:
            return bool(self.capture_command("list", timeout=3.0).strip())
        except Exception:
            return False

    # --- command transport (fire-and-forget) ---
    def send_command(self, cmd: str) -> str:
        self._mux.send(cmd)
        return ""

    def capture_command(self, full_cmd: str, timeout: float = 3.0) -> str:
        """Send a command and read its response back from console.log.

        BDS injection is fire-and-forget, so we record the log's current end,
        send the command, then poll until the new content stops growing (or the
        timeout elapses). Only prefixed server lines (``[.. INFO] …``) are kept —
        the bare echo of the typed command is dropped.
        """
        log_path = self.config.log_path
        with _capture_lock:
            try:
                start = log_path.stat().st_size
            except OSError:
                start = 0
            self.send_command(full_cmd)
            deadline = time.time() + timeout
            data = b""
            while time.time() < deadline:
                time.sleep(0.3)
                try:
                    with open(log_path, "rb") as f:
                        f.seek(start)
                        new = f.read()
                except OSError:
                    new = b""
                if new and new == data:
                    break  # output has settled -> response complete
                data = new
        return _format_console_response(data.decode("utf-8", errors="replace"))

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
            return
        # Still nothing. Before flooding the console with `save query` polls,
        # distinguish "server is slow" from "server is not running at all": if
        # the pane is back at a shell prompt, every injected line is being typed
        # into the SHELL, not the server — abort the backup instead.
        if self.probe_stopped(timeout=8) is True:
            raise RuntimeError("server is not running (its console is at a "
                               "shell prompt) — cannot take a live backup")
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
    @property
    def can_restart(self) -> bool:
        """Bedrock always runs under a mux (required at construction); it can
        relaunch as long as a start command is set (synthesized by default)."""
        return bool(self.config.mux_start_cmd)

    # BDS appends this line to the console as the final step of a clean
    # shutdown, after the world is saved and closed — the safe-to-edit signal.
    _SHUTDOWN_MARKER = "Quit correctly"

    def stop_server(self, log_fn=None) -> bool:
        """Request a stop and confirm BDS acknowledged it. Actual shutdown is
        confirmed separately by wait_until_stopped().

        Registers the shutdown-complete waiter BEFORE sending stop: on a small
        world BDS prints ``Quit correctly`` within a fraction of a second, so a
        waiter registered afterwards would miss it. wait_until_stopped() blocks
        on this waiter."""
        def log(msg):
            if log_fn:
                log_fn(msg)
        self._shutdown_waiter = (self._watcher.expect_line(self._SHUTDOWN_MARKER)
                                 if self._watcher else None)
        ok = self._send_confirmed("stop", "Server stop requested", retries=3, log=log)
        log("Stop requested" if ok else "Warning: 'stop' not confirmed")
        return ok

    def wait_until_stopped(self, timeout: float = 120) -> bool:
        """Wait until BDS has fully shut down — safe to edit the world db.

        Primary signal: the ``Quit correctly`` line BDS appends to the console
        (captured in console.log) as the last step of a clean shutdown, after
        the world is saved and closed. The waiter was registered in
        stop_server() *before* the stop was sent, so a fast shutdown isn't
        missed. Console silence is NOT a usable signal on its own — after "stop"
        BDS prints "Stopping server..." then goes quiet while it flushes and
        exits, so a quiet console can still mean a live server holding the db.

        Deliberately does NOT use the LevelDB lock: this BDS build never locks
        the world db (there is no LOCK file even while it runs), so it always
        looks free. If no LogWatcher is attached (unusual), fall back to
        confirming the mux pane has returned to a shell prompt (see
        ``_confirm_console_free``).
        """
        waiter = getattr(self, "_shutdown_waiter", None)
        self._shutdown_waiter = None
        if waiter is not None:
            if waiter.wait(timeout):
                time.sleep(2)   # settle: let the process finish exiting
                return True
            if self._watcher:
                self._watcher.cancel(waiter)
            return False

        # Fallback (no LogWatcher): confirm the console pane is free again.
        return bool(self._confirm_console_free(timeout))

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

    # --- name registry / portable player list (bedrock_players.json) ---
    def load_names(self) -> dict:
        return {xuid: e["name"] for xuid, e in _load_players(self.players_path).items()
                if e.get("name")}

    def register_name(self, player_id: str, name: str) -> bool:
        """Merge name + first/last_seen for an xuid (called on every connect).
        Returns True if the name was created/changed."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _players_lock:
            data = _load_players(self.players_path)
            entry = data.get(player_id, {})
            changed = entry.get("name") != name
            entry["name"] = name
            entry.setdefault("first_seen", now)
            entry["last_seen"] = now
            data[player_id] = entry
            _save_players(self.players_path, data)
            return changed

    def learn_player(self, name: str, xuid: str, idents: list) -> bool:
        """Merge a learned xuid -> identities binding (account-stable), preserving
        name/timestamps. Returns True if the identities were newly learned."""
        with _players_lock:
            data = _load_players(self.players_path)
            entry = data.get(xuid, {})
            if entry.get("identities") == idents:
                return False
            entry["identities"] = idents
            if name and not entry.get("name"):
                entry["name"] = name
            data[xuid] = entry
            _save_players(self.players_path, data)
            return True

    def player_identities(self, xuid: str) -> list:
        return _load_players(self.players_path).get(xuid, {}).get("identities", [])

    def known_identities(self) -> set:
        out = set()
        for e in _load_players(self.players_path).values():
            out.update(e.get("identities", []))
        return out

    def list_known_players(self, names: dict) -> list:
        return sorted(e["name"] for e in _load_players(self.players_path).values() if e.get("name"))

    # --- online-time stats (statistics.json, accumulated from join/leave) ---
    def record_player_session(self, event_type: str, player_id: str,
                              now: float | None = None) -> None:
        if not player_id:
            return
        now = time.time() if now is None else now
        with _stats_lock:
            data = _load_stats(self.stats_path)
            entry = data.get(player_id) or {"total_seconds": 0, "sessions": 0,
                                            "open_since": None}
            nm = _load_players(self.players_path).get(player_id, {}).get("name")
            if nm:
                entry["name"] = nm
            if event_type == EVENT_JOIN:
                entry["open_since"] = now  # overwrites any stale value
            elif event_type == EVENT_LEAVE:
                start = entry.get("open_since")
                if start:
                    entry["total_seconds"] = entry.get("total_seconds", 0) + max(0, now - start)
                    entry["sessions"] = entry.get("sessions", 0) + 1
                entry["open_since"] = None
            data[player_id] = entry
            _save_stats(self.stats_path, data)

    def reset_open_sessions(self) -> None:
        """Clear any open_since left dangling by a crash (called at startup
        before re-opening sessions for currently-online players)."""
        with _stats_lock:
            data = _load_stats(self.stats_path)
            touched = False
            for entry in data.values():
                if entry.get("open_since") is not None:
                    entry["open_since"] = None
                    touched = True
            if touched:
                _save_stats(self.stats_path, data)

    def close_open_sessions(self, now: float | None = None) -> None:
        """Flush open sessions (graceful shutdown)."""
        now = time.time() if now is None else now
        with _stats_lock:
            data = _load_stats(self.stats_path)
            touched = False
            for entry in data.values():
                start = entry.get("open_since")
                if start:
                    entry["total_seconds"] = entry.get("total_seconds", 0) + max(0, now - start)
                    entry["sessions"] = entry.get("sessions", 0) + 1
                    entry["open_since"] = None
                    touched = True
            if touched:
                _save_stats(self.stats_path, data)

    def checkpoint_open_sessions(self, now: float | None = None) -> None:
        """Bank elapsed time for open sessions but keep them open (open_since
        advances to now). Unlike close_open_sessions this does NOT count a
        session — it's a durability checkpoint, not a sign-off. Idempotent."""
        now = time.time() if now is None else now
        with _stats_lock:
            data = _load_stats(self.stats_path)
            touched = False
            for entry in data.values():
                start = entry.get("open_since")
                if start:
                    entry["total_seconds"] = entry.get("total_seconds", 0) + max(0, now - start)
                    entry["open_since"] = now
                    touched = True
            if touched:
                _save_stats(self.stats_path, data)

    def player_stats(self, names: dict) -> list:
        # Persist running time so the displayed value survives a later crash
        # and /stats can't show a number that isn't on disk.
        self.checkpoint_open_sessions()
        players = _load_players(self.players_path)
        result = []
        for xuid, e in _load_stats(self.stats_path).items():
            secs = e.get("total_seconds", 0)
            if e.get("open_since"):  # include the in-progress session
                secs += max(0, time.time() - e["open_since"])
            result.append({
                "name": e.get("name") or players.get(xuid, {}).get("name")
                or names.get(xuid, xuid),
                "time_played_hours": round(secs / 3600, 2),
                "sessions": e.get("sessions", 0),
                "last_seen": players.get(xuid, {}).get("last_seen", ""),
            })
        return result

    # --- per-player restore (stop -> edit world LevelDB -> relaunch) ---

    def list_player_versions(self, player_id: str, chain: tuple) -> list:
        """Restore points for a player from each backup zip's sidecar (deduped
        by construction — a player only appears in zips where they changed)."""
        idents = self.player_identities(player_id)
        if not idents:
            return []
        try:
            from utils import bedrock_player
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
        from utils import bedrock_player

        def status(msg):
            logger.info("[%s] RestorePlayer: %s", self.config.name, msg)
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

            # 2+3. Establish server state BEFORE injecting anything: if it is
            # already down (crashed, or an admin killed a hung shutdown by
            # hand), the pane is a shell prompt and `list`/`stop` typed there
            # would run as shell commands. Already down also means the world db
            # is already free — skip the online check and the stop.
            if self.probe_stopped(timeout=10) is True:
                status("Server is already stopped; world db free")
                stopped = db_unlocked = True
            else:
                # Player must be offline (server still up).
                try:
                    if self.is_player_online(username):
                        status(f"{username} is online — log them out first.")
                        return
                    status(f"{username} is offline")
                except Exception as e:
                    status(f"Online check failed: {e}")
                    return

                # Stop the server and wait for the db lock to release.
                status("Stopping server...")
                self.stop_server(status)
                stopped = True
                if not self.wait_until_stopped(timeout=120):
                    status("⚠️ Server did not fully stop in time. "
                           "Aborting; check the server manually.")
                    return
                db_unlocked = True
                status("Server stopped cleanly; world db free")

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
