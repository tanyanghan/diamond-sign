"""Telegram adapter — wraps pyTelegramBotAPI (telebot) long-polling.

No public URL needed: Telegram delivers updates via long-polling, which runs in
its own thread (started by ``bot.main``). Monospace blocks use Telegram's HTML
``<pre>`` parse mode; long messages are chunked at 4096 chars.
"""

import collections
import logging
import threading
import time

import telebot

from .base import ChatAdapter, Context, chunk_text

logger = logging.getLogger("mcnotifier")

_MAX_LEN = 4096


class _PollingHealth:
    """Tracks getUpdates 409 conflicts to tell a transient self-conflict from a
    genuine second poller.

    A lone 409 after a network blip is the bot's *own* orphaned long-poll, which
    Telegram releases within the long-poll window — it self-heals, so it's logged
    quietly. But a sustained run of 409s means another process is polling the same
    bot token, which never resolves on its own, so we escalate to a loud warning.

    A successful transaction (a dispatched update) resets the streak; so does a
    long enough quiet gap, since that means polling recovered. The loud warning is
    throttled to at most once per window so a true double-instance keeps reminding
    without spamming every poll.
    """

    WINDOW_SECONDS = 120      # look-back window for "sustained"
    ESCALATE_AFTER = 5        # this many 409s within the window -> escalate
    RESET_AFTER_QUIET = 60    # a 409 this long after the last starts a fresh streak

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = collections.deque()
        self._last_escalation = None

    def record_conflict(self, now) -> tuple:
        """Register a 409 at monotonic time ``now``; return (verdict, count)
        where verdict is 'escalate' or 'quiet'."""
        with self._lock:
            if self._hits and now - self._hits[-1] > self.RESET_AFTER_QUIET:
                self._hits.clear()                # recovered, then broke again
                self._last_escalation = None
            self._hits.append(now)
            while self._hits and now - self._hits[0] > self.WINDOW_SECONDS:
                self._hits.popleft()
            sustained = len(self._hits) >= self.ESCALATE_AFTER
            if sustained and (self._last_escalation is None
                              or now - self._last_escalation >= self.WINDOW_SECONDS):
                self._last_escalation = now
                return "escalate", len(self._hits)
            return "quiet", len(self._hits)

    def record_success(self) -> None:
        """A successful transaction clears the streak."""
        with self._lock:
            self._hits.clear()
            self._last_escalation = None


class _NetworkErrorFilter(logging.Filter):
    """Collapse TeleBot's noisy transient tracebacks into one-line warnings.

    Network errors and the transient getUpdates 409 conflict (see
    ``_PollingHealth``) are folded into a single ``retrying...`` warning; a
    sustained 409 streak escalates to an explicit "second instance?" warning.

    Installed once on the (telebot-global) "TeleBot" logger via ``install()``:
    its records carry no token, so per-adapter attribution is impossible — all
    adapters share one process-wide ``_PollingHealth``. Stacking one filter per
    adapter would also break counting (the first filter returning False drops
    the record before later filters run).
    """
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
    _CONFLICT = "terminated by other getUpdates"

    def __init__(self, health: _PollingHealth):
        super().__init__()
        self._health = health

    def filter(self, record):
        msg = record.getMessage()
        # telebot logs the 409 twice (a message line and an "Exception traceback"
        # line); count/announce on the first, suppress both.
        if self._CONFLICT in msg:
            if "Exception traceback" not in msg:
                verdict, count = self._health.record_conflict(time.monotonic())
                if verdict == "escalate":
                    logger.warning(
                        "Polling: getUpdates 409 conflict is persisting (%d in the "
                        "last %d min) - another process is polling this bot token; "
                        "check for a second mcnotifier instance using the same "
                        "BOT_TOKEN.", count, self._health.WINDOW_SECONDS // 60)
                else:
                    logger.warning("Polling: getUpdates conflict (transient, "
                                   "self-resolving), retrying...")
            return False
        for phrase, description in self._TRANSIENT:
            if phrase in msg:
                if "Exception traceback" not in msg:
                    logger.warning("Polling: %s, retrying...", description)
                return False
        return True

    @classmethod
    def install(cls, health: _PollingHealth) -> None:
        """Attach one instance to the shared "TeleBot" logger. Idempotent, so N
        adapters (N bots) end up with exactly one filter and one health."""
        lg = logging.getLogger("TeleBot")
        if not any(isinstance(f, cls) for f in lg.filters):
            lg.addFilter(cls(health))


# Process-wide polling health: telebot logs through one global "TeleBot" logger
# with no token on the records, so 409s can't be attributed to a specific bot.
_HEALTH = _PollingHealth()


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
        _NetworkErrorFilter.install(_HEALTH)

    def start(self, dispatch) -> None:
        @self._bot.message_handler(func=lambda m: True)
        def _on_message(message):
            # A delivered update means getUpdates succeeded -> clear any 409 streak.
            _HEALTH.record_success()
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
                # Group/supergroup/channel title (None in a private chat).
                chat_name=getattr(message.chat, "title", None),
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
