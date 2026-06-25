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

logger = logging.getLogger("mcnotifier")


class ConsoleMultiplexer:
    """Base class: knows how to send one command line to a session."""

    name = "mux"

    def __init__(self, target: str):
        self.target = target  # session (tmux) or session name / pid.name (screen)

    def send(self, cmd: str) -> None:  # pragma: no cover - subclass responsibility
        raise NotImplementedError

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

    def send(self, cmd: str) -> None:
        # `--` stops option parsing so a command starting with '-' is safe.
        # cmd and "Enter" are separate args so the line is submitted.
        _run(["tmux", "send-keys", "-t", self.target, "--", cmd, "Enter"])


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

    def send(self, cmd: str) -> None:
        # `stuff` injects literal text; the trailing newline submits the line.
        # `-p <window>` selects which window of the session receives it.
        _run(["screen", "-S", self.target, "-p", self.window,
              "-X", "stuff", cmd + "\n"])


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
