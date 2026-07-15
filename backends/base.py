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

import logging
import re
import secrets
import shlex
import time
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger("diamondsign")

# Minecraft formatting codes: the section sign (§, U+00A7) followed by one
# color/style char. They render as garbage in a chat message, so strip them
# from anything shown to the user.
_RE_MC_FORMAT = re.compile("§.")


def strip_minecraft_formatting(text: str) -> str:
    return _RE_MC_FORMAT.sub("", text)

# Normalized event types yielded by line parsing.
EVENT_JOIN = "join"
EVENT_LEAVE = "leave"
EVENT_DEATH = "death"
EVENT_ACHIEVEMENT = "achievement"
EVENT_CHAT = "chat"

# Non-event capabilities.
CAP_PLAYER_RESTORE = "player_restore"
CAP_STATS = "stats"  # backend can answer /stats and /playtime


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
        # Per-server state lives under data/<server-name>/ (config.data_dir),
        # so two servers in one process never share a state file.
        self.data_dir = config.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._watcher = None  # LogWatcher, attached after construction

    def _data_path(self, filename: str) -> Path:
        """Resolve a per-server state file under ``data/<key>/``."""
        return self.data_dir / filename

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
        """Whether the backend is CONFIGURED to issue commands (RCON set up and
        server.properties valid for Java; a mux session for Bedrock). This is a
        config check — it does NOT mean the server process is running; use
        ``is_online`` for liveness."""

    def is_online(self) -> bool:
        """Whether the server PROCESS is currently running/reachable — distinct
        from ``is_available`` (config only). Subclasses probe cheaply (Java: the
        RCON port; Bedrock: the world-db lock). Defaults True so a backend
        without a probe doesn't over-restrict command gating."""
        return True

    def wait_until_online(self, log_fn=None, *, stall: float = 120,
                          cap: float = 900) -> bool:
        """Wait for the server to accept commands, tolerating a slow boot.

        Polls ``is_online`` and EXTENDS the wait as long as the server log keeps
        growing (boot in progress) — so a server that takes minutes to start
        (Paper + plugins on a Pi) isn't abandoned on a fixed timeout. Gives up
        only if the log stays idle for ``stall`` seconds with the server still
        offline (stuck/crashed), or after ``cap`` seconds overall. Uses
        ``is_online`` (a port/lock probe), so it's robust across the log rotation
        a restart causes — unlike matching a one-off 'ready' log line."""
        def log(msg):
            if log_fn:
                log_fn(msg)

        def size():
            try:
                return self.config.log_path.stat().st_size
            except OSError:
                return -1

        start = time.time()
        last_size, last_activity = size(), time.time()
        announced = False
        while time.time() - start < cap:
            if self.is_online():
                return True
            time.sleep(3)
            cur = size()
            if cur != last_size:            # log grew -> boot is making progress
                last_size, last_activity = cur, time.time()
                if not announced:
                    log("Server is starting (log active); waiting for it to "
                        "accept commands...")
                    announced = True
            elif time.time() - last_activity > stall:
                log(f"Server log idle for {int(stall)}s and still not accepting "
                    "commands — giving up.")
                return False
        log(f"Server did not come online within {int(cap)}s.")
        return False

    # --- command transport ---
    @abstractmethod
    def send_command(self, cmd: str) -> str:
        """Send a console command. Returns the command's textual response when
        the transport supports it (Java/RCON); returns ``""`` for fire-and-forget
        transports (Bedrock mux), where output must be read from the log."""

    def capture_command(self, full_cmd: str, timeout: float = 3.0) -> str:
        """Run a console command and return its textual response.

        Default suits transports whose ``send_command`` already returns the
        response (Java/RCON). Fire-and-forget transports (Bedrock mux) override
        this to read the server's output back from the captured log.
        """
        return self.send_command(full_cmd)

    # The server-side command that manages the player allow/whitelist. Java calls
    # it ``whitelist``; Bedrock calls it ``allowlist``. Subclasses override.
    ALLOWLIST_VERB = "whitelist"

    def allowlist_command(self, args: list, timeout: float = 3.0) -> str:
        """Run an allow/whitelist subcommand (on/off/add/remove/list/reload) and
        return the server's response. ``args`` is the subcommand + operands."""
        cmd = self.ALLOWLIST_VERB
        if args:
            cmd += " " + " ".join(args)
        # Java RCON responses carry § colour codes; strip them so the chat reply
        # is clean (Bedrock's captured output is already plain).
        return strip_minecraft_formatting(self.capture_command(cmd, timeout=timeout))

    def broadcast(self, message: str) -> None:
        """Announce ``message`` to all players in-game via the console ``say``
        command. Works on both editions (Java over RCON, Bedrock over the mux).
        Newlines are stripped: ``say`` is a single line and the mux guard
        (backends/mux.py) refuses control characters anyway."""
        one_line = " ".join(message.split())
        if one_line:
            self.send_command(f"say {one_line}")

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

    # --- server lifecycle (stop -> replace files -> start) ---
    # Used by Bedrock per-player restore and by the world-restore command on both
    # editions. Bedrock always overrides these; Java overrides them only when a
    # mux + start command are configured (else they stay NotSupported, and
    # ``can_restart`` is False so /restore is refused with a clear message).
    @property
    def can_restart(self) -> bool:
        """Whether this backend can stop and relaunch the server (needed for a
        world restore). Subclasses override based on their transport."""
        return False

    def stop_server(self, log_fn=None) -> bool:
        """Request a stop and return once the server has acknowledged it (actual
        shutdown is confirmed by ``wait_until_stopped``)."""
        raise NotSupported("stop_server")

    def wait_until_stopped(self, timeout: float = 120) -> bool:
        """Block until the server process is fully down (Bedrock: world db lock
        released; Java: RCON no longer answering)."""
        raise NotSupported("wait_until_stopped")

    def _confirm_console_free(self, timeout: float, log_fn=None):
        """Confirm the server's mux pane has returned to a shell prompt — i.e.
        the server process has fully exited and its world files/db are released.

        The server runs in a tmux/screen pane as ``<start cmd> | tee``; while it
        is up, an injected line reaches the *server's* console stdin, and once it
        exits the pane falls back to the *shell*. So we inject a command only a
        shell can satisfy — write a nonce to a sentinel file — and watch for that
        file. A running server takes the injection as a bogus console command
        (logged as unknown) and cannot create the file, so the file appearing
        means the pane is a shell again. Keying on a file the server can't write
        (rather than grepping the log) rules out a false positive from the server
        echoing the command back. Re-injects each probe so a line lands as soon
        as the shell is ready.

        Returns True (confirmed free), False (timed out), or None if there is no
        mux to inject through — the caller then trusts its own weaker signal.
        """
        mux = getattr(self, "_mux", None)
        if mux is None:
            return None

        def log(msg):
            if log_fn:
                log_fn(msg)

        nonce = secrets.token_hex(8)
        sentinel = (self.data_dir / "stopcheck").resolve()
        try:
            sentinel.unlink()  # clear any stale sentinel from a prior run
        except OSError:
            pass
        # Absolute path so the shell's cwd doesn't matter; quoted for safety.
        cmd = f"echo {nonce} > {shlex.quote(sentinel.as_posix())}"

        deadline = time.time() + timeout
        while time.time() < deadline:
            mux.send(cmd)       # -> server console (ignored) or shell (runs it)
            time.sleep(3)
            try:
                if sentinel.read_text().strip() == nonce:
                    sentinel.unlink()
                    return True
            except OSError:
                pass            # not written yet -> pane still busy with the server
        try:
            sentinel.unlink()
        except OSError:
            pass
        log("Console did not return to a shell prompt in time")
        return False

    def probe_stopped(self, timeout: float = 10, log_fn=None) -> bool | None:
        """Establish whether the server process has ALREADY exited, safely in
        either state — unlike ``is_online``, whose Bedrock probe types ``list``
        into the pane and so must not run blind.

        Returns True (confirmed stopped: the pane is a shell prompt — any
        server command injected now would run as a SHELL command), False
        (something still owns the console; treat the server as running), or
        None (no mux to probe through; state unknown).

        Layered, most-trustworthy signal first:

        1. Pane process tree (``ConsoleMultiplexer.pane_at_prompt``): a pane
           shell with NO children is a bare prompt — instant, injection-free,
           and cannot be faked by a running server. Trusted only in the
           "stopped" direction: byobu-style wrapper shells can hold a child
           even at a prompt (seen in the wild), so "busy" is NOT proof of a
           running server.
        2. ``is_online()``: a live server answers its console/port. If it
           responds, it is definitively running.
        3. Sentinel echo (``_confirm_console_free``): pane looked busy (or
           unknown) yet the server did not respond — contradictory. The
           sentinel settles who owns the console: only a shell can write the
           nonce file; a running server logs one unknown command, harmless.
           ``timeout`` applies to this step.

        Every decision is logged, so a misread is diagnosable from the bot
        log. Lifecycle paths (stop, restore, the save dance) must call this
        before injecting: after a hung shutdown that an admin resolved by
        hand, the pane is a prompt and blind injection executes there.
        """
        mux = getattr(self, "_mux", None)
        if mux is None:
            return None
        at_prompt = mux.pane_at_prompt()
        if at_prompt is True:
            logger.info("[%s] Probe: pane is a bare shell prompt -> server "
                        "stopped", self.config.name)
            return True
        try:
            online = self.is_online()
        except Exception:
            online = False
        if online:
            logger.info("[%s] Probe: pane busy and server responding -> "
                        "running", self.config.name)
            return False
        logger.info("[%s] Probe: pane %s but server not responding — "
                    "sentinel probe decides...", self.config.name,
                    "busy" if at_prompt is False else "state unknown")
        result = self._confirm_console_free(timeout, log_fn=log_fn)
        logger.info("[%s] Probe: sentinel verdict: %s", self.config.name,
                    {True: "console free -> server stopped",
                     False: "console owned -> treating as running",
                     None: "no mux -> unknown"}[result])
        return result

    def force_stop(self, log_fn=None) -> bool:
        """Last-resort stop for a server that acknowledged ``stop`` but never
        exited (BDS is known to occasionally hang during shutdown): send
        Ctrl-C (SIGINT) to its console pane — exactly what an admin does by
        hand — and confirm the pane returned to a shell prompt. Retries the
        interrupt a few times; True only when the exit is confirmed.

        Only appropriate when losing unflushed state is acceptable (e.g. a
        world restore is about to replace the files anyway) — callers make
        that call.
        """
        mux = getattr(self, "_mux", None)
        if mux is None:
            return False

        def log(msg):
            if log_fn:
                log_fn(msg)

        for attempt in range(1, 4):
            log(f"Interrupting the server (Ctrl-C, attempt {attempt}/3)...")
            mux.interrupt()
            if self._confirm_console_free(20, log_fn=None):
                return True
        log("Server did not exit after Ctrl-C")
        return False

    def relaunch(self, log_fn=None) -> bool:
        """Relaunch the server and return once it reports ready."""
        raise NotSupported("relaunch")

    # --- per-player restore (edition-agnostic surface; gated by CAP_PLAYER_RESTORE) ---
    # The /restore_player handler is a thin 3-step state machine over these; the
    # edition-specific work (Java: replace one <uuid>.dat live; Bedrock: stop ->
    # edit the world LevelDB -> relaunch) lives in the subclasses.
    def resolve_player(self, name: str, names: dict):
        """Case-insensitive name -> (canonical_name, player_id), or None.

        player_id is the player-names registry key — a UUID on Java, an xuid on
        Bedrock. Default suits both; subclasses rarely need to override.
        """
        target = name.lower()
        for player_id, canonical in names.items():
            if canonical.lower() == target:
                return canonical, player_id
        return None

    def list_player_versions(self, player_id: str, chain: tuple) -> list:
        """Restore points for a player, newest first. Each is a dict with at
        least ``timestamp`` and ``source`` (for display) plus backend-internal
        keys consumed by ``restore_player``. ``chain`` is ``(chain_id,
        base_full)`` from the backup manifest."""
        raise NotSupported("list_player_versions")

    def restore_player(self, username: str, player_id: str, version: dict,
                       status_cb=None) -> None:
        """Perform the restore for one ``version`` from ``list_player_versions``.
        Assumes the caller holds the backup mutex. ``status_cb(msg)`` reports
        progress (the handler forwards it to Telegram)."""
        raise NotSupported("restore_player")

    def learn_player(self, name: str, player_id: str, idents: list) -> bool:
        """Persist a learned identity binding (Bedrock only; no-op elsewhere)."""
        return False

    def known_identities(self) -> set:
        """All identity uuids already bound to some player (Bedrock only)."""
        return set()

    # --- player-name registry (the bot's `names` dict is sourced from here) ---
    # Java keeps player_names.json keyed by UUID; Bedrock projects names from its
    # richer bedrock_players.json keyed by xuid. Either way bot.py just sees a
    # {player_id: name} dict.
    @abstractmethod
    def load_names(self) -> dict:
        """Return the {player_id: name} registry from disk."""

    @abstractmethod
    def register_name(self, player_id: str, name: str) -> bool:
        """Persist player_id -> name. Returns True if it created/changed an entry
        (used only for logging — Bedrock may still touch last_seen and return
        False)."""

    # --- stats (gated by CAP_STATS) ---
    def list_known_players(self, names: dict) -> list:
        """Player names for /list. Default: the registry's names."""
        return sorted(set(names.values()))

    def player_stats(self, names: dict) -> list:
        """Per-player stat dicts for /stats and /playtime. Each has at least
        ``name`` and ``time_played_hours``; richer editions add more fields."""
        raise NotSupported("player_stats")

    # --- online-time tracking (Bedrock only; no-op default) ---
    def record_player_session(self, event_type: str, player_id: str) -> None:
        """Accumulate connected time from a join/leave event."""

    def reset_open_sessions(self) -> None:
        """Clear sessions left open by a crash (called at startup)."""

    def checkpoint_open_sessions(self) -> None:
        """Bank elapsed time for open sessions without ending them, so a later
        crash loses at most the time since this checkpoint."""

    def close_open_sessions(self) -> None:
        """Flush any open sessions (called on graceful shutdown)."""
