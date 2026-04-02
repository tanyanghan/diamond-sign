import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from mcrcon import MCRcon
import telebot
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MINECRAFT_DIR = os.environ.get("MINECRAFT_DIR")

missing = [k for k, v in {"BOT_TOKEN": BOT_TOKEN, "MINECRAFT_DIR": MINECRAFT_DIR}.items() if not v]
if missing:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

LOG_PATH = Path(MINECRAFT_DIR) / "logs" / "latest.log"
STATS_DIR = Path(MINECRAFT_DIR) / "world" / "stats"

RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")
BACKUP_DIR = Path(os.path.expanduser(os.environ.get("BACKUP_DIR", "~/minecraft_backup")))
BACKUP_COPY_CMD = os.environ.get("BACKUP_COPY_CMD", "")
INCREMENTAL_BACKUP_ENABLED = os.environ.get("INCREMENTAL_BACKUP_ENABLED", "").lower() in ("1", "true", "yes")
INCREMENTAL_INTERVAL_MINUTES = int(os.environ.get("INCREMENTAL_INTERVAL_MINUTES", "15"))

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


def parse_line(line: str, names: dict) -> tuple:
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


# ---------------------------------------------------------------------------
# 5. LogWatcher
# ---------------------------------------------------------------------------
class LogWatcher(FileSystemEventHandler):
    def __init__(self, log_path: Path, names: dict, notify_cb):
        self._path = log_path
        self._names = names
        self._notify = notify_cb
        self._pos = 0
        self._inode = None
        self._lock = threading.Lock()
        self._seek_to_end()

    def _seek_to_end(self) -> None:
        try:
            stat = self._path.stat()
            self._inode = stat.st_ino
            self._pos = stat.st_size
        except FileNotFoundError:
            logger.warning("Log file not found at startup: %s (server may be offline)", self._path)

    def _check_rotation(self) -> bool:
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

    def on_modified(self, event):
        if not str(event.src_path).endswith("latest.log"):
            return
        with self._lock:
            self._check_rotation()
            try:
                with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._pos)
                    new_data = f.read()
                    self._pos = f.tell()
                for line in new_data.splitlines():
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


def rcon_command(cmd: str) -> str:
    # MCRcon.__init__ uses signal.SIGALRM which fails in non-main threads.
    # Bypass __init__ and set up the object manually.
    mcr = MCRcon.__new__(MCRcon)
    mcr.host = "localhost"
    mcr.password = RCON_PASSWORD
    mcr.port = 25575
    mcr.tlsmode = 0
    mcr.timeout = 5
    mcr.connect()
    try:
        return mcr.command(cmd)
    finally:
        mcr.disconnect()


def _log_pos() -> int:
    """Return the current size of latest.log (call before the RCON command)."""
    try:
        return LOG_PATH.stat().st_size
    except FileNotFoundError:
        return 0


