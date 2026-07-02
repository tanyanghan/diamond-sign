"""Central configuration for Diamond Sign.

Loads a hierarchical JSON config (``diamondsign.json``): a list of **bots**, each
with its chat identity (Telegram/Slack tokens + platforms) and a list of
Minecraft **servers**. This lets one process run either shape — many
single-server bots, or one bot fronting several servers, or any mix.

The rest of the code receives an ``AppConfig`` (``.bots`` -> ``BotConfig`` ->
``.servers`` -> ``ServerConfig``). Each ``ServerConfig`` is per-server (edition,
dir, rcon/mux, backups) and owns its ``data_dir`` (``data/<server-name>/``);
chat tokens live on ``BotConfig``.

A legacy flat ``.env`` install is auto-migrated to ``diamondsign.json`` on first
start. Also hosts the world-layout helpers (``read_server_properties``,
``get_level_name``) shared by ``bot.py`` and the backends.
"""

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Recognised server editions.
EDITION_JAVA = "java"
EDITION_BEDROCK = "bedrock"
_EDITIONS = (EDITION_JAVA, EDITION_BEDROCK)

_KNOWN_PLATFORMS = ("telegram", "slack")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "diamondsign.json"
_EXAMPLE_PATH = _REPO_ROOT / "diamondsign.example.json"
_ENV_PATH = _REPO_ROOT / ".env"            # legacy; migrated to JSON on first start
_DATA_DIR = _REPO_ROOT / "data"            # per-server state lives in data/<key>/


class ConfigError(Exception):
    """Raised when the config is missing or misconfigured. The message lists
    every problem; the caller (bot.py) prints it and stops."""


def _slug(name: str) -> str:
    """Filesystem-safe directory key for a server name (data/<key>/)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return s or "server"


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ServerConfig:
    """One Minecraft server. ``log_path`` is derived from the edition (Java:
    ``logs/latest.log``; Bedrock tails the captured-stdout ``console.log``).
    ``data_dir`` is where this server's state files live, namespaced by name."""
    name: str
    edition: str
    minecraft_dir: Path
    backup_dir: Path

    # Java (RCON) transport
    rcon_host: str = "localhost"
    rcon_port: int = 25575
    rcon_password: str = ""

    # Bedrock (terminal multiplexer) transport
    console_log: Path | None = None
    mux_session: str = ""
    mux_start_cmd: str = ""

    # Bedrock Script-API events (via the bedrock_pack behavior pack).
    bedrock_script_events: bool = False
    chat_relay: bool = False

    # Backup behaviour (edition-agnostic)
    incremental_enabled: bool = False
    incremental_interval_minutes: int = 15
    backup_hour: int = 4
    backup_schedule: str = "daily"
    # Optional off-server copy run after each backup; "{file}" is replaced with
    # the backup zip path (e.g. "rsync -az {file} user@nas:/backups/"). Empty
    # disables the copy step.
    backup_copy_cmd: str = ""

    @property
    def log_path(self) -> Path:
        if self.edition == EDITION_BEDROCK:
            return self.console_log or (self.minecraft_dir / "console.log")
        return self.minecraft_dir / "logs" / "latest.log"

    @property
    def key(self) -> str:
        """Unique, filesystem-safe key used for ``data/<key>/``."""
        return _slug(self.name)

    @property
    def data_dir(self) -> Path:
        return _DATA_DIR / self.key


@dataclass
class BotConfig:
    """One chat identity (one Telegram bot and/or one Slack app) serving a set
    of servers. Token attribute names match the old ServerConfig so the chat
    adapters need no changes."""
    name: str
    platforms: tuple = ("telegram",)
    bot_token: str = ""          # Telegram (from @BotFather)
    slack_bot_token: str = ""    # xoxb-… (Slack Web API)
    slack_app_token: str = ""    # xapp-… (Slack Socket Mode)
    servers: list = field(default_factory=list)   # list[ServerConfig]


