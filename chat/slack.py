"""Slack adapter — slack_bolt in Socket Mode (no public URL).

Socket Mode opens an outbound websocket, so the bot works behind NAT like the
Telegram long-poller. Commands are Slack **slash commands** (``/status`` …);
each must be declared in the Slack app manifest (see README), but a single
regex catch-all here routes them all into the shared CommandRouter. Replies and
announcements go out via ``chat.postMessage``; monospace uses triple backticks.
"""

import logging
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .base import ChatAdapter, Context, chunk_text

logger = logging.getLogger("mcnotifier")

# Slack hard limit is 40k chars; keep well under for readable, un-truncated posts.
_MAX_LEN = 3500


class _SlackNetworkErrorFilter(logging.Filter):
    """Collapse slack_sdk's noisy transient network / reconnect errors into a
    single one-line warning, mirroring the Telegram adapter's filter.

    During a network or DNS outage slack_sdk logs multi-line ``error`` records
    from several child loggers (``slack_sdk.web.base_client``,
    ``slack_sdk.socket_mode.builtin.client``, …); by default these reach Python's
    unformatted last-resort stderr handler and spam the log. ``install()`` routes
    the ``slack_sdk`` / ``slack_bolt`` loggers through the bot's own handlers and
    attaches this filter to them — a *handler* filter is consulted for records
    from every descendant logger (a logger filter is not), so one blip becomes a
    single ``Slack: network error, retrying...`` line. Non-transient and
    non-Slack records pass through untouched (and now land in the log file with
    the normal prefix).
    """
    _TRANSIENT = (
        "Temporary failure in name resolution",
        "Name or service not known",
        "Network is unreachable",
        "Failed to send a request to Slack API server",
        "Failed to check the current session or reconnect",
        "Connection to Slack failed",
        "timed out",
    )

    def filter(self, record) -> bool:
        if not record.name.startswith("slack"):
            return True  # not ours — leave every other record untouched
        msg = record.getMessage()
        if any(p in msg for p in self._TRANSIENT):
            logger.warning("Slack: network error, retrying...")
            return False  # drop the raw multi-line SDK error
        return True

    @classmethod
    def install(cls) -> None:
        """Route the slack_sdk / slack_bolt loggers through the bot's handlers
        with this filter attached. Idempotent; safe to call once per adapter."""
        mc_handlers = logging.getLogger("mcnotifier").handlers
        if not mc_handlers:
            return  # logging not set up yet; nothing to attach to
        filt = cls()
        for h in mc_handlers:
            if not any(isinstance(f, cls) for f in h.filters):
                h.addFilter(filt)
        for name in ("slack_sdk", "slack_bolt"):
            lg = logging.getLogger(name)
            for h in mc_handlers:
                if h not in lg.handlers:
                    lg.addHandler(h)
            lg.propagate = False  # don't also hit the unformatted last-resort handler

# Slack reserves some slash-command names (e.g. /status, /help) and rejects them
# in an app manifest. Those commands are declared under alternate names in the
# manifest (see README) and mapped back to the bot's canonical command here, so
# routing — and parity with Telegram — is unchanged. Canonical name -> Slack name.
_RENAMED = {"status": "online", "help": "commands"}
_RENAMED_INV = {slack: canon for canon, slack in _RENAMED.items()}


class SlackAdapter(ChatAdapter):
    name = "slack"

    def command_label(self, name: str) -> str:
        return "/" + _RENAMED.get(name, name)

    def __init__(self, config):
        super().__init__(config)
        # Quieten slack_sdk's transient network/reconnect spam (mirrors Telegram).
        _SlackNetworkErrorFilter.install()
        # token_verification_enabled=False so construction makes no network call
        # (make_adapters runs before start(); auth happens when the socket opens).
        self._app = App(token=config.slack_bot_token,
                        token_verification_enabled=False)
        self._handler = None

    def start(self, dispatch) -> None:
        @self._app.command(re.compile(r"/.*"))
        def _on_command(ack, command):
            ack()  # Slack requires acknowledging within 3s
            try:
                cmd = command.get("command", "")           # e.g. "/online"
                name = cmd[1:] if cmd.startswith("/") else cmd
                cmd = "/" + _RENAMED_INV.get(name, name)    # map "/online" -> "/status"
                arg_text = command.get("text", "") or ""    # args after the command
                full = cmd + ((" " + arg_text) if arg_text else "")
                channel_id = command["channel_id"]
                is_private = (command.get("channel_name") == "directmessage"
                              or channel_id.startswith("D"))
                ctx = Context(
                    adapter=self,
                    chat_id=channel_id,
                    user_id=command["user_id"],
                    is_private=is_private,
                    text=full,
                    args=arg_text.split(),
                    sender_label=command.get("user_name") or command["user_id"],
                    reply_to=None,  # slash commands aren't messages — no thread anchor
                )
                dispatch(ctx)
            except Exception:
                logger.exception("Slack: failed handling command")

        self._handler = SocketModeHandler(self._app, self.config.slack_app_token)
        self._handler.start()  # blocking

    def stop(self) -> None:
        if self._handler is not None:
            try:
                self._handler.close()
            except Exception:
                logger.exception("Slack: handler close failed")

    def send(self, chat_id, text, *, monospace=False, reply_to=None) -> None:
        # Chunk first, then wrap each chunk, so a code block is never split.
        limit = _MAX_LEN - 8 if monospace else _MAX_LEN  # 8 covers ```\n…\n```
        for chunk in chunk_text(text, limit):
            body = f"```\n{chunk}\n```" if monospace else chunk
            try:
                self._app.client.chat_postMessage(channel=chat_id, text=body)
            except Exception as e:
                logger.warning("Slack: send to %s failed: %s", chat_id, e)
