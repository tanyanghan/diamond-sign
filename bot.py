import gzip
import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from utils.backup_utils import (
    CHAIN_MARKER_NAME, RE_FULL, RE_INCR, build_file_manifest, new_chain_id,
    run_copy_command, wait_for_settle,
)
from utils.config import (
    load_config, backup_exclude_names, EDITION_BEDROCK,
)
from backends import (
    make_backend, BackendUnavailable, CAP_PLAYER_RESTORE, CAP_STATS,
    EVENT_DEATH, EVENT_ACHIEVEMENT,
)
from chat import make_adapters, CommandRouter

# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
# All environment reading lives in config.load_config(); the rest of the module
# reads from CONFIG (and a few thin aliases kept to minimise churn). Per-player
# data (stats dir, name registry) is owned by the backend, not bot.py.
CONFIG = load_config()

MINECRAFT_DIR = CONFIG.minecraft_dir          # Path
LOG_PATH = CONFIG.log_path                     # Java: logs/latest.log; Bedrock: console.log
BACKUP_DIR = CONFIG.backup_dir
INCREMENTAL_BACKUP_ENABLED = CONFIG.incremental_enabled
INCREMENTAL_INTERVAL_MINUTES = CONFIG.incremental_interval_minutes

# Server backend (Java RCON / Bedrock mux). Constructed in main().
BACKEND = None

# Files excluded from backups and the change manifest in addition to the chain
# marker: bot infrastructure that lives in the server directory but isn't server
# data. On Bedrock this is the captured-stdout console.log the bot tails, which
# tee appends to constantly (it would otherwise appear changed in every
# incremental). Matched by basename anywhere in the tree.
_BACKUP_EXCLUDE_NAMES = frozenset(backup_exclude_names())

