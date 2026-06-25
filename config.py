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
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Recognised server editions.
EDITION_JAVA = "java"
EDITION_BEDROCK = "bedrock"
_EDITIONS = (EDITION_JAVA, EDITION_BEDROCK)


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

    # Java (RCON) transport
    rcon_host: str = "localhost"
    rcon_port: int = 25575
    rcon_password: str = ""

    # Bedrock (terminal multiplexer) transport
    console_log: Path | None = None
    mux_session: str = ""

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
    """Load and validate configuration from ``.env`` / the environment."""
    load_dotenv(Path(__file__).parent / ".env")

    bot_token = os.environ.get("BOT_TOKEN")
    minecraft_dir = os.environ.get("MINECRAFT_DIR")
    missing = [k for k, v in {"BOT_TOKEN": bot_token,
                              "MINECRAFT_DIR": minecraft_dir}.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}")

    edition = os.environ.get("SERVER_EDITION", EDITION_JAVA).strip().lower()
    if edition not in _EDITIONS:
        raise EnvironmentError(
            f"SERVER_EDITION must be one of {_EDITIONS}, got '{edition}'")

    mc_dir = Path(minecraft_dir)
    backup_dir = Path(os.path.expanduser(
        os.environ.get("BACKUP_DIR", "~/minecraft_backup")))

    console_log_env = os.environ.get("CONSOLE_LOG", "").strip()
    console_log = Path(os.path.expanduser(console_log_env)) if console_log_env \
        else (mc_dir / "console.log" if edition == EDITION_BEDROCK else None)

    return ServerConfig(
        bot_token=bot_token,
        edition=edition,
        minecraft_dir=mc_dir,
        backup_dir=backup_dir,
        rcon_host=os.environ.get("RCON_HOST", "localhost"),
        rcon_port=int(os.environ.get("RCON_PORT", "25575")),
        rcon_password=os.environ.get("RCON_PASSWORD", ""),
        console_log=console_log,
        mux_session=os.environ.get("MUX_SESSION", "").strip(),
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
