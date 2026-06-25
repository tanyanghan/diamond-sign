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

import logging
import re
import time

from .base import (
    ServerBackend, BackendUnavailable, EVENT_JOIN, EVENT_LEAVE,
)
from .mux import detect

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
    CAPABILITIES = {EVENT_JOIN, EVENT_LEAVE}

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

    # --- backup freeze/flush ---
    def begin_save(self, log_fn=None) -> None:
        def log(msg):
            if log_fn:
                log_fn(msg)
        log("Holding save...")
        waiter = self._watcher.expect_line("Saving...")
        self.send_command("save hold")
        if waiter.wait(timeout=30):
            log("Save held")
        else:
            log("Warning: 'save hold' acknowledgement not seen, proceeding anyway")

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
        waiter = self._watcher.expect_line("are resumed")
        self.send_command("save resume")
        if waiter.wait(timeout=30):
            log("Save resumed")
        else:
            log("Warning: 'save resume' acknowledgement not seen")

    # --- server-side queries ---
    def query_online_players(self) -> list[str]:
        self.send_command("list")
        time.sleep(1)
        return self._scan_list_output()

    def is_player_online(self, username: str) -> bool:
        target = username.lower()
        return any(n.lower() == target for n in self.query_online_players())

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
