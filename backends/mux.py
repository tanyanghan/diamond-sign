"""Terminal-multiplexer command injection for Bedrock servers.

Bedrock Dedicated Server has no RCON; commands are typed on the server's stdin.
When the server runs inside a tmux or screen session (the user runs it under
byobu, which wraps either), we can inject a command by sending keystrokes to
that session. Command *output* is not captured here — it appears on the
server's stdout, which the caller tails from console.log.

``detect()`` finds whichever multiplexer is hosting the server, or returns None
(the BedrockBackend then refuses to start).
"""

import logging
import re
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("diamondsign")

# Serialise all injections process-wide: two senders interleaving keystrokes into
# the same pane is what corrupted "save hold" into "ave" in the wild. A short
# settle after each line keeps the server's stdin reader from coalescing rapid
# back-to-back commands.
_send_lock = threading.Lock()
_SETTLE_SECONDS = 0.3


class ConsoleMultiplexer:
    """Base class: knows how to send one command line to a session."""

    name = "mux"

    def __init__(self, target: str):
        self.target = target  # session (tmux) or session name / pid.name (screen)

    def send(self, cmd: str) -> None:
        """Inject one command line, after rejecting any that contains a control
        character.

        This is the single chokepoint for Bedrock console injection. Every legit
        command is either a fixed literal (``save hold``, ``list``, …) or
        ``allowlist <sub> <name>`` whose ``<name>`` came from ``str.split()`` and
        so can hold no whitespace — a control char here means the input was not
        what the caller thinks. Refuse rather than send a partial or a second
        line: a newline would submit an extra console command (injection), and a
        CR/other control byte can corrupt the keystroke stream. Defence in depth
        so a future caller that forwards richer text (e.g. shlex-quoted args or a
        chat relay) can't turn this into a command-injection sink."""
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in cmd):
            logger.warning("mux: refusing command with control character(s): %r",
                           cmd)
            return
        self._send_line(cmd)

    def _send_line(self, cmd: str) -> None:  # pragma: no cover - subclass duty
        """Actually inject one already-sanitised command line."""
        raise NotImplementedError

    def interrupt(self) -> None:  # pragma: no cover - subclass duty
        """Send Ctrl-C (SIGINT) to the pane's foreground process — what an
        admin does by hand to a server that hung during shutdown. Deliberately
        not routed through send(), which refuses control characters: this is a
        keystroke, not a command line."""
        raise NotImplementedError

    def pane_at_prompt(self) -> bool | None:
        """Whether the pane is sitting at a bare shell prompt — i.e. anything
        injected now would be typed into the SHELL, not the server.

        Reads the kernel process tree: the pane's shell with no child
        processes IS a prompt; any child (the server, or a wrapper script
        running it) means the pane is busy. Instant and injection-free, and —
        unlike tmux's ``pane_current_command``, which reports ``bash`` while a
        wrapper script runs the server — it cannot be fooled by wrappers,
        because the wrapper itself is a child.

        Returns True (prompt), False (something is running in the pane), or
        None when undeterminable (no per-window pid query on this mux, pid
        vanished mid-check, /proc and pgrep both unavailable) — callers then
        fall back to the sentinel-echo probe.
        """
        pid = self._pane_shell_pid()
        if pid is None:
            return None
        has_children = _pid_has_children(pid)
        if has_children is None:
            return None
        return not has_children

    def _pane_shell_pid(self) -> int | None:
        """PID of the shell owning the target pane/window, or None if this
        multiplexer can't be queried for it (then pane_at_prompt is unknown)."""
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(target={self.target!r})"


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run a command, returning the CompletedProcess or None if the binary is
    missing. stdout/stderr captured as text."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        logger.warning("mux: command timed out: %s", " ".join(args))
        return None


def _pid_has_children(pid: int) -> bool | None:
    """Whether ``pid`` has any child processes; None if undeterminable.

    Primary: /proc/<pid>/task/<tid>/children (world-readable; standard on
    modern kernels). Fallback: ``pgrep -P`` for kernels built without
    CONFIG_PROC_CHILDREN. A pid that vanished mid-check reports None so the
    caller re-resolves rather than trusting a stale answer.
    """
    try:
        for tid in Path(f"/proc/{pid}/task").iterdir():
            if (tid / "children").read_text().strip():
                return True
        return False
    except OSError:
        pass
    res = _run(["pgrep", "-P", str(pid)])
    if res is None or res.returncode not in (0, 1):
        return None
    return res.returncode == 0  # 0 = matched (children), 1 = none


