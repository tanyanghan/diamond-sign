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

import telebot
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from backup_utils import (
    CHAIN_MARKER_NAME, RE_FULL, RE_INCR, build_file_manifest, new_chain_id,
    run_copy_command, wait_for_settle,
)
from config import (
    load_config, get_level_name, backup_exclude_names, EDITION_BEDROCK,
)
from backends import (
    make_backend, BackendUnavailable, CAP_PLAYER_RESTORE,
    EVENT_DEATH, EVENT_ACHIEVEMENT,
)

# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
# All environment reading lives in config.load_config(); the rest of the module
# reads from CONFIG (and a few thin aliases kept to minimise churn). The world
# subdirectory name is configurable via `level-name` in server.properties — use
# _stats_dir() / _get_level_name() at call time instead of a constant.
CONFIG = load_config()

BOT_TOKEN = CONFIG.bot_token
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
# Helpers
# ---------------------------------------------------------------------------
def _tg_user(message) -> str:
    """Return a readable identifier for the Telegram sender."""
    u = message.from_user
    if u is None:
        return "unknown"
    return f"@{u.username}" if u.username else (u.full_name or str(u.id))


def _send_long_message(bot, chat_id, text, reply_to_id=None, max_len=4096,
                       parse_mode=None):
    """Split text into chunks and send as separate messages."""
    while text:
        if len(text) <= max_len:
            chunk, text = text, ""
        else:
            cut = text.rfind("\n", 0, max_len)
            if cut <= 0:
                cut = max_len
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        if reply_to_id:
            bot.send_message(chat_id, chunk, parse_mode=parse_mode,
                             reply_parameters=telebot.types.ReplyParameters(
                                 message_id=reply_to_id))
            reply_to_id = None  # only reply on the first chunk
        else:
            bot.send_message(chat_id, chunk, parse_mode=parse_mode)


# ---------------------------------------------------------------------------
# 2. Player Name Registry
# ---------------------------------------------------------------------------
_NAMES_PATH = Path(__file__).parent / "player_names.json"
_names_lock = threading.Lock()


