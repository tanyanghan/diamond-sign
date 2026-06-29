"""Chat adapters and the factory that builds the enabled set."""

from .base import ChatAdapter, Context, CommandRouter, chunk_text


def make_adapters(config) -> list:
    """Construct one ChatAdapter per platform in ``config.platforms``.

    ``config`` is a ``BotConfig`` (one chat identity). Adapters are imported
    lazily so a missing optional SDK (e.g. slack_bolt when only Telegram is
    enabled) doesn't break startup.
    """
    adapters = []
    for platform in config.platforms:
        if platform == "telegram":
            from .telegram import TelegramAdapter
            adapters.append(TelegramAdapter(config))
        elif platform == "slack":
            from .slack import SlackAdapter
            adapters.append(SlackAdapter(config))
        else:
            raise ValueError(f"Unknown chat platform: {platform}")
    return adapters


__all__ = ["ChatAdapter", "Context", "CommandRouter", "chunk_text",
           "make_adapters"]
