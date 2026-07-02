"""Server backends and the factory that selects one by edition."""

from utils.config import EDITION_JAVA, EDITION_BEDROCK
from .base import (
    ServerBackend, BackendUnavailable, NotSupported,
    EVENT_JOIN, EVENT_LEAVE, EVENT_DEATH, EVENT_ACHIEVEMENT, EVENT_CHAT,
    CAP_PLAYER_RESTORE, CAP_STATS,
)


def make_backend(config, migrate_legacy=False) -> ServerBackend:
    """Construct the backend for ``config.edition``. ``migrate_legacy`` lets the
    historical single-server install pull its repo-root state files into
    ``data/<key>/`` once (see ``ServerBackend._data_path``).

    May raise ``BackendUnavailable`` (e.g. Bedrock with no tmux/screen session),
    which ``main()`` handles by exiting with a clear message.
    """
    if config.edition == EDITION_BEDROCK:
        from .bedrock import BedrockBackend
        return BedrockBackend(config, migrate_legacy)
    if config.edition == EDITION_JAVA:
        from .java import JavaBackend
        return JavaBackend(config, migrate_legacy)
    raise ValueError(f"Unknown edition: {config.edition}")


__all__ = [
    "ServerBackend", "BackendUnavailable", "NotSupported", "make_backend",
    "EVENT_JOIN", "EVENT_LEAVE", "EVENT_DEATH", "EVENT_ACHIEVEMENT", "EVENT_CHAT",
    "CAP_PLAYER_RESTORE", "CAP_STATS",
]