@dataclass
class AppConfig:
    """The whole config: every bot (each with its servers)."""
    bots: list = field(default_factory=list)      # list[BotConfig]

    def all_servers(self) -> list:
        return [s for b in self.bots for s in b.servers]

    def bot(self, name: str):
        for b in self.bots:
            if b.name == name:
                return b
        return None


# ---------------------------------------------------------------------------
# JSON -> dataclasses
# ---------------------------------------------------------------------------
def _server_from_dict(d: dict) -> ServerConfig:
    edition = (d.get("edition") or EDITION_JAVA).strip().lower()
    mc_raw = (d.get("minecraft_dir") or "").strip()
    mc_dir = Path(os.path.expanduser(mc_raw)) if mc_raw else None
    name = (d.get("name") or "").strip()
    if not name and mc_dir is not None:
        # Fall back to the Minecraft level-name, slugified: the user didn't type
        # this, and level-names routinely contain spaces (e.g. "Bedrock level"),
        # so make it a safe key rather than tripping name validation. An
        # explicitly-set name is left as-is and validated (see validate_config).
        name = _slug(get_level_name(mc_dir))

    rcon = d.get("rcon") or {}
    mux = d.get("mux") or {}
    backup = d.get("backup") or {}
    incr = backup.get("incremental") or {}

    backup_dir = Path(os.path.expanduser(backup.get("dir") or "~/minecraft_backup"))

    console_raw = (d.get("console_log") or "").strip() if d.get("console_log") else ""
    if console_raw:
        console_log = Path(os.path.expanduser(console_raw))
    elif edition == EDITION_BEDROCK and mc_dir is not None:
        console_log = mc_dir / "console.log"
    else:
        console_log = None

    mux_start = (mux.get("start_cmd") or "").strip()
    if not mux_start and edition == EDITION_BEDROCK and mc_dir is not None:
        log_target = console_log or (mc_dir / "console.log")
        mux_start = (f"cd {shlex.quote(mc_dir.as_posix())} && "
                     f"./bedrock_server 2>&1 | tee -a "
                     f"{shlex.quote(log_target.as_posix())}")

    return ServerConfig(
        name=name,
        edition=edition,
        minecraft_dir=mc_dir if mc_dir is not None else Path(""),
        backup_dir=backup_dir,
        rcon_host=(rcon.get("host") or "localhost"),
        rcon_port=int(rcon.get("port") or 25575),
        rcon_password=(rcon.get("password") or ""),
        console_log=console_log,
        mux_session=(mux.get("session") or "").strip(),
        mux_start_cmd=mux_start,
        bedrock_script_events=bool(d.get("bedrock_script_events", False)),
        chat_relay=bool(d.get("chat_relay", False)),
        incremental_enabled=bool(incr.get("enabled", False)),
        incremental_interval_minutes=int(incr.get("interval_minutes") or 15),
        backup_hour=int(backup.get("hour") or 4),
        backup_schedule=(backup.get("schedule") or "daily").lower(),
        backup_copy_cmd=(backup.get("copy_cmd") or "").strip(),
    )


def _bot_from_dict(d: dict) -> BotConfig:
    platforms = tuple(str(p).strip().lower()
                      for p in (d.get("platforms") or ["telegram"])
                      if str(p).strip()) or ("telegram",)
    tg = d.get("telegram") or {}
    sl = d.get("slack") or {}
    return BotConfig(
        name=(d.get("name") or "").strip(),
        platforms=platforms,
        bot_token=(tg.get("bot_token") or "").strip(),
        slack_bot_token=(sl.get("bot_token") or "").strip(),
        slack_app_token=(sl.get("app_token") or "").strip(),
        servers=[_server_from_dict(s) for s in (d.get("servers") or [])],
    )


def _app_from_dict(doc: dict) -> AppConfig:
    return AppConfig(bots=[_bot_from_dict(b) for b in (doc.get("bots") or [])])


# Track which server had no explicit minecraft_dir so validation can flag it
# (Path("") would otherwise resolve to ".", which is a real directory).
def _has_dir(server: ServerConfig) -> bool:
    return str(server.minecraft_dir) not in ("", ".")