def load_player_names(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load player_names.json")
    return {}


def _save_player_names(names: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(names, f, indent=2)


def refresh_player_names(names: dict, path: Path) -> None:
    with _names_lock:
        try:
            if path.exists():
                with open(path) as f:
                    disk_names = json.load(f)
                names.clear()
                names.update(disk_names)
        except Exception:
            logger.exception("Failed to reload player_names.json")


def register_player(uuid: str, name: str, names: dict, path: Path) -> None:
    if names.get(uuid) == name:
        return
    with _names_lock:
        old = names.get(uuid)
        names[uuid] = name
        _save_player_names(names, path)
    if old:
        logger.info("Player registry: UUID %s renamed %s -> %s", uuid, old, name)
    else:
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


def player_join(name: str) -> None:
    with _online_lock:
        _online_players.add(name)


def player_leave(name: str) -> None:
    with _online_lock:
        _online_players.discard(name)


def get_online_players() -> list:
    with _online_lock:
        return sorted(_online_players)


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

    return None, None


# Bedrock Dedicated Server console lines (terser than Java's log). Names may
# contain spaces, so capture up to the ", xuid:" delimiter. BDS emits no death
# or achievement lines, so only join/leave are parsed.
RE_BEDROCK_CONNECT = re.compile(r'Player connected:\s*(.+?),\s*xuid:\s*(\d+)')
RE_BEDROCK_DISCONNECT = re.compile(r'Player disconnected:\s*(.+?),\s*xuid:\s*(\d+)')


def _parse_line_bedrock(line: str, names: dict) -> tuple:
    """Return (event_type, payload) or (None, None) for a Bedrock console line.

    Only join/leave are available on Bedrock (see RE_BEDROCK_*). The player's
    xuid is used as the registry key (Bedrock has no per-player UUID file).
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
def make_notify_callback(bot: telebot.TeleBot, auth: dict, names: dict,
                         achievements: dict, deaths: dict):
    _last_event: dict = {}
    _cooldown = 3

    def _send_to_chats(msg: str) -> None:
        chat_ids = auth.get("authorized_chat_ids", [])
        for chat_id in chat_ids:
            try:
                bot.send_message(chat_id, msg)
            except Exception as e:
                logger.warning("Failed to send notification to chat %s: %s", chat_id, e)

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
            chat_ids = auth.get("authorized_chat_ids", [])
            logger.info("Achievement: %s — %s — sending to %d chat(s)",
                        player, achievement, len(chat_ids))
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
            chat_ids = auth.get("authorized_chat_ids", [])
            logger.info("Death: %s %s — sending to %d chat(s)",
                        player, death_msg, len(chat_ids))
            _send_to_chats(msg)
            return

        name = payload
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

        chat_ids = auth.get("authorized_chat_ids", [])
        logger.info("Notification: player %s %s — sending to %d chat(s)", name, status, len(chat_ids))
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
def _ticks_to_hours(ticks: int) -> float:
    return round(ticks / 20 / 3600, 2)


def _cm_to_km(cm: int) -> float:
    return round(cm / 100000, 2)


def read_player_stats(stats_dir: Path, names: dict) -> list:
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


def _format_stats(p: dict) -> str:
    return (
        f"Player: {p['name']}\n"
        f"  Time played: {p['time_played_hours']}h\n"
        f"  Deaths: {p['deaths']}\n"
        f"  Diamonds mined: {p['diamonds_mined']}\n"
        f"  Ancient debris mined: {p['ancient_debris_mined']}\n"
        f"  Distance travelled: {p['distance_travelled_km']} km\n"
        f"  Villager trades: {p['villager_trades']}\n"
        f"  Mobs killed: {p['total_mobs_killed']}"
    )


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


# ---------------------------------------------------------------------------
# 7d. RCON & Backup
# ---------------------------------------------------------------------------
_backup_lock = threading.Lock()
_bot_ref: telebot.TeleBot | None = None
_auth_ref: dict | None = None
_watcher_ref: LogWatcher | None = None

# /restore_player pending-state, keyed by admin user_id.
# Forces the admin through the list -> select -> confirm sequence so a typo
# in a single command can't trigger a destructive restore.
_pending_player_restore: dict = {}
_pending_player_lock = threading.Lock()
_PENDING_PLAYER_RESTORE_TTL = 300  # seconds; older entries are treated as missing
_RESTORE_PLAYER_PAGE_SIZE = 10  # versions shown per page in /restore_player listing


def _get_level_name() -> str:
    """Return the world directory name from server.properties' `level-name`.
    Falls back to 'world' (the vanilla default) if the file is missing or
    the key isn't set."""
    return get_level_name(MINECRAFT_DIR)


def _stats_dir() -> Path:
    """Return the per-player stats directory under the active world.

    Minecraft 26.1+ moved player stats from <world>/stats to
    <world>/players/stats. Prefer the new layout but fall back to the old
    one if it's still present (older servers, mid-migration).
    """
    world = Path(MINECRAFT_DIR) / _get_level_name()
    new_path = world / "players" / "stats"
    old_path = world / "stats"
    if new_path.is_dir():
        return new_path
    if old_path.is_dir():
        return old_path
    return new_path  # default to new layout when neither exists yet


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


def run_backup(bot: telebot.TeleBot, auth: dict, status_cb=None):
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

# Player-data lives under the active world directory (named by `level-name`
# in server.properties — read via _get_level_name(), never hardcoded).
# Minecraft 26.1+ moved player data:
#   - Old layout (<= 1.21): <world>/playerdata/<uuid>.dat
#   - New layout (>= 26.1): <world>/players/data/<uuid>.dat
# We always read live files from whichever directory exists (preferring the
# new layout) and write restore output to the same one. Backup zips may use
# either layout depending on when they were created, so the version scanner
# checks both internal entry paths in each zip.
# Forward slashes here match the zip layout produced by run_backup /
# run_incremental_backup.


def _live_playerdata_dir() -> Path:
    """Return the live world's playerdata directory (new layout preferred)."""
    world = Path(MINECRAFT_DIR) / _get_level_name()
    new_path = world / "players" / "data"
    old_path = world / "playerdata"
    if new_path.is_dir():
        return new_path
    if old_path.is_dir():
        return old_path
    return new_path  # default to new layout when neither exists yet


def _resolve_player(name: str, names: dict) -> tuple | None:
    """Case-insensitive name lookup. Returns (canonical_name, uuid) or None.

    Minecraft usernames are case-insensitive in practice; player_names.json
    stores the case the server logged at join time. We accept any case from
    the admin and surface the stored canonical name.
    """
    target = name.lower()
    for uuid, canonical in names.items():
        if canonical.lower() == target:
            return canonical, uuid
    return None


def _scan_player_data_versions(uuid: str) -> list:
    """Find all available historical copies of a player's .dat file.

    Sources, latest first:
      1. Live <world>/players/data/<uuid>.dat[_old[.gz]]  (or pre-26.1 path)
      2. The current chain's full backup (base_full) if it contains the entry
      3. Every incremental zip in BACKUP_DIR matching the current chain_id
         that contains the entry

    Backup zips may have been created with either the old (1.21 and earlier)
    or the new (26.1+) on-disk layout. We probe both possible internal entry
    paths in each zip and record whichever exists.

    Each returned dict carries enough context for _run_player_restore to read
    the bytes back later: ("kind", "path", "entry") plus a display label and
    a sort key.
    """
    versions = []
    level_name = _get_level_name()
    candidate_entries = (
        f"{level_name}/players/data/{uuid}.dat",   # new (26.1+)
        f"{level_name}/playerdata/{uuid}.dat",     # old (<= 1.21)
    )
    playerdata_dir = _live_playerdata_dir()

    # 1. Live files (.dat, .dat_old, .dat_old.gz). Each gets its own entry
    # because the three are independently dated working copies, not snapshots.
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
                "sort_key": mtime,
                "source": label,
                "kind": kind,
                "path": p,
                "entry": None,
            })

    # 1b. Pre-restore safety copies left by previous /restore_player runs.
    # The timestamp in the filename is the authoritative save time — the
    # filesystem mtime reflects when the copy was made, not the save itself.
    if playerdata_dir.exists():
        for p in playerdata_dir.glob(f"{uuid}.dat.pre-restore-*"):
            suffix = p.name[len(f"{uuid}.dat.pre-restore-"):]
            try:
                ts_dt = datetime.strptime(suffix, "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            sort_key = ts_dt.timestamp()
            versions.append({
                "timestamp": ts_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "sort_key": sort_key,
                "source": "pre-restore backup",
                "kind": "live",
                "path": p,
                "entry": None,
            })

    # 2 + 3. All backup zips in BACKUP_DIR — current chain and previous chains.
    chain_id, base_full, _ = _load_manifest()
    if BACKUP_DIR.exists():
        for f in BACKUP_DIR.iterdir():
            if not f.is_file() or f.suffix != ".zip":
                continue
            ts_str = None
            label = None
            m_incr = RE_INCR.match(f.name)
            if m_incr:
                ts_str = m_incr.group(3)
                is_current = chain_id and m_incr.group(2) == chain_id
                label = "incremental backup" if is_current else "incremental backup (old chain)"
            else:
                m_full = RE_FULL.match(f.name)
                if m_full:
                    ts_str = m_full.group(2)
                    is_current = chain_id and f.name == base_full
                    label = "full backup" if is_current else "full backup (old chain)"
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
                "sort_key": ts_dt.timestamp(),
                "source": label,
                "kind": "zip",
                "path": f,
                "entry": found_entry,
            })

    versions.sort(key=lambda v: v["sort_key"], reverse=True)
    return versions


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


