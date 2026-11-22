"""The chat command layer: register_commands + its support helpers.

Registers every command on the platform-agnostic CommandRouter (handlers act on
ctx.server / ctx.bot, so the same logic serves Telegram and Slack), plus the
command-only helpers: player stats formatting, log-history scanning, the
list->select->confirm pending state for /restore and /restore_player, and the
admin-claim handler. Auth writes go through ctx.bot.auth_doc.
"""

import copy
import gzip
import html
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from backends import CAP_PLAYER_RESTORE, CAP_STATS, EVENT_DEATH, EVENT_ACHIEVEMENT
from backends.base import clean_chat_name, clean_chat_text, has_visible_text
from utils import restore_core
from core import updates
from core.auth import (
    AUTH_PATH, auth_lock, save_auth, auth_ns, is_admin, is_moderator,
)
from core.state import (
    refresh_player_names, register_player, uuid_by_name,
    record_achievement, record_death,
)
from core.presence import reconcile_online
from core.logparse import (
    death_cause, DEATH_PHRASES, ACH_TYPE_MAP,
    RE_UUID, RE_ACHIEVEMENT, RE_SERVER_MSG,
)

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


def _format_death_summary(deaths: dict, names: dict) -> str:
    """Render /death_summary: deaths grouped by what killed the player (most
    frequent cause first, "Other" last), each listed with player and time."""
    causes = {}
    for uuid, entries in deaths.items():
        player_name = names.get(uuid, uuid)
        for e in entries:
            causes.setdefault(death_cause(e["message"]), []).append(
                (e["timestamp"], player_name))
    total = sum(len(v) for v in causes.values())
    lines = [f"Death Summary ({total} total)", ""]
    for cause, entries in sorted(
            causes.items(),
            key=lambda x: (x[0] == "Other", -len(x[1]), x[0].lower())):
        lines.append(f"{cause}: {len(entries)}")
        for timestamp, player_name in sorted(entries):
            date_part, _, time_part = timestamp.partition(" ")
            try:
                date_part = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
            except ValueError:
                pass
            lines.append(f"  {player_name} ({date_part} {time_part})")
        lines.append("")
    return "\n".join(lines).rstrip()


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
        # A private temp file, never "<name>.log" beside the archive: that name
        # can be a real log, which writing then unlinking it would destroy.
        fd, tmp_name = tempfile.mkstemp(prefix="dsign_scan_", suffix=".log")
        extracted = Path(tmp_name)
        try:
            # fdopen first, so the descriptor is closed even if the archive
            # vanished since the glob (Windows can't unlink an open file).
            with os.fdopen(fd, "w", encoding="utf-8") as out_f:
                with gzip.open(gz_path, "rt", encoding="utf-8",
                               errors="replace") as gz_f:
                    out_f.write(gz_f.read())
            total += scan_fn(extracted, date_str, server)
        except Exception as e:
            server.log.warning("Scan: failed to process %s: %s", gz_path.name, e)
        finally:
            extracted.unlink(missing_ok=True)
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
# /update_server pending-state, same list->confirm discipline as the restores: an
# update stops the server and replaces its binary, and a Minecraft version bump
# migrates the world irreversibly, so it must never happen on one mistyped
# message.
_pending_update: dict = {}
_pending_update_lock = threading.Lock()
_PENDING_UPDATE_TTL = 300


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


def _version_id(version: dict) -> tuple:
    """What makes a /restore_player version the one the operator chose: the
    file it comes from and the entry within it. Not the row number (a backup
    landing after the listing shifts rows), and not the display label or
    timestamp either: ``source`` gains "(old chain)" once a new full backup
    starts a chain. A live Java player file is rewritten in place, though, so
    for those the timestamp (its mtime) is part of the identity: if the file
    changed since it was listed, the confirm is refused rather than restoring
    content the operator never saw."""
    in_place = version.get("kind") in ("live", "live_gz")
    return (str(version.get("path")), str(version.get("entry")),
            version.get("kind"), version["timestamp"] if in_place else None)


def _acquire_both(server, say) -> bool:
    """Take backup_lock then lifecycle_lock for an operation that stops or
    replaces the server, without waiting: holding backup_lock while queued
    behind a slow /start would make scheduled backups skip meanwhile. Always
    in this order; nothing takes them the other way round."""
    if not server.backup_lock.acquire(blocking=False):
        say("A backup, restore or update is already in progress.")
        return False
    if not server.lifecycle_lock.acquire(blocking=False):
        server.backup_lock.release()
        say("A start, restore, update or allowlist change is running on "
            f"{server.config.name} — send the same command again once it's "
            "finished.")
        return False
    return True


def _release_both(server) -> None:
    server.lifecycle_lock.release()
    server.backup_lock.release()


def _claim_pending(store: dict, lock, pkey: str, entry: dict, **expect) -> bool:
    """Take ``entry`` out of ``store`` -- once. Called after the operation's
    locks are held: only if it is still the pending entry for ``pkey`` and
    still holds ``expect`` (the setters update entries in place, so identity
    alone wouldn't show a re-selection). Two identical confirms racing each
    other then start the operation once, not twice."""
    with lock:
        if store.get(pkey) is not entry or any(
                entry.get(k) != v for k, v in expect.items()):
            return False
        store.pop(pkey)
        return True


# /msg: one message per user per _MSG_COOLDOWN seconds, so a group can't flood
# the game (or the shared Bedrock console) with them.
_MSG_COOLDOWN = 5.0
_msg_last: dict = {}
_msg_last_lock = threading.Lock()


