"""Telegram adapter — wraps pyTelegramBotAPI (telebot) long-polling.

No public URL needed: Telegram delivers updates via long-polling, which runs in
its own thread (started by ``bot.main``). Monospace blocks use Telegram's HTML
``<pre>`` parse mode; long messages are chunked at 4096 chars.
"""

import logging

import telebot

from .base import ChatAdapter, Context, chunk_text

logger = logging.getLogger("mcnotifier")

_MAX_LEN = 4096


class _NetworkErrorFilter(logging.Filter):
    """Collapse TeleBot's noisy transient network tracebacks into one warning."""
    _TRANSIENT = (
        ("Network is unreachable", "network unreachable"),
        ("NewConnectionError", "network unreachable"),
        ("Max retries exceeded", "network unreachable"),
        ("Read timed out", "read timed out"),
        ("read operation timed out", "read timed out"),
        ("handshake operation timed out", "SSL handshake timed out"),
        ("Bad Gateway", "Telegram returned 502 Bad Gateway"),
        ("Connection reset by peer", "connection reset by peer"),
        ("Remote end closed connection without response", "remote end closed connection"),
    )

    def filter(self, record):
        msg = record.getMessage()
        for phrase, description in self._TRANSIENT:
            if phrase in msg:
                if "Exception traceback" not in msg:
                    logger.warning("Polling: %s, retrying...", description)
                return False
        return True


def _sender_label(message) -> str:
    u = message.from_user
    if u is None:
        return "unknown"
    return f"@{u.username}" if u.username else (u.full_name or str(u.id))


class TelegramAdapter(ChatAdapter):
    name = "telegram"

    def __init__(self, config):
        super().__init__(config)
        self._bot = telebot.TeleBot(config.bot_token)
        logging.getLogger("TeleBot").addFilter(_NetworkErrorFilter())

    def start(self, dispatch) -> None:
        @self._bot.message_handler(func=lambda m: True)
        def _on_message(message):
            text = message.text or ""
            ctx = Context(
                adapter=self,
                chat_id=message.chat.id,
                user_id=message.from_user.id if message.from_user else "",
                is_private=(message.chat.type == "private"),
                text=text,
                args=text.split()[1:],
                sender_label=_sender_label(message),
                reply_to=message.message_id,
            )
            dispatch(ctx)

        self._bot.infinity_polling(timeout=30, long_polling_timeout=20)

    def stop(self) -> None:
        try:
            self._bot.stop_polling()
        except Exception:
            logger.exception("Telegram: stop_polling failed")

    def send(self, chat_id, text, *, monospace=False, reply_to=None) -> None:
        # Chunk the raw text first, then wrap each chunk, so a <pre> block is
        # never split across messages (the 11 chars cover "<pre></pre>").
        parse_mode = "HTML" if monospace else None
        limit = _MAX_LEN - 11 if monospace else _MAX_LEN
        for chunk in chunk_text(text, limit):
            body = f"<pre>{chunk}</pre>" if monospace else chunk
            params = {}
            if reply_to is not None:
                params["reply_parameters"] = telebot.types.ReplyParameters(
                    message_id=reply_to)
                reply_to = None  # only thread the first chunk
            try:
                self._bot.send_message(chat_id, body, parse_mode=parse_mode,
                                       **params)
            except Exception as e:
                logger.warning("Telegram: send to %s failed: %s", chat_id, e)
