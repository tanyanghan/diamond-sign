"""Authorization: the bot-namespaced ``auth.json`` doc plus admin/moderator/whitelist checks.

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
    messages — so logs and /chats show names, not raw IDs. ``paused_chats``
    are authorized chats whose outbound announcements are muted
    (/chats pause <N>); commands from them still work. ``moderators`` maps a
    user ID to the server keys they may run moderator commands on (in a DM);
    ``moderator_names`` is their last-seen display name, for /mods. A user
    left with no servers is dropped.
    """
    admin = ns.get("admin_user_id")
    mods = {str(u): [str(k) for k in keys]
            for u, keys in (ns.get("moderators") or {}).items()
            if isinstance(keys, list) and keys}
    return {
        "admin_user_id": str(admin) if admin is not None else None,
        "authorized_chat_ids": [str(c) for c in ns.get("authorized_chat_ids", [])],
        "chat_servers": {str(c): str(s)
                         for c, s in (ns.get("chat_servers") or {}).items()},
        "chat_names": {str(c): str(n)
                       for c, n in (ns.get("chat_names") or {}).items()},
        "paused_chats": [str(c) for c in ns.get("paused_chats", [])],
        "moderators": mods,
        "moderator_names": {str(u): str(n)
                            for u, n in (ns.get("moderator_names") or {}).items()
                            if str(u) in mods},
    }


def load_auth(path: Path, bots: list) -> dict:
    """Load the whole bot-namespaced auth doc for the configured ``bots`` (a list
    of BotConfig):
    ``{bot_name: {platform: {admin_user_id, authorized_chat_ids, chat_servers,
    chat_names, paused_chats, moderators, moderator_names}}}``.

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
         "chat_names": {}, "paused_chats": [], "moderators": {},
         "moderator_names": {}})
    ns.setdefault("chat_names", {})    # older docs predate chat_names
    ns.setdefault("paused_chats", [])  # ...and paused_chats
    ns.setdefault("moderators", {})    # ...and moderators
    ns.setdefault("moderator_names", {})
    return ns


def is_admin(platform: str, user_id, auth: dict) -> bool:
    admin = (auth.get(platform) or {}).get("admin_user_id")
    return admin is not None and str(admin) == str(user_id)


def moderated_servers(platform: str, user_id, auth: dict) -> set:
    """Server keys ``user_id`` moderates on ``platform`` (empty if none)."""
    mods = (auth.get(platform) or {}).get("moderators") or {}
    return set(mods.get(str(user_id)) or [])


def is_moderator(platform: str, user_id, auth: dict, server_key=None) -> bool:
    """True if ``user_id`` moderates ``server_key`` — or, with no key, any
    server on this platform."""
    servers = moderated_servers(platform, user_id, auth)
    return bool(servers) if server_key is None else str(server_key) in servers


def is_authorized(platform: str, chat_id, user_id, is_private: bool,
                  auth: dict) -> bool:
    """A command is processed only from the platform admin or a moderator (in
    private) or an authorized chat (in a group/channel). Which server a
    moderator may act on is enforced when the command's server is resolved
    (``Bot.resolve_command``)."""
    ns = auth.get(platform) or {}
    if is_private:
        return (is_admin(platform, user_id, auth)
                or is_moderator(platform, user_id, auth))
    return str(chat_id) in ns.get("authorized_chat_ids", [])