# ---------------------------------------------------------------------------
# Logging setup (configured in main, used everywhere via module-level logger)
# ---------------------------------------------------------------------------
logger = logging.getLogger("mcnotifier")


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"log_{timestamp}.txt"

    fmt = logging.Formatter("%(asctime)s  %(name)-12s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    telebot_logger = logging.getLogger("TeleBot")
    telebot_logger.addHandler(file_handler)

    logger.info("Logging started — writing to %s", log_file)




# ---------------------------------------------------------------------------
# 2. Player Name Registry
# ---------------------------------------------------------------------------
# The {player_id: name} registry is owned by the backend (Java: player_names.json
# keyed by UUID; Bedrock: projected from bedrock_players.json keyed by xuid). The
# in-memory `names` dict and these helper signatures are kept so the rest of the
# bot is unchanged; the `path` argument is vestigial (ignored).
_NAMES_PATH = None  # retained only so existing call sites pass *something*
_names_lock = threading.Lock()


def load_player_names(path=None) -> dict:
    return BACKEND.load_names()


def refresh_player_names(names: dict, path=None) -> None:
    with _names_lock:
        names.clear()
        names.update(BACKEND.load_names())


def register_player(uuid: str, name: str, names: dict, path=None) -> None:
    old = names.get(uuid)
    names[uuid] = name
    changed = BACKEND.register_name(uuid, name)
    if old and old != name:
        logger.info("Player registry: %s renamed %s -> %s", uuid, old, name)
    elif changed and not old:
        logger.info("Player registry: registered %s (%s)", name, uuid)


def _uuid_by_name(player_name: str, names: dict) -> str | None:
    for uuid, name in names.items():
        if name == player_name:
            return uuid
    return None


# ---------------------------------------------------------------------------
# 2b. Achievements Storage
# ---------------------------------------------------------------------------
_ACHIEVEMENTS_PATH = Path(__file__).parent / "player_achievements.json"
_achievements_lock = threading.Lock()


def load_achievements(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load player_achievements.json")
    return {}


def _save_achievements(achievements: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(achievements, f, indent=2)


def record_achievement(uuid: str, achievement: str, ach_type: str,
                       timestamp: str, achievements: dict, path: Path) -> bool:
    with _achievements_lock:
        entries = achievements.setdefault(uuid, [])
        for e in entries:
            if e["achievement"] == achievement and e["timestamp"] == timestamp:
                return False
        entries.append({
            "achievement": achievement,
            "type": ach_type,
            "timestamp": timestamp,
        })
        _save_achievements(achievements, path)
    return True


# ---------------------------------------------------------------------------
# 2c. Deaths Storage
# ---------------------------------------------------------------------------
_DEATHS_PATH = Path(__file__).parent / "player_deaths.json"
_deaths_lock = threading.Lock()


def load_deaths(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load player_deaths.json")
    return {}


def _save_deaths(deaths: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(deaths, f, indent=2)


def record_death(uuid: str, message: str, timestamp: str,
                 deaths: dict, path: Path) -> bool:
    with _deaths_lock:
        entries = deaths.setdefault(uuid, [])
        for e in entries:
            if e["message"] == message and e["timestamp"] == timestamp:
                return False
        entries.append({"message": message, "timestamp": timestamp})
        _save_deaths(deaths, path)
    return True


# ---------------------------------------------------------------------------
# 3. Online Players State
# ---------------------------------------------------------------------------
_online_players: set = set()
_online_lock = threading.Lock()

# Bedrock identity learning: xuids seen online since the last backup (kept even
# after a player leaves, so a short session that only triggers a post-leave
# backup is still attributable). Pruned to the still-online set after each
# learn attempt. See _maybe_learn_player.
_session_xuids: set = set()
_session_lock = threading.Lock()


def player_join(name: str) -> None:
    with _online_lock:
        _online_players.add(name)


def player_leave(name: str) -> None:
    with _online_lock:
        _online_players.discard(name)


def get_online_players() -> list:
    with _online_lock:
        return sorted(_online_players)


def _note_active_xuid(xuid: str) -> None:
    if xuid:
        with _session_lock:
            _session_xuids.add(xuid)


# ---------------------------------------------------------------------------
# 4. Log Parsing
# ---------------------------------------------------------------------------
RE_JOIN = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) joined the game')
RE_LEAVE = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) left the game')
RE_UUID = re.compile(r'^\[[\d:]+\] \[User Authenticator #\d+/INFO\]: UUID of player (\w+) is ([0-9a-f-]+)')
RE_ACHIEVEMENT = re.compile(
    r'^\[([\d:]+)\] \[Server thread/INFO\]: (\w+) has '
    r'(made the advancement|reached the goal|completed the challenge) '
    r'\[(.+?)\]'
)
_ACH_TYPE_MAP = {
    "made the advancement": "advancement",
    "reached the goal": "goal",
    "completed the challenge": "challenge",
}
_ACH_VERB_MAP = {v: k for k, v in _ACH_TYPE_MAP.items()}

RE_SERVER_MSG = re.compile(r'^\[([\d:]+)\] \[Server thread/INFO\]: (\w+) (.+)$')
# Player chat, e.g. "[12:34:56] [Server thread/INFO]: <Steve> hello" (or a
# Paper-style "[Async Chat Thread - #0/INFO]:"). The <> brackets distinguish it
# from join/leave/death lines (which start with a bare \w name).
RE_CHAT = re.compile(r'^\[[\d:]+\] \[[^\]]*/INFO\]: <([^>]+)> (.+)$')
_DEATH_PHRASES = (
    "was slain by", "was shot by", "was killed",
    "was blown up by", "was squashed by", "was fireballed by",
    "was pummeled by", "was stung by", "was impaled",
    "was skewered by", "was struck by lightning",
    "was burnt to", "was frozen to death", "was pricked to death",
    "was poked to death", "was doomed to fall",
    "was roasted in dragon", "was obliterated by",
    "was squished",
    "drowned", "suffocated", "starved to death",
    "burned to death",
    "fell from", "fell off", "fell out of", "fell into", "fell while",
    "hit the ground too hard",
    "tried to swim in lava",
    "walked into",
    "froze to death", "withered away",
    "experienced kinetic energy",
    "went up in flames", "went off with a bang",
    "died", "didn't want to live",
    "discovered the floor was lava",
    "blew up",
    "left the confines of this world",
)


_DEATH_CATEGORIES = [
    ("Combat (was slain by)", ["was slain by"]),
    ("Shot by", ["was shot by"]),
    ("Blown up", ["was blown up by"]),
    ("Falls", ["fell from", "fell off", "fell out of", "fell into",
               "fell while", "hit the ground too hard"]),
    ("Lava", ["tried to swim in lava"]),
    ("Fire", ["burned to death", "was burnt to", "went up in flames",
              "walked into fire"]),
    ("Drowning", ["drowned"]),
    ("Withered away", ["withered away"]),
    ("Impaled", ["was impaled"]),
    ("Frozen", ["froze to death", "was frozen to death"]),
    ("Lightning", ["was struck by lightning"]),
    ("Kinetic energy", ["experienced kinetic energy"]),
    ("Suffocation", ["suffocated"]),
    ("Starvation", ["starved to death"]),
    ("Cactus", ["walked into a cactus", "was pricked to death",
                "was poked to death"]),
    ("Dragon", ["was doomed to fall", "was roasted in dragon"]),
    ("Sonic shriek", ["was obliterated by"]),
    ("Explosions", ["blew up", "went off with a bang"]),
    ("Void", ["left the confines of this world"]),
    ("Magic", ["was killed by magic", "was killed by even more magic"]),
]


def _categorize_death(message: str) -> str:
    for category, phrases in _DEATH_CATEGORIES:
        if any(message.startswith(p) for p in phrases):
            return category
    return "Other"


_pending_uuids: dict = {}  # name -> uuid, populated by UUID line, consumed by join line


def _parse_line_java(line: str, names: dict) -> tuple:
    """Return (event_type, payload) or (None, None).

    For join/leave, payload is the player name string.
    For achievement, payload is a dict with player, achievement, type, time.
    """
    line = line.strip()

    m = RE_UUID.match(line)
    if m:
        name, uuid = m.group(1), m.group(2)
        _pending_uuids[name] = uuid
        register_player(uuid, name, names, _NAMES_PATH)
        return None, None

    m = RE_JOIN.match(line)
    if m:
        name = m.group(1)
        uuid = _pending_uuids.pop(name, None)
        if uuid:
            register_player(uuid, name, names, _NAMES_PATH)
        player_join(name)
        return "join", name

    m = RE_LEAVE.match(line)
    if m:
        name = m.group(1)
        player_leave(name)
        return "leave", name

    m = RE_ACHIEVEMENT.match(line)
    if m:
        time_str, name, ach_type_full, achievement = m.groups()
        return "achievement", {
            "player": name,
            "achievement": achievement,
            "type": _ACH_TYPE_MAP[ach_type_full],
            "time": time_str,
        }

    m = RE_SERVER_MSG.match(line)
    if m:
        time_str, name, msg = m.groups()
        if any(msg.startswith(p) for p in _DEATH_PHRASES):
            return "death", {
                "player": name,
                "message": msg,
                "time": time_str,
            }

    if CONFIG.chat_relay:
        m = RE_CHAT.match(line)
        if m:
            return "chat", {"player": m.group(1), "message": m.group(2)}

    return None, None


# Bedrock Dedicated Server console lines (terser than Java's log). Names may
# contain spaces, so capture up to the ", xuid:" delimiter. BDS's own console
# reports only join/leave; death and chat come from the bedrock_pack behavior
# pack as `MCNOTIFIER {json}` marker lines (see below).
RE_BEDROCK_CONNECT = re.compile(r'Player connected:\s*(.+?),\s*xuid:\s*(\d+)')
RE_BEDROCK_DISCONNECT = re.compile(r'Player disconnected:\s*(.+?),\s*xuid:\s*(\d+)')

# Behavior-pack event marker, e.g.
#   [<ts> WARN] [Scripting] MCNOTIFIER {"t":"death","player":"X","cause":"lava"}
_BEDROCK_MARKER = "MCNOTIFIER "

# Bedrock damage cause -> death phrase, worded to mirror Java so the same
# _categorize_death / _DEATH_CATEGORIES logic works for /death_summary. "{by}"
# is filled with the prettified killer entity when present.
_BEDROCK_DEATH_PHRASES = {
    "lava": "tried to swim in lava",
    "fire": "went up in flames",
    "fire_tick": "burned to death",
    "fall": "fell from a high place",
    "drowning": "drowned",
    "suffocation": "suffocated in a wall",
    "starve": "starved to death",
    "freezing": "froze to death",
    "lightning": "was struck by lightning",
    "void": "fell out of the world",
    "contact": "was pricked to death",
    "magma": "discovered the floor was lava",
    "wither": "withered away",
    "anvil": "was squashed by a falling anvil",
    "falling_block": "was squashed by a falling block",
    "magic": "was killed by magic",
    "sonic_boom": "was obliterated by a sonically-charged shriek",
    "block_explosion": "blew up",
    "entity_explosion": "blew up",
    "entity_attack": "was slain",
    "projectile": "was shot",
    "thorns": "was killed trying to hurt",
    "self_destruct": "blew up",
}


def _pretty_entity(type_id: str) -> str:
    """'minecraft:zombie' -> 'Zombie'."""
    return type_id.split(":")[-1].replace("_", " ").title()


def _bedrock_death_message(cause: str, by) -> str:
    """Build a Java-style death message from a Bedrock damage cause + killer."""
    phrase = _BEDROCK_DEATH_PHRASES.get((cause or "").lower())
    killer = _pretty_entity(by) if by else None
    if phrase is None:
        return f"was killed by {killer}" if killer else "died"
    # Causes that read naturally with a "by <killer>" suffix.
    if cause.lower() in ("entity_attack", "projectile", "thorns") and killer:
        verb = {"entity_attack": "was slain by", "projectile": "was shot by",
                "thorns": "was killed trying to hurt"}[cause.lower()]
        return f"{verb} {killer}"
    return phrase


def _parse_line_bedrock(line: str, names: dict) -> tuple:
    """Return (event_type, payload) or (None, None) for a Bedrock console line.

    Join/leave come from BDS itself; death/chat come from the bedrock_pack
    behavior pack's MCNOTIFIER marker lines (gated by config). The player's xuid
    is the registry key (Bedrock has no per-player UUID file).
    """
    line = line.strip()
    m = RE_BEDROCK_CONNECT.search(line)
    if m:
        name, xuid = m.group(1).strip(), m.group(2).strip()
        if xuid:
            register_player(xuid, name, names, _NAMES_PATH)
        player_join(name)
        return "join", name
    m = RE_BEDROCK_DISCONNECT.search(line)
    if m:
        name = m.group(1).strip()
        player_leave(name)
        return "leave", name

    # Behavior-pack markers (anywhere after the log/[Scripting] prefixes).
    idx = line.find(_BEDROCK_MARKER)
    if idx != -1:
        try:
            ev = json.loads(line[idx + len(_BEDROCK_MARKER):])
        except (ValueError, TypeError):
            return None, None
        t = ev.get("t")
        if t == "death" and CONFIG.bedrock_script_events:
            return "death", {
                "player": ev.get("player", "?"),
                "message": _bedrock_death_message(ev.get("cause"), ev.get("by")),
                "time": datetime.now().strftime("%H:%M:%S"),
            }
        if t == "chat" and CONFIG.chat_relay:
            return "chat", {"player": ev.get("player", "?"),
                            "message": ev.get("msg", "")}
    return None, None


def parse_line(line: str, names: dict) -> tuple:
    """Dispatch line parsing to the edition-specific parser."""
    if CONFIG.edition == EDITION_BEDROCK:
        return _parse_line_bedrock(line, names)
    return _parse_line_java(line, names)


# ---------------------------------------------------------------------------
# 5. LogWatcher
# ---------------------------------------------------------------------------
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
    def __init__(self, log_path: Path, names: dict, notify_cb):
        self._path = log_path
        self._filename = log_path.name  # "latest.log" (Java) or "console.log" (Bedrock)
        self._names = names
        self._notify = notify_cb
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
            logger.warning("Log file not found at startup: %s (server may be offline)", self._path)

    def _check_rotation(self) -> bool:
        """Detect if latest.log was replaced (rotated) by checking inode."""
        try:
            inode = self._path.stat().st_ino
            if inode != self._inode:
                self._inode = inode
                self._pos = 0
                logger.info("Log file rotation detected, resetting position")
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
                    # Check player event notifications
                    event_type, payload = parse_line(line, self._names)
                    if event_type and payload:
                        self._notify(event_type, payload)
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception("Error reading Minecraft log")


# ---------------------------------------------------------------------------
# 6. Notification Callback
# ---------------------------------------------------------------------------
def make_notify_callback(names: dict, achievements: dict, deaths: dict):
    _last_event: dict = {}
    _cooldown = 3

    def _send_to_chats(msg: str) -> None:
        _announce(msg)

    def notify(event_type: str, payload) -> None:
        if event_type == "achievement":
            player = payload["player"]
            achievement = payload["achievement"]
            ach_type = payload["type"]
            time_str = payload["time"]
            key = f"{player}-achievement-{achievement}"
            now = time.time()
            if now - _last_event.get(key, 0) < _cooldown:
                return
            _last_event[key] = now

            timestamp = f"{datetime.now().strftime('%Y-%m-%d')} {time_str}"
            uuid = _uuid_by_name(player, names)
            if uuid:
                record_achievement(uuid, achievement, ach_type, timestamp,
                                   achievements, _ACHIEVEMENTS_PATH)

            verb = _ACH_VERB_MAP[ach_type]
            msg = f"{player} has {verb} [{achievement}]"
            logger.info("Achievement: %s — %s — sending to %d chat(s)",
                        player, achievement, _announce_chat_count())
            _send_to_chats(msg)
            return

        if event_type == "death":
            player = payload["player"]
            death_msg = payload["message"]
            time_str = payload["time"]
            timestamp = f"{datetime.now().strftime('%Y-%m-%d')} {time_str}"
            uuid = _uuid_by_name(player, names)
            if uuid:
                record_death(uuid, death_msg, timestamp, deaths, _DEATHS_PATH)

            msg = f"{player} {death_msg}"
            logger.info("Death: %s %s — sending to %d chat(s)",
                        player, death_msg, _announce_chat_count())
            _send_to_chats(msg)
            return

        if event_type == "chat":
            # In-game chat relayed to the platforms (one-way; no cooldown so
            # distinct messages aren't suppressed). Gated by config.chat_relay
            # at the parser; nothing recorded.
            player = payload["player"]
            message = payload["message"]
            logger.info("Chat: %s: %s — sending to %d chat(s)",
                        player, message, _announce_chat_count())
            _send_to_chats(f"\U0001f4ac {player}: {message}")
            return

        name = payload
        # Online-time accumulation (Bedrock; no-op on Java). Done before the
        # cooldown gate so a quick rejoin still records the session boundary.
        pid = _uuid_by_name(name, names)
        if pid:
            BACKEND.record_player_session(event_type, pid)
            _note_active_xuid(pid)  # candidate for identity learning
            if event_type == "leave":
                # Refresh last_seen to the disconnect time (connect already set
                # it on join). No-op on Java for an unchanged name.
                BACKEND.register_name(pid, name)

        key = f"{name}-{event_type}"
        now = time.time()
        if now - _last_event.get(key, 0) < _cooldown:
            return
        _last_event[key] = now

        online = get_online_players()
        count = len(online)
        names_str = ", ".join(online) if online else "none"

        verb = "joined the game" if event_type == "join" else "left the game"
        status = "online" if event_type == "join" else "offline"
        msg = f"{name} {verb}\nPlayers online: {count} ({names_str})"

        logger.info("Notification: player %s %s — sending to %d chat(s)",
                    name, status, _announce_chat_count())
        _send_to_chats(msg)

        # Incremental backup triggers
        if event_type == "join" and count == 1:
            _start_incremental_cycle()
        elif event_type == "leave" and count == 0:
            _stop_incremental_cycle(final=True)

    return notify


# ---------------------------------------------------------------------------
# 7. Stats Logic
# ---------------------------------------------------------------------------
# Per-player stat dicts come from the backend (Java: world stat files; Bedrock:
# accumulated online time). Every dict has `name` + `time_played_hours`; only
# Java adds the richer fields, so the formatter shows whatever is present.
_STAT_FIELDS = [
    ("time_played_hours", "Time played", "h"),
    ("sessions", "Sessions", ""),
    ("deaths", "Deaths", ""),
    ("diamonds_mined", "Diamonds mined", ""),
    ("ancient_debris_mined", "Ancient debris mined", ""),
    ("distance_travelled_km", "Distance travelled", " km"),
    ("villager_trades", "Villager trades", ""),
    ("total_mobs_killed", "Mobs killed", ""),
    ("last_seen", "Last seen", ""),
]


def _format_stats(p: dict) -> str:
    lines = [f"Player: {p['name']}"]
    for key, label, unit in _STAT_FIELDS:
        if key in p and p[key] != "":
            lines.append(f"  {label}: {p[key]}{unit}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7b. Achievement Scanning
# ---------------------------------------------------------------------------
RE_GZ_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})-\d+\.log\.gz')


def _scan_log_for_achievements(file_path: Path, date_str: str,
                               names: dict, achievements: dict) -> int:
    count = 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = RE_UUID.match(line)
            if m:
                name, uuid = m.group(1), m.group(2)
                register_player(uuid, name, names, _NAMES_PATH)
                continue
            m = RE_ACHIEVEMENT.match(line)
            if m:
                time_str, player, ach_type_full, achievement = m.groups()
                ach_type = _ACH_TYPE_MAP[ach_type_full]
                timestamp = f"{date_str} {time_str}"
                uuid = _uuid_by_name(player, names)
                if uuid:
                    if record_achievement(uuid, achievement, ach_type,
                                          timestamp, achievements,
                                          _ACHIEVEMENTS_PATH):
                        count += 1
                else:
                    logger.warning("Scan: no UUID for player %s, skipping achievement", player)
    return count


def _scan_log_for_deaths(file_path: Path, date_str: str,
                         names: dict, deaths: dict) -> int:
    count = 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = RE_UUID.match(line)
            if m:
                name, uuid = m.group(1), m.group(2)
                register_player(uuid, name, names, _NAMES_PATH)
                continue
            m = RE_SERVER_MSG.match(line)
            if m:
                time_str, player, msg = m.groups()
                if any(msg.startswith(p) for p in _DEATH_PHRASES):
                    timestamp = f"{date_str} {time_str}"
                    uuid = _uuid_by_name(player, names)
                    if uuid:
                        if record_death(uuid, msg, timestamp, deaths,
                                        _DEATHS_PATH):
                            count += 1
                    else:
                        logger.warning("Scan: no UUID for player %s, skipping death", player)
    return count


def _scan_logs(scan_fn, names: dict, store: dict) -> int:
    """Run ``scan_fn`` over every rotated ``*.log.gz`` (by embedded date) plus the
    live log, returning the total newly-recorded count. Shared by /scan_deaths
    and /scan_achievements."""
    logs_dir = LOG_PATH.parent
    total = 0
    for gz_path in sorted(logs_dir.glob("*.log.gz")):
        m = RE_GZ_DATE.match(gz_path.name)
        if not m:
            continue
        date_str = m.group(1)
        extracted = gz_path.with_suffix("")  # remove .gz
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as gz_f:
                with open(extracted, "w", encoding="utf-8") as out_f:
                    out_f.write(gz_f.read())
            total += scan_fn(extracted, date_str, names, store)
        except Exception as e:
            logger.warning("Scan: failed to process %s: %s", gz_path.name, e)
        finally:
            if extracted.exists():
                extracted.unlink()
    if LOG_PATH.exists():
        total += scan_fn(LOG_PATH, datetime.now().strftime("%Y-%m-%d"), names, store)
    return total


# ---------------------------------------------------------------------------
# 7d. RCON & Backup
# ---------------------------------------------------------------------------
_backup_lock = threading.Lock()
_watcher_ref: LogWatcher | None = None

# Active chat adapters (Telegram, Slack, …) and the per-platform auth object,
# set in main(). Announcements fan out across all of them; command replies go
# only to the originating adapter (Context.reply).
ADAPTERS: list = []
_AUTH: dict = {}


def _announce(msg: str) -> int:
    """Send an announcement to every authorized chat on every platform. Returns
    how many chats it was sent to."""
    sent = 0
    for adapter in ADAPTERS:
        ns = _AUTH.get(adapter.name) or {}
        for chat_id in ns.get("authorized_chat_ids", []):
            try:
                adapter.send(chat_id, msg)
                sent += 1
            except Exception as e:
                logger.warning("Announce to %s/%s failed: %s",
                               adapter.name, chat_id, e)
    return sent


def _alert_admins(msg: str) -> None:
    """Send an operational alert to each platform's admin (if claimed)."""
    for adapter in ADAPTERS:
        admin = (_AUTH.get(adapter.name) or {}).get("admin_user_id")
        if admin:
            try:
                adapter.send(admin, msg)
            except Exception:
                logger.warning("Failed to alert %s admin", adapter.name)


def _announce_chat_count() -> int:
    return sum(len((_AUTH.get(a.name) or {}).get("authorized_chat_ids", []))
               for a in ADAPTERS)

# /restore_player pending-state, keyed by admin user_id.
# Forces the admin through the list -> select -> confirm sequence so a typo
# in a single command can't trigger a destructive restore.
_pending_player_restore: dict = {}
_pending_player_lock = threading.Lock()
_PENDING_PLAYER_RESTORE_TTL = 300  # seconds; older entries are treated as missing
_RESTORE_PLAYER_PAGE_SIZE = 10  # versions shown per page in /restore_player listing


def _add_world_file_to_zip(zf, fp: Path, rel: str, ready_map: dict | None) -> None:
    """Add ``fp`` to the zip under ``rel``, honouring an optional snapshot map.

    ``ready_map`` is None for Java (copy the file whole). For Bedrock it maps a
    relative path to the byte length reported by ``save query``:
      - listed file  -> copied truncated to that length (the consistent snapshot;
        Bedrock keeps appending past it),
      - unlisted file under a world ``db/`` directory -> skipped (a stale LevelDB
        fragment not part of the snapshot),
      - anything else (server.properties, packs, level.dat, ...) -> copied whole.
    """
    if ready_map is not None:
        if rel in ready_map:
            # Truncate to the snapshot length. Build the entry from the file so
            # its mode/mtime are preserved (writestr with a bare name drops the
            # Unix mode, which restore needs e.g. for executables).
            zi = zipfile.ZipInfo.from_file(fp, rel)
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(fp, "rb") as src:
                zf.writestr(zi, src.read(ready_map[rel]))
            return
        if "/db/" in rel:
            return
    zf.write(fp, rel)


# Bedrock per-player restore reads player data from a sidecar embedded in each
# backup zip (the live LevelDB is locked while the server runs). Dedup state of
# {player_server_key: sha256} so incrementals only carry players that changed.
_PLAYER_STATE_PATH = Path(__file__).parent / "bedrock_player_state.json"


def _load_player_state() -> dict:
    try:
        return json.loads(_PLAYER_STATE_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed to read bedrock_player_state.json")
        return {}


def _save_player_state(hashes: dict) -> None:
    try:
        _PLAYER_STATE_PATH.write_text(json.dumps(hashes))
    except Exception:
        logger.exception("Failed to write bedrock_player_state.json")


def _maybe_learn_player(sidecar: dict, log) -> None:
    """Bind a player's xuid to their (account-stable) identity uuids by process
    of elimination from a backup's changed-player sidecar.

    The xuid->identity link isn't in the world db, so it's inferred: the changed
    sidecar lists exactly the players whose data changed since the last backup,
    grouped into server keys (each with its MsaId+SelfSignedId). If exactly one
    of those keys is still unattributed AND exactly one xuid that was online
    since the last backup still has no identities, they must be the same player —
    bind them. Handles a lone player (even one who already left) and "N online,
    N-1 already known". Ambiguous cases are skipped and retried next backup.

    Lives here (not the backend) because it needs the bot's session/online state
    and the name registry; the backend supplies the known-identity facts.
    """
    # Changed server keys -> their identity uuids.
    by_key: dict = {}
    for ident, key in sidecar.get("mappings", {}).items():
        by_key.setdefault(key, []).append(ident)
    if not by_key:
        return

    known = BACKEND.known_identities()
    unattributed = [k for k, idents in by_key.items()
                    if not any(i in known for i in idents)]

    names = load_player_names()
    with _session_lock:
        session = list(_session_xuids)
    unknown_active = [x for x in session if not BACKEND.player_identities(x)]

    if len(unattributed) == 1 and len(unknown_active) == 1:
        key = unattributed[0]
        xuid = unknown_active[0]
        name = names.get(xuid, xuid)
        if BACKEND.learn_player(name, xuid, by_key[key]):
            log(f"Learned Bedrock identity for {name}")

    # Drop players who have left; keep still-online ones for the next backup.
    online_xuids = {_uuid_by_name(n, names) for n in get_online_players()}
    online_xuids.discard(None)
    with _session_lock:
        _session_xuids.intersection_update(online_xuids)


def _write_player_sidecar(zf, ready, full_backup: bool, log) -> None:
    """Embed the _players.json sidecar into an open Bedrock backup zip.

    No-op for Java or when there's no snapshot file set. Generates the sidecar
    from the snapshot db files, hash-dedups against the persisted state (full
    backup = baseline/everyone + reset state; incremental = changed-only), and
    never fails the backup — if the amulet libs are missing it just logs and
    skips, so per-player restore is unavailable for that zip but the backup is
    otherwise complete.
    """
    if CONFIG.edition != EDITION_BEDROCK or not ready:
        return
    try:
        from utils import bedrock_player
    except Exception as e:
        log(f"Player sidecar skipped (amulet libs unavailable: {e})")
        return
    db_files = [(p, n) for (p, n) in ready
                if "/db/" in str(p).replace("\\", "/")]
    if not db_files:
        return
    try:
        sidecar = bedrock_player.build_sidecar_from_files(db_files)
        prev = {} if full_backup else _load_player_state()
        filtered, new_hashes = bedrock_player.filter_sidecar_changed(sidecar, prev)
        zf.writestr(bedrock_player.SIDECAR_NAME, json.dumps(filtered))
        _save_player_state(new_hashes)
        log(f"Player sidecar: {len(filtered['players'])} player(s) "
            f"({len(new_hashes)} total)")
        _maybe_learn_player(filtered, log)
    except Exception as e:
        logger.exception("Player sidecar generation failed")
        log(f"Player sidecar generation failed: {e}")


def run_backup(status_cb=None):
    """Run a full server backup.

    Full backup process:
      1. begin_save: freeze the world and flush pending writes
         (Java: save-off + save-all; Bedrock: save hold).
      2. Determine the consistent file set (files_ready):
         - Java returns None -> wait for the filesystem to settle, then zip the
           whole server directory.
         - Bedrock returns [(path, max_bytes), ...] -> zip each file truncated
           to its snapshot length.
      3. end_save: resume normal saving (always, even if zip fails).
      4. Run BACKUP_COPY_CMD if configured (e.g., rsync to remote storage).
      5. Start a new incremental chain: generate a fresh chain ID, rebuild
         the file manifest (mtime baseline), and write the chain marker.
    """
    def status(msg):
        logger.info("Backup: %s", msg)
        if status_cb:
            status_cb(msg)

    if not BACKEND.is_available():
        raise RuntimeError("Server backend not available")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: Freeze the world and flush pending writes (edition-specific).
    BACKEND.begin_save(status)

    mc_dir = Path(MINECRAFT_DIR)
    # Uses relative paths inside the zip so the restore tool can extract
    # directly into any target directory.
    dir_name = mc_dir.name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_path = BACKUP_DIR / f"{dir_name}_{timestamp}.zip"
    backup_dir_resolved = BACKUP_DIR.resolve()

    try:
        # Step 2: Get the consistent file set to copy. Java returns None (copy
        # the whole settled directory); Bedrock returns snapshot byte-lengths so
        # the walk truncates listed files and skips stale, unlisted db/ files.
        ready = BACKEND.files_ready(status)
        ready_map = None
        if ready is None:
            # Java: the server may still be flushing to disk after save-all,
            # so wait for the filesystem to settle before zipping.
            wait_for_settle(mc_dir, BACKUP_DIR, log_fn=status,
                            exclude_names=_BACKUP_EXCLUDE_NAMES)
        else:
            ready_map = {str(p.relative_to(mc_dir)).replace("\\", "/"): n
                         for p, n in ready}

        status(f"Zipping {mc_dir} ...")
        with zipfile.ZipFile(final_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirnames, filenames in os.walk(mc_dir):
                dp = Path(dirpath)
                # Skip the backup directory if it's inside the server directory
                try:
                    dp.resolve().relative_to(backup_dir_resolved)
                    continue
                except ValueError:
                    pass
                for fn in filenames:
                    # Chain marker and bot infrastructure are not server data
                    if fn == CHAIN_MARKER_NAME or fn in _BACKUP_EXCLUDE_NAMES:
                        continue
                    fp = dp / fn
                    rel = str(fp.relative_to(mc_dir)).replace("\\", "/")
                    _add_world_file_to_zip(zf, fp, rel, ready_map)
            # Bedrock: embed the full player-data sidecar (baseline).
            _write_player_sidecar(zf, ready, full_backup=True, log=status)
        size_mb = final_path.stat().st_size / (1024 * 1024)
        status(f"Backup saved: {final_path.name} ({size_mb:.1f} MB)")
    finally:
        # Step 3: Always resume normal saving, even if zip fails
        BACKEND.end_save(status)

    # Step 4: Copy off-server if configured (e.g., rsync to NAS/cloud)
    run_copy_command(final_path, log_fn=status)

    # Step 5: Start a new incremental chain
    # Every full backup starts a fresh chain. The manifest records the mtime
    # of every file, which becomes the baseline for detecting changes in
    # subsequent incremental backups. The chain marker is written to the
    # server directory so the bot can detect if the server state is replaced
    # while it's offline.
    try:
        chain_id = new_chain_id(BACKUP_DIR)
        fresh_files = build_file_manifest(Path(MINECRAFT_DIR), BACKUP_DIR,
                                          _BACKUP_EXCLUDE_NAMES)
        _save_manifest(fresh_files, chain_id=chain_id, base_full=final_path.name)
        _write_chain_marker(chain_id)
        logger.info("Backup: new chain %s established (base: %s)",
                    chain_id, final_path.name)
    except Exception:
        logger.exception("Failed to reset incremental manifest after full backup")

    return str(final_path)


# ---------------------------------------------------------------------------
# 7e. Incremental Backup
# ---------------------------------------------------------------------------
# Incremental backups capture only files that changed since the last backup
# (full or incremental). They are triggered automatically while players are
# online, on a configurable interval (INCREMENTAL_INTERVAL_MINUTES).
#
# How change detection works:
#   - The manifest (backup_manifest.json) stores {relative_path: mtime} for
#     every file in the server directory at the time of the last backup.
#   - Before each incremental, we walk the server directory again and compare
#     mtimes against the manifest. Files with different mtimes or new files
#     are "changed"; files in the manifest but missing from disk are "deleted".
#   - Only changed/added files are zipped. Deleted paths are recorded in a
#     _deletions.json file inside the zip.
#
# Each incremental zip also contains _meta.json with the chain_id and
# base_full filename, making it self-describing for the restore tool.
#
# The incremental cycle is player-activity-driven:
#   - Starts when the first player joins the server
#   - Runs every INCREMENTAL_INTERVAL_MINUTES while players are online
#   - Stops when the last player leaves (with one final backup)
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).parent / "backup_manifest.json"
_CHAIN_MARKER_PATH = Path(MINECRAFT_DIR) / ".mcnotifier_chain"
_incr_timer: threading.Timer | None = None
_incr_lock = threading.Lock()  # protects _incr_timer


def _diff_manifest(old: dict, new: dict) -> tuple:
    """Compare two file manifests and return (changed_or_added, deleted).

    Each manifest is {relative_path: mtime}. A file is "changed" if its mtime
    differs or it's new. A file is "deleted" if it was in old but not in new.
    """
    changed = []
    for path, mtime in new.items():
        if path not in old or old[path] != mtime:
            changed.append(path)
    deleted = [path for path in old if path not in new]
    return changed, deleted


def _load_manifest() -> tuple:
    """Load backup_manifest.json. Returns (chain_id, base_full, files_dict).

    Returns ("", "", {}) if the manifest is missing or corrupt, which
    effectively means "no chain established — skip incremental backups".
    """
    if _MANIFEST_PATH.exists():
        try:
            with open(_MANIFEST_PATH) as f:
                data = json.load(f)
            return (data.get("chain_id", ""),
                    data.get("base_full", ""),
                    data.get("files", {}))
        except Exception:
            logger.exception("Failed to load backup_manifest.json")
    return "", "", {}


def _save_manifest(files: dict, chain_id: str, base_full: str) -> None:
    """Write the manifest with the current chain state and file mtimes."""
    with open(_MANIFEST_PATH, "w") as f:
        json.dump({"chain_id": chain_id, "base_full": base_full,
                    "files": files}, f)


def _write_chain_marker(chain_id: str) -> None:
    """Write chain ID to .mcnotifier_chain in MINECRAFT_DIR.

    This marker file lets the bot detect on startup if the server state
    was replaced while it was offline (e.g., manual restore). If the marker
    doesn't match the manifest's chain_id, the chain is considered invalid.
    """
    try:
        with open(_CHAIN_MARKER_PATH, "w") as f:
            f.write(chain_id)
    except Exception:
        logger.exception("Failed to write chain marker")


def _read_chain_marker() -> str:
    """Read chain ID from .mcnotifier_chain, returns '' if missing."""
    try:
        return _CHAIN_MARKER_PATH.read_text().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        logger.exception("Failed to read chain marker")
        return ""


# ---------------------------------------------------------------------------
# Player-data restore helpers (used by /restore_player)
# ---------------------------------------------------------------------------


def _format_versions_reply(username: str, uuid: str, versions: list,
                           offset: int = 0) -> str:
    """Render one page of the numbered list reply for /restore_player <username>."""
    if not versions:
        return (f"No player data found for {username} ({uuid}).\n"
                f"Live file missing and no backups in the active chain.")
    page = versions[offset:offset + _RESTORE_PLAYER_PAGE_SIZE]
    lines = [f"Player data versions for {username}  (UUID: {uuid})",
             "Latest first. To select, send: /restore_player "
             f"{username} <number>", ""]
    for i, v in enumerate(page, offset + 1):
        lines.append(f"  {i:3d}.  {v['timestamp']}   {v['source']}")
    remaining = len(versions) - (offset + _RESTORE_PLAYER_PAGE_SIZE)
    if remaining > 0:
        lines.append(f"\n{remaining} more. Send: /restore_player {username} more")
    return "\n".join(lines)


def _format_confirm_reply(username: str, uuid: str, n: int, version: dict) -> str:
    """Render the step-2 confirmation block."""
    return (
        "Confirm restore:\n"
        f"  Player:    {username}\n"
        f"  UUID:      {uuid}\n"
        f"  Timestamp: {version['timestamp']}\n"
        f"  Source:    {version['source']}\n"
        "\n"
        "  To proceed, send:\n"
        f"    /restore_player {username} {n} confirm"
    )


def _get_pending_player_restore(user_id: int,
                                expected_username: str | None = None,
                                expected_stage: str | None = None) -> dict | None:
    """Lookup pending player restore for an admin, with TTL and match checks.

    Returns the pending entry if it exists, hasn't expired, matches the
    expected username (case-insensitive), and is in the expected stage.
    Otherwise returns None and (if expired) discards the stale entry.
    """
    with _pending_player_lock:
        entry = _pending_player_restore.get(user_id)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _PENDING_PLAYER_RESTORE_TTL:
            _pending_player_restore.pop(user_id, None)
            return None
        if (expected_username is not None
                and entry["username"].lower() != expected_username.lower()):
            return None
        if expected_stage is not None and entry["stage"] != expected_stage:
            return None
        return entry


def _set_pending_player_restore(user_id: int, **fields) -> None:
    """Store/update a pending player restore entry with the current timestamp."""
    with _pending_player_lock:
        existing = _pending_player_restore.get(user_id, {})
        existing.update(fields)
        existing["ts"] = time.time()
        _pending_player_restore[user_id] = existing


def _clear_pending_player_restore(user_id: int) -> None:
    with _pending_player_lock:
        _pending_player_restore.pop(user_id, None)


def run_incremental_backup() -> str | None:
    """Run an incremental backup of changed files. Returns zip path or None.

    Incremental backup process:
      1. Load the manifest to get the current chain_id and file mtime baseline.
      2. Walk the server directory and compare mtimes to detect changes.
      3. If changes found: RCON save-off/save-all to flush world data, then
         re-scan to capture any newly flushed changes.
      4. Zip only the changed/added files, plus _deletions.json and _meta.json.
      5. Update the manifest with the new file mtimes (same chain_id).
      6. RCON save-on to re-enable auto-save.
      7. Run BACKUP_COPY_CMD if configured.

    Returns None if: no chain established, no changes detected, another backup
    is in progress, or the backup fails.
    """
    if not _backup_lock.acquire(blocking=False):
        logger.info("Incremental backup skipped: another backup is in progress")
        return None

    try:
        mc_dir = Path(MINECRAFT_DIR)
        chain_id, base_full, old_files = _load_manifest()

        # A chain must be established (by a full backup or restore) before
        # incremental backups can run. Without a chain, we don't know which
        # full backup these incrementals belong to.
        if not chain_id:
            logger.warning("Incremental backup skipped: no chain established. "
                           "Run a full backup first.")
            return None

        # First pass: quick scan to see if anything changed at all
        new_manifest = build_file_manifest(mc_dir, BACKUP_DIR, _BACKUP_EXCLUDE_NAMES)

        changed, deleted = _diff_manifest(old_files, new_manifest)
        if not changed and not deleted:
            logger.info("Incremental backup: no changes detected, skipping")
            return None

        logger.info("Incremental backup: %d changed/added, %d deleted",
                     len(changed), len(deleted))

        if not BACKEND.is_available():
            logger.warning("Incremental backup skipped: server backend not available")
            return None

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        inc_log = lambda msg: logger.info("Incremental backup: %s", msg)

        # Freeze the world state and flush pending writes (edition-specific) —
        # ensures we zip consistent file state, not partially-written files.
        BACKEND.begin_save(inc_log)

        try:
            # Determine the consistent file set, then re-scan to capture any
            # changes flushed by the save before computing the final diff.
            ready = BACKEND.files_ready(inc_log)
            if ready is None:
                # Java: the server may still be flushing after the save, so wait
                # for the filesystem to settle before diffing.
                new_manifest = wait_for_settle(mc_dir, BACKUP_DIR, log_fn=inc_log,
                                               exclude_names=_BACKUP_EXCLUDE_NAMES)
                ready_map = None
            else:
                # Bedrock: snapshot lengths are authoritative; no settle needed.
                new_manifest = build_file_manifest(mc_dir, BACKUP_DIR, _BACKUP_EXCLUDE_NAMES)
                ready_map = {str(p.relative_to(mc_dir)).replace("\\", "/"): n
                             for p, n in ready}
            changed, deleted = _diff_manifest(old_files, new_manifest)

            # Build the incremental zip with chain ID in the filename
            dir_name = mc_dir.name
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_name = f"{dir_name}_incr_{chain_id}_{timestamp}"
            zip_path = BACKUP_DIR / f"{zip_name}.zip"

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add changed/added files. On Bedrock, listed files are truncated
                # to their snapshot length and stale unlisted db/ files skipped.
                for rel_path in changed:
                    full_path = mc_dir / rel_path
                    if not full_path.exists():
                        continue
                    _add_world_file_to_zip(zf, full_path, rel_path, ready_map)
                # Record deleted files so restore can remove them too
                if deleted:
                    zf.writestr("_deletions.json", json.dumps(deleted, indent=2))
                # Embed chain metadata so restore.py can discover this
                # incremental's chain membership without external state
                zf.writestr("_meta.json", json.dumps({
                    "chain_id": chain_id, "base_full": base_full}))
                # Bedrock: embed the changed-only player-data sidecar.
                _write_player_sidecar(zf, ready, full_backup=False, log=inc_log)

            size_mb = zip_path.stat().st_size / (1024 * 1024)
            logger.info("Incremental backup saved: %s (%.1f MB, %d files)",
                        zip_path.name, size_mb, len(changed))

            # Update the manifest: same chain, but new mtime baseline
            _save_manifest(new_manifest, chain_id=chain_id, base_full=base_full)

        finally:
            # Always resume normal saving
            BACKEND.end_save(inc_log)

        # Checkpoint online-time stats so a crash loses at most one interval of
        # in-progress playtime (no-op on Java).
        BACKEND.checkpoint_open_sessions()

        # Copy off-server if configured
        run_copy_command(zip_path, log_fn=lambda msg: logger.info("Incremental backup: %s", msg))

        return str(zip_path)

    except Exception:
        logger.exception("Incremental backup failed")
        return None
    finally:
        _backup_lock.release()


# --- Incremental backup cycle (player-activity-driven) ---
# Uses a threading.Timer to run incremental backups at regular intervals
# while players are online. The cycle starts when the first player joins
# and stops when the last player leaves.

def _incremental_cycle():
    """Run one incremental backup, then reschedule if the cycle is still active."""
    global _incr_timer
    try:
        run_incremental_backup()
    finally:
        with _incr_lock:
            if _incr_timer is not None:  # cycle still active (not stopped)
                _incr_timer = threading.Timer(
                    INCREMENTAL_INTERVAL_MINUTES * 60, _incremental_cycle)
                _incr_timer.daemon = True
                _incr_timer.start()


def _start_incremental_cycle():
    """Start the incremental backup cycle if not already running.

    Called when the first player joins the server.
    """
    global _incr_timer
    if not INCREMENTAL_BACKUP_ENABLED:
        return
    with _incr_lock:
        if _incr_timer is not None:
            return  # already running
        logger.info("Incremental backup cycle started (every %d min)",
                    INCREMENTAL_INTERVAL_MINUTES)
        _incr_timer = threading.Timer(
            INCREMENTAL_INTERVAL_MINUTES * 60, _incremental_cycle)
        _incr_timer.daemon = True
        _incr_timer.start()


def _stop_incremental_cycle(final: bool = False):
    """Stop the incremental backup cycle.

    Called when the last player leaves the server.
    If final=True, runs one last incremental backup to capture any remaining
    changes from the play session.
    """
    global _incr_timer
    if not INCREMENTAL_BACKUP_ENABLED:
        return
    with _incr_lock:
        if _incr_timer is None:
            return
        _incr_timer.cancel()
        _incr_timer = None
    logger.info("Incremental backup cycle stopped")
    if final:
        logger.info("Running final incremental backup before stop")
        threading.Thread(target=run_incremental_backup, daemon=True).start()


# ---------------------------------------------------------------------------
# 8. Authorization System
# ---------------------------------------------------------------------------
_AUTH_PATH = Path(__file__).parent / "auth.json"
_auth_lock = threading.Lock()


def _normalize_ns(ns: dict) -> dict:
    """Normalize one platform's auth namespace: string IDs throughout."""
    admin = ns.get("admin_user_id")
    return {
        "admin_user_id": str(admin) if admin is not None else None,
        "authorized_chat_ids": [str(c) for c in ns.get("authorized_chat_ids", [])],
    }


def load_auth(path: Path) -> dict:
    """Load per-platform auth: {platform: {admin_user_id, authorized_chat_ids}}.

    Migrates the pre-multi-platform flat shape ({admin_user_id,
    authorized_chat_ids}) into the ``telegram`` namespace so existing installs
    keep their admin and whitelist.
    """
    data = {}
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            logger.exception("Failed to load auth.json")
    if "admin_user_id" in data or "authorized_chat_ids" in data:
        data = {"telegram": data}  # migrate flat -> telegram namespace
    return {platform: _normalize_ns(ns) for platform, ns in data.items()}


def save_auth(auth: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(auth, f, indent=2)


def _auth_ns(auth: dict, platform: str) -> dict:
    return auth.setdefault(
        platform, {"admin_user_id": None, "authorized_chat_ids": []})


def is_admin(platform: str, user_id, auth: dict) -> bool:
    admin = (auth.get(platform) or {}).get("admin_user_id")
    return admin is not None and str(admin) == str(user_id)


def is_authorized(platform: str, chat_id, user_id, is_private: bool,
                  auth: dict) -> bool:
    """A command is processed only from the platform admin (in private) or an
    authorized chat (in a group/channel)."""
    ns = auth.get(platform) or {}
    if is_private:
        admin = ns.get("admin_user_id")
        return admin is not None and str(admin) == str(user_id)
    return str(chat_id) in ns.get("authorized_chat_ids", [])


# ---------------------------------------------------------------------------
# 9. Bot Commands
# ---------------------------------------------------------------------------
def register_commands(router, auth: dict, names: dict,
                      achievements: dict, deaths: dict) -> None:
    """Register every command on the platform-agnostic router. Handlers take a
    Context and reply via it, so the same logic serves any chat platform."""

    # --- /start, /help ---
    def cmd_help(ctx):
        logger.info("Help: requested by %s", ctx.sender_label)
        lines = [
            "Available commands:",
            "/status — show online players",
            "/list — list all known players",
        ]
        if BACKEND.supports(CAP_STATS):
            lines += [
                "/stats [player] — player statistics",
                "/playtime — playtime leaderboard",
            ]
        if BACKEND.supports(EVENT_ACHIEVEMENT):
            lines.append("/achievements [player] — player achievements")
        if BACKEND.supports(EVENT_DEATH):
            lines += [
                "/deaths [player] — death history",
                "/death_summary — deaths grouped by cause",
            ]
        lines.append("/chat_id — show this chat's ID")
        if ctx.is_private and is_admin(ctx.platform, ctx.user_id, auth):
            lines += [
                "/authorize <chat_id> — whitelist a chat",
                "/revoke <chat_id> — remove a chat from whitelist",
                "/listchats — list authorized chats",
            ]
            if BACKEND.supports(EVENT_ACHIEVEMENT):
                lines.append("/scan_achievements — scan all logs for achievements")
            if BACKEND.supports(EVENT_DEATH):
                lines.append("/scan_deaths — scan all logs for deaths")
            lines.append("/backup — trigger a server backup now")
            lines.append(f"/allowlist <on|off|add|remove|list|reload> [player] "
                         f"— server {BACKEND.ALLOWLIST_VERB}")
        ctx.reply("\n".join(lines))
    router.register(["start", "help"], cmd_help)

    # --- /status ---
    def cmd_status(ctx):
        logger.info("Status: requested by %s", ctx.sender_label)
        online = get_online_players()
        if online:
            ctx.reply(f"Players online: {len(online)} ({', '.join(online)})")
        else:
            ctx.reply("No players currently online.")
    router.register("status", cmd_status)

    # --- /stats ---
    def cmd_stats(ctx):
        refresh_player_names(names)
        target = ctx.args[0].lower() if ctx.args else None
        logger.info("Stats: requested by %s (player=%s)", ctx.sender_label, target or "all")
        all_stats = BACKEND.player_stats(names)
        if not all_stats:
            ctx.reply("No player statistics recorded yet.")
            return
        if target:
            matches = [p for p in all_stats if p["name"].lower() == target]
            if not matches:
                ctx.reply(f"No player found matching '{target}'.")
                return
            ctx.reply(_format_stats(matches[0]))
        else:
            lines = [_format_stats(p) for p in sorted(all_stats, key=lambda p: p["name"].lower())]
            ctx.reply("\n\n".join(lines))
    router.register("stats", cmd_stats, cap=lambda: BACKEND.supports(CAP_STATS),
                    cap_message="Player statistics are not available on this server edition.")

    # --- /playtime ---
    def cmd_playtime(ctx):
        refresh_player_names(names)
        logger.info("Playtime: requested by %s", ctx.sender_label)
        all_stats = BACKEND.player_stats(names)
        if not all_stats:
            ctx.reply("No player statistics recorded yet.")
            return
        ranked = sorted(all_stats, key=lambda p: p["time_played_hours"], reverse=True)
        lines = [f"{i+1}. {p['name']} — {p['time_played_hours']}h" for i, p in enumerate(ranked)]
        ctx.reply("Playtime leaderboard:\n" + "\n".join(lines))
    router.register("playtime", cmd_playtime, cap=lambda: BACKEND.supports(CAP_STATS),
                    cap_message="Playtime is not available on this server edition.")

    # --- /list ---
    def cmd_list(ctx):
        refresh_player_names(names)
        logger.info("List: requested by %s", ctx.sender_label)
        entries = BACKEND.list_known_players(names)
        if not entries:
            ctx.reply("No players found.")
            return
        ctx.reply("Known players:\n" + "\n".join(entries))
    router.register("list", cmd_list)

    # --- /achievements ---
    def cmd_achievements(ctx):
        refresh_player_names(names)
        target = ctx.args[0].lower() if ctx.args else None
        logger.info("Achievements: requested by %s (player=%s)", ctx.sender_label, target or "all")
        if not achievements:
            ctx.reply("No achievements recorded yet.")
            return
        if target:
            uuid = next((u for u, n in names.items() if n.lower() == target), None)
            if not uuid or uuid not in achievements:
                ctx.reply(f"No achievements found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            lines = [f"Achievements for {player_name}:"]
            current_date = None
            for e in sorted(achievements[uuid], key=lambda x: x["timestamp"]):
                date_part, time_part = e["timestamp"].split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_part[:5]} | {e['type']:<11} | {e['achievement']}")
            ctx.reply("\n".join(lines), monospace=True)
        else:
            lines = []
            for uuid, entries in sorted(achievements.items(),
                                        key=lambda x: names.get(x[0], x[0]).lower()):
                lines.append(f"{names.get(uuid, uuid)}: {len(entries)} achievement(s)")
            ctx.reply("Achievements summary:\n" + "\n".join(lines))
    router.register("achievements", cmd_achievements,
                    cap=lambda: BACKEND.supports(EVENT_ACHIEVEMENT),
                    cap_message="Achievements are not tracked on this server edition.")

    # --- /scan_achievements ---
    def cmd_scan_achievements(ctx):
        logger.info("ScanAchievements: requested by %s", ctx.sender_label)
        refresh_player_names(names)
        ctx.reply("Scanning log files for achievements...")
        total = _scan_logs(_scan_log_for_achievements, names, achievements)
        ctx.reply(f"Scan complete. {total} new achievement(s) recorded.")
        logger.info("ScanAchievements: %d new achievement(s) found", total)
    router.register("scan_achievements", cmd_scan_achievements,
                    private_only=True, admin_only=True,
                    cap=lambda: BACKEND.supports(EVENT_ACHIEVEMENT),
                    cap_message="Achievements are not tracked on this server edition.")

    # --- /deaths ---
    def cmd_deaths(ctx):
        refresh_player_names(names)
        target = ctx.args[0].lower() if ctx.args else None
        logger.info("Deaths: requested by %s (player=%s)", ctx.sender_label, target or "all")
        if not deaths:
            ctx.reply("No deaths recorded yet.")
            return
        if target:
            uuid = next((u for u, n in names.items() if n.lower() == target), None)
            if not uuid or uuid not in deaths:
                ctx.reply(f"No deaths found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            entries = deaths[uuid]
            lines = [f"Deaths for {player_name} ({len(entries)} total):"]
            current_date = None
            for e in sorted(entries, key=lambda x: x["timestamp"]):
                date_part, time_part = e["timestamp"].split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_part[:5]} | {e['message']}")
            ctx.reply("\n".join(lines), monospace=True)
        else:
            ranked = sorted(((names.get(u, u), len(e)) for u, e in deaths.items()),
                            key=lambda x: x[1], reverse=True)
            lines = ["Death summary:"]
            for player_name, count in ranked:
                lines.append(f"  {player_name}: {count} death(s)")
            ctx.reply("\n".join(lines), monospace=True)
    router.register("deaths", cmd_deaths, cap=lambda: BACKEND.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /death_summary ---
    def cmd_death_summary(ctx):
        refresh_player_names(names)
        logger.info("DeathSummary: requested by %s", ctx.sender_label)
        if not deaths:
            ctx.reply("No deaths recorded yet.")
            return
        categories = {}
        grand_total = 0
        for uuid, entries in deaths.items():
            player_name = names.get(uuid, uuid)
            for e in entries:
                cat = _categorize_death(e["message"])
                counts = categories.setdefault(cat, {})
                counts[player_name] = counts.get(player_name, 0) + 1
                grand_total += 1
        lines = [f"Death Summary ({grand_total} total)", ""]
        ordered = [cat for cat, _ in _DEATH_CATEGORIES]
        for cat in ordered:
            if cat not in categories:
                continue
            counts = categories.pop(cat)
            lines.append(f"{cat}: {sum(counts.values())}")
            for player_name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")
        if "Other" in categories:
            counts = categories["Other"]
            lines.append(f"Other: {sum(counts.values())}")
            for player_name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")
        ctx.reply("\n".join(lines).rstrip(), monospace=True)
    router.register("death_summary", cmd_death_summary,
                    cap=lambda: BACKEND.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /scan_deaths ---
    def cmd_scan_deaths(ctx):
        logger.info("ScanDeaths: requested by %s", ctx.sender_label)
        refresh_player_names(names)
        ctx.reply("Scanning log files for deaths...")
        total = _scan_logs(_scan_log_for_deaths, names, deaths)
        ctx.reply(f"Scan complete. {total} new death(s) recorded.")
        logger.info("ScanDeaths: %d new death(s) found", total)
    router.register("scan_deaths", cmd_scan_deaths,
                    private_only=True, admin_only=True,
                    cap=lambda: BACKEND.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /backup ---
    def cmd_backup(ctx):
        if not _backup_lock.acquire(blocking=False):
            ctx.reply("A backup is already in progress.")
            return
        logger.info("Backup: manually triggered by %s", ctx.sender_label)
        ctx.reply("Starting backup...")
        say = lambda m: ctx.adapter.send(ctx.chat_id, m)

        def run():
            try:
                path = run_backup(status_cb=say)
                say(f"Backup complete: {Path(path).name}")
            except Exception as e:
                logger.exception("Backup failed")
                say(f"Backup failed: {e}")
            finally:
                _backup_lock.release()

        threading.Thread(target=run, daemon=True).start()
    router.register("backup", cmd_backup, private_only=True, admin_only=True)

    # --- /allowlist (server whitelist/allowlist passthrough) ---
    _ALLOWLIST_SUBS = {"on", "off", "add", "remove", "list", "reload"}

    def cmd_allowlist(ctx):
        verb = BACKEND.ALLOWLIST_VERB
        usage = (f"Usage: /allowlist <on|off|add|remove|list|reload> [player]\n"
                 f"(runs the server '{verb}' command)")
        if not ctx.args:
            ctx.reply(usage)
            return
        sub = ctx.args[0].lower()
        if sub not in _ALLOWLIST_SUBS:
            ctx.reply(f"Unknown subcommand '{ctx.args[0]}'.\n{usage}")
            return
        if sub in ("add", "remove") and len(ctx.args) < 2:
            ctx.reply(f"Usage: /allowlist {sub} <player>")
            return
        if not BACKEND.is_available():
            ctx.reply("Server is not reachable right now — try again once it's up.")
            return
        logger.info("Allowlist: %s ran '%s %s'", ctx.sender_label, verb,
                    " ".join(ctx.args))

        # Run in a thread: Bedrock capture polls the log for up to a few seconds.
        def run():
            try:
                resp = BACKEND.allowlist_command(ctx.args)
            except Exception as e:
                logger.exception("Allowlist command failed")
                ctx.adapter.send(ctx.chat_id, f"{verb} command failed: {e}")
                return
            ctx.adapter.send(ctx.chat_id,
                             resp.strip() or "(server returned no output)",
                             monospace=True)

        threading.Thread(target=run, daemon=True).start()
    router.register("allowlist", cmd_allowlist, private_only=True, admin_only=True)

    # --- /restore_player ---
    def cmd_restore_player(ctx):
        pkey = f"{ctx.platform}:{ctx.user_id}"
        if not ctx.args:
            ctx.reply("Usage:\n"
                      "  /restore_player <username>\n"
                      "  /restore_player <username> more\n"
                      "  /restore_player <username> <N>\n"
                      "  /restore_player <username> <N> confirm")
            return
        typed_name = ctx.args[0]
        typed_n = ctx.args[1] if len(ctx.args) >= 2 else None
        typed_confirm = ctx.args[2] if len(ctx.args) >= 3 else None
        if typed_confirm is not None and typed_confirm.lower() != "confirm":
            ctx.reply(f"Unexpected argument: '{typed_confirm}' (did you mean 'confirm'?)")
            return
        resolved = BACKEND.resolve_player(typed_name, names)
        if resolved is None:
            ctx.reply(f"Unknown player: {typed_name}")
            return
        canonical, uuid = resolved

        if typed_n is None:
            versions = BACKEND.list_player_versions(uuid, _load_manifest()[:2])
            if not versions:
                _clear_pending_player_restore(pkey)
                ctx.reply(_format_versions_reply(canonical, uuid, versions))
                return
            _set_pending_player_restore(
                pkey, stage="listed", username=canonical, uuid=uuid,
                versions=versions, selected_n=None, page_offset=0)
            logger.info("RestorePlayer: %s listed %d version(s) for %s",
                        ctx.sender_label, len(versions), canonical)
            ctx.reply(_format_versions_reply(canonical, uuid, versions, offset=0))
            return

        if typed_n.lower() == "more":
            entry = _get_pending_player_restore(pkey, expected_username=canonical)
            if entry is None:
                ctx.reply(f"Run /restore_player {canonical} first to see the list.")
                return
            new_offset = entry.get("page_offset", 0) + _RESTORE_PLAYER_PAGE_SIZE
            versions = entry["versions"]
            if new_offset >= len(versions):
                ctx.reply(f"No more versions for {canonical}.")
                return
            _set_pending_player_restore(pkey, page_offset=new_offset)
            logger.info("RestorePlayer: %s paged to offset %d for %s",
                        ctx.sender_label, new_offset, canonical)
            ctx.reply(_format_versions_reply(canonical, uuid, versions, offset=new_offset))
            return

        try:
            n = int(typed_n)
        except ValueError:
            ctx.reply(f"Invalid selection: '{typed_n}'. Use a number or 'more'.")
            return

        if typed_confirm is not None:
            entry = _get_pending_player_restore(
                pkey, expected_username=canonical, expected_stage="selected")
            if entry is None or entry.get("selected_n") != n:
                ctx.reply(f"You must select a timestamp first with "
                          f"/restore_player {canonical} {n}")
                return
            versions = BACKEND.list_player_versions(uuid, _load_manifest()[:2])
            if not (1 <= n <= len(versions)):
                _clear_pending_player_restore(pkey)
                ctx.reply(f"Selection {n} is no longer valid (only "
                          f"{len(versions)} version(s) available). "
                          f"Run /restore_player {canonical} again.")
                return
            version = versions[n - 1]
            logger.info("RestorePlayer: %s confirmed restore of %s to %s (source: %s)",
                        ctx.sender_label, canonical, version["timestamp"], version["source"])
            ctx.reply(f"Starting restore of {canonical} to {version['timestamp']}...")
            _clear_pending_player_restore(pkey)
            say = lambda m: ctx.adapter.send(ctx.chat_id, m)

            def run():
                if not _backup_lock.acquire(blocking=False):
                    say("A backup or restore is in progress.")
                    return
                try:
                    BACKEND.restore_player(canonical, uuid, version, say)
                finally:
                    _backup_lock.release()

            threading.Thread(target=run, daemon=True).start()
            return

        entry = _get_pending_player_restore(pkey, expected_username=canonical)
        if entry is None:
            ctx.reply(f"Run /restore_player {canonical} first to see the list.")
            return
        versions = entry["versions"]
        if not (1 <= n <= len(versions)):
            ctx.reply(f"Invalid selection: {n}. Choose 1-{len(versions)}.")
            return
        version = versions[n - 1]
        _set_pending_player_restore(
            pkey, stage="selected", username=canonical, uuid=uuid,
            versions=versions, selected_n=n)
        logger.info("RestorePlayer: %s selected version %d (%s) for %s",
                    ctx.sender_label, n, version["source"], canonical)
        ctx.reply(_format_confirm_reply(canonical, uuid, n, version))
    router.register("restore_player", cmd_restore_player,
                    private_only=True, admin_only=True,
                    cap=lambda: BACKEND.supports(CAP_PLAYER_RESTORE),
                    cap_message="Per-player restore is not available on this server edition.")

    # --- /chat_id (public — lets an unauthorized chat learn its ID) ---
    def cmd_chat_id(ctx):
        logger.info("ChatID: requested by %s (chat=%s)", ctx.sender_label, ctx.chat_id)
        ctx.reply(f"Chat ID: {ctx.chat_id}")
    router.register("chat_id", cmd_chat_id, public=True)

    # --- /authorize ---
    def cmd_authorize(ctx):
        if not ctx.args:
            ctx.reply("Usage: /authorize <chat_id>")
            return
        target_id = str(ctx.args[0]).strip()
        with _auth_lock:
            ns = _auth_ns(auth, ctx.platform)
            if target_id not in ns["authorized_chat_ids"]:
                ns["authorized_chat_ids"].append(target_id)
                save_auth(auth, _AUTH_PATH)
        logger.info("Authorize: chat %s added by %s on %s",
                    target_id, ctx.sender_label, ctx.platform)
        ctx.reply(f"Chat {target_id} is now authorized.")
    router.register("authorize", cmd_authorize, private_only=True, admin_only=True)

    # --- /revoke ---
    def cmd_revoke(ctx):
        if not ctx.args:
            ctx.reply("Usage: /revoke <chat_id>")
            return
        target_id = str(ctx.args[0]).strip()
        with _auth_lock:
            ns = _auth_ns(auth, ctx.platform)
            if target_id in ns["authorized_chat_ids"]:
                ns["authorized_chat_ids"].remove(target_id)
                save_auth(auth, _AUTH_PATH)
                logger.info("Revoke: chat %s removed by %s on %s",
                            target_id, ctx.sender_label, ctx.platform)
                ctx.reply(f"Chat {target_id} has been revoked.")
            else:
                ctx.reply(f"Chat {target_id} was not authorized.")
    router.register("revoke", cmd_revoke, private_only=True, admin_only=True)

    # --- /listchats ---
    def cmd_listchats(ctx):
        logger.info("ListChats: requested by %s", ctx.sender_label)
        ids = (auth.get(ctx.platform) or {}).get("authorized_chat_ids", [])
        if ids:
            ctx.reply("Authorized chats:\n" + "\n".join(str(i) for i in ids))
        else:
            ctx.reply("No authorized chats.")
    router.register("listchats", cmd_listchats, private_only=True, admin_only=True)


def _make_unclaimed_handler(auth: dict):
    """Build the admin-claim hook: the first private message on a platform with
    no admin yet claims that platform's admin."""
    def on_unclaimed(ctx) -> bool:
        ns = auth.get(ctx.platform) or {}
        if ns.get("admin_user_id") is not None or not ctx.is_private:
            return False
        with _auth_lock:
            if _auth_ns(auth, ctx.platform).get("admin_user_id") is not None:
                return False
            _auth_ns(auth, ctx.platform)["admin_user_id"] = ctx.user_id
            save_auth(auth, _AUTH_PATH)
        logger.info("Admin claimed on %s by %s (id=%s)",
                    ctx.platform, ctx.sender_label, ctx.user_id)
        ctx.reply("You are now the admin.")
        return True
    return on_unclaimed


# ---------------------------------------------------------------------------
# 10. Main
# ---------------------------------------------------------------------------
def main():
    setup_logging(Path(__file__).parent / "logs")

    auth = load_auth(_AUTH_PATH)
    achievements = load_achievements(_ACHIEVEMENTS_PATH)
    deaths = load_deaths(_DEATHS_PATH)

    global _watcher_ref, BACKEND, ADAPTERS, _AUTH
    _AUTH = auth

    # Build the chat adapters (Telegram, Slack, …). They're constructed now but
    # only started later, after the backend and command router are ready.
    ADAPTERS = make_adapters(CONFIG)
    logger.info("Chat platforms: %s", ", ".join(a.name for a in ADAPTERS))

    # Construct the edition-specific backend — the name registry is sourced from
    # it. Bedrock raises BackendUnavailable if no tmux/screen session is hosting
    # the server — exit gracefully (alerting each platform's admin).
    try:
        BACKEND = make_backend(CONFIG)
    except BackendUnavailable as e:
        logger.error("Cannot start backend for edition '%s': %s", CONFIG.edition, e)
        _alert_admins(f"⚠️ mcnotifier cannot start: {e}")
        return
    logger.info("Server edition: %s", CONFIG.edition)

    names = load_player_names()  # now backed by BACKEND.load_names()

    # Command router shared by all adapters; replies go to the originating chat.
    router = CommandRouter(
        auth,
        is_admin=lambda platform, uid: is_admin(platform, uid, auth),
        is_authorized=lambda platform, cid, uid, priv:
            is_authorized(platform, cid, uid, priv, auth),
        on_unclaimed=_make_unclaimed_handler(auth),
        logger=logger,
    )
    register_commands(router, auth, names, achievements, deaths)

    notify = make_notify_callback(names, achievements, deaths)
    watcher = LogWatcher(LOG_PATH, names, notify)
    _watcher_ref = watcher
    BACKEND.attach_watcher(watcher)
    observer = Observer()
    observer.schedule(watcher, path=str(LOG_PATH.parent), recursive=False)
    observer.start()
    logger.info("Watching %s for join/leave events", LOG_PATH)

    # Validate server.properties RCON settings before attempting any RCON commands.
    # If validation fails, skip RCON entirely — the settings are wrong and
    # connections will fail regardless.
    backend_available = BACKEND.is_available(log_warnings=True)

    # Capture any players already online via RCON /list.
    # The server may already be running (bot restarted), or it may still be
    # starting up. We try /list immediately; if it fails, we watch latest.log
    # for the "RCON running on" line and retry. If that also times out, the
    # server is likely not running — alert the admin.
    if backend_available:
        online = None
        try:
            online = BACKEND.query_online_players()
        except Exception as e:
            logger.warning("Online query failed, server may still be starting: %s", e)
            if BACKEND.wait_for_ready(timeout=120):
                logger.info("Server is now ready, retrying online query")
                try:
                    online = BACKEND.query_online_players()
                except Exception as e2:
                    logger.warning("Online query retry failed: %s", e2)

        if online is not None:
            # Online-time bookkeeping: clear any sessions left open by a prior
            # crash, then open a session for each currently-online player (their
            # join event predates this bot run).
            BACKEND.reset_open_sessions()
            for name in online:
                player_join(name)
            current = get_online_players()
            if current:
                logger.info("%d player(s) already online: %s",
                            len(current), ", ".join(current))
                for name in current:
                    pid = _uuid_by_name(name, names)
                    if pid:
                        BACKEND.record_player_session("join", pid)
                # The join notify callback never fired for these players, so
                # start the incremental backup cycle here.
                _start_incremental_cycle()
            else:
                logger.info("No players online")
        else:
            # Never connected — server is likely not running. Alert the admin.
            logger.error("Could not connect to the Minecraft server. "
                         "Server may not be running.")
            _alert_admins(
                "\u26a0\ufe0f Could not connect to the Minecraft server.\n"
                "The server may not be running or not ready yet.\n"
                "Backups and /list will not work until the server is up.")

    # Validate incremental backup chain
    chain_id, base_full, _ = _load_manifest()
    if chain_id:
        marker = _read_chain_marker()
        if marker == chain_id:
            logger.info("Backup chain %s valid (base: %s)", chain_id, base_full)
        else:
            logger.warning("Backup chain invalid: manifest chain %s does not match "
                          "marker %s. Incremental backups will be skipped until "
                          "a full backup is run.", chain_id, marker or "(missing)")
            # Clear chain_id so incrementals are skipped
            _save_manifest({}, chain_id="", base_full="")
    else:
        logger.info("No backup chain established. Run /backup to start one.")

    # Scheduled full backup
    _BACKUP_HOUR = CONFIG.backup_hour
    _BACKUP_SCHEDULE = CONFIG.backup_schedule

    def _next_backup_time(now: datetime) -> datetime:
        """Calculate the next scheduled backup time."""
        from calendar import monthrange

        target = now.replace(hour=_BACKUP_HOUR, minute=0, second=0, microsecond=0)

        if _BACKUP_SCHEDULE == "weekly":
            # Run on Monday at _BACKUP_HOUR
            days_ahead = (7 - now.weekday()) % 7  # Monday = 0
            target = target + timedelta(days=days_ahead)
            if target <= now:
                target = target + timedelta(weeks=1)
        elif _BACKUP_SCHEDULE == "monthly":
            # Run on the 1st of each month at _BACKUP_HOUR
            target = target.replace(day=1)
            if target <= now:
                # Advance to 1st of next month
                if now.month == 12:
                    target = target.replace(year=now.year + 1, month=1)
                else:
                    target = target.replace(month=now.month + 1)
        else:
            # Daily (default)
            if target <= now:
                target = target + timedelta(days=1)

        return target

    def _scheduled_backup_loop():
        while True:
            now = datetime.now()
            target = _next_backup_time(now)
            wait = (target - now).total_seconds()
            logger.info("Next %s backup in %.0f seconds (at %s)", _BACKUP_SCHEDULE,
                        wait, target.strftime("%Y-%m-%d %H:%M"))
            time.sleep(wait)

            if not BACKEND.is_available():
                logger.warning("Scheduled backup skipped: server backend not available")
                continue

            logger.info("Scheduled %s backup starting", _BACKUP_SCHEDULE)
            def status_cb(msg):
                _alert_admins(f"[Backup] {msg}")

            try:
                path = run_backup(status_cb=status_cb)
                status_cb(f"Complete: {Path(path).name}")
            except Exception as e:
                logger.exception("Scheduled backup failed")
                status_cb(f"Failed: {e}")

    backup_thread = threading.Thread(target=_scheduled_backup_loop, daemon=True)
    backup_thread.start()

    # Start each chat adapter in its own daemon thread (each runs a blocking
    # poll/socket loop); the main thread waits for Ctrl-C.
    for adapter in ADAPTERS:
        threading.Thread(target=adapter.start, args=(router.dispatch,),
                         name=f"chat-{adapter.name}", daemon=True).start()
        logger.info("Started %s adapter", adapter.name)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for adapter in ADAPTERS:
            try:
                adapter.stop()
            except Exception:
                logger.exception("Failed to stop %s adapter", adapter.name)
        # Flush any open online-time sessions so playtime isn't lost on a clean
        # stop (a hard crash still loses the in-progress session).
        try:
            BACKEND.close_open_sessions()
        except Exception:
            logger.exception("Failed to close open sessions on shutdown")
        observer.stop()
        observer.join()
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