def validate_config(app: AppConfig) -> list:
    """Return a human-readable list of every problem; empty means good to go."""
    problems = []
    if not app.bots:
        problems.append('no bots configured (need at least one entry under "bots")')

    seen_bots = set()
    seen_keys = {}
    for bi, bot in enumerate(app.bots):
        blabel = bot.name or f"bot[{bi}]"
        if not bot.name:
            problems.append(f"{blabel}: missing \"name\"")
        elif bot.name in seen_bots:
            problems.append(f"bot name '{bot.name}' is used more than once")
        seen_bots.add(bot.name)

        bad = [p for p in bot.platforms if p not in _KNOWN_PLATFORMS]
        if bad:
            problems.append(f"{blabel}: unknown platforms {bad}; "
                            f"allowed {list(_KNOWN_PLATFORMS)}")
        if "telegram" in bot.platforms and not bot.bot_token:
            problems.append(f"{blabel}: telegram.bot_token required for telegram "
                            "(get one from @BotFather)")
        if "slack" in bot.platforms:
            if not bot.slack_bot_token:
                problems.append(f"{blabel}: slack.bot_token required (xoxb-...)")
            if not bot.slack_app_token:
                problems.append(f"{blabel}: slack.app_token required (xapp-...)")

        if not bot.servers:
            problems.append(f"{blabel}: has no servers")
        for si, s in enumerate(bot.servers):
            slabel = f"{blabel} / {s.name or f'server[{si}]'}"
            if not _has_dir(s):
                problems.append(f"{slabel}: minecraft_dir is required")
            elif not s.minecraft_dir.is_dir():
                problems.append(f"{slabel}: minecraft_dir does not exist: "
                                f"{s.minecraft_dir}")
            if s.edition not in _EDITIONS:
                problems.append(f"{slabel}: edition must be one of "
                                f"{list(_EDITIONS)} (got '{s.edition}')")
            if not s.name:
                problems.append(f"{slabel}: could not determine a server name; "
                                "set \"name\"")
            elif _slug(s.name) != s.name:
                # The name is used verbatim as the data-dir key, the chat->server
                # binding, and the /use & /authorize argument, so it must be
                # filesystem- and command-token-safe: letters, digits, '.', '_',
                # '-' only, no spaces.
                problems.append(
                    f"{slabel}: server name '{s.name}' isn't allowed — use only "
                    f"letters, digits, '.', '_' and '-' with no spaces (replace "
                    f"each space with '-' or '_'). Try \"{_slug(s.name)}\".")
            else:
                key = s.key
                if key in seen_keys:
                    problems.append(
                        f"server name '{s.name}' collides with '{seen_keys[key]}'; "
                        f"give one a unique \"name\"")
                else:
                    seen_keys[key] = s.name
    return problems


