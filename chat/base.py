"""Chat-platform abstraction: adapters, message context, command router.

The rest of the bot talks to chat platforms only through these types, so adding
a platform (Telegram, Slack, …) is a matter of writing a ``ChatAdapter``. Several
adapters can run at once: commands are answered on the platform they arrived on
(``Context.reply``), while announcements fan out to every adapter's authorized
chats (see ``bot.make_notify_callback``).

Identity is namespaced per platform in ``auth.json`` (Telegram int IDs vs Slack
``U…``/``C…`` strings), and all IDs are handled as strings here.
"""

from abc import ABC, abstractmethod


class Context:
    """A normalized inbound message, handed to command handlers.

    Replaces the telebot ``message`` object. ``reply`` sends back through the
    adapter the message arrived on, so a command is answered only on its own
    platform/chat.
    """

    def __init__(self, adapter, chat_id, user_id, is_private, text, args,
                 sender_label, reply_to=None):
        self.adapter = adapter
        self.platform = adapter.name
        self.chat_id = str(chat_id)
        self.user_id = str(user_id)
        self.is_private = is_private
        self.text = text
        self.args = args                 # command args, whitespace-split, sans the /cmd
        self.sender_label = sender_label  # human-readable, for logging
        self.reply_to = reply_to          # opaque per-adapter handle for threaded replies

    def reply(self, text, *, monospace=False):
        self.adapter.send(self.chat_id, text, monospace=monospace,
                          reply_to=self.reply_to)


class ChatAdapter(ABC):
    """One running connection to a chat platform."""

    name = "chat"

    def __init__(self, config):
        self.config = config

    @abstractmethod
    def start(self, dispatch) -> None:
        """Connect and deliver each inbound message to ``dispatch(Context)``.
        Blocking — ``bot.main`` runs each adapter in its own daemon thread."""

    @abstractmethod
    def stop(self) -> None:
        """Disconnect (best-effort; called on shutdown)."""

    @abstractmethod
    def send(self, chat_id, text, *, monospace=False, reply_to=None) -> None:
        """Send ``text`` to ``chat_id``. The adapter formats monospace for its
        platform and splits over-long messages into chunks."""

    def command_label(self, name: str) -> str:
        """The user-facing ``/command`` string for a canonical command name on
        this platform. Defaults to ``/name``; a platform overrides this where it
        must expose a command under a different name (e.g. Slack reserves
        ``/status`` and ``/help``, so it renames them). Used to render help text
        that matches what users can actually type."""
        return "/" + name


def chunk_text(text, max_len):
    """Split ``text`` into <= max_len pieces, preferring to break on newlines."""
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


class CommandRouter:
    """Maps ``/command`` names to handlers and enforces access rules.

    Replaces telebot's ``@bot.message_handler`` decorators and the per-handler
    ``guard``/``is_admin``/``chat.type`` boilerplate. ``auth`` is the per-platform
    authorization object (see ``bot`` auth helpers).
    """

    def __init__(self, auth, is_admin, is_authorized, on_unclaimed=None,
                 logger=None):
        self._cmds = {}              # name -> spec dict
        self._auth = auth
        self._is_admin = is_admin
        self._is_authorized = is_authorized
        self._on_unclaimed = on_unclaimed  # called for any message when admin unclaimed
        self._log = logger

    def register(self, names, handler, *, private_only=False, admin_only=False,
                 cap=None, cap_message=None, public=False):
        if isinstance(names, str):
            names = [names]
        spec = {"handler": handler, "private_only": private_only,
                "admin_only": admin_only, "cap": cap, "cap_message": cap_message,
                "public": public}
        for n in names:
            self._cmds[n] = spec

    def dispatch(self, ctx) -> None:
        """Parse and route one inbound message. Silently ignores non-commands and
        unauthorized callers (no reply), matching the original Telegram behaviour."""
        # Admin-claim hook: before an admin exists on this platform, a private
        # message may claim it.
        if self._on_unclaimed and self._on_unclaimed(ctx):
            return

        text = (ctx.text or "").strip()
        if not text.startswith("/"):
            return
        name = text[1:].split(maxsplit=1)[0].lstrip("/")
        # Strip a Telegram-style @botname suffix (/status@MyBot).
        name = name.split("@", 1)[0].lower()
        spec = self._cmds.get(name)
        if spec is None:
            return

        # Authorization: a command is processed only from the platform admin (in
        # private) or an authorized chat — except public commands (e.g. /chat_id,
        # which lets an unauthorized chat learn its own ID).
        if not spec["public"] and not self._is_authorized(
                ctx.platform, ctx.chat_id, ctx.user_id, ctx.is_private):
            return
        if spec["private_only"] and not ctx.is_private:
            return
        if spec["admin_only"] and not self._is_admin(ctx.platform, ctx.user_id):
            return
        if spec["cap"] is not None and not spec["cap"]():
            if spec["cap_message"]:
                ctx.reply(spec["cap_message"])
            return
        try:
            spec["handler"](ctx)
        except Exception:
            if self._log:
                self._log.exception("Command handler failed: /%s", name)
