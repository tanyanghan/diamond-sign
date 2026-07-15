"""The chat command layer: register_commands + its support helpers.

Registers every command on the platform-agnostic CommandRouter (handlers act on
ctx.server / ctx.bot, so the same logic serves Telegram and Slack), plus the
command-only helpers: player stats formatting, log-history scanning, the
list->select->confirm pending state for /restore and /restore_player, and the
admin-claim handler. Auth writes go through ctx.bot.auth_doc.
"""

import gzip
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from backends import CAP_PLAYER_RESTORE, CAP_STATS, EVENT_DEATH, EVENT_ACHIEVEMENT
from utils import restore_core
from core.auth import AUTH_PATH, auth_lock, save_auth, auth_ns, is_admin
from core.state import (
    refresh_player_names, register_player, uuid_by_name,
    record_achievement, record_death,
)
from core.presence import reconcile_online
from core.logparse import (
    categorize_death, DEATH_CATEGORIES, DEATH_PHRASES, ACH_TYPE_MAP,
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

        # Discovery is pinned to this server's name (the world dir basename
        # backups are created with): a renamed/quarantined zip like
        # corrupt_<name>_incr_<chain>_<ts>.zip must not rejoin the chain.
        server_name = server.config.minecraft_dir.name

        def discover():
            return restore_core.list_restore_points(
                restore_core.discover_chains(server.config.backup_dir,
                                             server_name))

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
        chains = restore_core.discover_chains(server.config.backup_dir,
                                              server_name)

        if not confirm:
            # Verify every zip this point needs BEFORE offering the confirm
            # step — a corrupt backup must surface while choosing, not after
            # the world is wiped. (restore_world re-validates at confirm.)
            problems = restore_core.validate_chain_files(
                chains[point["chain_idx"]], point["point_idx"])
            if problems:
                server.log.warning("Restore: point %d unusable: %s",
                                   n, "; ".join(problems))
                ctx.reply(f"Point {n} cannot be restored — corrupt backup "
                          "file(s):\n  " + "\n  ".join(problems)
                          + "\nChoose another restore point.")
                return
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

        # Log every status line as well as sending it to chat, so the restore is
        # fully traced in the bot log (mirrors restore_player's status()).
        def say(m):
            server.log.info("Restore: %s", m)
            ctx.adapter.send(ctx.chat_id, m)

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

        def say(m):
            server.log.info("Start: %s", m)
            ctx.adapter.send(ctx.chat_id, m)

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


def make_unclaimed_handler(auth: dict):
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

