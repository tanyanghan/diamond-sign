"""Authorization: the bot-namespaced ``auth.json`` doc plus admin/whitelist checks.

The process-wide doc (``{bot_name: {platform: ns}}``) is built by ``main`` via
``load_auth`` and handed to each ``Bot`` as ``bot.auth_doc`` (with ``bot.auth``
its own ``{platform: ns}`` slice, sharing the same dict objects). Mutations go
through a bot's slice and are persisted with ``save_auth(bot.auth_doc,
AUTH_PATH)`` under ``auth_lock``. Leaf module — stdlib + logger only.
"""

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("diamondsign")

# auth.json lives at the repo root (per-bot, not per-server). This module is
# core/auth.py, so the repo root is two levels up.
AUTH_PATH = Path(__file__).resolve().parent.parent / "auth.json"
auth_lock = threading.Lock()


def _normalize_ns(ns: dict) -> dict:
    """Normalize one platform's auth namespace: string IDs throughout.

    ``chat_servers`` binds an authorized chat to a specific server key (used
    once a bot fronts several servers); single-server bots ignore it and
    announce to every authorized chat. ``chat_names`` records each authorized
    chat's human name (group/channel title) — learned + refreshed from inbound
    messages — so logs and /listchats show names, not raw IDs.
    """
    admin = ns.get("admin_user_id")
    return {
        "admin_user_id": str(admin) if admin is not None else None,
        "authorized_chat_ids": [str(c) for c in ns.get("authorized_chat_ids", [])],
        "chat_servers": {str(c): str(s)
                         for c, s in (ns.get("chat_servers") or {}).items()},
        "chat_names": {str(c): str(n)
                       for c, n in (ns.get("chat_names") or {}).items()},
    }


def load_auth(path: Path, bots: list) -> dict:
    """Load the whole bot-namespaced auth doc for the configured ``bots`` (a list
    of BotConfig):
    ``{bot_name: {platform: {admin_user_id, authorized_chat_ids, chat_servers,
    chat_names}}}``.

    Every configured bot gets a namespace (so ``auth_doc[bot.name]`` always
    resolves) and each namespace is normalized (defaults filled, ids
    stringified). Any bot namespaces already on disk for bots not in this run
    are carried through untouched. Top-level or platform entries that aren't the
    expected ``{bot: {platform: ns}}`` shape are ignored rather than crashing
    startup (there is no migration of older on-disk shapes).
    """
    data = {}
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            logger.exception("Failed to load auth.json")
    if not isinstance(data, dict):
        data = {}
    for b in bots:
        data.setdefault(b.name, {})
    return {bname: {p: _normalize_ns(ns) for p, ns in bns.items()
                    if isinstance(ns, dict)}
            for bname, bns in data.items() if isinstance(bns, dict)}


def save_auth(auth: dict, path: Path) -> None:
    # Write-then-rename so a crash mid-write can't corrupt auth.json.
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(auth, f, indent=2)
    os.replace(tmp, path)


def auth_ns(auth: dict, platform: str) -> dict:
    ns = auth.setdefault(
        platform,
        {"admin_user_id": None, "authorized_chat_ids": [], "chat_servers": {},
         "chat_names": {}})
    ns.setdefault("chat_names", {})  # older docs predate chat_names
    return ns


def is_admin(platform: str, user_id, auth: dict) -> bool:
    admin = (auth.get(platform) or {}).get("admin_user_id")
    return admin is not None and str(admin) == str(user_id)


def is_authorized(platform: str, chat_id, user_id, is_private: bool,
                  auth: dict) -> bool:
    """A command is processed only from the platform admin (in private) or an
    authorized chat (in a group/channel)."""
    ns = auth.get(platform) or {}
    if is_private:
        admin = ns.get("admin_user_id")
        return admin is not None and str(admin) == str(user_id)
    return str(chat_id) in ns.get("authorized_chat_ids", [])