# ---------------------------------------------------------------------------
# Legacy .env -> JSON migration
# ---------------------------------------------------------------------------
def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _env_to_doc() -> dict:
    """Build a one-bot/one-server config doc from a legacy flat ``.env``."""
    load_dotenv(_ENV_PATH)
    edition = os.environ.get("SERVER_EDITION", EDITION_JAVA).strip().lower()
    mc_dir = os.environ.get("MINECRAFT_DIR", "").strip()
    platforms = [p.strip().lower()
                 for p in os.environ.get("CHAT_PLATFORMS", "telegram").split(",")
                 if p.strip()] or ["telegram"]
    name = (_slug(get_level_name(Path(os.path.expanduser(mc_dir))))
            if mc_dir else "")

    server = {
        "name": name,
        "edition": edition,
        "minecraft_dir": mc_dir,
        "rcon": {
            "password": os.environ.get("RCON_PASSWORD", ""),
            "host": os.environ.get("RCON_HOST", "localhost"),
            "port": int(os.environ.get("RCON_PORT", "25575") or 25575),
        },
        "mux": {
            "session": os.environ.get("MUX_SESSION", "").strip(),
            "start_cmd": os.environ.get("MUX_START_CMD", "").strip(),
        },
        "console_log": os.environ.get("CONSOLE_LOG", "").strip() or None,
        "bedrock_script_events": _env_bool("BEDROCK_SCRIPT_EVENTS"),
        "chat_relay": _env_bool("CHAT_RELAY"),
        "backup": {
            "dir": os.environ.get("BACKUP_DIR", "~/minecraft_backup"),
            "schedule": os.environ.get("BACKUP_SCHEDULE", "daily"),
            "hour": int(os.environ.get("BACKUP_HOUR", "4") or 4),
            "copy_cmd": os.environ.get("BACKUP_COPY_CMD", "").strip(),
            "incremental": {
                "enabled": _env_bool("INCREMENTAL_BACKUP_ENABLED"),
                "interval_minutes": int(
                    os.environ.get("INCREMENTAL_INTERVAL_MINUTES", "15") or 15),
            },
        },
    }
    bot = {
        "name": "default",
        "platforms": platforms,
        "telegram": {"bot_token": os.environ.get("BOT_TOKEN", "").strip()},
        "slack": {"bot_token": os.environ.get("SLACK_BOT_TOKEN", "").strip(),
                  "app_token": os.environ.get("SLACK_APP_TOKEN", "").strip()},
        "servers": [server],
    }
    return {"version": 1, "bots": [bot]}


