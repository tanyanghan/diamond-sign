"""Tail the server's log/console for player events and RCON confirmations.

``LogWatcher`` is the single reader of the active log file (Java ``latest.log`` /
Bedrock ``console.log``): it parses each new line via ``core.logparse.parse_line``
and forwards events to the notify callback, fires the on-server-start resync, and
signals any ``LogLineWaiter`` registered (before an RCON command) via
``expect_line``.
"""

import threading
import time

from watchdog.events import FileSystemEventHandler

from core.logparse import parse_line


class LogLineWaiter:
    """A handle returned by LogWatcher.expect_line().

    Register this BEFORE sending the RCON command that will produce the
    expected log line. Then call wait() to block until the line appears
    or the timeout expires. This eliminates the race condition of capturing
    a file position after the command — the waiter is already listening
    when the command is sent.
    """
    def __init__(self, phrase: str):
        self.phrase = phrase
        self._event = threading.Event()

    def _signal(self):
        """Called by LogWatcher when a matching line is found."""
        self._event.set()

    def wait(self, timeout: float = 60) -> bool:
        """Block until the phrase is seen or timeout. Returns True if found."""
        return self._event.wait(timeout)

    def triggered(self) -> bool:
        """Whether the phrase has been seen (non-blocking)."""
        return self._event.is_set()


class LogWatcher(FileSystemEventHandler):
    """Watches the Minecraft server's latest.log for player events and
    RCON confirmations.

    This is the single point of access for reading latest.log. It handles
    log file rotation (when the server starts, it renames the old log and
    creates a new one) by tracking the file's inode.

    Two consumers:
      - Player event notifications: parsed via parse_line() and forwarded
        to the notify callback (joins, leaves, achievements, deaths).
      - RCON confirmation waiters: registered via expect_line(), which
        returns a LogLineWaiter. The waiter is signalled when a matching
        line appears in the log.
    """
    # Lines that mean the server is (re)started and ready to answer queries.
    # Java logs RCON readiness; Bedrock logs "Server started." (its console.log
    # is appended via tee, so there's no inode rotation to key off).
    _START_MARKERS = ("RCON running on", "Server started")
    _START_DEBOUNCE = 15  # seconds; collapse a burst of start lines into one

    def __init__(self, server, notify_cb, on_server_start=None):
        self._path = server.config.log_path
        self._filename = self._path.name  # "latest.log" (Java) or "console.log" (Bedrock)
        self._server = server  # the Server whose log this tails; parse into it
        self._notify = notify_cb
        self._on_server_start = on_server_start  # called when the server (re)starts
        self._last_start_trigger = 0.0
        self._pos = 0
        self._inode = None
        self._lock = threading.Lock()
        # Waiters: list of LogLineWaiter objects waiting for specific phrases
        self._waiters: list[LogLineWaiter] = []
        self._waiters_lock = threading.Lock()
        self._seek_to_end()

    def _seek_to_end(self) -> None:
        try:
            stat = self._path.stat()
            self._inode = stat.st_ino
            self._pos = stat.st_size
        except FileNotFoundError:
            self._server.log.warning("Log file not found at startup: %s (server may be offline)", self._path)

    def reset(self) -> None:
        """Re-seek to the current end of the log file. Used after a restore
        replaces the server directory (new inode / new latest.log), so tailing
        resumes cleanly from the current file rather than a stale position."""
        with self._lock:
            self._seek_to_end()

    def _check_rotation(self) -> bool:
        """Detect if latest.log was replaced (rotated) by checking inode."""
        try:
            inode = self._path.stat().st_ino
            if inode != self._inode:
                self._inode = inode
                self._pos = 0
                self._server.log.info("Log file rotation detected, resetting position")
                return True
        except FileNotFoundError:
            pass
        return False

    def expect_line(self, phrase: str) -> LogLineWaiter:
        """Register a waiter for a log line containing phrase.

        Call this BEFORE sending the RCON command that produces the line.
        The returned LogLineWaiter.wait(timeout) blocks until the line
        appears or times out.
        """
        waiter = LogLineWaiter(phrase)
        with self._waiters_lock:
            self._waiters.append(waiter)
        return waiter

    def cancel(self, waiter: LogLineWaiter) -> None:
        """Deregister a waiter that never fired (e.g. an error-line watcher that
        wasn't triggered), so it doesn't linger in the waiters list."""
        with self._waiters_lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _check_waiters(self, line: str) -> None:
        """Check a log line against all registered waiters."""
        with self._waiters_lock:
            triggered = []
            for waiter in self._waiters:
                if waiter.phrase in line:
                    waiter._signal()
                    triggered.append(waiter)
            # Remove triggered waiters
            for waiter in triggered:
                self._waiters.remove(waiter)

    def _maybe_server_start(self, line: str) -> None:
        """If a line signals the server (re)started, fire the on_server_start
        callback in a background thread (debounced). Runs off-thread so the
        reconcile's live query doesn't stall log reading."""
        if self._on_server_start is None:
            return
        if not any(marker in line for marker in self._START_MARKERS):
            return
        now = time.time()
        if now - self._last_start_trigger < self._START_DEBOUNCE:
            return
        self._last_start_trigger = now
        self._server.log.info("Server start detected in log; resyncing online status")
        threading.Thread(target=self._on_server_start, daemon=True).start()

    def on_modified(self, event):
        if not str(event.src_path).endswith(self._filename):
            return
        with self._lock:
            self._check_rotation()
            try:
                with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._pos)
                    new_data = f.read()
                    self._pos = f.tell()
                for line in new_data.splitlines():
                    # Check RCON confirmation waiters
                    self._check_waiters(line)
                    # Server (re)start -> resync online status (debounced).
                    self._maybe_server_start(line)
                    # Check player event notifications
                    event_type, payload = parse_line(line, self._server)
                    if event_type and payload:
                        self._notify(event_type, payload)
            except FileNotFoundError:
                pass
            except Exception:
                self._server.log.exception("Error reading Minecraft log")
