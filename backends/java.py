"""Java server backend: RCON transport, verbose latest.log, per-player .dat.

This wraps the behaviour Diamond Sign shipped before edition support was added,
behind the ``ServerBackend`` interface. Functionally it is the original RCON
code path (``rcon_command``, the save-off/save-all/save-on dance,
``_wait_for_rcon_ready``, ``_validate_server_properties``, the ``list`` parsing)
relocated unchanged.
"""

import gzip
import json
import logging
import os
import re
import shutil
import signal
import socket
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from mcrcon import MCRcon


def _neutralize_rcon_alarm() -> None:
    """mcrcon's _read() arms signal.alarm(timeout) around every recv, and its
    __init__ installs the SIGALRM handler. We bypass __init__ (signal.signal
    can't run in a worker thread), so the handler is never installed and a
    blocked read would fire SIGALRM with its default disposition — terminating
    the whole process ("Alarm clock"). Ignore SIGALRM so a stray alarm can't
    kill us; recv is instead bounded by an explicit socket timeout (see
    send_command)."""
    try:
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
    except (ValueError, OSError):
        pass  # not the main thread — send_command's socket timeout is the bound

from utils.backup_utils import RE_FULL, RE_INCR, wait_for_settle
from utils.config import read_server_properties, get_level_name, backup_exclude_names
from .base import (
    ServerBackend, EVENT_JOIN, EVENT_LEAVE, EVENT_DEATH, EVENT_ACHIEVEMENT,
    CAP_PLAYER_RESTORE, CAP_STATS,
)
from .mux import detect

logger = logging.getLogger("diamondsign")

# Response to the `list` command: "There are X of a max of Y players online: a, b"
_RE_LIST = re.compile(r'There are \d+ of a max of \d+ players online:(.*)')

# Java name registry: UUID -> name, learned from server logs. The file
# (player_names.json) lives under data/<server-name>/ — path set per-instance.
_names_lock = threading.Lock()


def _ticks_to_hours(ticks: int) -> float:
    return round(ticks / 20 / 3600, 2)


def _cm_to_km(cm: int) -> float:
    return round(cm / 100000, 2)


