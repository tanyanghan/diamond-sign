"""Java server backend: RCON transport, verbose latest.log, per-player .dat.

This wraps the behaviour mcnotifier shipped before edition support was added,
behind the ``ServerBackend`` interface. Functionally it is the original RCON
code path (``rcon_command``, the save-off/save-all/save-on dance,
``_wait_for_rcon_ready``, ``_validate_server_properties``, the ``list`` parsing)
relocated unchanged.
"""

import gzip
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from mcrcon import MCRcon

from backup_utils import RE_FULL, RE_INCR, wait_for_settle
from config import read_server_properties, get_level_name, backup_exclude_names
from .base import (
    ServerBackend, EVENT_JOIN, EVENT_LEAVE, EVENT_DEATH, EVENT_ACHIEVEMENT,
    CAP_PLAYER_RESTORE,
)

logger = logging.getLogger("mcnotifier")

# Response to the `list` command: "There are X of a max of Y players online: a, b"
_RE_LIST = re.compile(r'There are \d+ of a max of \d+ players online:(.*)')


class JavaBackend(ServerBackend):
    CAPABILITIES = {EVENT_JOIN, EVENT_LEAVE, EVENT_DEATH, EVENT_ACHIEVEMENT,
                    CAP_PLAYER_RESTORE}

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

    def wait_for_ready(self, timeout: float = 120) -> bool:
        """Wait for the 'RCON running on' line in latest.log via the watcher."""
        logger.info("Waiting for RCON to be ready (monitoring latest.log)...")
        waiter = self._watcher.expect_line("RCON running on")
        return waiter.wait(timeout=timeout)

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
            return mcr.command(cmd)
        finally:
            mcr.disconnect()

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
            logger.info("RestorePlayer: %s", msg)
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
                            log_fn=status, exclude_names=backup_exclude_names())

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
            logger.exception("RestorePlayer failed")
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