# One log scan per server at a time: _scan_logs unpacks each archive to a
# fixed path beside it, which two concurrent scans would fight over.
_scan_locks: dict = {}
_scan_locks_guard = threading.Lock()


def _scan_lock(server) -> threading.Lock:
    with _scan_locks_guard:
        return _scan_locks.setdefault(server.config.key, threading.Lock())


def _run_scan(ctx, what: str, scan_fn) -> int | None:
    """Shared body of /scan_achievements and /scan_deaths: the number of new
    records, or None if another scan of this server was already running."""
    lock = _scan_lock(ctx.server)
    if not lock.acquire(blocking=False):
        ctx.reply(f"A log scan is already running on {ctx.server.config.name}.")
        return
    try:
        refresh_player_names(ctx.server)
        ctx.reply(f"Scanning log files for {what}s...")
        total = _scan_logs(scan_fn, ctx.server)
    finally:
        lock.release()
    ctx.reply(f"Scan complete. {total} new {what}(s) recorded.")
    return total


# ---------------------------------------------------------------------------
# Bot Commands
# ---------------------------------------------------------------------------
def _cmd_log(ctx, action: str, extra: str = "") -> None:
    """Uniform command audit line, prefixed with the handling bot:
    ``[<bot>] <action>: requested by [<sender>] on [<chat>]<extra>`` — where
    <chat> is the group/channel name (or 'direct' for a DM)."""
    # Labels come from the chat platform (display names, group titles), so
    # they're cleaned: a newline in one must not forge a line in the log.
    ctx.bot.log.info("%s: requested by [%s] on [%s]%s", action,
                     clean_chat_text(ctx.sender_label),
                     clean_chat_text(ctx.chat_label), extra)


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
            "/seed — show the world seed",
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
        lines.append(f"{ctx.adapter.command_label('msg')} <text> — send a message "
                     "to players in game")
        lines.append("/chat_id — show this chat's ID and your user ID")
        admin = ctx.is_private and is_admin(ctx.platform, ctx.user_id, auth)
        mod = ctx.is_private and not admin and is_moderator(
            ctx.platform, ctx.user_id, auth, ctx.server.config.key)
        if admin or mod:
            if len(ctx.bot.allowed_servers(ctx)) > 1:
                lines.append("/use [number|server] — list/pick the server your commands act on")
            if admin:
                lines += [
                    "/chats — list authorized chats (numbered)",
                    "/chats authorize <chat_id> — whitelist a chat",
                    "/chats revoke <chat_id> — remove a chat from whitelist",
                    "/chats pause|resume <N> — mute/unmute announcements to a chat",
                    "/mods — list moderators",
                    "/mods add <user_id> [server] — let a user run moderator commands",
                    "/mods remove <user_id> [server] — revoke a moderator",
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
        # A DM reports every server the sender may act on — all of them for the
        # admin (a whole-bot overview), a moderator's granted ones otherwise;
        # an authorized group/channel reports just the server bound to it.
        if ctx.is_private:
            servers = ctx.bot.allowed_servers(ctx)
            if not servers:
                ctx.reply("No servers are running on this bot."
                          if is_admin(ctx.platform, ctx.user_id, auth)
                          else "You're not a moderator of any server on this bot.")
                return
        else:
            server = ctx.bot.resolve_target_server(ctx)
            if server is None:
                ctx.reply("This chat isn't bound to a server. Ask an admin to "
                          "authorize it for one (/chats authorize).")
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

    # --- /seed (world seed: console command on Java, level.dat on Bedrock) ---
    def cmd_seed(ctx):
        _cmd_log(ctx, "Seed")
        server = ctx.server
        backend = server.backend
        # Deliberately NOT registered needs_online: only the console-based
        # implementation (Java/RCON) requires a running server. Bedrock reads
        # level.dat, so it answers whether the server is up or down.
        if backend.SEED_NEEDS_ONLINE and not backend.is_online():
            ctx.reply(f"⚠️ {server.config.name} is offline — /seed needs the "
                      "server running. Start it with /start.")
            return

        # Run in a thread: the Bedrock capture polls console.log for up to a
        # few seconds, and blocking the dispatcher would stall other commands.
        def run():
            try:
                resp = backend.seed_command()
            except Exception as e:
                server.log.exception("Seed command failed")
                ctx.adapter.send(ctx.chat_id, f"Seed command failed: {e}")
                return
            resp = resp.strip()
            server.log.info("Seed: replied to [%s] — %s",
                            ctx.sender_label, resp or "(no output)")
            ctx.adapter.send(ctx.chat_id,
                             resp or "(server returned no output)",
                             monospace=True)

        threading.Thread(target=run, daemon=True).start()
    router.register("seed", cmd_seed)

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
        total = _run_scan(ctx, "achievement", _scan_log_for_achievements)
        if total is not None:
            ctx.bot.log.info("ScanAchievements: %d new achievement(s) found", total)
    router.register("scan_achievements", cmd_scan_achievements,
                    private_only=True, admin_only=True, moderator=True,
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
        ctx.reply(_format_death_summary(deaths, names), monospace=True)
    router.register("death_summary", cmd_death_summary,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /scan_deaths ---
    def cmd_scan_deaths(ctx):
        _cmd_log(ctx, "ScanDeaths")
        total = _run_scan(ctx, "death", _scan_log_for_deaths)
        if total is not None:
            ctx.bot.log.info("ScanDeaths: %d new death(s) found", total)
    router.register("scan_deaths", cmd_scan_deaths,
                    private_only=True, admin_only=True, moderator=True,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /backup ---
    def cmd_backup(ctx):
        server = ctx.server
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

        if not server.backup_lock.acquire(blocking=False):
            ctx.reply("A backup is already in progress.")
            return
        # Until the worker is running, the lock is ours to release: anything
        # failing before that must not leave it held.
        try:
            server.log.info("Backup: manually triggered by [%s] on [%s]",
                            ctx.sender_label, ctx.chat_label)
            ctx.reply("Starting backup...")
            threading.Thread(target=run, daemon=True).start()
        except BaseException:
            server.backup_lock.release()
            raise
    router.register("backup", cmd_backup, private_only=True, admin_only=True,
                    moderator=True, needs_online=True)

    # --- /update_server (server version) ---
    # Named update_server, not update: Slack reserves /update.
    def cmd_update(ctx):
        server = ctx.server
        confirm = bool(ctx.args) and ctx.args[0].lower() == "confirm"
        _cmd_log(ctx, "Update", f" (args={ctx.args})")
        pkey = (f"{ctx.bot.config.name}:{server.config.key}:"
                f"{ctx.platform}:{ctx.user_id}")

        if not confirm:
            ctx.reply("Checking for a newer server version...")

            def look():
                try:
                    # A human asked directly, so show what is on offer
                    # even with no recorded baseline to compare to;
                    # installing it is what records one.
                    release = updates.available_update(
                        server, require_baseline=False)
                except Exception as e:
                    server.log.exception("Update check failed")
                    ctx.adapter.send(ctx.chat_id, f"Update check failed: {e}")
                    return
                if release is None:
                    installed = server.load_installed_version()
                    current = installed.get("mc_version") or "unknown"
                    ctx.adapter.send(
                        ctx.chat_id,
                        f"{server.config.name} is up to date (running "
                        f"{current}).")
                    return
                with _pending_update_lock:
                    _pending_update[pkey] = {"release": release,
                                             "ts": time.time()}
                ctx.adapter.send(
                    ctx.chat_id,
                    updates.describe_update(server, release)
                    + "\n\n" + updates.backup_plan(server)
                    + "\nThe server is then stopped, updated and "
                      "restarted, and a fresh full backup runs afterwards to "
                      "re-base the backup chain."
                    + "\nSend `/update_server confirm` to proceed.")

            threading.Thread(target=look, daemon=True).start()
            return

        with _pending_update_lock:
            entry = _pending_update.get(pkey)
            if entry is not None and \
                    time.time() - entry["ts"] > _PENDING_UPDATE_TTL:
                _pending_update.pop(pkey, None)
                entry = None
        if entry is None:
            ctx.reply("Nothing to confirm \u2014 run /update_server first to see what "
                      "is available.")
            return

        release = entry["release"]
        # Locks first: if something else is running, the confirmation stays
        # pending so the same command can simply be sent again.
        if not _acquire_both(server, ctx.reply):
            return
        say = lambda m: ctx.adapter.send(ctx.chat_id, m)

        def run():
            try:
                updates.update_server(server, release, say=say)
            finally:
                _release_both(server)

        if not _claim_pending(_pending_update, _pending_update_lock, pkey, entry):
            _release_both(server)
            ctx.reply("That confirmation was already used — nothing more was started.")
            return
        try:
            ctx.reply(f"Updating to {release.describe()}...")
            threading.Thread(target=run, daemon=True).start()
        except BaseException:
            _release_both(server)
            raise
    router.register("update_server", cmd_update, private_only=True, admin_only=True,
                    cap=lambda c: c.server.backend.can_restart,
                    cap_message="Updating needs a restart transport \u2014 set "
                                "mux.session + mux.start_cmd for this server.")

    # --- /allowlist (server whitelist/allowlist passthrough) ---
    _ALLOWLIST_SUBS = {"on", "off", "add", "remove", "list", "reload"}
    # Java names are [A-Za-z0-9_]{3,16}; Bedrock gamertags are letters, digits
    # and spaces (each word arrives as its own argument).
    _RE_ALLOWLIST_NAME = re.compile(r"(?=.{1,32}$)[A-Za-z0-9_]+(?: [A-Za-z0-9_]+)*")

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
        if sub not in ("add", "remove") and len(ctx.args) > 1:
            ctx.reply(f"/allowlist {sub} takes no player name.")
            return
        args = [sub]
        if sub in ("add", "remove"):
            # One name: the rest of the line, with any quotes the user typed
            # dropped. (The other user text typed into the pane is /msg, which
            # Bedrock escapes to [A-Za-z0-9] + \uXXXX.) Letters, digits, _ and
            # single spaces only: on Bedrock
            # the line is typed into the server's tmux/screen pane, which is a
            # shell prompt if the server has just exited, so nothing a shell
            # would interpret may get through. A name with a space (a Bedrock
            # gamertag) is sent double-quoted, as BDS requires -- still inert
            # to a shell, given those characters.
            name = " ".join(ctx.args[1:]).strip().strip('"\u201c\u201d').strip()
            if not _RE_ALLOWLIST_NAME.fullmatch(name):
                ctx.reply(f"Invalid player name '{name}' — use letters, "
                          "digits, _ and single spaces only.")
                return
            args.append(f'"{name}"' if " " in name else name)
        if not backend.is_available():
            ctx.reply("Server is not reachable right now — try again once it's up.")
            return
        server = ctx.server
        server.log.info("Allowlist: '%s %s' by [%s] on [%s]", verb,
                        " ".join(args), ctx.sender_label, ctx.chat_label)

        # Run in a thread: Bedrock capture polls the log for up to a few seconds.
        def run():
            # Held while the command is sent, so a restore/update/start can't
            # stop or replace the server between the check and the send (a
            # change made mid-swap would also be lost with the old directory).
            if not server.lifecycle_lock.acquire(blocking=False):
                ctx.adapter.send(ctx.chat_id, "A restore, update or start is "
                                 "running — try again once it's finished.")
                return
            try:
                if not backend.is_online():
                    ctx.adapter.send(ctx.chat_id, f"{server.config.name} went "
                                     "offline — nothing was sent.")
                    return
                resp = backend.allowlist_command(args)
            except Exception as e:
                server.log.exception("Allowlist command failed")
                ctx.adapter.send(ctx.chat_id, f"{verb} command failed: {e}")
                return
            finally:
                server.lifecycle_lock.release()
            ctx.adapter.send(ctx.chat_id,
                             resp.strip() or "(server returned no output)",
                             monospace=True)

        threading.Thread(target=run, daemon=True).start()
    router.register("allowlist", cmd_allowlist, private_only=True,
                    admin_only=True, moderator=True, needs_online=True)

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
            # Restore the version that was selected and shown in the confirm
            # prompt -- not whatever row N is now: a backup taken meanwhile
            # shifts the list. Re-list only to check it still exists.
            chosen = _version_id(entry["versions"][n - 1])
            versions = backend.list_player_versions(uuid, server.load_manifest()[:2])
            version = next((v for v in versions if _version_id(v) == chosen), None)
            if version is None:
                _clear_pending_player_restore(pkey)
                ctx.reply("The selected backup is no longer available. "
                          f"Run /restore_player {canonical} again.")
                return
            # Locks first: if something else is running, the selection stays
            # pending so the same confirm can simply be sent again.
            if not _acquire_both(server, ctx.reply):
                return
            say = lambda m: ctx.adapter.send(ctx.chat_id, m)

            def run():
                try:
                    backend.restore_player(canonical, uuid, version, say)
                    # A Bedrock restore restarts the server (stop -> edit ->
                    # start), kicking everyone with no "disconnect" lines, so the
                    # in-memory online set would be left stale. Resync it against
                    # the freshly-restarted server (no-op on Java — no restart).
                    reconcile_online(server, reason="after restore")
                finally:
                    _release_both(server)

            if not _claim_pending(_pending_player_restore, _pending_player_lock,
                                  pkey, entry, stage="selected", selected_n=n):
                _release_both(server)
                ctx.reply("That confirmation was already used — nothing more was started.")
                return
            try:
                server.log.info("RestorePlayer: [%s] on [%s] confirmed restore "
                                "of %s to %s (source: %s)", ctx.sender_label,
                                ctx.chat_label, canonical, version["timestamp"],
                                version["source"])
                ctx.reply(f"Starting restore of {canonical} to "
                          f"{version['timestamp']}...")
                threading.Thread(target=run, daemon=True).start()
            except BaseException:
                _release_both(server)
                raise
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
                    private_only=True, admin_only=True, moderator=True,
                    needs_online=True,
                    cap=lambda ctx: ctx.server.backend.supports(CAP_PLAYER_RESTORE),
                    cap_message="Per-player restore is not available on this server edition.")

    # --- /restore (whole-world restore: stop -> replace -> restart) ---
    def cmd_restore(ctx):
        server = ctx.server
        pkey = (f"{ctx.bot.config.name}:{server.config.key}:"
                f"{ctx.platform}:{ctx.user_id}")
        sub = ctx.args[0] if ctx.args else None

        # Discovery is pinned to this server's name (the world dir basename
        # backups are created with): a renamed/quarantined zip like
        # corrupt_<name>_incr_<chain>_<ts>.zip must not rejoin the chain.
        server_name = server.config.minecraft_dir.name

        def discover():
            return restore_core.list_restore_points(
                restore_core.discover_chains(server.config.backup_dir,
                                             server_name))

        # Bare /restore or "/restore more": (re)show the paged list.
        #
        # The list is SNAPSHOT here and every later step reads that snapshot.
        # Re-deriving it per command used to renumber the rows underneath the
        # operator: an incremental lands every few minutes while players are
        # online, and one that arrived 3 seconds after a listing moved the
        # chosen row three places by the time the selection came in. Paging
        # reads the snapshot too, so page 2 continues page 1 rather than
        # describing a directory that has moved on.
        if sub is None or sub.lower() == "more":
            if sub is None:
                points = discover()
                offset = 0
                _set_pending_world_restore(pkey, stage="listed", offset=0,
                                           points=points)
            else:
                entry = _get_pending_world_restore(pkey)
                if entry is None or entry.get("points") is None:
                    ctx.reply("Send /restore first to see the list.")
                    return
                points = entry["points"]
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

        # n is an index into the list the operator was SHOWN, which is the
        # only list they could have counted rows in.
        listing = _get_pending_world_restore(pkey)
        points = (listing or {}).get("points")
        if not points:
            ctx.reply("Send /restore first to see the list, then /restore "
                      f"{n}. (The numbering belongs to a specific listing — "
                      "backups are added and pruned, so a number from an "
                      "older list can mean a different point.)")
            return
        if not (1 <= n <= len(points)):
            ctx.reply(f"Invalid selection: {n}. Choose 1-{len(points)}.")
            return
        point = points[n - 1]

        # Match the remembered row to a backup that is still on disk, by
        # identity rather than by position.
        chains = restore_core.discover_chains(server.config.backup_dir,
                                              server_name)
        found = restore_core.locate_point(chains, point)
        if found is None:
            ctx.reply(f"Point {n} ([{point['kind']}] {point['pretty_ts']}) is "
                      f"no longer in the backup directory — {point.get('file')} "
                      "has been pruned, renamed or quarantined since that list "
                      "was printed.\nSend /restore for a fresh list.")
            return
        chain, point_idx = found

        if not confirm:
            # Check every zip this point needs BEFORE offering the confirm
            # step — a corrupt backup must surface while choosing, not after
            # the world is wiped.
            #
            # Structural check only (open each zip, read its central
            # directory): it catches truncation, which is the failure mode
            # actually seen in the wild, in milliseconds. The full CRC pass
            # decompresses everything and took ~50s on an 84-zip chain, which
            # is a long time to leave /restore <N> unanswered. Nothing is lost
            # by deferring it: the restore extracts before touching the world
            # and zipfile raises BadZipFile: Bad CRC-32 on any bad entry.
            problems = restore_core.validate_chain_structure(chain, point_idx)
            if problems:
                server.log.warning("Restore: point %d unusable: %s",
                                   n, "; ".join(problems))
                ctx.reply(f"Point {n} cannot be restored — corrupt backup "
                          "file(s):\n  " + "\n  ".join(problems)
                          + "\nChoose another restore point.")
                return
            _set_pending_world_restore(pkey, stage="selected",
                                       selected_n=n,
                                       selected_point=point)
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
        # Restore what the confirm prompt named. Re-resolving n here read the
        # directory a third time, so a backup arriving between the prompt and
        # the confirmation could restore a point the operator was never shown.
        point = entry.get("selected_point", point)
        found = restore_core.locate_point(chains, point)
        if found is None:
            _clear_pending_world_restore(pkey)
            ctx.reply(f"[{point['kind']}] {point['pretty_ts']} is no longer in "
                      f"the backup directory ({point.get('file')}). Nothing "
                      "was touched — send /restore for a fresh list.")
            return
        chain, point_idx = found
        # Locks first: if something else is running, the selection stays
        # pending so the same confirm can simply be sent again.
        if not _acquire_both(server, ctx.reply):
            return

        # Log every status line as well as sending it to chat, so the restore is
        # fully traced in the bot log (mirrors restore_player's status()).
        def say(m):
            server.log.info("Restore: %s", m)
            ctx.adapter.send(ctx.chat_id, m)

        def run():
            try:
                server.restore_world(chain, point_idx, say=say)
            finally:
                _release_both(server)

        if not _claim_pending(_pending_world_restore, _pending_world_lock, pkey,
                              entry, stage="selected", selected_n=n):
            _release_both(server)
            ctx.reply("That confirmation was already used — nothing more was started.")
            return
        try:
            server.log.info("Restore: [%s] on [%s] confirmed world restore to "
                            "%s (%s)", ctx.sender_label, ctx.chat_label,
                            point["pretty_ts"], point["kind"])
            ctx.reply(f"Starting world restore to {point['pretty_ts']}...")
            threading.Thread(target=run, daemon=True).start()
        except BaseException:
            _release_both(server)
            raise
    router.register("restore", cmd_restore, private_only=True, admin_only=True,
                    moderator=True,
                    cap=lambda ctx: ctx.server.backend.can_restart,
                    cap_message="World restore needs a restart transport — set "
                                "mux.session + mux.start_cmd for this server.")

    # --- /start (admin/moderator: bring the server up via its start command) ---
    def cmd_start(ctx):
        server = ctx.server
        _cmd_log(ctx, "Start")

        def say(m):
            server.log.info("Start: %s", m)
            ctx.adapter.send(ctx.chat_id, m)

        def run():
            # relaunch = type the start command into the mux session, then wait
            # for the server to report ready. We deliberately do NOT auto-start
            # from other commands — an operator invokes /start explicitly.
            try:
                ok = server.backend.relaunch(say)
            finally:
                server.lifecycle_lock.release()
            if ok:
                reconcile_online(server, reason="after /start")
                say(f"{server.config.name} is up.")
            else:
                say(f"Could not start {server.config.name}. Check the mux "
                    f"session / start command:\n  {server.config.mux_start_cmd}")

        # lifecycle_lock is held through the relaunch: a restore or update
        # holds it while the server is down, and starting it then would bring
        # it up on a half-replaced world or binary. It also stops two /starts
        # racing. (Not backup_lock: a slow start mustn't make backups skip.)
        if not server.lifecycle_lock.acquire(blocking=False):
            ctx.reply("A restore, update or start is already running — "
                      f"{server.config.name} will be started by it if needed.")
            return
        handed_off = False
        try:
            if server.backend.is_online():
                ctx.reply(f"{server.config.name} is already running.")
                return
            ctx.reply(f"Starting {server.config.name}...")
            threading.Thread(target=run, daemon=True).start()
            handed_off = True
        finally:
            if not handed_off:
                server.lifecycle_lock.release()

    router.register("start", cmd_start, private_only=True, admin_only=True,
                    moderator=True,
                    cap=lambda ctx: ctx.server.backend.can_restart,
                    cap_message="/start needs a start transport — set "
                                "mux.session + mux.start_cmd for this server.")

    # --- /chat_id (public — lets an unauthorized chat learn its ID, and a
    # would-be moderator learn their user ID for /mods add) ---
    def cmd_chat_id(ctx):
        _cmd_log(ctx, "ChatID", f" (chat_id={ctx.chat_id})")
        ctx.reply(f"Chat ID: {ctx.chat_id}\nYour user ID: {ctx.user_id}")
    router.register("chat_id", cmd_chat_id, public=True, needs_server=False)

    # --- /msg (Slack: /say) — a chat user's line in the game chat ---
    # Shown to every player as "<ethereal voice> <handle>: <text>". Open to
    # anyone who can run commands here: an authorised group (its bound server)
    # or the admin/moderators in a DM (their servers).
    def cmd_msg(ctx):
        server = ctx.server
        label = ctx.adapter.command_label("msg")
        parts = (ctx.text or "").split(maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""
        if ctx.platform == "slack":
            text = html.unescape(text)  # Slack sends & < > as entities
        text = clean_chat_text(text)
        if not has_visible_text(text):
            ctx.reply(f"Usage: {label} <message> — shows it to everyone in "
                      f"game on {server.config.name}.")
            return
        key = (ctx.platform, ctx.user_id)
        now = time.monotonic()
        with _msg_last_lock:
            last = _msg_last.get(key)
            if last is not None and now - last < _MSG_COOLDOWN:
                ctx.reply("Please wait a few seconds between messages.")
                return
            # In flight until the worker finishes (inf never ages out), so a
            # slow send can't let the same user's next message through early.
            _msg_last[key] = float("inf")
        name = clean_chat_name(str(ctx.sender_label).lstrip("@"))[:32].strip()
        if not has_visible_text(name):
            name = str(ctx.user_id)
        _cmd_log(ctx, "Msg")

        def finish(stamp):
            # stamp=None: refused or failed -- give the slot back, "try again"
            # must mean now. Otherwise the cooldown runs from ``stamp``.
            with _msg_last_lock:
                if stamp is None:
                    _msg_last.pop(key, None)
                else:
                    _msg_last[key] = stamp

        def run():
            backend = server.backend
            try:
                # Probed WITHOUT the lifecycle lock (a Bedrock probe takes ~3 s);
                # tell_all takes the lock only around the send. An offline
                # reply keeps the cooldown: it still typed a probe into the pane.
                if not backend.is_online():
                    finish(now)
                    ctx.adapter.send(ctx.chat_id, f"\u26a0\ufe0f {server.config.name} "
                                     "is offline — the message wasn't sent.")
                    return
                result = backend.tell_all(name, text, server.lifecycle_lock)
            except Exception as e:
                finish(None)
                server.log.exception("Msg failed")
                ctx.adapter.send(ctx.chat_id, f"Couldn't send the message: {e}")
                return
            if result is None:
                finish(None)
                ctx.adapter.send(ctx.chat_id, "A restore, update or start is "
                                 "running — try again once it's finished.")
                return
            finish(time.monotonic())  # spacing counts from the actual send
            sent, trimmed = result
            server.log.info("Msg: [%s] (%s) on [%s] -> %s: %s",
                            clean_chat_text(ctx.sender_label), ctx.user_id,
                            clean_chat_text(ctx.chat_label), server.config.name,
                            sent)
            # Typed into the console; nothing confirms players saw it.
            ctx.adapter.send(ctx.chat_id,
                             f"Sent (shortened to {len(sent)} characters)."
                             if trimmed else "Sent.")

        threading.Thread(target=run, daemon=True).start()
    router.register("msg", cmd_msg)

    # --- /use (multi-server: pick this user's target server) ---
    def cmd_use(ctx):
        _cmd_log(ctx, "Use", f" (args={ctx.args})")
        ctx.bot.set_use(ctx)
    router.register("use", cmd_use, private_only=True, admin_only=True,
                    moderator=True, needs_server=False)

    # --- /chats (list / authorize / revoke / pause / resume) ---
    # One umbrella for chat administration. /chat_id stays separate on
    # purpose: it is PUBLIC (an unauthorized group runs it to learn its ID so
    # the admin can then `/chats authorize` it), while everything here is
    # admin-DM-only.

    def _chats_authorize(ctx, args):
        multi = len(ctx.bot.servers) > 1
        usage = ("Usage: /chats authorize <chat_id> <server>" if multi
                 else "Usage: /chats authorize <chat_id>")
        if not args:
            ctx.reply(usage)
            return
        target_id = str(args[0]).strip()
        # Resolve the target server: single-server bots auto-bind to the sole
        # server; multi-server bots require an explicit <server> arg so the
        # channel's events aren't silently misrouted.
        if len(args) >= 2:
            server = ctx.bot.find_server(args[1])
            if server is None:
                names = ", ".join(sorted(ctx.bot.by_name)) or "(none)"
                ctx.reply(f"Unknown server '{args[1]}'.\nServers: {names}")
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
        ctx.bot.log.info("Chats: authorize %s -> %s by [%s] on [%s]",
                         ctx.bot.chat_display(ctx.platform, target_id),
                         server.config.name, ctx.sender_label, ctx.chat_label)
        ctx.reply(f"Chat {target_id} is now authorized for "
                  f"{server.config.name}.")

    def _chats_revoke(ctx, args):
        if not args:
            ctx.reply("Usage: /chats revoke <chat_id>")
            return
        target_id = str(args[0]).strip()
        with auth_lock:
            ns = auth_ns(auth, ctx.platform)
            display = ctx.bot.chat_display(ctx.platform, target_id)
            was_bound = ns["chat_servers"].pop(target_id, None) is not None
            was_authed = target_id in ns["authorized_chat_ids"]
            if was_authed:
                ns["authorized_chat_ids"].remove(target_id)
                ns.get("chat_names", {}).pop(target_id, None)
                # Drop any pause state too — a re-authorized chat starts fresh.
                if target_id in ns.get("paused_chats", []):
                    ns["paused_chats"].remove(target_id)
            if was_authed or was_bound:
                save_auth(ctx.bot.auth_doc, AUTH_PATH)
                ctx.bot.log.info("Chats: revoke %s by [%s] on [%s]",
                                 display, ctx.sender_label, ctx.chat_label)
                ctx.reply(f"Chat {target_id} has been revoked.")
            else:
                ctx.reply(f"Chat {target_id} was not authorized.")

    def _chats_pause_resume(ctx, args, sub):
        # Numbers refer to the /chats listing — the (stable)
        # authorized_chat_ids list for THIS platform.
        ids = (auth.get(ctx.platform) or {}).get("authorized_chat_ids", [])
        if not ids:
            ctx.reply("No authorized chats.")
            return
        if not args or not args[0].isdigit():
            ctx.reply(f"Usage: /chats {sub} <chat number> "
                      "(numbers as shown by /chats)")
            return
        n = int(args[0])
        if not (1 <= n <= len(ids)):
            ctx.reply(f"Invalid chat number: {n}. Choose 1-{len(ids)}.")
            return
        cid = ids[n - 1]
        display = ctx.bot.chat_display(ctx.platform, cid)
        with auth_lock:
            paused = auth_ns(auth, ctx.platform)["paused_chats"]
            if sub == "pause":
                if cid in paused:
                    ctx.reply(f"{display} is already paused.")
                    return
                paused.append(cid)
            else:
                if cid not in paused:
                    ctx.reply(f"{display} is not paused.")
                    return
                paused.remove(cid)
            save_auth(ctx.bot.auth_doc, AUTH_PATH)
        ctx.bot.log.info("Chats: %s %s by [%s] on [%s]",
                         sub, display, ctx.sender_label, ctx.chat_label)
        ctx.reply(f"{display} announcements "
                  + ("paused. Commands from it still work; resume with "
                     f"/chats resume {n}." if sub == "pause" else "resumed."))

    def _chats_list(ctx):
        ns = auth.get(ctx.platform) or {}
        ids = ns.get("authorized_chat_ids", [])
        if not ids:
            ctx.reply("No authorized chats. Add one with "
                      "/chats authorize <chat_id>.")
            return
        names = ns.get("chat_names", {})
        binding = ns.get("chat_servers", {})
        paused = ns.get("paused_chats", [])
        multi = len(ctx.bot.servers) > 1
        lines = ["Authorized chats (mute one with /chats pause <N>):"]
        for i, cid in enumerate(ids, 1):
            name = names.get(cid)
            who = f"{name} ({cid})" if name else cid
            mark = "  [PAUSED]" if cid in paused else ""
            if multi:
                srv = ctx.bot.by_key.get(binding.get(cid))
                label = srv.config.name if srv else (binding.get(cid) or "(unbound)")
                lines.append(f"  {i}. {who} -> {label}{mark}")
            else:
                lines.append(f"  {i}. {who}{mark}")
        ctx.reply("\n".join(lines))

    _CHATS_USAGE = ("Usage: /chats  |  /chats authorize <chat_id>"
                    "  |  /chats revoke <chat_id>"
                    "  |  /chats pause <N>  |  /chats resume <N>")

    def cmd_chats(ctx):
        _cmd_log(ctx, "Chats", f" (args={ctx.args})" if ctx.args else "")
        sub = ctx.args[0].lower() if ctx.args else None
        rest = ctx.args[1:]
        if sub is None:
            _chats_list(ctx)
        elif sub == "authorize":
            _chats_authorize(ctx, rest)
        elif sub == "revoke":
            _chats_revoke(ctx, rest)
        elif sub in ("pause", "resume"):
            _chats_pause_resume(ctx, rest, sub)
        else:
            ctx.reply(_CHATS_USAGE)
    router.register("chats", cmd_chats, private_only=True,
                    admin_only=True, needs_server=False)

    # --- /mods (list / add / remove moderators) ---
    # A moderator may run the moderator commands (/backup, /allowlist,
    # /restore_player, /restore, /start, /scan_*, /use) in a DM with the bot,
    # on the servers granted here. Grants are per platform: /mods edits the
    # platform it is sent from.

    def _mods_server_arg(ctx, args, usage):
        """The <server> for /mods add|remove: the sole server of a one-server
        bot, else the named one. Replies and returns None if missing/unknown."""
        if len(args) >= 2:
            server = ctx.bot.find_server(args[1])
            if server is None:
                names = ", ".join(sorted(ctx.bot.by_name)) or "(none)"
                ctx.reply(f"Unknown server '{args[1]}'.\nServers: {names}")
            return server
        if len(ctx.bot.servers) == 1:
            return ctx.bot.servers[0]
        ctx.reply(f"{usage}\nServers: {', '.join(sorted(ctx.bot.by_name))}")
        return None

    def _copy_mods(ns):
        return ({u: list(k) for u, k in ns["moderators"].items()},
                dict(ns["moderator_names"]))

    def _save_mods(ctx, ns, mods, names):
        """Write a /mods change (under auth_lock) to a staged copy of the auth
        doc first, and publish it to the live namespace only once auth.json
        holds it: a grant that failed to save never authorises a command, and
        a revoke that failed never lives only in memory (to return at the next
        restart). Replies and returns False on failure, with nothing changed."""
        staged = copy.deepcopy(ctx.bot.auth_doc)
        sns = staged[ctx.bot.config.name][ctx.platform]
        sns["moderators"], sns["moderator_names"] = mods, names
        try:
            save_auth(staged, AUTH_PATH)
        except Exception as e:
            ctx.bot.log.exception("Mods: could not save auth.json")
            ctx.reply(f"Could not save auth.json ({e}) — nothing was changed.")
            return False
        ns["moderators"], ns["moderator_names"] = mods, names
        return True

    def _mods_add(ctx, args):
        multi = len(ctx.bot.servers) > 1
        usage = ("Usage: /mods add <user_id> <server>" if multi
                 else "Usage: /mods add <user_id>")
        uid = args[0].strip() if args else ""
        if not uid:
            ctx.reply(usage + "\n(They can get their user ID by sending /chat_id.)")
            return
        if is_admin(ctx.platform, uid, auth):
            ctx.reply("That's the admin — they can already run every command.")
            return
        server = _mods_server_arg(ctx, args, usage)
        if server is None:
            return
        key = server.config.key
        with auth_lock:
            ns = auth_ns(auth, ctx.platform)
            already = key in ns["moderators"].get(uid, [])
            if not already:
                mods, names = _copy_mods(ns)
                mods.setdefault(uid, []).append(key)
                if not _save_mods(ctx, ns, mods, names):
                    return
        if already:
            ctx.reply(f"{uid} is already a moderator of {server.config.name}.")
            return
        ctx.bot.log.info("Mods: add %s -> %s by [%s] on [%s]", uid,
                         server.config.name, ctx.sender_label, ctx.chat_label)
        ctx.reply(f"{uid} is now a moderator of {server.config.name}. They run "
                  "moderator commands in a DM with the bot.")

    def _mods_remove(ctx, args):
        usage = "Usage: /mods remove <user_id> [<server>]"
        uid = args[0].strip() if args else ""
        if not uid:
            ctx.reply(usage)
            return
        server = None
        if len(args) >= 2:
            server = _mods_server_arg(ctx, args, usage)
            if server is None:
                return
        with auth_lock:
            ns = auth_ns(auth, ctx.platform)
            granted = ns["moderators"].get(uid)
            if not granted or (server is not None
                               and server.config.key not in granted):
                where = f" of {server.config.name}" if server else ""
                ctx.reply(f"{uid} is not a moderator{where}.")
                return
            mods, names = _copy_mods(ns)
            if server is not None:
                mods[uid].remove(server.config.key)
            if server is None or not mods[uid]:
                mods.pop(uid, None)
                names.pop(uid, None)
            if not _save_mods(ctx, ns, mods, names):
                return
        # Drop any /use selection so nothing stays pointed at a server they
        # lost (resolution would refuse it anyway).
        ctx.bot.clear_use(ctx.platform, uid)
        what = server.config.name if server else "all servers"
        ctx.bot.log.info("Mods: remove %s from %s by [%s] on [%s]", uid, what,
                         ctx.sender_label, ctx.chat_label)
        ctx.reply(f"{uid} is no longer a moderator of {what}.")

    def _mods_list(ctx):
        ns = auth.get(ctx.platform) or {}
        mods = ns.get("moderators") or {}
        if not mods:
            ctx.reply("No moderators. Add one with /mods add <user_id>"
                      + (" <server>" if len(ctx.bot.servers) > 1 else "") + ".")
            return
        names = ns.get("moderator_names") or {}
        lines = ["Moderators:"]
        for uid, keys in mods.items():
            who = f"{names[uid]} ({uid})" if uid in names else uid
            servers = ", ".join(
                (ctx.bot.by_key[k].config.name if k in ctx.bot.by_key else k)
                for k in keys)
            lines.append(f"  {who} -> {servers}")
        ctx.reply("\n".join(lines))

    _MODS_USAGE = ("Usage: /mods  |  /mods add <user_id> [<server>]"
                   "  |  /mods remove <user_id> [<server>]")

    def cmd_mods(ctx):
        _cmd_log(ctx, "Mods", f" (args={ctx.args})" if ctx.args else "")
        sub = ctx.args[0].lower() if ctx.args else None
        rest = ctx.args[1:]
        if sub is None:
            _mods_list(ctx)
        elif sub == "add":
            _mods_add(ctx, rest)
        elif sub == "remove":
            _mods_remove(ctx, rest)
        else:
            ctx.reply(_MODS_USAGE)
    router.register("mods", cmd_mods, private_only=True, admin_only=True,
                    needs_server=False)


def make_unclaimed_handler(auth: dict):
    """Build the admin-claim hook: the first private message on a platform with
    no admin yet claims that platform's admin."""
    def on_unclaimed(ctx) -> bool:
        ns = auth.get(ctx.platform) or {}
        # A namespace with moderators but no admin is not a fresh install (its
        # admin_user_id was lost) — never hand admin to whoever DMs next.
        if ns.get("admin_user_id") is not None or not ctx.is_private \
                or ns.get("moderators"):
            return False
        with auth_lock:
            fresh = auth_ns(auth, ctx.platform)
            if fresh.get("admin_user_id") is not None or fresh.get("moderators"):
                return False
            auth_ns(auth, ctx.platform)["admin_user_id"] = ctx.user_id
            save_auth(ctx.bot.auth_doc, AUTH_PATH)
        ctx.bot.log.info("Admin claimed on %s by [%s] (id=%s)",
                         ctx.platform, ctx.sender_label, ctx.user_id)
        ctx.reply("You are now the admin.")
        return True
    return on_unclaimed

