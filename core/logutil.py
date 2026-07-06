"""The tag-prefixing log adapter shared by the ``Server`` and ``Bot`` runtimes.

Lives in its own leaf module so both ``core.server`` (Server) and ``bot.py``
(Bot) can import it without a cycle.
"""

import logging


class TagLogAdapter(logging.LoggerAdapter):
    """Prefix every record with [<tag>] so interleaved multi-bot / multi-server
    logs stay attributable (per-server backups/notifications, per-bot commands)."""

    def process(self, msg, kwargs):
        return f"[{self.extra['tag']}] {msg}", kwargs