def _wait_for_log_line(phrase: str, pos: int, timeout: float = 60) -> bool:
    """Wait for a line containing phrase in latest.log from pos onwards."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            size = LOG_PATH.stat().st_size
            if size > pos:
                with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    new_data = f.read()
                    pos = f.tell()
                for line in new_data.splitlines():
                    if phrase in line:
                        return True
        except FileNotFoundError:
            pass
        time.sleep(0.5)
    return False


def run_backup(bot: telebot.TeleBot, auth: dict, status_cb=None):
    """Run a full server backup. status_cb(msg) is called with progress updates."""
    def status(msg):
        logger.info("Backup: %s", msg)
        if status_cb:
            status_cb(msg)

    if not RCON_PASSWORD:
        raise RuntimeError("RCON_PASSWORD not configured")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Disable auto-save, flush world data, then zip while world is frozen
    status("Disabling auto-save...")
    pos = _log_pos()
    rcon_command("save-off")
    if _wait_for_log_line("Automatic saving is now disabled", pos, timeout=30):
        status("Auto-save disabled")
    else:
        status("Warning: save-off confirmation not seen in log, proceeding anyway")

    try:
        status("Saving world...")
        pos = _log_pos()
        rcon_command("save-all")
        if _wait_for_log_line("Saved the game", pos, timeout=120):
            status("World save complete")
        else:
            status("Warning: save-all confirmation not seen in log, proceeding anyway")

        # Zip the directory
        mc_dir = Path(MINECRAFT_DIR)
        dir_name = mc_dir.name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"{dir_name}_{timestamp}"
        zip_path = BACKUP_DIR / zip_name

        status(f"Zipping {mc_dir} ...")
        shutil.make_archive(str(zip_path), "zip", root_dir=str(mc_dir.parent),
                            base_dir=dir_name)
        final_path = Path(f"{zip_path}.zip")
        size_mb = final_path.stat().st_size / (1024 * 1024)
        status(f"Backup saved: {final_path.name} ({size_mb:.1f} MB)")
    finally:
        # Always re-enable auto-save, even if zip fails
        pos = _log_pos()
        rcon_command("save-on")
        if _wait_for_log_line("Automatic saving is now enabled", pos, timeout=30):
            status("Auto-save re-enabled")
        else:
            status("Warning: save-on confirmation not seen in log")

    # Copy off-server if configured
    if BACKUP_COPY_CMD:
        copy_cmd = BACKUP_COPY_CMD.replace("{file}", str(final_path))
        status(f"Running copy command...")
        try:
            result = subprocess.run(copy_cmd, shell=True, capture_output=True,
                                    text=True, timeout=600)
            if result.returncode == 0:
                status("Copy command completed successfully")
            else:
                status(f"Copy command failed (rc={result.returncode}): {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            status("Copy command timed out after 10 minutes")
        except Exception as e:
            status(f"Copy command error: {e}")

    # Reset incremental manifest baseline after full backup
    try:
        fresh_manifest = _build_manifest(Path(MINECRAFT_DIR))
        _save_manifest(fresh_manifest)
        logger.info("Backup: incremental manifest reset after full backup")
    except Exception:
        logger.exception("Failed to reset incremental manifest after full backup")

    return str(final_path)


# ---------------------------------------------------------------------------
# 7e. Incremental Backup
# ---------------------------------------------------------------------------
_MANIFEST_PATH = Path(__file__).parent / "backup_manifest.json"
_incr_timer: threading.Timer | None = None
_incr_lock = threading.Lock()  # protects _incr_timer


def _build_manifest(root: Path) -> dict:
    """Walk root and return {relative_path: mtime} for every file."""
    manifest = {}
    backup_dir_resolved = BACKUP_DIR.resolve()
    for dirpath, _dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        # Skip the backup output directory if it's inside the server dir
        try:
            dp.resolve().relative_to(backup_dir_resolved)
            continue
        except ValueError:
            pass
        for fn in filenames:
            fp = dp / fn
            try:
                rel = str(fp.relative_to(root)).replace("\\", "/")
                manifest[rel] = fp.stat().st_mtime
            except OSError:
                pass
    return manifest


def _diff_manifest(old: dict, new: dict) -> tuple:
    """Return (changed_or_added, deleted) lists of relative paths."""
    changed = []
    for path, mtime in new.items():
        if path not in old or old[path] != mtime:
            changed.append(path)
    deleted = [path for path in old if path not in new]
    return changed, deleted


def _load_manifest() -> dict:
    if _MANIFEST_PATH.exists():
        try:
            with open(_MANIFEST_PATH) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load backup_manifest.json")
    return {}


def _save_manifest(manifest: dict) -> None:
    with open(_MANIFEST_PATH, "w") as f:
        json.dump(manifest, f)


def run_incremental_backup() -> str | None:
    """Run an incremental backup of changed files. Returns zip path or None."""
    if not _backup_lock.acquire(blocking=False):
        logger.info("Incremental backup skipped: another backup is in progress")
        return None

    try:
        mc_dir = Path(MINECRAFT_DIR)
        old_manifest = _load_manifest()
        new_manifest = _build_manifest(mc_dir)

        changed, deleted = _diff_manifest(old_manifest, new_manifest)
        if not changed and not deleted:
            logger.info("Incremental backup: no changes detected, skipping")
            return None

        logger.info("Incremental backup: %d changed/added, %d deleted",
                     len(changed), len(deleted))

        if not RCON_PASSWORD:
            logger.warning("Incremental backup skipped: RCON_PASSWORD not configured")
            return None

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        # RCON save-off / save-all / save-on for consistency
        pos = _log_pos()
        rcon_command("save-off")
        if _wait_for_log_line("Automatic saving is now disabled", pos, timeout=30):
            logger.info("Incremental backup: auto-save disabled")
        else:
            logger.warning("Incremental backup: save-off confirmation not seen, proceeding")

        try:
            pos = _log_pos()
            rcon_command("save-all")
            if _wait_for_log_line("Saved the game", pos, timeout=120):
                logger.info("Incremental backup: world save complete")
            else:
                logger.warning("Incremental backup: save-all confirmation not seen, proceeding")

            # Rebuild manifest after save-all to capture flushed changes
            new_manifest = _build_manifest(mc_dir)
            changed, deleted = _diff_manifest(old_manifest, new_manifest)

            if not changed and not deleted:
                logger.info("Incremental backup: no changes after save-all, skipping")
                _save_manifest(new_manifest)
                return None

            dir_name = mc_dir.name
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_name = f"{dir_name}_incr_{timestamp}"
            zip_path = BACKUP_DIR / f"{zip_name}.zip"

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel_path in changed:
                    full_path = mc_dir / rel_path
                    if full_path.exists():
                        zf.write(full_path, rel_path)
                if deleted:
                    zf.writestr("_deletions.json", json.dumps(deleted, indent=2))

            size_mb = zip_path.stat().st_size / (1024 * 1024)
            logger.info("Incremental backup saved: %s (%.1f MB, %d files)",
                        zip_path.name, size_mb, len(changed))

            _save_manifest(new_manifest)

        finally:
            pos = _log_pos()
            rcon_command("save-on")
            if _wait_for_log_line("Automatic saving is now enabled", pos, timeout=30):
                logger.info("Incremental backup: auto-save re-enabled")
            else:
                logger.warning("Incremental backup: save-on confirmation not seen")

        # Copy off-server if configured
        if BACKUP_COPY_CMD:
            copy_cmd = BACKUP_COPY_CMD.replace("{file}", str(zip_path))
            try:
                result = subprocess.run(copy_cmd, shell=True, capture_output=True,
                                        text=True, timeout=600)
                if result.returncode == 0:
                    logger.info("Incremental backup: copy command completed")
                else:
                    logger.warning("Incremental backup: copy command failed (rc=%d): %s",
                                   result.returncode, result.stderr.strip())
            except subprocess.TimeoutExpired:
                logger.warning("Incremental backup: copy command timed out")
            except Exception as e:
                logger.warning("Incremental backup: copy command error: %s", e)

        return str(zip_path)

    except Exception:
        logger.exception("Incremental backup failed")
        return None
    finally:
        _backup_lock.release()


def _incremental_cycle():
    """Run one incremental backup, then reschedule."""
    global _incr_timer
    try:
        run_incremental_backup()
    finally:
        with _incr_lock:
            if _incr_timer is not None:  # still active (not stopped)
                _incr_timer = threading.Timer(
                    INCREMENTAL_INTERVAL_MINUTES * 60, _incremental_cycle)
                _incr_timer.daemon = True
                _incr_timer.start()


def _start_incremental_cycle():
    """Start the incremental backup cycle if not already running."""
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
    """Stop the incremental backup cycle. If final=True, run one last backup."""
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
            "/achievements [player] — player achievements",
            "/deaths [player] — death history",
            "/death_summary — deaths grouped by cause",
            "/chat_id — show this chat's ID",
        ]
        if message.chat.type == "private" and is_admin(message.from_user.id, auth):
            lines += [
                "/authorize <chat_id> — whitelist a chat",
                "/revoke <chat_id> — remove a chat from whitelist",
                "/listchats — list authorized chats",
                "/scan_achievements — scan all logs for achievements",
                "/scan_deaths — scan all logs for deaths",
                "/backup — trigger a server backup now",
            ]
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

        all_stats = read_player_stats(STATS_DIR, names)
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
        all_stats = read_player_stats(STATS_DIR, names)
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
        if not STATS_DIR.exists():
            bot.reply_to(message, "Stats directory not found.")
            return
        entries = sorted(
            names.get(f.stem, f.stem)
            for f in STATS_DIR.glob("*.json")
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

    global _bot_ref, _auth_ref
    _bot_ref = bot
    _auth_ref = auth

    register_handlers(bot, auth, names, achievements, deaths)

    notify = make_notify_callback(bot, auth, names, achievements, deaths)
    watcher = LogWatcher(LOG_PATH, names, notify)
    observer = Observer()
    observer.schedule(watcher, path=str(LOG_PATH.parent), recursive=False)
    observer.start()
    logger.info("Watching %s for join/leave events", LOG_PATH)

    # Capture any players already online via RCON /list
    if RCON_PASSWORD:
        try:
            resp = rcon_command("list")
            # Response: "There are X of a max of Y players online: p1, p2"
            m = re.match(r'There are \d+ of a max of \d+ players online:(.*)', resp)
            if m:
                names_part = m.group(1).strip()
                if names_part:
                    for name in names_part.split(", "):
                        name = name.strip()
                        if name:
                            player_join(name)
                    online = get_online_players()
                    logger.info("RCON /list: %d player(s) already online: %s",
                                len(online), ", ".join(online))
                else:
                    logger.info("RCON /list: no players online")
            else:
                logger.info("RCON /list: no players online")
        except Exception as e:
            logger.warning("RCON /list failed (server may be offline): %s", e)

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
    _BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR", "4"))
    _BACKUP_SCHEDULE = os.environ.get("BACKUP_SCHEDULE", "daily").lower()

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

            if not RCON_PASSWORD:
                logger.warning("Scheduled backup skipped: RCON_PASSWORD not configured")
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
