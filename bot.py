import argparse
import gzip
import json
import logging
import os
import re
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.observers import Observer

from utils.backup_utils import (
    CHAIN_MARKER_NAME, META_FILES,
    build_file_manifest, new_chain_id, run_copy_command, wait_for_settle,
)
from utils.config import (
    load_config, backup_exclude_names, EDITION_BEDROCK, ConfigError,
)
from utils import restore_core
from backends import (
    make_backend, BackendUnavailable, CAP_PLAYER_RESTORE, CAP_STATS,
    EVENT_DEATH, EVENT_ACHIEVEMENT,
)
from chat import make_adapters, CommandRouter
from core.logutil import TagLogAdapter
from core.state import (
    refresh_player_names, register_player, uuid_by_name,
    load_achievements, record_achievement, load_deaths, record_death,
)
from core.auth import (
    AUTH_PATH, auth_lock, load_auth, save_auth, auth_ns,
    is_admin, is_authorized,
)
from core.logparse import (
    parse_line, categorize_death, DEATH_CATEGORIES, DEATH_PHRASES,
    ACH_TYPE_MAP,
    RE_UUID, RE_ACHIEVEMENT, RE_SERVER_MSG,
)
from core.presence import reconcile_online, recover_online_identities
from core.notifications import make_notify_callback
from core.logwatch import LogWatcher
from core.server import Server

# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
# All config reading lives in config.load_config(), which returns an AppConfig
# (bots -> servers). load_config() reads diamondsign.json or runs a first-run
# wizard; on a misconfiguration it raises ConfigError, which we surface cleanly
# and stop.
try:
    APP_CONFIG = load_config()
except ConfigError as e:
    print(f"\n{e}\n", file=sys.stderr)
    sys.exit(1)

# main() builds a Server per server-config and a Bot per bot-config from
# APP_CONFIG and loops over all of them (see main / _bring_up_*). No module-level
# server/bot/backend singletons — every instance carries its own config, backend,
# and state.

# Per-server state lives under data/<server-name>/ so multiple servers in one
# process never collide. (auth.json stays at the repo root — it's per-bot, not
# per-server.) Each Server resolves its own paths under config.data_dir; see
# Server._data_path.


# ---------------------------------------------------------------------------
# Logging setup (configured in main, used everywhere via module-level logger)
# ---------------------------------------------------------------------------
logger = logging.getLogger("diamondsign")


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"log_{timestamp}.txt"

    fmt = logging.Formatter("%(asctime)s  %(name)-12s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    telebot_logger = logging.getLogger("TeleBot")
    telebot_logger.addHandler(file_handler)

    logger.info("Logging started — writing to %s", log_file)




class Bot:
    """One chat identity (its Telegram bot and/or Slack app) fronting a set of
    servers. Owns the adapters, the command router, and this bot's slice of the
    auth doc; delivers announcements only to its own authorized chats. main()
    builds one today and will loop over several once multi-bot lands.

    Announcements are per (bot, server): a single-server bot fans out to every
    authorized chat (today's behavior), while a multi-server bot delivers only
    to chats bound to that server via the ``chat_servers`` auth binding.
    """

    def __init__(self, config, servers):
        self.config = config
        self.log = TagLogAdapter(logger, {"tag": config.name})
        self.servers = list(servers)
        self.by_name = {s.config.name: s for s in self.servers}
        self.by_key = {s.config.key: s for s in self.servers}
        # Per-admin /use selection, in-memory only: {platform: {user_id: key}}.
        self._admin_session: dict = {}
        # Set in main() once the adapters and auth doc are built.
        self.adapters: list = []
        self.router = None
        self.auth_doc: dict = {}   # whole {bot: {platform: ns}} doc (for saves)
        self.auth: dict = {}       # this bot's slice: {platform: ns}

    def drop_server(self, server) -> None:
        """Remove a server from this bot (e.g. its backend failed to start), so
        resolution and announcements ignore it."""
        self.servers = [s for s in self.servers if s is not server]
        self.by_name.pop(server.config.name, None)
        self.by_key.pop(server.config.key, None)

    def find_server(self, token: str):
        """Resolve a user-typed server token (its name or data key) to a Server,
        or None if it matches neither."""
        if not token:
            return None
        return self.by_name.get(token) or self.by_key.get(token)

    def note_chat_name(self, ctx) -> None:
        """Learn/refresh an authorized chat's human name from an inbound message
        and persist it in auth.json. Called for every dispatched message, so a
        group/channel rename is picked up on its next message. Only authorized
        chats are recorded (keeps the map bounded and relevant)."""
        if ctx.is_private or not ctx.chat_name:
            return
        ns = self.auth.get(ctx.platform)
        if ns is None or ctx.chat_id not in ns.get("authorized_chat_ids", []):
            return
        names = ns.setdefault("chat_names", {})
        if names.get(ctx.chat_id) == ctx.chat_name:
            return  # unchanged — no write
        with auth_lock:
            names[ctx.chat_id] = ctx.chat_name
            save_auth(self.auth_doc, AUTH_PATH)
        self.log.info("Chat name learned: %s = [%s]", ctx.chat_id, ctx.chat_name)

    def chat_display(self, platform: str, chat_id: str) -> str:
        """Human label for a chat ID from the persisted names, else the ID —
        for logs/listings that have no live inbound message to read a name from."""
        ns = self.auth.get(platform) or {}
        name = (ns.get("chat_names") or {}).get(str(chat_id))
        return f"{name} ({chat_id})" if name else str(chat_id)

    def resolve_target_server(self, ctx):
        """Pick the Server a server-scoped command should act on, or None if it's
        ambiguous (a multi-server bot with no channel binding and no /use
        selection). Resolution order:
          1. single-server bot -> its only server (implicit);
          2. a bound group/channel -> the server in its chat_servers binding;
          3. an admin DM -> the /use session selection.
        """
        if len(self.servers) == 1:
            return self.servers[0]
        ns = self.auth.get(ctx.platform) or {}
        if not ctx.is_private:
            key = (ns.get("chat_servers") or {}).get(ctx.chat_id)
            return self.by_key.get(key)
        sel = (self._admin_session.get(ctx.platform) or {}).get(ctx.user_id)
        return self.by_key.get(sel)

    def _server_menu(self, current=None) -> str:
        """Numbered server list for /use and the disambiguation prompt. The
        numbers are 1-based over ``self.servers`` (stable order) so a user can
        pick with ``/use <number>``. ``current`` (a Server or None) is marked."""
        lines = []
        for i, s in enumerate(self.servers, 1):
            mark = "*" if s is current else " "
            lines.append(f" {mark} {i}. {s.config.name}")
        return "\n".join(lines)

    def _resolve_use_token(self, token: str):
        """Resolve a /use argument to a Server: a 1-based list number (as shown
        by ``_server_menu``), or a server name/key. Returns None if it matches
        neither. A numeric token is always read as a list index — server names
        are never bare numbers (config rejects unsafe names)."""
        token = (token or "").strip()
        if token.isdigit():
            i = int(token)
            return self.servers[i - 1] if 1 <= i <= len(self.servers) else None
        return self.find_server(token)

    def resolve_command(self, ctx) -> bool:
        """CommandRouter resolve hook: set ctx.server, or reply with a
        disambiguation message and return False."""
        server = self.resolve_target_server(ctx)
        if server is None:
            ctx.reply("This bot serves multiple servers. Pick one with "
                      "/use <number> (or /use <name>):\n" + self._server_menu())
            return False
        ctx.server = server
        return True

    def set_use(self, ctx) -> None:
        """Handle /use: bare form lists servers (numbered) + current selection;
        /use <number> or /use <server> sets this admin's session target for
        subsequent commands."""
        current = (self._admin_session.get(ctx.platform) or {}).get(ctx.user_id)
        cur = self.by_key.get(current)
        if not ctx.args:
            ctx.reply("Servers (pick with /use <number> or /use <name>):\n"
                      + self._server_menu(cur)
                      + f"\n\nCurrent: {cur.config.name if cur else '(none)'}")
            return
        target = self._resolve_use_token(ctx.args[0])
        if target is None:
            ctx.reply(f"Unknown server '{ctx.args[0]}'.\n" + self._server_menu(cur))
            return
        self._admin_session.setdefault(ctx.platform, {})[ctx.user_id] = target.config.key
        ctx.reply(f"Now using {target.config.name} for your commands.")

    def _server_chats(self, adapter, server):
        """Yield the authorized chat IDs on ``adapter`` that should receive
        ``server``'s announcements: all of them for a single-server bot, else
        only those bound to the server's key."""
        ns = self.auth.get(adapter.name) or {}
        chat_ids = ns.get("authorized_chat_ids", [])
        if len(self.servers) == 1:
            yield from chat_ids
            return
        binding = ns.get("chat_servers", {})
        key = server.config.key
        for chat_id in chat_ids:
            if binding.get(chat_id) == key:
                yield chat_id

    def announce(self, server, msg: str) -> int:
        """Send an announcement about ``server`` to its authorized chats on every
        platform. Returns how many chats it reached."""
        sent = 0
        for adapter in self.adapters:
            for chat_id in self._server_chats(adapter, server):
                try:
                    adapter.send(chat_id, msg)
                    sent += 1
                except Exception as e:
                    logger.warning("Announce to %s/%s failed: %s",
                                   adapter.name, chat_id, e)
        return sent

    def alert_admins(self, msg: str) -> None:
        """Send an operational alert to each platform's admin (if claimed)."""
        for adapter in self.adapters:
            admin = (self.auth.get(adapter.name) or {}).get("admin_user_id")
            if admin:
                try:
                    adapter.send(admin, msg)
                except Exception:
                    logger.warning("Failed to alert %s admin", adapter.name)


# ---------------------------------------------------------------------------
# 7. Stats Logic
# ---------------------------------------------------------------------------
# Per-player stat dicts come from the backend (Java: world stat files; Bedrock:
# accumulated online time). Every dict has `name` + `time_played_hours`; only
# Java adds the richer fields, so the formatter shows whatever is present.
_STAT_FIELDS = [
    ("time_played_hours", "Time played", "h"),
    ("sessions", "Sessions", ""),
    ("deaths", "Deaths", ""),
    ("diamonds_mined", "Diamonds mined", ""),
    ("ancient_debris_mined", "Ancient debris mined", ""),
    ("distance_travelled_km", "Distance travelled", " km"),
    ("villager_trades", "Villager trades", ""),
    ("total_mobs_killed", "Mobs killed", ""),
    ("last_seen", "Last seen", ""),
]


def _format_stats(p: dict) -> str:
    lines = [f"Player: {p['name']}"]
    for key, label, unit in _STAT_FIELDS:
        if key in p and p[key] != "":
            lines.append(f"  {label}: {p[key]}{unit}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7b. Achievement Scanning
# ---------------------------------------------------------------------------
RE_GZ_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})-\d+\.log\.gz')