def _read_player_data_bytes(version: dict) -> bytes:
    """Pull raw .dat bytes from a version entry, transparently gunzipping
    a .dat_old.gz source. Always returns plain (uncompressed) NBT bytes
    suitable for writing into <uuid>.dat."""
    kind = version["kind"]
    if kind == "zip":
        with zipfile.ZipFile(version["path"], "r") as zf:
            return zf.read(version["entry"])
    if kind == "live_gz":
        with gzip.open(version["path"], "rb") as f:
            return f.read()
    return version["path"].read_bytes()


def _run_player_restore(username: str, uuid: str, version: dict,
                        bot: telebot.TeleBot, chat_id: int) -> None:
    """Background worker that performs the player .dat restore.

    Holds _backup_lock for the duration to mutex with /backup and the
    incremental cycle. Saving is always re-enabled in a finally block,
    even if the file replacement step raises.

    Only reached on editions with CAP_PLAYER_RESTORE (Java); the caller gates.
    """
    def status(msg):
        logger.info("RestorePlayer: %s", msg)
        try:
            bot.send_message(chat_id, msg)
        except Exception:
            logger.exception("Failed to send status message")

    if not _backup_lock.acquire(blocking=False):
        bot.send_message(chat_id, "A backup or restore is in progress.")
        return

    save_started = False
    try:
        # Precondition: player must be offline (confirmed by the server, not
        # the bot's in-memory set, which can drift if log lines were missed).
        try:
            if BACKEND.is_player_online(username):
                status(f"Player {username} is online — log them out first.")
                return
            status(f"{username} is offline")
        except Exception as e:
            status(f"Online check failed: {e}")
            return

        # Mirror the backup freeze/flush dance so any outstanding writes finish
        # before we touch the file.
        BACKEND.begin_save(status)
        save_started = True

        wait_for_settle(Path(MINECRAFT_DIR), BACKUP_DIR, log_fn=status,
                        exclude_names=_BACKUP_EXCLUDE_NAMES)

        # Read source bytes, then atomically replace <uuid>.dat. Keep the
        # current .dat as <uuid>.dat.pre-restore-<ts> as a safety net so
        # the admin can manually undo a wrong choice.  The target follows
        # the live layout (new in 26.1+, falls back to old) regardless of
        # which layout the source bytes came from.
        target = _live_playerdata_dir() / f"{uuid}.dat"
        source_bytes = _read_player_data_bytes(version)

        if target.exists():
            # Tag the safety copy with the live .dat's mtime (i.e. when the
            # player data was last saved by the server), not datetime.now().
            # That way the suffix matches the timestamp the version list
            # would show for this file, making the filename self-documenting
            # about the state it captures rather than when the restore ran.
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
                BACKEND.end_save(status)
            except Exception:
                logger.exception("Failed to re-enable auto-save")
        _backup_lock.release()


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

            size_mb = zip_path.stat().st_size / (1024 * 1024)
            logger.info("Incremental backup saved: %s (%.1f MB, %d files)",
                        zip_path.name, size_mb, len(changed))

            # Update the manifest: same chain, but new mtime baseline
            _save_manifest(new_manifest, chain_id=chain_id, base_full=base_full)

        finally:
            # Always resume normal saving
            BACKEND.end_save(inc_log)

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