class TmuxMux(ConsoleMultiplexer):
    name = "tmux"

    @classmethod
    def detect(cls, session: str = "") -> "TmuxMux | None":
        res = _run(["tmux", "list-sessions", "-F", "#{session_name}"])
        if res is None or res.returncode != 0:
            return None
        sessions = [s.strip() for s in res.stdout.splitlines() if s.strip()]
        if not sessions:
            return None
        if session:
            # ``session`` may be a full send-keys target: "name",
            # "name:window", or "name:window.pane". Only the part before the
            # first ':' is the session name to verify; the whole string is kept
            # as the target so a specific window/pane can be pinned. Pinning the
            # window matters with byobu grouped sessions, where the active-window
            # pointer is shared and unreliable.
            name = session.split(":", 1)[0]
            return cls(session) if name in sessions else None
        return cls(sessions[0])

    def _send_line(self, cmd: str) -> None:
        # `-l` sends the command as a LITERAL string so tmux never interprets a
        # word as a key name (and never coalesces/escapes characters). Enter is
        # sent as a separate keystroke. Both are serialised + settled so two
        # commands can't interleave in the pane.
        with _send_lock:
            _run(["tmux", "send-keys", "-t", self.target, "-l", "--", cmd])
            _run(["tmux", "send-keys", "-t", self.target, "Enter"])
            time.sleep(_SETTLE_SECONDS)

    def interrupt(self) -> None:
        with _send_lock:
            _run(["tmux", "send-keys", "-t", self.target, "C-c"])
            time.sleep(_SETTLE_SECONDS)

    def _pane_shell_pid(self) -> int | None:
        # `-t` accepts the same session[:window[.pane]] target as send-keys, so
        # the pid is read from exactly the pane commands are injected into.
        res = _run(["tmux", "display-message", "-p", "-t", self.target,
                    "#{pane_pid}"])
        if res is None or res.returncode != 0:
            return None
        try:
            return int(res.stdout.strip())
        except ValueError:
            return None


# screen -ls lines look like: "\t12345.mc\t(Detached)"
_RE_SCREEN = re.compile(r'^\s*(\d+\.\S+)', re.MULTILINE)


class ScreenMux(ConsoleMultiplexer):
    name = "screen"

    def __init__(self, target: str, window: str = "0"):
        super().__init__(target)
        self.window = window

    @classmethod
    def detect(cls, session: str = "") -> "ScreenMux | None":
        # `screen -ls` exits non-zero even when sessions exist, so parse stdout.
        res = _run(["screen", "-ls"])
        if res is None:
            return None
        entries = _RE_SCREEN.findall(res.stdout)  # e.g. ["12345.mc", ...]
        if not entries:
            return None
        if session:
            # ``session`` may be "name" or "name:window"; the window (a screen
            # window number) pins which window receives commands.
            name, _, window = session.partition(":")
            window = window or "0"
            for e in entries:
                if e == name or e.split(".", 1)[-1] == name:
                    return cls(e, window)
            return None
        return cls(entries[0])

    def _send_line(self, cmd: str) -> None:
        # `stuff` injects literal text; the trailing newline submits the line.
        # `-p <window>` selects which window of the session receives it.
        # Serialised + settled so commands can't interleave in the window.
        with _send_lock:
            _run(["screen", "-S", self.target, "-p", self.window,
                  "-X", "stuff", cmd + "\n"])
            time.sleep(_SETTLE_SECONDS)

    def interrupt(self) -> None:
        # ^C is ASCII 0x03; `stuff` delivers it as a raw keystroke.
        with _send_lock:
            _run(["screen", "-S", self.target, "-p", self.window,
                  "-X", "stuff", "\x03"])
            time.sleep(_SETTLE_SECONDS)


def detect(session: str = "") -> ConsoleMultiplexer | None:
    """Return the multiplexer hosting the server (tmux preferred), or None.

    If ``session`` is given it must match; otherwise the first live session of
    the first available multiplexer is used.
    """
    mux = TmuxMux.detect(session) or ScreenMux.detect(session)
    if mux:
        logger.info("Detected %s session for command injection: %s",
                    mux.name, mux.target)
    return mux
