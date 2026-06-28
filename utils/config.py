"""Central configuration for mcnotifier.

All environment reading happens here so the rest of the code receives a single
``ServerConfig`` object instead of scattering ``os.environ`` lookups and
hardcoded paths across modules. The config is edition-aware (Java vs Bedrock)
and is injected into the server backends.

Also hosts a couple of small world-layout helpers (``read_server_properties``,
``get_level_name``) that both ``bot.py`` and the backends need; keeping them
here avoids a circular import between ``bot.py`` and ``backends``.
"""

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Recognised server editions.
EDITION_JAVA = "java"
EDITION_BEDROCK = "bedrock"
_EDITIONS = (EDITION_JAVA, EDITION_BEDROCK)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _REPO_ROOT / ".env"
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"

# A line like ``KEY=value`` or a commented ``#KEY=value`` placeholder.
_SETTING_RE = re.compile(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class ConfigError(Exception):
    """Raised when the .env is missing or misconfigured. The message lists every
    problem; the caller (bot.py) prints it and stops."""


def _iter_example_entries(text: str):
    """Yield (key, block_lines) for each setting in .env.example, where
    block_lines is the contiguous preceding comment/blank lines plus the setting
    line. Lets us append a missing field together with its explanatory comment."""
    pending = []
    for line in text.splitlines():
        m = _SETTING_RE.match(line)
        if m:
            yield m.group(1), pending + [line]
            pending = []
        else:
            pending.append(line)


def _example_settings(text: str) -> dict:
    """{key: (is_commented, value)} for every setting in .env.example. Used to
    detect a .env value that's still the example placeholder."""
    out = {}
    for line in text.splitlines():
        m = _SETTING_RE.match(line)
        if m:
            out[m.group(1)] = (line.lstrip().startswith("#"), m.group(2).strip())
    return out


def sync_env_from_example(env_path=_ENV_PATH, example_path=_ENV_EXAMPLE_PATH) -> list:
    """Add any fields/comments present in .env.example but missing from .env,
    preserving all existing values. Returns the list of keys added (or a single
    sentinel when .env was created from scratch). Idempotent: nothing is written
    when nothing is missing. Best-effort — a write failure is non-fatal."""
    env_path, example_path = Path(env_path), Path(example_path)
    if not example_path.exists():
        return []
    example_text = example_path.read_text()

    if not env_path.exists():
        try:
            env_path.write_text(example_text)
        except OSError:
            return []
        return ["(created .env from .env.example)"]

    env_text = env_path.read_text()
    present = {m.group(1) for line in env_text.splitlines()
               if (m := _SETTING_RE.match(line))}

    additions, added = [], []
    for key, block in _iter_example_entries(example_text):
        if key in present:
            continue
        present.add(key)
        block = list(block)
        while block and not block[0].strip():   # drop leading blank separators
            block.pop(0)
        additions.append("\n".join(block))
        added.append(key)

    if additions:
        sep = "" if env_text.endswith("\n") else "\n"
        new = (env_text + sep + "\n# --- added from .env.example ---\n"
               + "\n\n".join(additions) + "\n")
        try:
            env_path.write_text(new)
        except OSError:
            return []
    return added


@dataclass
class ServerConfig:
    """Resolved runtime configuration.

    ``minecraft_dir`` and ``backup_dir`` are stored as ``Path`` objects so call
    sites don't have to wrap them. ``log_path`` is derived from the edition:
    Java writes ``logs/latest.log``; Bedrock has no such file, so the bot tails
    the captured-stdout ``console.log`` instead.
    """
    bot_token: str
    edition: str
    minecraft_dir: Path
    backup_dir: Path

    # Chat platforms served simultaneously (e.g. ["telegram", "slack"]).
    chat_platforms: tuple = ("telegram",)
    slack_bot_token: str = ""   # xoxb-… (Slack Web API)
    slack_app_token: str = ""   # xapp-… (Slack Socket Mode)

    # Java (RCON) transport
    rcon_host: str = "localhost"
    rcon_port: int = 25575
    rcon_password: str = ""

    # Bedrock (terminal multiplexer) transport
    console_log: Path | None = None
    mux_session: str = ""
    # Command sent to the mux window to relaunch BDS after a player restore's
    # stop->edit->restart. Derived from minecraft_dir if not set explicitly.
    mux_start_cmd: str = ""

    # Bedrock Script-API events (via the bedrock_pack behavior pack).
    bedrock_script_events: bool = False   # ingest death markers; enables /deaths
    chat_relay: bool = False              # relay in-game chat to the chat platforms

    # Backup behaviour (edition-agnostic)
    incremental_enabled: bool = False
    incremental_interval_minutes: int = 15
    backup_hour: int = 4
    backup_schedule: str = "daily"

    @property
    def log_path(self) -> Path:
        if self.edition == EDITION_BEDROCK:
            return self.console_log or (self.minecraft_dir / "console.log")
        return self.minecraft_dir / "logs" / "latest.log"


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def load_config() -> ServerConfig:
    """Load and validate configuration from ``.env``.

    On startup the .env is first brought up to date with .env.example (missing
    fields and their comments are appended; existing values are kept). All
    missing/misconfigured required fields are then collected and raised together
    as a single ``ConfigError`` so the bot stops with a clear, complete list.
    """
    added = sync_env_from_example(_ENV_PATH, _ENV_EXAMPLE_PATH)
    load_dotenv(_ENV_PATH)

    example = (_example_settings(_ENV_EXAMPLE_PATH.read_text())
               if _ENV_EXAMPLE_PATH.exists() else {})

    def _missing(key: str) -> bool:
        """True if a required key is unset or still the example placeholder."""
        val = os.environ.get(key, "").strip()
        if not val:
            return True
        commented, exval = example.get(key, (True, ""))
        return (not commented) and val == exval  # untouched placeholder

    problems = []

    minecraft_dir = os.environ.get("MINECRAFT_DIR", "").strip()
    if _missing("MINECRAFT_DIR"):
        problems.append("MINECRAFT_DIR: set it to your Minecraft server directory")
    elif not Path(os.path.expanduser(minecraft_dir)).is_dir():
        problems.append(f"MINECRAFT_DIR: directory does not exist: {minecraft_dir}")

    # Chat platforms (csv). Each enabled platform requires its own token(s).
    platforms = tuple(p.strip().lower() for p in
                      os.environ.get("CHAT_PLATFORMS", "telegram").split(",")
                      if p.strip()) or ("telegram",)
    _KNOWN = ("telegram", "slack")
    bad = [p for p in platforms if p not in _KNOWN]
    if bad:
        problems.append(f"CHAT_PLATFORMS: unknown {bad}; allowed: {list(_KNOWN)}")
    platforms = tuple(p for p in platforms if p in _KNOWN) or ("telegram",)

    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    slack_app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if "telegram" in platforms and _missing("BOT_TOKEN"):
        problems.append("BOT_TOKEN: required for telegram (get one from @BotFather)")
    if "slack" in platforms:
        if not slack_bot_token:
            problems.append("SLACK_BOT_TOKEN: required for slack (xoxb-...)")
        if not slack_app_token:
            problems.append("SLACK_APP_TOKEN: required for slack (xapp-...)")

    edition = os.environ.get("SERVER_EDITION", EDITION_JAVA).strip().lower()
    if edition not in _EDITIONS:
        problems.append(f"SERVER_EDITION: must be one of {list(_EDITIONS)} "
                        f"(got '{edition}')")

    if problems:
        msg = ("mcnotifier cannot start - fix the following in "
               f"{_ENV_PATH}:\n" + "\n".join(f"  - {p}" for p in problems))
        if added and added != ["(created .env from .env.example)"]:
            msg += "\n\n(.env was updated with new template fields: "
            msg += ", ".join(added) + ")"
        elif added:
            msg += "\n\n(a fresh .env was created from .env.example - fill it in)"
        raise ConfigError(msg)

    mc_dir = Path(minecraft_dir)
    backup_dir = Path(os.path.expanduser(
        os.environ.get("BACKUP_DIR", "~/minecraft_backup")))

    console_log_env = os.environ.get("CONSOLE_LOG", "").strip()
    console_log = Path(os.path.expanduser(console_log_env)) if console_log_env \
        else (mc_dir / "console.log" if edition == EDITION_BEDROCK else None)

    # Relaunch command for the mux window. Default mirrors the documented launch
    # (`./bedrock_server 2>&1 | tee -a console.log` from the server dir); override
    # with MUX_START_CMD for non-standard setups.
    mux_start_cmd = os.environ.get("MUX_START_CMD", "").strip()
    if not mux_start_cmd and edition == EDITION_BEDROCK:
        log_target = console_log if console_log else (mc_dir / "console.log")
        mux_start_cmd = (f"cd {shlex.quote(mc_dir.as_posix())} && "
                         f"./bedrock_server 2>&1 | tee -a {shlex.quote(log_target.as_posix())}")

    return ServerConfig(
        bot_token=bot_token,
        edition=edition,
        minecraft_dir=mc_dir,
        backup_dir=backup_dir,
        chat_platforms=platforms,
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        rcon_host=os.environ.get("RCON_HOST", "localhost"),
        rcon_port=int(os.environ.get("RCON_PORT", "25575")),
        rcon_password=os.environ.get("RCON_PASSWORD", ""),
        console_log=console_log,
        mux_session=os.environ.get("MUX_SESSION", "").strip(),
        mux_start_cmd=mux_start_cmd,
        bedrock_script_events=_env_bool("BEDROCK_SCRIPT_EVENTS"),
        chat_relay=_env_bool("CHAT_RELAY"),
        incremental_enabled=_env_bool("INCREMENTAL_BACKUP_ENABLED"),
        incremental_interval_minutes=int(
            os.environ.get("INCREMENTAL_INTERVAL_MINUTES", "15")),
        backup_hour=int(os.environ.get("BACKUP_HOUR", "4")),
        backup_schedule=os.environ.get("BACKUP_SCHEDULE", "daily").lower(),
    )


# ---------------------------------------------------------------------------
# World-layout helpers (shared by bot.py and the backends)
# ---------------------------------------------------------------------------
def read_server_properties(minecraft_dir: Path) -> dict:
    """Parse ``<minecraft_dir>/server.properties`` into a dict.

    Java .properties files escape special characters with backslashes
    (e.g. ``\\!`` ``\\:`` ``\\=`` ``\\\\``). Values are unescaped so they
    compare cleanly against equivalents from .env. Returns an empty dict if the
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


def backup_exclude_names() -> set:
    """Basenames to exclude from backups (and the change manifest) in addition
    to the chain marker, derived from the current environment.

    This is bot infrastructure that lives in the server directory but isn't
    server data — the Bedrock ``console.log`` the bot tails. Shared by ``bot.py``
    and ``restore.py`` so both keep the backup zips and the manifest in sync.
    Assumes ``.env`` is already loaded.
    """
    console_log_env = os.environ.get("CONSOLE_LOG", "").strip()
    if console_log_env:
        return {Path(console_log_env).name}
    edition = os.environ.get("SERVER_EDITION", EDITION_JAVA).strip().lower()
    return {"console.log"} if edition == EDITION_BEDROCK else set()