def load_auth(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load auth.json")
    return {"admin_user_id": None, "authorized_chat_ids": []}


def save_auth(auth: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(auth, f, indent=2)


def is_admin(user_id: int, auth: dict) -> bool:
    return auth.get("admin_user_id") == user_id


def is_authorized(chat_id: int, auth: dict) -> bool:
    if auth.get("admin_user_id") is not None and chat_id == auth["admin_user_id"]:
        return True
    return chat_id in auth.get("authorized_chat_ids", [])


def _guard(message, auth: dict) -> bool:
    """Return True if the message should be processed."""
    chat_type = message.chat.type
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None

    if chat_type == "private":
        admin_id = auth.get("admin_user_id")
        if admin_id is None:
            return True  # unclaimed — allow for admin claim
        return user_id == admin_id

    if chat_type in ("group", "supergroup"):
        return chat_id in auth.get("authorized_chat_ids", [])

    return False


# ---------------------------------------------------------------------------
# 9. Bot Commands
# ---------------------------------------------------------------------------
def register_handlers(bot: telebot.TeleBot, auth: dict, names: dict,
                      achievements: dict, deaths: dict) -> None:

    def guard(message) -> bool:
        return _guard(message, auth)

    # --- Admin claim (private chat, unclaimed) ---
    @bot.message_handler(func=lambda m: (
        m.chat.type == "private"
        and auth.get("admin_user_id") is None
    ))
    def claim_admin(message):
        with _auth_lock:
            if auth.get("admin_user_id") is not None:
                return
            auth["admin_user_id"] = message.from_user.id
            save_auth(auth, _AUTH_PATH)
        logger.info("Admin claimed by %s (id=%s)", _tg_user(message), message.from_user.id)
        bot.reply_to(message, "You are now the admin.")

    # --- /start, /help ---
    @bot.message_handler(commands=["start", "help"])
    def cmd_help(message):
        if not guard(message):
            return
        logger.info("Help: requested by %s", _tg_user(message))
        lines = [
            "Available commands:",
            "/status — show online players",
            "/list — list all known players",
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
        if message.chat.type == "private" and is_admin(message.from_user.id, auth):
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
        bot.reply_to(message, "\n".join(lines))

    # --- /status ---
    @bot.message_handler(commands=["status"])
    def cmd_status(message):
        if not guard(message):
            return
        logger.info("Status: requested by %s", _tg_user(message))
        online = get_online_players()
        if online:
            bot.reply_to(message, f"Players online: {len(online)} ({', '.join(online)})")
        else:
            bot.reply_to(message, "No players currently online.")

    # --- /stats ---
    @bot.message_handler(commands=["stats"])
    def cmd_stats(message):
        if not guard(message):
            return
        refresh_player_names(names, _NAMES_PATH)
        args = message.text.split(maxsplit=1)
        target = args[1].strip().lower() if len(args) > 1 else None
        logger.info("Stats: requested by %s (player=%s)", _tg_user(message), target or "all")

        all_stats = read_player_stats(_stats_dir(), names)
        if not all_stats:
            bot.reply_to(message, "Stats directory not found or empty.")
            return

        if target:
            matches = [p for p in all_stats if p["name"].lower() == target]
            if not matches:
                bot.reply_to(message, f"No player found matching '{target}'.")
                return
            bot.reply_to(message, _format_stats(matches[0]))
        else:
            lines = [_format_stats(p) for p in sorted(all_stats, key=lambda p: p["name"].lower())]
            bot.reply_to(message, "\n\n".join(lines))

    # --- /playtime ---
    @bot.message_handler(commands=["playtime"])
    def cmd_playtime(message):
        if not guard(message):
            return
        refresh_player_names(names, _NAMES_PATH)
        logger.info("Playtime: requested by %s", _tg_user(message))
        all_stats = read_player_stats(_stats_dir(), names)
        if not all_stats:
            bot.reply_to(message, "Stats directory not found or empty.")
            return
        ranked = sorted(all_stats, key=lambda p: p["time_played_hours"], reverse=True)
        lines = [f"{i+1}. {p['name']} — {p['time_played_hours']}h" for i, p in enumerate(ranked)]
        bot.reply_to(message, "Playtime leaderboard:\n" + "\n".join(lines))

    # --- /list ---
    @bot.message_handler(commands=["list"])
    def cmd_list(message):
        if not guard(message):
            return
        refresh_player_names(names, _NAMES_PATH)
        logger.info("List: requested by %s", _tg_user(message))
        if not _stats_dir().exists():
            bot.reply_to(message, "Stats directory not found.")
            return
        entries = sorted(
            names.get(f.stem, f.stem)
            for f in _stats_dir().glob("*.json")
        )
        if not entries:
            bot.reply_to(message, "No players found in stats directory.")
            return
        bot.reply_to(message, "Known players:\n" + "\n".join(entries))

    # --- /achievements ---
    @bot.message_handler(commands=["achievements"])
    def cmd_achievements(message):
        if not guard(message):
            return
        if not BACKEND.supports(EVENT_ACHIEVEMENT):
            bot.reply_to(message, "Achievements are not tracked on this server edition.")
            return
        refresh_player_names(names, _NAMES_PATH)
        args = message.text.split(maxsplit=1)
        target = args[1].strip().lower() if len(args) > 1 else None
        logger.info("Achievements: requested by %s (player=%s)", _tg_user(message), target or "all")

        if not achievements:
            bot.reply_to(message, "No achievements recorded yet.")
            return

        if target:
            uuid = None
            for u, n in names.items():
                if n.lower() == target:
                    uuid = u
                    break
            if not uuid or uuid not in achievements:
                bot.reply_to(message, f"No achievements found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            entries = achievements[uuid]
            lines = [f"Achievements for {player_name}:"]
            sorted_entries = sorted(entries, key=lambda x: x["timestamp"])
            current_date = None
            for e in sorted_entries:
                ts = e["timestamp"]
                date_part, time_part = ts.split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                time_short = time_part[:5]  # HH:MM
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_short} | {e['type']:<11} | {e['achievement']}")
            text = "<pre>" + "\n".join(lines) + "</pre>"
            _send_long_message(bot, message.chat.id, text,
                               reply_to_id=message.message_id,
                               parse_mode="HTML")
        else:
            lines = []
            for uuid, entries in sorted(achievements.items(),
                                        key=lambda x: names.get(x[0], x[0]).lower()):
                player_name = names.get(uuid, uuid)
                lines.append(f"{player_name}: {len(entries)} achievement(s)")
            _send_long_message(bot, message.chat.id,
                               "Achievements summary:\n" + "\n".join(lines),
                               reply_to_id=message.message_id)

    # --- /scan_achievements ---
    @bot.message_handler(commands=["scan_achievements"])
    def cmd_scan_achievements(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        if not BACKEND.supports(EVENT_ACHIEVEMENT):
            bot.reply_to(message, "Achievements are not tracked on this server edition.")
            return
        logger.info("ScanAchievements: requested by %s", _tg_user(message))
        refresh_player_names(names, _NAMES_PATH)
        bot.reply_to(message, "Scanning log files for achievements...")

        logs_dir = LOG_PATH.parent
        total = 0

        gz_files = sorted(logs_dir.glob("*.log.gz"))
        for gz_path in gz_files:
            m = RE_GZ_DATE.match(gz_path.name)
            if not m:
                continue
            date_str = m.group(1)
            extracted = gz_path.with_suffix("")  # remove .gz
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as gz_f:
                    with open(extracted, "w", encoding="utf-8") as out_f:
                        out_f.write(gz_f.read())
                total += _scan_log_for_achievements(extracted, date_str,
                                                    names, achievements)
            except Exception as e:
                logger.warning("Scan: failed to process %s: %s", gz_path.name, e)
            finally:
                if extracted.exists():
                    extracted.unlink()

        # Scan latest.log
        if LOG_PATH.exists():
            date_str = datetime.now().strftime("%Y-%m-%d")
            total += _scan_log_for_achievements(LOG_PATH, date_str,
                                                names, achievements)

        bot.send_message(message.chat.id,
                         f"Scan complete. {total} new achievement(s) recorded.")
        logger.info("ScanAchievements: %d new achievement(s) found", total)

    # --- /deaths ---
    @bot.message_handler(commands=["deaths"])
    def cmd_deaths(message):
        if not guard(message):
            return
        if not BACKEND.supports(EVENT_DEATH):
            bot.reply_to(message, "Deaths are not tracked on this server edition.")
            return
        refresh_player_names(names, _NAMES_PATH)
        args = message.text.split(maxsplit=1)
        target = args[1].strip().lower() if len(args) > 1 else None
        logger.info("Deaths: requested by %s (player=%s)", _tg_user(message), target or "all")

        if not deaths:
            bot.reply_to(message, "No deaths recorded yet.")
            return

        if target:
            uuid = None
            for u, n in names.items():
                if n.lower() == target:
                    uuid = u
                    break
            if not uuid or uuid not in deaths:
                bot.reply_to(message, f"No deaths found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            entries = deaths[uuid]
            lines = [f"Deaths for {player_name} ({len(entries)} total):"]
            sorted_entries = sorted(entries, key=lambda x: x["timestamp"])
            current_date = None
            for e in sorted_entries:
                ts = e["timestamp"]
                date_part, time_part = ts.split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                time_short = time_part[:5]
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_short} | {e['message']}")
            text = "<pre>" + "\n".join(lines) + "</pre>"
            _send_long_message(bot, message.chat.id, text,
                               reply_to_id=message.message_id,
                               parse_mode="HTML")
        else:
            ranked = []
            for uuid, entries in deaths.items():
                player_name = names.get(uuid, uuid)
                ranked.append((player_name, len(entries)))
            ranked.sort(key=lambda x: x[1], reverse=True)
            lines = ["Death summary:"]
            for player_name, count in ranked:
                lines.append(f"  {player_name}: {count} death(s)")
            text = "<pre>" + "\n".join(lines) + "</pre>"
            _send_long_message(bot, message.chat.id, text,
                               reply_to_id=message.message_id,
                               parse_mode="HTML")

    # --- /death_summary ---
    @bot.message_handler(commands=["death_summary"])
    def cmd_death_summary(message):
        if not guard(message):
            return
        if not BACKEND.supports(EVENT_DEATH):
            bot.reply_to(message, "Deaths are not tracked on this server edition.")
            return
        refresh_player_names(names, _NAMES_PATH)
        logger.info("DeathSummary: requested by %s", _tg_user(message))

        if not deaths:
            bot.reply_to(message, "No deaths recorded yet.")
            return

        # Build category -> {player_name: count}
        categories = {}
        grand_total = 0
        for uuid, entries in deaths.items():
            player_name = names.get(uuid, uuid)
            for e in entries:
                cat = _categorize_death(e["message"])
                counts = categories.setdefault(cat, {})
                counts[player_name] = counts.get(player_name, 0) + 1
                grand_total += 1

        # Format output
        lines = [f"Death Summary ({grand_total} total)", ""]

        # Use the defined category order, then "Other" last
        ordered = [cat for cat, _ in _DEATH_CATEGORIES]
        for cat in ordered:
            if cat not in categories:
                continue
            counts = categories.pop(cat)
            cat_total = sum(counts.values())
            lines.append(f"{cat}: {cat_total}")
            ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
            for player_name, count in ranked:
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")

        # Any remaining "Other" deaths
        if "Other" in categories:
            counts = categories["Other"]
            cat_total = sum(counts.values())
            lines.append(f"Other: {cat_total}")
            ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
            for player_name, count in ranked:
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")

        text = "<pre>" + "\n".join(lines).rstrip() + "</pre>"
        _send_long_message(bot, message.chat.id, text,
                           reply_to_id=message.message_id,
                           parse_mode="HTML")

    # --- /scan_deaths ---
    @bot.message_handler(commands=["scan_deaths"])
    def cmd_scan_deaths(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        if not BACKEND.supports(EVENT_DEATH):
            bot.reply_to(message, "Deaths are not tracked on this server edition.")
            return
        logger.info("ScanDeaths: requested by %s", _tg_user(message))
        refresh_player_names(names, _NAMES_PATH)
        bot.reply_to(message, "Scanning log files for deaths...")

        logs_dir = LOG_PATH.parent
        total = 0

        gz_files = sorted(logs_dir.glob("*.log.gz"))
        for gz_path in gz_files:
            m = RE_GZ_DATE.match(gz_path.name)
            if not m:
                continue
            date_str = m.group(1)
            extracted = gz_path.with_suffix("")
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as gz_f:
                    with open(extracted, "w", encoding="utf-8") as out_f:
                        out_f.write(gz_f.read())
                total += _scan_log_for_deaths(extracted, date_str,
                                              names, deaths)
            except Exception as e:
                logger.warning("Scan: failed to process %s: %s", gz_path.name, e)
            finally:
                if extracted.exists():
                    extracted.unlink()

        if LOG_PATH.exists():
            date_str = datetime.now().strftime("%Y-%m-%d")
            total += _scan_log_for_deaths(LOG_PATH, date_str, names, deaths)

        bot.send_message(message.chat.id,
                         f"Scan complete. {total} new death(s) recorded.")
        logger.info("ScanDeaths: %d new death(s) found", total)

    # --- /backup ---
    @bot.message_handler(commands=["backup"])
    def cmd_backup(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        if not _backup_lock.acquire(blocking=False):
            bot.reply_to(message, "A backup is already in progress.")
            return
        logger.info("Backup: manually triggered by %s", _tg_user(message))
        bot.reply_to(message, "Starting backup...")

        def run():
            try:
                def status_cb(msg):
                    bot.send_message(message.chat.id, msg)
                path = run_backup(bot, auth, status_cb=status_cb)
                bot.send_message(message.chat.id, f"Backup complete: {Path(path).name}")
            except Exception as e:
                logger.exception("Backup failed")
                bot.send_message(message.chat.id, f"Backup failed: {e}")
            finally:
                _backup_lock.release()

        threading.Thread(target=run, daemon=True).start()

    # --- /restore_player ---
    @bot.message_handler(commands=["restore_player"])
    def cmd_restore_player(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        if not BACKEND.supports(CAP_PLAYER_RESTORE):
            bot.reply_to(message, "Per-player restore is not available on this "
                                  "server edition.")
            return

        # Parse: /restore_player <username> [<N>] [confirm]
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message,
                         "Usage:\n"
                         "  /restore_player <username>\n"
                         "  /restore_player <username> more\n"
                         "  /restore_player <username> <N>\n"
                         "  /restore_player <username> <N> confirm")
            return
        typed_name = args[1]
        typed_n = args[2] if len(args) >= 3 else None
        typed_confirm = args[3] if len(args) >= 4 else None
        if typed_confirm is not None and typed_confirm.lower() != "confirm":
            bot.reply_to(message, f"Unexpected argument: '{typed_confirm}' "
                                  f"(did you mean 'confirm'?)")
            return

        # Resolve UUID and canonical name
        resolved = _resolve_player(typed_name, names)
        if resolved is None:
            bot.reply_to(message, f"Unknown player: {typed_name}")
            return
        canonical, uuid = resolved
        user_id = message.from_user.id

        # --- Step 1: list ---
        if typed_n is None:
            versions = _scan_player_data_versions(uuid)
            if not versions:
                _clear_pending_player_restore(user_id)
                bot.reply_to(message, _format_versions_reply(canonical, uuid, versions))
                return
            _set_pending_player_restore(
                user_id, stage="listed", username=canonical, uuid=uuid,
                versions=versions, selected_n=None, page_offset=0)
            logger.info("RestorePlayer: %s listed %d version(s) for %s",
                        _tg_user(message), len(versions), canonical)
            bot.reply_to(message, _format_versions_reply(canonical, uuid, versions, offset=0))
            return

        # --- "more": show next page of the listing ---
        if typed_n.lower() == "more":
            entry = _get_pending_player_restore(user_id, expected_username=canonical)
            if entry is None:
                bot.reply_to(message,
                             f"Run /restore_player {canonical} first to see the list.")
                return
            new_offset = entry.get("page_offset", 0) + _RESTORE_PLAYER_PAGE_SIZE
            versions = entry["versions"]
            if new_offset >= len(versions):
                bot.reply_to(message, f"No more versions for {canonical}.")
                return
            _set_pending_player_restore(user_id, page_offset=new_offset)
            logger.info("RestorePlayer: %s paged to offset %d for %s",
                        _tg_user(message), new_offset, canonical)
            bot.reply_to(message,
                         _format_versions_reply(canonical, uuid, versions, offset=new_offset))
            return

        # Steps 2 and 3 require numeric N
        try:
            n = int(typed_n)
        except ValueError:
            bot.reply_to(message, f"Invalid selection: '{typed_n}'. "
                                  "Use a number or 'more'.")
            return

        # --- Step 3: confirm + execute ---
        if typed_confirm is not None:
            entry = _get_pending_player_restore(
                user_id, expected_username=canonical, expected_stage="selected")
            if entry is None or entry.get("selected_n") != n:
                bot.reply_to(message,
                             f"You must select a timestamp first with "
                             f"/restore_player {canonical} {n}")
                return
            # Re-scan and validate the chosen version still exists. Backup files
            # could in theory have been deleted between selection and confirm.
            versions = _scan_player_data_versions(uuid)
            if not (1 <= n <= len(versions)):
                _clear_pending_player_restore(user_id)
                bot.reply_to(message,
                             f"Selection {n} is no longer valid (only "
                             f"{len(versions)} version(s) available). "
                             f"Run /restore_player {canonical} again.")
                return
            version = versions[n - 1]
            logger.info("RestorePlayer: %s confirmed restore of %s to %s "
                        "(source: %s)", _tg_user(message), canonical,
                        version["timestamp"], version["source"])
            bot.reply_to(message, f"Starting restore of {canonical} to "
                                  f"{version['timestamp']}...")
            _clear_pending_player_restore(user_id)

            def run():
                _run_player_restore(canonical, uuid, version, bot, message.chat.id)

            threading.Thread(target=run, daemon=True).start()
            return

        # --- Step 2: select ---
        # Must have come from step 1 (or a previous step 2) for this same user.
        entry = _get_pending_player_restore(
            user_id, expected_username=canonical)
        if entry is None:
            bot.reply_to(message,
                         f"Run /restore_player {canonical} first to see the list.")
            return
        versions = entry["versions"]
        if not (1 <= n <= len(versions)):
            bot.reply_to(message,
                         f"Invalid selection: {n}. Choose 1-{len(versions)}.")
            return
        version = versions[n - 1]
        _set_pending_player_restore(
            user_id, stage="selected", username=canonical, uuid=uuid,
            versions=versions, selected_n=n)
        logger.info("RestorePlayer: %s selected version %d (%s) for %s",
                    _tg_user(message), n, version["source"], canonical)
        bot.reply_to(message, _format_confirm_reply(canonical, uuid, n, version))

    # --- /chat_id ---
    @bot.message_handler(commands=["chat_id"])
    def cmd_chat_id(message):
        logger.info("ChatID: requested by %s (chat=%s)", _tg_user(message), message.chat.id)
        bot.reply_to(message, f"Chat ID: {message.chat.id}")

    # --- /authorize ---
    @bot.message_handler(commands=["authorize"])
    def cmd_authorize(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        args = message.text.split(maxsplit=1)
        if len(args) > 1:
            try:
                target_id = int(args[1].strip())
            except ValueError:
                bot.reply_to(message, "Invalid chat ID.")
                return
        else:
            bot.reply_to(message, "Usage: /authorize <chat_id>")
            return
        with _auth_lock:
            if target_id not in auth["authorized_chat_ids"]:
                auth["authorized_chat_ids"].append(target_id)
                save_auth(auth, _AUTH_PATH)
        logger.info("Authorize: chat %s added by %s", target_id, _tg_user(message))
        bot.reply_to(message, f"Chat {target_id} is now authorized.")

    # --- /revoke ---
    @bot.message_handler(commands=["revoke"])
    def cmd_revoke(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Usage: /revoke <chat_id>")
            return
        try:
            target_id = int(args[1].strip())
        except ValueError:
            bot.reply_to(message, "Invalid chat ID.")
            return
        with _auth_lock:
            if target_id in auth["authorized_chat_ids"]:
                auth["authorized_chat_ids"].remove(target_id)
                save_auth(auth, _AUTH_PATH)
                logger.info("Revoke: chat %s removed by %s", target_id, _tg_user(message))
                bot.reply_to(message, f"Chat {target_id} has been revoked.")
            else:
                bot.reply_to(message, f"Chat {target_id} was not authorized.")

    # --- /listchats ---
    @bot.message_handler(commands=["listchats"])
    def cmd_listchats(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        logger.info("ListChats: requested by %s", _tg_user(message))
        ids = auth.get("authorized_chat_ids", [])
        if ids:
            bot.reply_to(message, "Authorized chats:\n" + "\n".join(str(i) for i in ids))
        else:
            bot.reply_to(message, "No authorized chats.")


# ---------------------------------------------------------------------------
# 10. Main
# ---------------------------------------------------------------------------
def main():
    setup_logging(Path(__file__).parent / "logs")

    auth = load_auth(_AUTH_PATH)
    names = load_player_names(_NAMES_PATH)
    achievements = load_achievements(_ACHIEVEMENTS_PATH)
    deaths = load_deaths(_DEATHS_PATH)

    bot = telebot.TeleBot(BOT_TOKEN)

    global _bot_ref, _auth_ref, _watcher_ref, BACKEND
    _bot_ref = bot
    _auth_ref = auth

    # Construct the edition-specific backend. Bedrock raises BackendUnavailable
    # if no tmux/screen session is hosting the server — exit gracefully.
    try:
        BACKEND = make_backend(CONFIG)
    except BackendUnavailable as e:
        logger.error("Cannot start backend for edition '%s': %s", CONFIG.edition, e)
        admin_id = auth.get("admin_user_id")
        if admin_id:
            try:
                bot.send_message(admin_id, f"⚠️ mcnotifier cannot start: {e}")
            except Exception:
                pass
        return
    logger.info("Server edition: %s", CONFIG.edition)

    register_handlers(bot, auth, names, achievements, deaths)

    notify = make_notify_callback(bot, auth, names, achievements, deaths)
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
            for name in online:
                player_join(name)
            current = get_online_players()
            if current:
                logger.info("%d player(s) already online: %s",
                            len(current), ", ".join(current))
                # The join notify callback never fired for these players, so
                # start the incremental backup cycle here.
                _start_incremental_cycle()
            else:
                logger.info("No players online")
        else:
            # Never connected — server is likely not running. Alert the admin.
            logger.error("Could not connect to the Minecraft server. "
                         "Server may not be running.")
            admin_id = auth.get("admin_user_id")
            if admin_id:
                try:
                    bot.send_message(
                        admin_id,
                        "\u26a0\ufe0f Could not connect to the Minecraft server.\n"
                        "The server may not be running or not ready yet.\n"
                        "Backups and /list will not work until the server is up.")
                except Exception:
                    logger.warning("Failed to send connection alert to admin")

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

    class _NetworkErrorFilter(logging.Filter):
        _TRANSIENT = (
            ("Network is unreachable", "network unreachable"),
            ("NewConnectionError",     "network unreachable"),
            ("Max retries exceeded",   "network unreachable"),
            ("Read timed out",         "read timed out"),
            ("read operation timed out", "read timed out"),
            ("handshake operation timed out", "SSL handshake timed out"),
            ("Bad Gateway",            "Telegram returned 502 Bad Gateway"),
            ("Connection reset by peer", "connection reset by peer"),
            ("Remote end closed connection without response", "remote end closed connection"),
        )

        def filter(self, record):
            msg = record.getMessage()
            for phrase, description in self._TRANSIENT:
                if phrase in msg:
                    # Only log warning for the exception line, not the traceback
                    if "Exception traceback" not in msg:
                        logger.warning("Polling: %s, retrying...", description)
                    return False  # suppress from TeleBot logger
            return True

    logging.getLogger("TeleBot").addFilter(_NetworkErrorFilter())

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
                admin_id = auth.get("admin_user_id")
                if admin_id:
                    try:
                        bot.send_message(admin_id, f"[Backup] {msg}")
                    except Exception:
                        pass

            try:
                path = run_backup(bot, auth, status_cb=status_cb)
                status_cb(f"Complete: {Path(path).name}")
            except Exception as e:
                logger.exception("Scheduled backup failed")
                status_cb(f"Failed: {e}")

    backup_thread = threading.Thread(target=_scheduled_backup_loop, daemon=True)
    backup_thread.start()

    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=20)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