class JavaBackend(ServerBackend):
    CAPABILITIES = {EVENT_JOIN, EVENT_LEAVE, EVENT_DEATH, EVENT_ACHIEVEMENT,
                    CAP_PLAYER_RESTORE, CAP_STATS}

    def __init__(self, config, migrate_legacy=False):
        super().__init__(config, migrate_legacy)
        self.names_path = self._data_path("player_names.json")
        # Constructed in the main thread (main() startup), so we can safely
        # disarm mcrcon's process-killing SIGALRM here.
        _neutralize_rcon_alarm()
        # Optional mux — only needed to RESTART the JVM for a world restore
        # (RCON dies with the server, so the start command must be typed into the
        # session hosting it). Absent for a normal Java install; when unset the
        # world-restore command is refused (see can_restart). Never fatal.
        self._mux = detect(config.mux_session) if config.mux_session else None

    # --- availability / readiness ---
    def is_available(self, log_warnings: bool = False) -> bool:
        """RCON usable only if a password is set and server.properties agrees."""
        if not self.config.rcon_password:
            return False
        warnings = self._validate_server_properties()
        if warnings:
            if log_warnings:
                for w in warnings:
                    logger.warning(w)
                logger.warning("RCON commands disabled due to server.properties issues")
            return False
        return True

    def _validate_server_properties(self) -> list:
        props_path = self.config.minecraft_dir / "server.properties"
        if not props_path.exists():
            return [f"server.properties not found at {props_path}"]

        props = read_server_properties(self.config.minecraft_dir)
        warnings = []
        if props.get("enable-rcon", "false").lower() != "true":
            warnings.append("server.properties: enable-rcon is not set to true")
        server_port = props.get("rcon.port", str(self.config.rcon_port))
        if server_port != str(self.config.rcon_port):
            warnings.append(f"server.properties: rcon.port is {server_port}, "
                            f"but bot is configured to use {self.config.rcon_port}")
        if props.get("rcon.password", "") != self.config.rcon_password:
            warnings.append("server.properties: rcon.password does not match "
                            "RCON_PASSWORD from .env")
        return warnings

    def is_online(self) -> bool:
        """True if the RCON port is accepting connections — i.e. the JVM is up.
        A plain TCP connect (no RCON round-trip), so it can't hang."""
        try:
            with socket.create_connection(
                    (self.config.rcon_host, self.config.rcon_port), timeout=2):
                return True
        except OSError:
            return False

    # --- command transport ---
    def send_command(self, cmd: str) -> str:
        """Send an RCON command and return the response.

        MCRcon.__init__ uses signal.SIGALRM which fails in non-main threads, so
        we bypass __init__ and set the object up manually.
        """
        mcr = MCRcon.__new__(MCRcon)
        mcr.host = self.config.rcon_host
        mcr.password = self.config.rcon_password
        mcr.port = self.config.rcon_port
        mcr.tlsmode = 0
        mcr.timeout = 5
        mcr.connect()
        try:
            # Bound recv in THIS thread so a slow/dead server can't hang the read
            # (mcrcon's own signal.alarm timeout is neutralized — see
            # _neutralize_rcon_alarm).
            if mcr.socket is not None:
                mcr.socket.settimeout(mcr.timeout)
            return mcr.command(cmd)
        finally:
            mcr.disconnect()

    # --- server lifecycle (world restore: stop -> replace files -> start) ---
    @property
    def can_restart(self) -> bool:
        """Java can stop/restart only if it runs under a mux with a start command
        (RCON alone can stop but not relaunch the JVM)."""
        return bool(self._mux and self.config.mux_start_cmd)

    def stop_server(self, log_fn=None) -> bool:
        """Issue the RCON ``stop`` command. The RCON link drops as the JVM shuts
        down (so the call may error mid-shutdown — that's fine); actual shutdown
        is confirmed by wait_until_stopped()."""
        def log(msg):
            if log_fn:
                log_fn(msg)
        try:
            self.send_command("stop")
        except Exception:
            pass  # connection dropped as the server went down
        log("Stop requested")
        return True

    def wait_until_stopped(self, timeout: float = 120) -> bool:
        """Poll the RCON port with a plain TCP connect until it stops accepting —
        i.e. the JVM (and its RCON listener) is gone. Deliberately does NOT send
        an RCON command: a shutting-down server can accept the socket but never
        reply, blocking the read (mcrcon would fire its process-killing alarm).
        A bare connect probe can't hang and needs no command round-trip."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(
                        (self.config.rcon_host, self.config.rcon_port),
                        timeout=2):
                    pass  # port still accepting -> server still up
            except OSError:
                return True  # refused / unreachable -> server is down
            time.sleep(1)
        return False

    def relaunch(self, log_fn=None) -> bool:
        """Type the start command into the mux session ONCE, then wait
        (activity-aware) for the server to accept RCON.

        Sends once and confirms readiness via wait_until_online() — which polls
        the RCON port and extends while latest.log keeps growing. This is
        rotation-proof (a restart rotates latest.log) and never re-types the
        start command into a now-running console, which previously produced
        'Unknown command' spam and a false 'relaunch not confirmed'."""
        def log(msg):
            if log_fn:
                log_fn(msg)
        if not self.can_restart:
            log("No mux/start command configured; cannot relaunch the server")
            return False
        if self.is_online():
            return True  # already up — don't type the start cmd into the console
        self._mux.send(self.config.mux_start_cmd)
        log("Start command sent; waiting for the server to come up...")
        if self.wait_until_online(log_fn):
            log("Server relaunched")
            return True
        return False

    # --- backup freeze/flush ---
    def begin_save(self, log_fn=None) -> None:
        """Disable auto-save and flush pending world data to disk."""
        def log(msg):
            if log_fn:
                log_fn(msg)

        log("Disabling auto-save...")
        waiter = self._watcher.expect_line("Automatic saving is now disabled")
        self.send_command("save-off")
        if waiter.wait(timeout=30):
            log("Auto-save disabled")
        else:
            log("Warning: save-off confirmation not seen in log, proceeding anyway")

        log("Saving world...")
        waiter = self._watcher.expect_line("Saved the game")
        self.send_command("save-all")
        if waiter.wait(timeout=120):
            log("World save complete")
        else:
            log("Warning: save-all confirmation not seen in log, proceeding anyway")

    def files_ready(self, log_fn=None):
        """Java has no snapshot manifest — caller settles then copies the dir."""
        return None

    def end_save(self, log_fn=None) -> None:
        """Re-enable auto-save."""
        def log(msg):
            if log_fn:
                log_fn(msg)

        waiter = self._watcher.expect_line("Automatic saving is now enabled")
        self.send_command("save-on")
        if waiter.wait(timeout=30):
            log("Auto-save re-enabled")
        else:
            log("Warning: save-on confirmation not seen in log")

    # --- server-side queries ---
    def query_online_players(self) -> list[str]:
        resp = self.send_command("list")
        m = _RE_LIST.match(resp)
        if not m:
            return []
        names_part = m.group(1).strip()
        if not names_part:
            return []
        return [n.strip() for n in names_part.split(", ") if n.strip()]

    def is_player_online(self, username: str) -> bool:
        target = username.lower()
        return any(n.lower() == target for n in self.query_online_players())

    # --- name registry (player_names.json, keyed by UUID) ---
    def load_names(self) -> dict:
        if self.names_path.exists():
            try:
                with open(self.names_path) as f:
                    return json.load(f)
            except Exception:
                logger.exception("Failed to load player_names.json")
        return {}

    def register_name(self, player_id: str, name: str) -> bool:
        with _names_lock:
            data = self.load_names()
            if data.get(player_id) == name:
                return False
            data[player_id] = name
            try:
                with open(self.names_path, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                logger.exception("Failed to write player_names.json")
            return True

    # --- stats (read the world's per-player stat files) ---
    def _stats_dir(self) -> Path:
        """Per-player stats dir. 26.1+ moved <world>/stats to
        <world>/players/stats; prefer the new layout, fall back to old."""
        world = self.config.minecraft_dir / get_level_name(self.config.minecraft_dir)
        new_path = world / "players" / "stats"
        old_path = world / "stats"
        if new_path.is_dir():
            return new_path
        if old_path.is_dir():
            return old_path
        return new_path

    def list_known_players(self, names: dict) -> list:
        stats_dir = self._stats_dir()
        if not stats_dir.exists():
            return []
        return sorted(names.get(f.stem, f.stem) for f in stats_dir.glob("*.json"))

    def player_stats(self, names: dict) -> list:
        stats_dir = self._stats_dir()
        if not stats_dir.exists():
            return []
        result = []
        for stat_file in stats_dir.glob("*.json"):
            try:
                with open(stat_file) as f:
                    data = json.load(f)
            except Exception:
                logger.exception("Failed to read stats file %s", stat_file)
                continue
            uuid = stat_file.stem
            name = names.get(uuid, uuid)
            stats = data.get("stats", {})
            custom = stats.get("minecraft:custom", {})
            mined = stats.get("minecraft:mined", {})
            killed = stats.get("minecraft:killed", {})
            distance_cm = (
                custom.get("minecraft:walk_one_cm", 0)
                + custom.get("minecraft:sprint_one_cm", 0)
                + custom.get("minecraft:swim_one_cm", 0)
                + custom.get("minecraft:fly_one_cm", 0)
            )
            diamonds = (
                mined.get("minecraft:diamond_ore", 0)
                + mined.get("minecraft:deepslate_diamond_ore", 0)
            )
            result.append({
                "name": name,
                "time_played_hours": _ticks_to_hours(custom.get("minecraft:play_time", 0)),
                "deaths": custom.get("minecraft:deaths", 0),
                "diamonds_mined": diamonds,
                "ancient_debris_mined": mined.get("minecraft:ancient_debris", 0),
                "distance_travelled_km": _cm_to_km(distance_cm),
                "villager_trades": custom.get("minecraft:traded_with_villager", 0),
                "total_mobs_killed": sum(killed.values()) if killed else 0,
            })
        return result

    # --- per-player restore (replace one <uuid>.dat, live, player offline) ---
    # Player-data lives under the active world (named by `level-name`):
    #   - Old layout (<= 1.21): <world>/playerdata/<uuid>.dat
    #   - New layout (>= 26.1): <world>/players/data/<uuid>.dat
    def _live_playerdata_dir(self) -> Path:
        world = self.config.minecraft_dir / get_level_name(self.config.minecraft_dir)
        new_path = world / "players" / "data"
        old_path = world / "playerdata"
        if new_path.is_dir():
            return new_path
        if old_path.is_dir():
            return old_path
        return new_path  # default to new layout when neither exists yet

    def list_player_versions(self, player_id: str, chain: tuple) -> list:
        """Historical copies of a player's .dat, newest first: live working
        files, pre-restore safety copies, and every backup zip that contains
        the entry. ``chain`` is ``(chain_id, base_full)``."""
        uuid = player_id
        chain_id, base_full = chain
        versions = []
        level_name = get_level_name(self.config.minecraft_dir)
        candidate_entries = (
            f"{level_name}/players/data/{uuid}.dat",   # new (26.1+)
            f"{level_name}/playerdata/{uuid}.dat",     # old (<= 1.21)
        )
        playerdata_dir = self._live_playerdata_dir()

        # 1. Live files (.dat, .dat_old, .dat_old.gz) — independently dated.
        for suffix, kind, label in (
            (".dat", "live", "live .dat"),
            (".dat_old", "live", "live .dat_old"),
            (".dat_old.gz", "live_gz", "live .dat_old.gz"),
        ):
            p = playerdata_dir / f"{uuid}{suffix}"
            if p.exists():
                mtime = p.stat().st_mtime
                versions.append({
                    "timestamp": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "sort_key": mtime, "source": label,
                    "kind": kind, "path": p, "entry": None,
                })

        # 1b. Pre-restore safety copies left by previous restores. The timestamp
        # in the filename is the authoritative save time.
        if playerdata_dir.exists():
            for p in playerdata_dir.glob(f"{uuid}.dat.pre-restore-*"):
                suffix = p.name[len(f"{uuid}.dat.pre-restore-"):]
                try:
                    ts_dt = datetime.strptime(suffix, "%Y%m%d_%H%M%S")
                except ValueError:
                    continue
                versions.append({
                    "timestamp": ts_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "sort_key": ts_dt.timestamp(), "source": "pre-restore backup",
                    "kind": "live", "path": p, "entry": None,
                })

        # 2 + 3. All backup zips — current and previous chains.
        if self.config.backup_dir.exists():
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
                try:
                    with zipfile.ZipFile(f, "r") as zf:
                        names = zf.namelist()
                    found_entry = next(
                        (e for e in candidate_entries if e in names), None)
                    if found_entry is None:
                        continue
                except (zipfile.BadZipFile, OSError):
                    logger.warning("Skipping unreadable backup zip: %s", f.name)
                    continue
                try:
                    ts_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                except ValueError:
                    continue
                versions.append({
                    "timestamp": ts_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "sort_key": ts_dt.timestamp(), "source": label,
                    "kind": "zip", "path": f, "entry": found_entry,
                })

        versions.sort(key=lambda v: v["sort_key"], reverse=True)
        return versions

    def restore_player(self, username: str, player_id: str, version: dict,
                       status_cb=None) -> None:
        """Atomically replace <uuid>.dat with the chosen version, keeping the
        current file as a pre-restore safety copy. Assumes the backup mutex is
        held by the caller; the save-off/save-all/save-on dance freezes writes."""
        uuid = player_id

        def status(msg):
            logger.info("[%s] RestorePlayer: %s", self.config.name, msg)
            if status_cb:
                status_cb(msg)

        save_started = False
        try:
            try:
                if self.is_player_online(username):
                    status(f"Player {username} is online — log them out first.")
                    return
                status(f"{username} is offline")
            except Exception as e:
                status(f"Online check failed: {e}")
                return

            self.begin_save(status)
            save_started = True
            wait_for_settle(self.config.minecraft_dir, self.config.backup_dir,
                            log_fn=status,
                            exclude_names=backup_exclude_names(self.config))

            target = self._live_playerdata_dir() / f"{uuid}.dat"
            source_bytes = _read_player_data_bytes(version)
            if target.exists():
                ts_label = datetime.fromtimestamp(
                    target.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
                backup_path = target.with_name(f"{uuid}.dat.pre-restore-{ts_label}")
                shutil.copy2(target, backup_path)
                status(f"Saved current .dat as {backup_path.name}")

            tmp = target.with_name(f"{uuid}.dat.tmp")
            tmp.write_bytes(source_bytes)
            os.replace(tmp, target)
            status(f"Restored {username}.dat from {version['source']} "
                   f"({version['timestamp']})")
        except Exception as e:
            logger.exception("[%s] RestorePlayer failed", self.config.name)
            status(f"Restore failed: {e}")
        finally:
            if save_started:
                try:
                    self.end_save(status)
                except Exception:
                    logger.exception("Failed to re-enable auto-save")


def _read_player_data_bytes(version: dict) -> bytes:
    """Raw .dat bytes from a version entry, gunzipping a .dat_old.gz source."""
    kind = version["kind"]
    if kind == "zip":
        with zipfile.ZipFile(version["path"], "r") as zf:
            return zf.read(version["entry"])
    if kind == "live_gz":
        with gzip.open(version["path"], "rb") as f:
            return f.read()
    return version["path"].read_bytes()
