"""Java server backend: RCON transport, verbose latest.log, per-player .dat.

This wraps the behaviour mcnotifier shipped before edition support was added,
behind the ``ServerBackend`` interface. Functionally it is the original RCON
code path (``rcon_command``, the save-off/save-all/save-on dance,
``_wait_for_rcon_ready``, ``_validate_server_properties``, the ``list`` parsing)
relocated unchanged.
"""

import logging
import re

from mcrcon import MCRcon

from config import read_server_properties
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