def _scan_log_for_achievements(file_path: Path, date_str: str, server) -> int:
    count = 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = RE_UUID.match(line)
            if m:
                name, uuid = m.group(1), m.group(2)
                register_player(server, uuid, name)
                continue
            m = RE_ACHIEVEMENT.match(line)
            if m:
                time_str, player, ach_type_full, achievement = m.groups()
                ach_type = ACH_TYPE_MAP[ach_type_full]
                timestamp = f"{date_str} {time_str}"
                uuid = uuid_by_name(player, server.names)
                if uuid:
                    if record_achievement(uuid, achievement, ach_type,
                                          timestamp, server.achievements,
                                          server.achievements_path):
                        count += 1
                else:
                    server.log.warning("Scan: no UUID for player %s, skipping achievement", player)
    return count


def _scan_log_for_deaths(file_path: Path, date_str: str, server) -> int:
    count = 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = RE_UUID.match(line)
            if m:
                name, uuid = m.group(1), m.group(2)
                register_player(server, uuid, name)
                continue
            m = RE_SERVER_MSG.match(line)
            if m:
                time_str, player, msg = m.groups()
                if any(msg.startswith(p) for p in DEATH_PHRASES):
                    timestamp = f"{date_str} {time_str}"
                    uuid = uuid_by_name(player, server.names)
                    if uuid:
                        if record_death(uuid, msg, timestamp, server.deaths,
                                        server.deaths_path):
                            count += 1
                    else:
                        server.log.warning("Scan: no UUID for player %s, skipping death", player)
    return count


def _scan_logs(scan_fn, server) -> int:
    """Run ``scan_fn`` over every rotated ``*.log.gz`` (by embedded date) plus the
    live log, returning the total newly-recorded count. Shared by /scan_deaths
    and /scan_achievements."""
    log_path = server.config.log_path
    logs_dir = log_path.parent
    total = 0
    for gz_path in sorted(logs_dir.glob("*.log.gz")):
        m = RE_GZ_DATE.match(gz_path.name)
        if not m:
            continue
        date_str = m.group(1)
        extracted = gz_path.with_suffix("")  # remove .gz
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as gz_f:
                with open(extracted, "w", encoding="utf-8") as out_f:
                    out_f.write(gz_f.read())
            total += scan_fn(extracted, date_str, server)
        except Exception as e:
            server.log.warning("Scan: failed to process %s: %s", gz_path.name, e)
        finally:
            if extracted.exists():
                extracted.unlink()
    if log_path.exists():
        total += scan_fn(log_path, datetime.now().strftime("%Y-%m-%d"), server)
    return total


# ---------------------------------------------------------------------------
# 7d. RCON & Backup
# ---------------------------------------------------------------------------

# /restore_player pending-state, keyed by admin user_id.
# Forces the admin through the list -> select -> confirm sequence so a typo
# in a single command can't trigger a destructive restore.
_pending_player_restore: dict = {}
_pending_player_lock = threading.Lock()
_PENDING_PLAYER_RESTORE_TTL = 300  # seconds; older entries are treated as missing
_RESTORE_PLAYER_PAGE_SIZE = 10  # versions shown per page in /restore_player listing

# /restore (whole-world) pending-state, keyed like the player restore by
# "{bot}:{server}:{platform}:{user}". Same list -> select -> confirm gate so an
# accidental /restore can't wipe and rebuild the world in one command.
_pending_world_restore: dict = {}
_pending_world_lock = threading.Lock()
_PENDING_WORLD_RESTORE_TTL = 300
_RESTORE_PAGE_SIZE = 10  # restore points shown per page


def _get_pending_world_restore(pkey: str, expected_stage: str | None = None) -> dict | None:
    with _pending_world_lock:
        entry = _pending_world_restore.get(pkey)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _PENDING_WORLD_RESTORE_TTL:
            _pending_world_restore.pop(pkey, None)
            return None
        if expected_stage is not None and entry.get("stage") != expected_stage:
            return None
        return entry


def _set_pending_world_restore(pkey: str, **fields) -> None:
    with _pending_world_lock:
        existing = _pending_world_restore.get(pkey, {})
        existing.update(fields)
        existing["ts"] = time.time()
        _pending_world_restore[pkey] = existing


