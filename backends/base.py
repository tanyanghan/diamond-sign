"""Edition-agnostic server backend interface.

A ``ServerBackend`` hides every difference between a Java server (RCON transport,
verbose ``latest.log``, per-player ``.dat`` files) and a Bedrock server (terminal
multiplexer transport, terse ``console.log``, LevelDB-embedded player data) from
the rest of the bot.

The edition-agnostic core (Telegram handlers, backup-chain bookkeeping,
scheduling) talks only to this interface and checks ``CAPABILITIES`` before
offering features that a given edition can't support.

Line parsing and the shared player-state bookkeeping deliberately stay in
``bot.py`` (edition-dispatched) to avoid a circular import; the backend owns
command transport, the save/flush sequence, and server-side queries.
"""

from abc import ABC, abstractmethod
from pathlib import Path

# Normalized event types yielded by line parsing.
EVENT_JOIN = "join"
EVENT_LEAVE = "leave"
EVENT_DEATH = "death"
EVENT_ACHIEVEMENT = "achievement"

# Non-event capabilities.
CAP_PLAYER_RESTORE = "player_restore"


class BackendUnavailable(Exception):
    """Raised when a backend cannot operate at all (e.g. Bedrock with no
    tmux/screen session hosting the server). ``main()`` catches this and exits
    gracefully with a clear message."""


class NotSupported(Exception):
    """Raised by a backend method that an edition does not implement (e.g.
    per-player restore on Bedrock). Callers should gate on ``CAPABILITIES``
    rather than relying on catching this, but it guards against misuse."""


class ServerBackend(ABC):
    """Abstract transport + save + query surface for one server edition."""

    # Set of capability/event strings this backend supports. Subclasses override.
    CAPABILITIES: set[str] = set()

    def __init__(self, config):
        self.config = config
        self._watcher = None  # LogWatcher, attached after construction

    def attach_watcher(self, watcher) -> None:
        """Give the backend the LogWatcher it uses to await server log lines.

        Construction happens before the watcher exists in ``main()``, so the
        watcher is wired in afterwards.
        """
        self._watcher = watcher

    def supports(self, cap: str) -> bool:
        return cap in self.CAPABILITIES

    # --- availability / readiness ---
    @abstractmethod
    def is_available(self, log_warnings: bool = False) -> bool:
        """Whether the backend can currently issue commands (RCON configured and
        server.properties valid for Java; a live mux session for Bedrock)."""

    @abstractmethod
    def wait_for_ready(self, timeout: float = 120) -> bool:
        """Block until the server is ready to accept commands, or timeout."""

    # --- command transport ---
    @abstractmethod
    def send_command(self, cmd: str) -> str:
        """Send a console command. Returns the command's textual response when
        the transport supports it (Java/RCON); returns ``""`` for fire-and-forget
        transports (Bedrock mux), where output must be read from the log."""

    # --- backup freeze/flush ---
    @abstractmethod
    def begin_save(self, log_fn=None) -> None:
        """Freeze the world and flush pending writes (Java: save-off + save-all;
        Bedrock: save hold)."""

    @abstractmethod
    def files_ready(self, log_fn=None) -> list[tuple[Path, int]] | None:
        """Return the consistent file set to copy.

        Java: returns ``None`` — the caller copies the whole directory after the
        filesystem settles. Bedrock: returns ``[(path, max_bytes), ...]`` from
        ``save query``; each file must be copied truncated to ``max_bytes``.
        """

    @abstractmethod
    def end_save(self, log_fn=None) -> None:
        """Resume normal saving (Java: save-on; Bedrock: save resume)."""

    # --- server-side queries ---
    @abstractmethod
    def query_online_players(self) -> list[str]:
        """Ask the server who is online. Raises on transport failure."""

    @abstractmethod
    def is_player_online(self, username: str) -> bool:
        """Whether ``username`` is currently online, per the server (not the
        bot's in-memory set). Used as the destructive-restore precondition."""

    # Per-player data restore (live ``.dat`` scanning + LevelDB editing) is
    # edition-specific and gated by ``CAP_PLAYER_RESTORE``. The Java
    # implementation lives in ``bot.py`` because it is tightly coupled to the
    # backup manifest/chain state; Bedrock simply omits the capability. The
    # backend's role there is limited to the save dance and ``is_player_online``.

    # --- server lifecycle (Bedrock per-player restore: stop -> edit -> start) ---
    # Java doesn't need these (RCON edits live), so they default to NotSupported
    # and only the Bedrock backend overrides them.
    def stop_server(self, log_fn=None) -> bool:
        """Stop the server and return once it has shut down."""
        raise NotSupported("stop_server")

    def wait_for_db_unlock(self, timeout: float = 120) -> bool:
        """Wait until the world db is no longer locked by the server."""
        raise NotSupported("wait_for_db_unlock")

    def relaunch(self, log_fn=None) -> bool:
        """Relaunch the server and return once it reports ready."""
        raise NotSupported("relaunch")