def _write_doc(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Interactive first-run setup (emits diamondsign.json)
# ---------------------------------------------------------------------------
def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _prompt(question: str, default: str = "", validate=None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        ans = input(f"  {question}{suffix}: ").strip() or default
        if not ans:
            print("    (required)")
            continue
        if validate:
            err = validate(ans)
            if err:
                print(f"    {err}")
                continue
        return ans


def _wizard_doc() -> dict:
    """Prompt for a single bot + single server and return a config doc."""
    print("\nDiamond Sign setup")
    print("No config found. Answer the prompts to get started")
    print(f"(saved to {_CONFIG_PATH}; press Ctrl-C to abort).\n")

    edition = _prompt(
        f"Server edition ({'/'.join(_EDITIONS)})", default=EDITION_JAVA,
        validate=lambda v: None if v.lower() in _EDITIONS
        else f"choose one of: {', '.join(_EDITIONS)}").lower()
    mc = _prompt("Minecraft server directory",
                 validate=lambda v: None if Path(os.path.expanduser(v)).is_dir()
                 else "no such directory; check the path and try again")

    def _vplat(v):
        bad = [p.strip().lower() for p in v.split(",")
               if p.strip() and p.strip().lower() not in _KNOWN_PLATFORMS]
        return (f"unknown: {bad}; allowed: {', '.join(_KNOWN_PLATFORMS)}"
                if bad else None)
    raw = _prompt(f"Chat platforms ({', '.join(_KNOWN_PLATFORMS)}; comma-separated)",
                  default="telegram", validate=_vplat)
    platforms = [p.strip().lower() for p in raw.split(",") if p.strip()]

    bot = {"name": "default", "platforms": platforms,
           "servers": [{"edition": edition, "minecraft_dir": mc}]}
    if "telegram" in platforms:
        bot["telegram"] = {"bot_token":
                           _prompt("Telegram bot token (from @BotFather)")}
    if "slack" in platforms:
        bot["slack"] = {"bot_token": _prompt("Slack bot token (xoxb-...)"),
                        "app_token": _prompt("Slack app token (xapp-...)")}
    return {"version": 1, "bots": [bot]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def load_config() -> AppConfig:
    """Load and validate the Diamond Sign config.

    Order of precedence: an existing ``diamondsign.json``; else auto-migrate a
    legacy ``.env`` (writing the JSON and continuing); else a first-run wizard
    (interactive) or an emitted example + ``ConfigError`` (non-interactive). All
    problems are collected and raised together as one ``ConfigError``.
    """
    migrated = False
    if _CONFIG_PATH.exists():
        try:
            doc = json.loads(_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"Could not read {_CONFIG_PATH}: {e}")
        app = _app_from_dict(doc)
    elif _ENV_PATH.exists():
        doc = _env_to_doc()
        app = _app_from_dict(doc)
        try:
            _write_doc(_CONFIG_PATH, doc)
            print(f"Migrated legacy .env -> {_CONFIG_PATH.name}", file=sys.stderr)
            migrated = True
        except OSError:
            pass
    elif _interactive():
        try:
            doc = _wizard_doc()
        except (EOFError, KeyboardInterrupt):
            raise ConfigError("Setup cancelled.")
        app = _app_from_dict(doc)
        try:
            _write_doc(_CONFIG_PATH, doc)
            print(f"\nSaved {_CONFIG_PATH.name}. Starting Diamond Sign...\n")
        except OSError:
            pass
    else:
        try:
            _write_doc(_EXAMPLE_PATH, _EXAMPLE_DOC)
        except OSError:
            pass
        raise ConfigError(
            f"No config found. A template was written to {_EXAMPLE_PATH.name}; "
            f"fill it in and save it as {_CONFIG_PATH.name}.")

    problems = validate_config(app)
    if problems:
        where = _CONFIG_PATH if not migrated else f"{_CONFIG_PATH} (migrated from .env)"
        raise ConfigError("Diamond Sign cannot start - fix the following in "
                          f"{where}:\n" + "\n".join(f"  - {p}" for p in problems))
    return app


_EXAMPLE_DOC = {
    "version": 1,
    "bots": [
        {
            "name": "default",
            "platforms": ["telegram"],
            "telegram": {"bot_token": "123456:your-telegram-bot-token"},
            "servers": [
                {
                    "name": "survival",
                    "edition": "java",
                    "minecraft_dir": "/path/to/your/server",
                    "rcon": {"password": "your_rcon_password"},
                    "backup": {"dir": "~/minecraft_backup", "schedule": "daily",
                               "hour": 4,
                               "copy_cmd": "",  # e.g. "rsync -az {file} user@nas:/backups/"
                               "incremental": {"enabled": True,
                                               "interval_minutes": 15}},
                }
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# World-layout helpers (shared by bot.py and the backends)
# ---------------------------------------------------------------------------
def read_server_properties(minecraft_dir: Path) -> dict:
    """Parse ``<minecraft_dir>/server.properties`` into a dict.

    Java .properties files escape special characters with backslashes
    (e.g. ``\\!`` ``\\:`` ``\\=`` ``\\\\``). Values are unescaped so they
    compare cleanly against equivalents from config. Returns an empty dict if the
    file is missing or unreadable. (Bedrock also ships a server.properties with
    a ``level-name`` key, so this is edition-agnostic.)
    """
    props_path = Path(minecraft_dir) / "server.properties"
    props: dict = {}
    try:
        with open(props_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    value = value.strip().replace("\\!", "!").replace("\\:", ":") \
                                 .replace("\\=", "=").replace("\\\\", "\\")
                    props[key.strip()] = value
    except FileNotFoundError:
        pass
    except Exception:
        # Caller's logger isn't available here; swallow and return what we have.
        pass
    return props


def get_level_name(minecraft_dir: Path) -> str:
    """Return the world directory name from ``server.properties`` ``level-name``.

    Falls back to ``world`` (the vanilla default) if the file is missing or the
    key isn't set.
    """
    return read_server_properties(minecraft_dir).get("level-name", "world")


def backup_exclude_names(server: ServerConfig) -> set:
    """Basenames to exclude from backups (and the change manifest) in addition
    to the chain marker, for one server: bot infrastructure that lives in the
    server directory but isn't server data — the Bedrock ``console.log`` the bot
    tails. Shared by ``bot.py`` and ``restore.py`` so both keep the backup zips
    and the manifest in sync.
    """
    if server.console_log is not None:
        return {server.console_log.name}
    return {"console.log"} if server.edition == EDITION_BEDROCK else set()