def _clear_pending_world_restore(pkey: str) -> None:
    with _pending_world_lock:
        _pending_world_restore.pop(pkey, None)


def _format_restore_points(points: list, offset: int = 0) -> str:
    """Render one page of the numbered restore-point list for /restore."""
    if not points:
        return ("No backups found for this server.\n"
                "Run /backup first, or check the backup directory.")
    page = points[offset:offset + _RESTORE_PAGE_SIZE]
    lines = ["World restore points (latest first).",
             "To select, send: /restore <number>", ""]
    for p in page:
        chain = f"chain {p['chain_id']}" if p['chain_id'] else "standalone"
        lines.append(f"  {p['n']:3d}.  [{p['kind']}] {p['pretty_ts']}   "
                     f"({p['pretty_size']}, {chain})")
    remaining = len(points) - (offset + _RESTORE_PAGE_SIZE)
    if remaining > 0:
        lines.append(f"\n{remaining} more. Send: /restore more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Player-data restore helpers (used by /restore_player)
# ---------------------------------------------------------------------------


def _format_versions_reply(username: str, uuid: str, versions: list,
                           offset: int = 0) -> str:
    """Render one page of the numbered list reply for /restore_player <username>."""
    if not versions:
        return (f"No player data found for {username} ({uuid}).\n"
                f"Live file missing and no backups in the active chain.")
    page = versions[offset:offset + _RESTORE_PLAYER_PAGE_SIZE]
    lines = [f"Player data versions for {username}  (UUID: {uuid})",
             "Latest first. To select, send: /restore_player "
             f"{username} <number>", ""]
    for i, v in enumerate(page, offset + 1):
        lines.append(f"  {i:3d}.  {v['timestamp']}   {v['source']}")
    remaining = len(versions) - (offset + _RESTORE_PLAYER_PAGE_SIZE)
    if remaining > 0:
        lines.append(f"\n{remaining} more. Send: /restore_player {username} more")
    return "\n".join(lines)


def _format_confirm_reply(username: str, uuid: str, n: int, version: dict) -> str:
    """Render the step-2 confirmation block."""
    return (
        "Confirm restore:\n"
        f"  Player:    {username}\n"
        f"  UUID:      {uuid}\n"
        f"  Timestamp: {version['timestamp']}\n"
        f"  Source:    {version['source']}\n"
        "\n"
        "  To proceed, send:\n"
        f"    /restore_player {username} {n} confirm"
    )


def _get_pending_player_restore(user_id: int,
                                expected_username: str | None = None,
                                expected_stage: str | None = None) -> dict | None:
    """Lookup pending player restore for an admin, with TTL and match checks.

    Returns the pending entry if it exists, hasn't expired, matches the
    expected username (case-insensitive), and is in the expected stage.
    Otherwise returns None and (if expired) discards the stale entry.
    """
    with _pending_player_lock:
        entry = _pending_player_restore.get(user_id)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _PENDING_PLAYER_RESTORE_TTL:
            _pending_player_restore.pop(user_id, None)
            return None
        if (expected_username is not None
                and entry["username"].lower() != expected_username.lower()):
            return None
        if expected_stage is not None and entry["stage"] != expected_stage:
            return None
        return entry


def _set_pending_player_restore(user_id: int, **fields) -> None:
    """Store/update a pending player restore entry with the current timestamp."""
    with _pending_player_lock:
        existing = _pending_player_restore.get(user_id, {})
        existing.update(fields)
        existing["ts"] = time.time()
        _pending_player_restore[user_id] = existing


def _clear_pending_player_restore(user_id: int) -> None:
    with _pending_player_lock:
        _pending_player_restore.pop(user_id, None)


# ---------------------------------------------------------------------------
# Bot Commands
# ---------------------------------------------------------------------------
def _cmd_log(ctx, action: str, extra: str = "") -> None:
    """Uniform command audit line, prefixed with the handling bot:
    ``[<bot>] <action>: requested by [<sender>] on [<chat>]<extra>`` — where
    <chat> is the group/channel name (or 'direct' for a DM)."""
    ctx.bot.log.info("%s: requested by [%s] on [%s]%s",
                     action, ctx.sender_label, ctx.chat_label, extra)


def _status_line(server) -> str:
    """One-line up/down + player summary for a server (used by /status).

    Liveness first, so a down server gets a clear 'offline' answer instead of a
    slow/last-known one; when online, the server is queried live (reconciling
    the in-memory set) so the count reflects reality, not the last log parse."""
    name = server.config.name
    if not server.backend.is_online():
        hint = " Start it with /start." if server.backend.can_restart else ""
        return f"🔴 {name} is offline.{hint}"
    online = reconcile_online(server, reason="/status")
    suffix = ""
    if online is None:  # reachable a moment ago but the query failed — last-known
        online = server.get_online_players()
        suffix = " (last known — live query failed)"
    if online:
        return (f"🟢 {name} is online — {len(online)} player(s): "
                f"{', '.join(online)}{suffix}")
    return f"🟢 {name} is online — no players online.{suffix}"


def register_commands(router, auth: dict) -> None:
    """Register every command on the platform-agnostic router. Handlers take a
    Context and reply via it, so the same logic serves any chat platform. Each
    server-scoped handler acts on ``ctx.server`` (set by the router's resolve
    hook) — its backend, name registry, achievements, and deaths."""

    # --- /start, /help ---
    def cmd_help(ctx):
        _cmd_log(ctx, "Help")
        backend = ctx.server.backend
        lines = [
            "Available commands:",
            f"{ctx.adapter.command_label('status')} — show online players",
            "/list — list all known players",
        ]
        if backend.supports(CAP_STATS):
            lines += [
                "/stats [player] — player statistics",
                "/playtime — playtime leaderboard",
            ]
        if backend.supports(EVENT_ACHIEVEMENT):
            lines.append("/achievements [player] — player achievements")
        if backend.supports(EVENT_DEATH):
            lines += [
                "/deaths [player] — death history",
                "/death_summary — deaths grouped by cause",
            ]
        lines.append("/chat_id — show this chat's ID")
        if ctx.is_private and is_admin(ctx.platform, ctx.user_id, auth):
            if len(ctx.bot.servers) > 1:
                lines.append("/use [number|server] — list/pick the server your commands act on")
            lines += [
                "/authorize <chat_id> — whitelist a chat",
                "/revoke <chat_id> — remove a chat from whitelist",
                "/listchats — list authorized chats",
            ]
            if backend.supports(EVENT_ACHIEVEMENT):
                lines.append("/scan_achievements — scan all logs for achievements")
            if backend.supports(EVENT_DEATH):
                lines.append("/scan_deaths — scan all logs for deaths")
            lines.append("/backup — trigger a server backup now")
            if backend.supports(CAP_PLAYER_RESTORE):
                lines.append("/restore_player <username> — restore one player's data")
            if backend.can_restart:
                lines.append("/restore [<N>] — restore the whole world (stops + "
                             "restarts the server)")
                lines.append("/start — start the server if it's offline")
            lines.append(f"/allowlist <on|off|add|remove|list|reload> [player] "
                         f"— server {backend.ALLOWLIST_VERB}")
        ctx.reply("\n".join(lines))
    router.register(["start", "help"], cmd_help)

    # --- /status ---
    def cmd_status(ctx):
        _cmd_log(ctx, "Status")
        # An admin DM reports every server this bot fronts (a whole-bot overview);
        # an authorized group/channel reports just the server bound to it, as
        # before. A private chat is only reachable by the admin (is_authorized),
        # so is_private here implies admin — the is_admin check is belt-and-braces.
        if ctx.is_private and is_admin(ctx.platform, ctx.user_id, auth):
            servers = ctx.bot.servers
        else:
            server = ctx.bot.resolve_target_server(ctx)
            if server is None:
                ctx.reply("This chat isn't bound to a server. Ask an admin to "
                          "/authorize it for one.")
                return
            servers = [server]
        reply = "\n".join(_status_line(s) for s in servers)
        ctx.reply(reply)
        ctx.bot.log.info("Status: replied to [%s] — %d server(s)",
                         ctx.sender_label, len(servers))
    router.register("status", cmd_status, needs_server=False)

    # --- /stats ---
    def cmd_stats(ctx):
        refresh_player_names(ctx.server)
        target = ctx.args[0].lower() if ctx.args else None
        _cmd_log(ctx, "Stats", f" (player={target or 'all'})")
        all_stats = ctx.server.backend.player_stats(ctx.server.names)
        if not all_stats:
            ctx.reply("No player statistics recorded yet.")
            return
        if target:
            matches = [p for p in all_stats if p["name"].lower() == target]
            if not matches:
                ctx.reply(f"No player found matching '{target}'.")
                return
            ctx.reply(_format_stats(matches[0]))
        else:
            lines = [_format_stats(p) for p in sorted(all_stats, key=lambda p: p["name"].lower())]
            ctx.reply("\n\n".join(lines))
    router.register("stats", cmd_stats,
                    cap=lambda ctx: ctx.server.backend.supports(CAP_STATS),
                    cap_message="Player statistics are not available on this server edition.")

    # --- /playtime ---
    def cmd_playtime(ctx):
        refresh_player_names(ctx.server)
        _cmd_log(ctx, "Playtime")
        all_stats = ctx.server.backend.player_stats(ctx.server.names)
        if not all_stats:
            ctx.reply("No player statistics recorded yet.")
            return
        ranked = sorted(all_stats, key=lambda p: p["time_played_hours"], reverse=True)
        lines = [f"{i+1}. {p['name']} — {p['time_played_hours']}h" for i, p in enumerate(ranked)]
        ctx.reply("Playtime leaderboard:\n" + "\n".join(lines))
    router.register("playtime", cmd_playtime,
                    cap=lambda ctx: ctx.server.backend.supports(CAP_STATS),
                    cap_message="Playtime is not available on this server edition.")

    # --- /list ---
    def cmd_list(ctx):
        refresh_player_names(ctx.server)
        _cmd_log(ctx, "List")
        entries = ctx.server.backend.list_known_players(ctx.server.names)
        if not entries:
            ctx.reply("No players found.")
            ctx.bot.log.info("List: replied to [%s] — no known players", ctx.sender_label)
            return
        ctx.reply("Known players:\n" + "\n".join(entries))
        ctx.bot.log.info("List: replied to [%s] — %d known player(s)",
                         ctx.sender_label, len(entries))
    router.register("list", cmd_list)

    # --- /achievements ---
    def cmd_achievements(ctx):
        refresh_player_names(ctx.server)
        names = ctx.server.names
        achievements = ctx.server.achievements
        target = ctx.args[0].lower() if ctx.args else None
        _cmd_log(ctx, "Achievements", f" (player={target or 'all'})")
        if not achievements:
            ctx.reply("No achievements recorded yet.")
            return
        if target:
            uuid = next((u for u, n in names.items() if n.lower() == target), None)
            if not uuid or uuid not in achievements:
                ctx.reply(f"No achievements found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            lines = [f"Achievements for {player_name}:"]
            current_date = None
            for e in sorted(achievements[uuid], key=lambda x: x["timestamp"]):
                date_part, time_part = e["timestamp"].split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_part[:5]} | {e['type']:<11} | {e['achievement']}")
            ctx.reply("\n".join(lines), monospace=True)
        else:
            lines = []
            for uuid, entries in sorted(achievements.items(),
                                        key=lambda x: names.get(x[0], x[0]).lower()):
                lines.append(f"{names.get(uuid, uuid)}: {len(entries)} achievement(s)")
            ctx.reply("Achievements summary:\n" + "\n".join(lines))
    router.register("achievements", cmd_achievements,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_ACHIEVEMENT),
                    cap_message="Achievements are not tracked on this server edition.")

    # --- /scan_achievements ---
    def cmd_scan_achievements(ctx):
        _cmd_log(ctx, "ScanAchievements")
        refresh_player_names(ctx.server)
        ctx.reply("Scanning log files for achievements...")
        total = _scan_logs(_scan_log_for_achievements, ctx.server)
        ctx.reply(f"Scan complete. {total} new achievement(s) recorded.")
        ctx.bot.log.info("ScanAchievements: %d new achievement(s) found", total)
    router.register("scan_achievements", cmd_scan_achievements,
                    private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_ACHIEVEMENT),
                    cap_message="Achievements are not tracked on this server edition.")

    # --- /deaths ---
    def cmd_deaths(ctx):
        refresh_player_names(ctx.server)
        names = ctx.server.names
        deaths = ctx.server.deaths
        target = ctx.args[0].lower() if ctx.args else None
        _cmd_log(ctx, "Deaths", f" (player={target or 'all'})")
        if not deaths:
            ctx.reply("No deaths recorded yet.")
            return
        if target:
            uuid = next((u for u, n in names.items() if n.lower() == target), None)
            if not uuid or uuid not in deaths:
                ctx.reply(f"No deaths found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            entries = deaths[uuid]
            lines = [f"Deaths for {player_name} ({len(entries)} total):"]
            current_date = None
            for e in sorted(entries, key=lambda x: x["timestamp"]):
                date_part, time_part = e["timestamp"].split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_part[:5]} | {e['message']}")
            ctx.reply("\n".join(lines), monospace=True)
        else:
            ranked = sorted(((names.get(u, u), len(e)) for u, e in deaths.items()),
                            key=lambda x: x[1], reverse=True)
            lines = ["Death summary:"]
            for player_name, count in ranked:
                lines.append(f"  {player_name}: {count} death(s)")
            ctx.reply("\n".join(lines), monospace=True)
    router.register("deaths", cmd_deaths,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /death_summary ---
    def cmd_death_summary(ctx):
        refresh_player_names(ctx.server)
        names = ctx.server.names
        deaths = ctx.server.deaths
        _cmd_log(ctx, "DeathSummary")
        if not deaths:
            ctx.reply("No deaths recorded yet.")
            return
        categories = {}
        grand_total = 0
        for uuid, entries in deaths.items():
            player_name = names.get(uuid, uuid)
            for e in entries:
                cat = categorize_death(e["message"])
                counts = categories.setdefault(cat, {})
                counts[player_name] = counts.get(player_name, 0) + 1
                grand_total += 1
        lines = [f"Death Summary ({grand_total} total)", ""]
        ordered = [cat for cat, _ in DEATH_CATEGORIES]
        for cat in ordered:
            if cat not in categories:
                continue
            counts = categories.pop(cat)
            lines.append(f"{cat}: {sum(counts.values())}")
            for player_name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")
        if "Other" in categories:
            counts = categories["Other"]
            lines.append(f"Other: {sum(counts.values())}")
            for player_name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")
        ctx.reply("\n".join(lines).rstrip(), monospace=True)
    router.register("death_summary", cmd_death_summary,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /scan_deaths ---
    def cmd_scan_deaths(ctx):
        _cmd_log(ctx, "ScanDeaths")
        refresh_player_names(ctx.server)
        ctx.reply("Scanning log files for deaths...")
        total = _scan_logs(_scan_log_for_deaths, ctx.server)
        ctx.reply(f"Scan complete. {total} new death(s) recorded.")
        ctx.bot.log.info("ScanDeaths: %d new death(s) found", total)
    router.register("scan_deaths", cmd_scan_deaths,
                    private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /backup ---
    def cmd_backup(ctx):
        server = ctx.server
        if not server.backup_lock.acquire(blocking=False):
            ctx.reply("A backup is already in progress.")
            return
        server.log.info("Backup: manually triggered by [%s] on [%s]",
                        ctx.sender_label, ctx.chat_label)
        ctx.reply("Starting backup...")
        say = lambda m: ctx.adapter.send(ctx.chat_id, m)

        def run():
            try:
                path = server.run_backup(status_cb=say)
                say(f"Backup complete: {Path(path).name}")
            except Exception as e:
                server.log.exception("Backup failed")
                say(f"Backup failed: {e}")
            finally:
                server.backup_lock.release()

        threading.Thread(target=run, daemon=True).start()
    router.register("backup", cmd_backup, private_only=True, admin_only=True,
                    needs_online=True)

    # --- /allowlist (server whitelist/allowlist passthrough) ---
    _ALLOWLIST_SUBS = {"on", "off", "add", "remove", "list", "reload"}

    def cmd_allowlist(ctx):
        backend = ctx.server.backend
        verb = backend.ALLOWLIST_VERB
        usage = (f"Usage: /allowlist <on|off|add|remove|list|reload> [player]\n"
                 f"(runs the server '{verb}' command)")
        if not ctx.args:
            ctx.reply(usage)
            return
        sub = ctx.args[0].lower()
        if sub not in _ALLOWLIST_SUBS:
            ctx.reply(f"Unknown subcommand '{ctx.args[0]}'.\n{usage}")
            return
        if sub in ("add", "remove") and len(ctx.args) < 2:
            ctx.reply(f"Usage: /allowlist {sub} <player>")
            return
        if not backend.is_available():
            ctx.reply("Server is not reachable right now — try again once it's up.")
            return
        ctx.server.log.info("Allowlist: '%s %s' by [%s] on [%s]", verb,
                            " ".join(ctx.args), ctx.sender_label, ctx.chat_label)

        # Run in a thread: Bedrock capture polls the log for up to a few seconds.
        def run():
            try:
                resp = backend.allowlist_command(ctx.args)
            except Exception as e:
                ctx.server.log.exception("Allowlist command failed")
                ctx.adapter.send(ctx.chat_id, f"{verb} command failed: {e}")
                return
            ctx.adapter.send(ctx.chat_id,
                             resp.strip() or "(server returned no output)",
                             monospace=True)

        threading.Thread(target=run, daemon=True).start()
    router.register("allowlist", cmd_allowlist, private_only=True,
                    admin_only=True, needs_online=True)

    # --- /restore_player ---
    def cmd_restore_player(ctx):
        server = ctx.server
        backend = server.backend
        # Pending state is scoped to (bot, server, platform, admin): the
        # list -> select -> confirm sequence must not survive a /use switch (or
        # a same-admin second bot), or the confirm would restore onto a
        # different server with an index chosen from another server's list.
        pkey = (f"{ctx.bot.config.name}:{server.config.key}:"
                f"{ctx.platform}:{ctx.user_id}")
        if not ctx.args:
            ctx.reply("Usage:\n"
                      "  /restore_player <username>\n"
                      "  /restore_player <username> more\n"
                      "  /restore_player <username> <N>\n"
                      "  /restore_player <username> <N> confirm")
            return
        typed_name = ctx.args[0]
        typed_n = ctx.args[1] if len(ctx.args) >= 2 else None
        typed_confirm = ctx.args[2] if len(ctx.args) >= 3 else None
        if typed_confirm is not None and typed_confirm.lower() != "confirm":
            ctx.reply(f"Unexpected argument: '{typed_confirm}' (did you mean 'confirm'?)")
            return
        resolved = backend.resolve_player(typed_name, server.names)
        if resolved is None:
            ctx.reply(f"Unknown player: {typed_name}")
            return
        canonical, uuid = resolved

        if typed_n is None:
            versions = backend.list_player_versions(uuid, server.load_manifest()[:2])
            if not versions:
                _clear_pending_player_restore(pkey)
                ctx.reply(_format_versions_reply(canonical, uuid, versions))
                return
            _set_pending_player_restore(
                pkey, stage="listed", username=canonical, uuid=uuid,
                versions=versions, selected_n=None, page_offset=0)
            server.log.info("RestorePlayer: [%s] on [%s] listed %d version(s) for %s",
                        ctx.sender_label, ctx.chat_label, len(versions), canonical)
            ctx.reply(_format_versions_reply(canonical, uuid, versions, offset=0))
            return

        if typed_n.lower() == "more":
            entry = _get_pending_player_restore(pkey, expected_username=canonical)
            if entry is None:
                ctx.reply(f"Run /restore_player {canonical} first to see the list.")
                return
            new_offset = entry.get("page_offset", 0) + _RESTORE_PLAYER_PAGE_SIZE
            versions = entry["versions"]
            if new_offset >= len(versions):
                ctx.reply(f"No more versions for {canonical}.")
                return
            _set_pending_player_restore(pkey, page_offset=new_offset)
            server.log.info("RestorePlayer: [%s] on [%s] paged to offset %d for %s",
                        ctx.sender_label, ctx.chat_label, new_offset, canonical)
            ctx.reply(_format_versions_reply(canonical, uuid, versions, offset=new_offset))
            return

        try:
            n = int(typed_n)
        except ValueError:
            ctx.reply(f"Invalid selection: '{typed_n}'. Use a number or 'more'.")
            return

        if typed_confirm is not None:
            entry = _get_pending_player_restore(
                pkey, expected_username=canonical, expected_stage="selected")
            if entry is None or entry.get("selected_n") != n:
                ctx.reply(f"You must select a timestamp first with "
                          f"/restore_player {canonical} {n}")
                return
            versions = backend.list_player_versions(uuid, server.load_manifest()[:2])
            if not (1 <= n <= len(versions)):
                _clear_pending_player_restore(pkey)
                ctx.reply(f"Selection {n} is no longer valid (only "
                          f"{len(versions)} version(s) available). "
                          f"Run /restore_player {canonical} again.")
                return
            version = versions[n - 1]
            server.log.info("RestorePlayer: [%s] on [%s] confirmed restore of %s "
                        "to %s (source: %s)", ctx.sender_label, ctx.chat_label,
                        canonical, version["timestamp"], version["source"])
            ctx.reply(f"Starting restore of {canonical} to {version['timestamp']}...")
            _clear_pending_player_restore(pkey)
            say = lambda m: ctx.adapter.send(ctx.chat_id, m)

            def run():
                if not server.backup_lock.acquire(blocking=False):
                    say("A backup or restore is in progress.")
                    return
                try:
                    backend.restore_player(canonical, uuid, version, say)
                    # A Bedrock restore restarts the server (stop -> edit ->
                    # start), kicking everyone with no "disconnect" lines, so the
                    # in-memory online set would be left stale. Resync it against
                    # the freshly-restarted server (no-op on Java — no restart).
                    reconcile_online(server, reason="after restore")
                finally:
                    server.backup_lock.release()

            threading.Thread(target=run, daemon=True).start()
            return

        entry = _get_pending_player_restore(pkey, expected_username=canonical)
        if entry is None:
            ctx.reply(f"Run /restore_player {canonical} first to see the list.")
            return
        versions = entry["versions"]
        if not (1 <= n <= len(versions)):
            ctx.reply(f"Invalid selection: {n}. Choose 1-{len(versions)}.")
            return
        version = versions[n - 1]
        _set_pending_player_restore(
            pkey, stage="selected", username=canonical, uuid=uuid,
            versions=versions, selected_n=n)
        server.log.info("RestorePlayer: [%s] on [%s] selected version %d (%s) for %s",
                    ctx.sender_label, ctx.chat_label, n, version["source"], canonical)
        ctx.reply(_format_confirm_reply(canonical, uuid, n, version))
    router.register("restore_player", cmd_restore_player,
                    private_only=True, admin_only=True, needs_online=True,
                    cap=lambda ctx: ctx.server.backend.supports(CAP_PLAYER_RESTORE),
                    cap_message="Per-player restore is not available on this server edition.")

    # --- /restore (whole-world restore: stop -> replace -> restart) ---
    def cmd_restore(ctx):
        server = ctx.server
        pkey = (f"{ctx.bot.config.name}:{server.config.key}:"
                f"{ctx.platform}:{ctx.user_id}")
        sub = ctx.args[0] if ctx.args else None

        def discover():
            return restore_core.list_restore_points(
                restore_core.discover_chains(server.config.backup_dir))

        # Bare /restore or "/restore more": (re)show the paged list.
        if sub is None or sub.lower() == "more":
            points = discover()
            if sub is None:
                offset = 0
                _set_pending_world_restore(pkey, stage="listed", offset=0)
            else:
                entry = _get_pending_world_restore(pkey)
                if entry is None:
                    ctx.reply("Send /restore first to see the list.")
                    return
                offset = entry.get("offset", 0) + _RESTORE_PAGE_SIZE
                if offset >= len(points):
                    ctx.reply("No more restore points.")
                    return
                _set_pending_world_restore(pkey, offset=offset)
            server.log.info("Restore: [%s] on [%s] listed %d point(s)",
                            ctx.sender_label, ctx.chat_label, len(points))
            ctx.reply(_format_restore_points(points, offset=offset))
            return

        # "/restore <N> [confirm]"
        try:
            n = int(sub)
        except ValueError:
            ctx.reply("Usage: /restore  |  /restore <N>  |  /restore <N> confirm")
            return
        confirm = len(ctx.args) >= 2 and ctx.args[1].lower() == "confirm"
        points = discover()
        if not (1 <= n <= len(points)):
            ctx.reply(f"Invalid selection: {n}. Choose 1-{len(points)}.")
            return
        point = points[n - 1]
        chains = restore_core.discover_chains(server.config.backup_dir)

        if not confirm:
            _set_pending_world_restore(pkey, stage="selected", selected_n=n)
            server.log.info("Restore: [%s] on [%s] selected point %d ([%s] %s)",
                            ctx.sender_label, ctx.chat_label, n,
                            point["kind"], point["pretty_ts"])
            pre = ("A fresh full backup of the current world will be taken first."
                   if server.config.pre_restore_backup
                   else "No pre-restore backup will be taken (backup.pre_restore_"
                        "backup is off).")
            ctx.reply(
                "Confirm WORLD restore — this stops the server, replaces the "
                "world, and restarts it:\n"
                f"  Point:  [{point['kind']}] {point['pretty_ts']}\n"
                f"  Chain:  {point['chain_id'] or 'standalone'}\n"
                f"  {pre}\n"
                f"  Players will be warned {server.config.restore_warning_seconds}s "
                "in-game, then disconnected.\n\n"
                f"  To proceed, send:  /restore {n} confirm")
            return

        entry = _get_pending_world_restore(pkey, expected_stage="selected")
        if entry is None or entry.get("selected_n") != n:
            ctx.reply(f"Select first: /restore {n}")
            return
        _clear_pending_world_restore(pkey)
        server.log.info("Restore: [%s] on [%s] confirmed world restore to %s (%s)",
                        ctx.sender_label, ctx.chat_label,
                        point["pretty_ts"], point["kind"])
        ctx.reply(f"Starting world restore to {point['pretty_ts']}...")
        say = lambda m: ctx.adapter.send(ctx.chat_id, m)

        def run():
            if not server.backup_lock.acquire(blocking=False):
                say("A backup or restore is already in progress.")
                return
            try:
                server.restore_world(chains[point["chain_idx"]],
                                     point["point_idx"], say=say)
            finally:
                server.backup_lock.release()

        threading.Thread(target=run, daemon=True).start()
    router.register("restore", cmd_restore, private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.can_restart,
                    cap_message="World restore needs a restart transport — set "
                                "mux.session + mux.start_cmd for this server.")

    # --- /start (admin: bring the server up via its start command) ---
    def cmd_start(ctx):
        server = ctx.server
        _cmd_log(ctx, "Start")
        if server.backend.is_online():
            ctx.reply(f"{server.config.name} is already running.")
            return
        ctx.reply(f"Starting {server.config.name}...")
        say = lambda m: ctx.adapter.send(ctx.chat_id, m)

        def run():
            # relaunch = type the start command into the mux session, then wait
            # for the server to report ready. We deliberately do NOT auto-start
            # from other commands — the admin invokes /start explicitly.
            if server.backend.relaunch(say):
                reconcile_online(server, reason="after /start")
                say(f"{server.config.name} is up.")
            else:
                say(f"Could not start {server.config.name}. Check the mux "
                    f"session / start command:\n  {server.config.mux_start_cmd}")

        threading.Thread(target=run, daemon=True).start()
    router.register("start", cmd_start, private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.can_restart,
                    cap_message="/start needs a start transport — set "
                                "mux.session + mux.start_cmd for this server.")

    # --- /chat_id (public — lets an unauthorized chat learn its ID) ---
    def cmd_chat_id(ctx):
        _cmd_log(ctx, "ChatID", f" (chat_id={ctx.chat_id})")
        ctx.reply(f"Chat ID: {ctx.chat_id}")
    router.register("chat_id", cmd_chat_id, public=True, needs_server=False)

    # --- /use (multi-server: pick this admin's target server) ---
    def cmd_use(ctx):
        _cmd_log(ctx, "Use", f" (args={ctx.args})")
        ctx.bot.set_use(ctx)
    router.register("use", cmd_use, private_only=True, admin_only=True,
                    needs_server=False)

    # --- /authorize ---
    def cmd_authorize(ctx):
        multi = len(ctx.bot.servers) > 1
        usage = ("Usage: /authorize <chat_id> <server>" if multi
                 else "Usage: /authorize <chat_id>")
        if not ctx.args:
            ctx.reply(usage)
            return
        target_id = str(ctx.args[0]).strip()
        # Resolve the target server: single-server bots auto-bind to the sole
        # server; multi-server bots require an explicit <server> arg so the
        # channel's events aren't silently misrouted.
        if len(ctx.args) >= 2:
            server = ctx.bot.find_server(ctx.args[1])
            if server is None:
                names = ", ".join(sorted(ctx.bot.by_name)) or "(none)"
                ctx.reply(f"Unknown server '{ctx.args[1]}'.\nServers: {names}")
                return
        elif multi:
            names = ", ".join(sorted(ctx.bot.by_name))
            ctx.reply(f"{usage}\nServers: {names}")
            return
        else:
            server = ctx.bot.servers[0]
        with auth_lock:
            ns = auth_ns(auth, ctx.platform)
            if target_id not in ns["authorized_chat_ids"]:
                ns["authorized_chat_ids"].append(target_id)
            ns["chat_servers"][target_id] = server.config.key
            save_auth(ctx.bot.auth_doc, AUTH_PATH)
        ctx.bot.log.info("Authorize: chat %s -> %s by [%s] on [%s]",
                         ctx.bot.chat_display(ctx.platform, target_id),
                         server.config.name, ctx.sender_label, ctx.chat_label)
        ctx.reply(f"Chat {target_id} is now authorized for "
                  f"{server.config.name}.")
    router.register("authorize", cmd_authorize, private_only=True,
                    admin_only=True, needs_server=False)

    # --- /revoke ---
    def cmd_revoke(ctx):
        if not ctx.args:
            ctx.reply("Usage: /revoke <chat_id>")
            return
        target_id = str(ctx.args[0]).strip()
        with auth_lock:
            ns = auth_ns(auth, ctx.platform)
            display = ctx.bot.chat_display(ctx.platform, target_id)
            was_bound = ns["chat_servers"].pop(target_id, None) is not None
            was_authed = target_id in ns["authorized_chat_ids"]
            if was_authed:
                ns["authorized_chat_ids"].remove(target_id)
                ns.get("chat_names", {}).pop(target_id, None)
            if was_authed or was_bound:
                save_auth(ctx.bot.auth_doc, AUTH_PATH)
                ctx.bot.log.info("Revoke: chat %s by [%s] on [%s]",
                                 display, ctx.sender_label, ctx.chat_label)
                ctx.reply(f"Chat {target_id} has been revoked.")
            else:
                ctx.reply(f"Chat {target_id} was not authorized.")
    router.register("revoke", cmd_revoke, private_only=True,
                    admin_only=True, needs_server=False)

    # --- /listchats ---
    def cmd_listchats(ctx):
        _cmd_log(ctx, "ListChats")
        ns = auth.get(ctx.platform) or {}
        ids = ns.get("authorized_chat_ids", [])
        if not ids:
            ctx.reply("No authorized chats.")
            return
        binding = ns.get("chat_servers", {})
        names = ns.get("chat_names", {})
        multi = len(ctx.bot.servers) > 1
        lines = ["Authorized chats:"]
        for cid in ids:
            name = names.get(cid)
            who = f"{name} ({cid})" if name else cid
            if multi:
                srv = ctx.bot.by_key.get(binding.get(cid))
                label = srv.config.name if srv else (binding.get(cid) or "(unbound)")
                lines.append(f"  {who} -> {label}")
            else:
                lines.append(f"  {who}")
        ctx.reply("\n".join(lines))
    router.register("listchats", cmd_listchats, private_only=True,
                    admin_only=True, needs_server=False)


def _make_unclaimed_handler(auth: dict):
    """Build the admin-claim hook: the first private message on a platform with
    no admin yet claims that platform's admin."""
    def on_unclaimed(ctx) -> bool:
        ns = auth.get(ctx.platform) or {}
        if ns.get("admin_user_id") is not None or not ctx.is_private:
            return False
        with auth_lock:
            if auth_ns(auth, ctx.platform).get("admin_user_id") is not None:
                return False
            auth_ns(auth, ctx.platform)["admin_user_id"] = ctx.user_id
            save_auth(ctx.bot.auth_doc, AUTH_PATH)
        ctx.bot.log.info("Admin claimed on %s by [%s] (id=%s)",
                         ctx.platform, ctx.sender_label, ctx.user_id)
        ctx.reply("You are now the admin.")
        return True
    return on_unclaimed


# ---------------------------------------------------------------------------
# 10. Main
# ---------------------------------------------------------------------------
def _capture_initial_online(server, bot) -> None:
    """On startup, capture players already online (the bot may have restarted
    mid-session) and open their online-time sessions. Alerts the bot's admins if
    the server can't be reached at all."""
    backend = server.backend
    if not backend.is_available(log_warnings=True):
        return
    online = None
    try:
        online = backend.query_online_players()
    except Exception as e:
        # Server not reachable yet — it may be booting. Wait activity-aware
        # (extends while the log grows), then retry once it's online.
        server.log.info("Online query failed (%s); waiting for the server to "
                        "come up...", e)
        if backend.wait_until_online(log_fn=server.log.info):
            try:
                online = backend.query_online_players()
            except Exception as e2:
                server.log.warning("Online query retry failed: %s", e2)

    if online is None:
        # Never connected — server is likely not running. Alert the bot's admins.
        logger.error("[%s] Could not connect to the Minecraft server. "
                     "Server may not be running.", server.config.name)
        bot.alert_admins(
            f"⚠️ Could not connect to {server.config.name}.\n"
            "The server may not be running or not ready yet.\n"
            "Backups and /list will not work until the server is up.")
        return

    # Online-time bookkeeping: clear any sessions left open by a prior crash, then
    # open a session for each currently-online player (their join predates us).
    backend.reset_open_sessions()
    for name in online:
        server.player_join(name)
    current = server.get_online_players()
    if not current:
        logger.info("[%s] No players online", server.config.name)
        return
    logger.info("[%s] %d player(s) already online: %s", server.config.name,
                len(current), ", ".join(current))
    # The bot missed these players' connect lines (started mid-session), so
    # recover their xuids from the log to register them (Bedrock; no-op on Java).
    recover_online_identities(server, current)
    for name in current:
        pid = uuid_by_name(name, server.names)
        if pid:
            backend.record_player_session("join", pid)
    # The join notify callback never fired for these players, so start the cycle.
    server.start_incremental_cycle()


def _validate_chain(server) -> None:
    """Validate the incremental backup chain against the on-disk marker; clear a
    stale chain so incrementals are skipped until the next full backup."""
    chain_id, base_full, _ = server.load_manifest()
    if not chain_id:
        logger.info("[%s] No backup chain established. Run /backup to start one.",
                    server.config.name)
        return
    marker = server.read_chain_marker()
    if marker == chain_id:
        logger.info("[%s] Backup chain %s valid (base: %s)",
                    server.config.name, chain_id, base_full)
    else:
        logger.warning("[%s] Backup chain invalid: manifest chain %s does not "
                       "match marker %s. Incremental backups will be skipped "
                       "until a full backup is run.",
                       server.config.name, chain_id, marker or "(missing)")
        server.save_manifest({}, chain_id="", base_full="")


def _start_scheduled_backup(server, bot) -> None:
    """Start the per-server scheduled full-backup thread. Status is reported to
    the owning bot's admins."""
    hour = server.config.backup_hour
    schedule = server.config.backup_schedule

    def _next_backup_time(now: datetime) -> datetime:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if schedule == "weekly":
            days_ahead = (7 - now.weekday()) % 7  # Monday = 0
            target = target + timedelta(days=days_ahead)
            if target <= now:
                target = target + timedelta(weeks=1)
        elif schedule == "monthly":
            target = target.replace(day=1)
            if target <= now:
                if now.month == 12:
                    target = target.replace(year=now.year + 1, month=1)
                else:
                    target = target.replace(month=now.month + 1)
        else:  # daily (default)
            if target <= now:
                target = target + timedelta(days=1)
        return target

    def _loop():
        while True:
            now = datetime.now()
            target = _next_backup_time(now)
            wait = (target - now).total_seconds()
            logger.info("[%s] Next %s backup in %.0f seconds (at %s)",
                        server.config.name, schedule, wait,
                        target.strftime("%Y-%m-%d %H:%M"))
            time.sleep(wait)

            if not server.backend.is_available():
                logger.warning("[%s] Scheduled backup skipped: backend not available",
                               server.config.name)
                continue

            # Same exclusivity as /backup and the incremental cycle: never
            # overlap another backup's save-hold on this server.
            if not server.backup_lock.acquire(blocking=False):
                logger.warning("[%s] Scheduled backup skipped: another backup "
                               "is in progress", server.config.name)
                continue

            logger.info("[%s] Scheduled %s backup starting",
                        server.config.name, schedule)

            def status_cb(msg):
                bot.alert_admins(f"[Backup {server.config.name}] {msg}")

            try:
                path = server.run_backup(status_cb=status_cb)
                status_cb(f"Complete: {Path(path).name}")
            except Exception as e:
                logger.exception("[%s] Scheduled backup failed", server.config.name)
                status_cb(f"Failed: {e}")
            finally:
                server.backup_lock.release()

    threading.Thread(target=_loop, daemon=True,
                     name=f"backup-{server.config.key}").start()


def _bring_up_server(server, bot) -> bool:
    """Construct one server's backend and start watching + backing it up. Returns
    True on success; on backend failure alerts the bot's admins and returns False
    (the caller drops the server so the bot skips it)."""
    try:
        server.backend = make_backend(server.config)
    except BackendUnavailable as e:
        logger.error("[%s] Cannot start backend (edition '%s'): %s",
                     server.config.name, server.config.edition, e)
        bot.alert_admins(f"⚠️ Diamond Sign cannot start "
                         f"{server.config.name}: {e}")
        return False
    logger.info("[%s] Server edition: %s", server.config.name, server.config.edition)

    try:
        # Player registry, achievements, deaths (registry is backend-sourced).
        server.load_state()

        notify = make_notify_callback(bot, server)
        log_path = server.config.log_path
        # inotify needs the watch directory to exist. For Java the server
        # creates logs/ on start; create it here so a not-yet-started server
        # (or one whose dir was just replaced by a restore) doesn't fail the
        # watcher. LogWatcher already tolerates the log file itself being absent.
        log_path.parent.mkdir(parents=True, exist_ok=True)
        watcher = LogWatcher(server, notify,
                             on_server_start=lambda: reconcile_online(
                                 server, reason="server start"))
        server.watcher = watcher
        server.backend.attach_watcher(watcher)
        observer = Observer()
        observer.schedule(watcher, path=str(log_path.parent), recursive=False)
        observer.start()
        server.observer = observer
        logger.info("[%s] Watching %s for join/leave events",
                    server.config.name, log_path)

        _validate_chain(server)
        _start_scheduled_backup(server, bot)
        # Capturing who's already online can wait up to ~2 min for RCON when the
        # server is still booting (or down). Do it off the startup path so a
        # slow/down server doesn't delay this bot's chat adapters and leave it
        # unresponsive — join/leave events and reconcile fill the online set
        # meanwhile.
        threading.Thread(
            target=_capture_initial_online, args=(server, bot), daemon=True,
            name=f"online-{server.config.key}").start()
        return True
    except Exception as e:
        # One server failing to come up must not crash the whole process (other
        # bots/servers keep running); drop it and alert this bot's admins.
        logger.exception("[%s] Failed to bring up server", server.config.name)
        bot.alert_admins(f"⚠️ Diamond Sign could not bring up "
                         f"{server.config.name}: {e}")
        if server.observer is not None:
            try:
                server.observer.stop()
                server.observer.join()
            except Exception:
                pass
            server.observer = None
        return False


def _bring_up_bot(bot) -> None:
    """Build a bot's command router and start its adapter threads. Its adapters
    and servers must already be built (backends ready)."""
    auth = bot.auth
    logger.info("[%s] Chat platforms: %s", bot.config.name,
                ", ".join(a.name for a in bot.adapters))

    # Command router shared by this bot's adapters; replies go to the originating
    # chat. bot + resolve give handlers ctx.bot and the resolved ctx.server.
    router = CommandRouter(
        is_admin=lambda platform, uid: is_admin(platform, uid, auth),
        is_authorized=lambda platform, cid, uid, priv:
            is_authorized(platform, cid, uid, priv, auth),
        on_unclaimed=_make_unclaimed_handler(auth),
        logger=logger,
        bot=bot,
        resolve=bot.resolve_command,
    )
    bot.router = router
    register_commands(router, auth)

    for adapter in bot.adapters:
        threading.Thread(target=adapter.start, args=(router.dispatch,),
                         name=f"chat-{bot.config.name}-{adapter.name}",
                         daemon=True).start()
        logger.info("[%s] Started %s adapter", bot.config.name, adapter.name)


def _shutdown(bots) -> None:
    """Stop every bot's adapters and every server's watcher + flush its sessions."""
    for bot in bots:
        for adapter in bot.adapters:
            try:
                adapter.stop()
            except Exception:
                logger.exception("[%s] Failed to stop %s adapter",
                                 bot.config.name, adapter.name)
        for server in bot.servers:
            server.stop_incremental_cycle()
            # Flush open online-time sessions so playtime isn't lost on a clean
            # stop (a hard crash still loses the in-progress session).
            try:
                server.backend.close_open_sessions()
            except Exception:
                logger.exception("[%s] Failed to close open sessions on shutdown",
                                 server.config.name)
            if server.observer is not None:
                server.observer.stop()
                server.observer.join()
    logger.info("Diamond Sign stopped")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="diamondsign",
        description="Diamond Sign - Minecraft chat notifier + backups")
    parser.add_argument("--only", metavar="BOT",
                        help="run only the bot with this name (per-process "
                             "isolation); default runs every configured bot")
    return parser.parse_args(argv)


def main():
    args = _parse_args()
    setup_logging(Path(__file__).parent / "logs")

    bots_cfg = APP_CONFIG.bots
    if args.only:
        bots_cfg = [b for b in APP_CONFIG.bots if b.name == args.only]
        if not bots_cfg:
            names = ", ".join(b.name for b in APP_CONFIG.bots)
            print(f"--only: no bot named '{args.only}'. Configured bots: {names}",
                  file=sys.stderr)
            sys.exit(1)

    # One auth.json for the whole process; each bot operates on its own slice
    # (shared dict objects, so save_auth(bot.auth_doc) persists in-place
    # mutations). Pass the FULL bot list (not the --only-filtered one) so every
    # configured bot gets a normalized namespace regardless of --only.
    auth_doc = load_auth(AUTH_PATH, APP_CONFIG.bots)

    bots = []
    for bcfg in bots_cfg:
        servers = [Server(scfg) for scfg in bcfg.servers]
        bot = Bot(bcfg, servers)
        bot.auth_doc = auth_doc
        bot.auth = auth_doc[bcfg.name]
        # Adapters first so a server backend failure can still alert the admins.
        bot.adapters = make_adapters(bcfg)
        # Bring up each server; drop any whose backend won't start.
        for server in list(bot.servers):
            if not _bring_up_server(server, bot):
                bot.drop_server(server)
        if not bot.servers:
            logger.error("[%s] No servers came up; skipping this bot", bcfg.name)
            for adapter in bot.adapters:
                try:
                    adapter.stop()
                except Exception:
                    pass
            continue
        bots.append(bot)

    if not bots:
        logger.error("No bots could be started (no reachable servers). Exiting.")
        return

    # Now that every server is up, wire each bot's router + start its adapters.
    for bot in bots:
        _bring_up_bot(bot)

    logger.info("Diamond Sign running: %d bot(s), %d server(s)",
                len(bots), sum(len(b.servers) for b in bots))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown(bots)


if __name__ == "__main__":
    main()
