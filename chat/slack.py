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
