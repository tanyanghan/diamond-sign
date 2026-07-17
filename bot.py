import argparse
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.observers import Observer

from utils.config import load_config, ConfigError
from backends import make_backend, BackendUnavailable
from chat import make_adapters, CommandRouter
from core.logutil import TagLogAdapter
from core.state import uuid_by_name
from core.auth import (
    AUTH_PATH, auth_lock, load_auth, save_auth, is_admin, is_authorized,
)
from core.presence import reconcile_online, recover_online_identities
from core.notifications import make_notify_callback
from core.logwatch import LogWatcher
from core.server import Server
from core.commands import register_commands, make_unclaimed_handler

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
        only those bound to the server's key. Chats paused via
        /chats pause are skipped — they stay authorized (commands from
        them still work) but receive no announcements until resumed."""
        ns = self.auth.get(adapter.name) or {}
        paused = set(ns.get("paused_chats", []))
        chat_ids = [c for c in ns.get("authorized_chat_ids", [])
                    if c not in paused]
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
        # No answer. Establish WHY before waiting: a server that is simply
        # not running is answered decisively by the probe in seconds —
        # without it, the 2-minute is_online poll below types `list` into
        # the idle shell prompt every few seconds.
        if backend.probe_stopped(timeout=10) is True:
            logger.warning("[%s] Server is not running (its console is a "
                           "shell prompt).", server.config.name)
            bot.alert_admins(
                f"⚠️ {server.config.name} is not running.\n"
                "Presence, backups and /list are unavailable until it is "
                "started (/start).")
            return
        # Console is owned but silent — the server may be booting. Wait
        # activity-aware (extends while the log grows), then retry.
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


# Serialize scheduled full backups across every server in the process: when
# several servers share a backup hour they otherwise all fire at once and thrash
# the disk and uplink concurrently, which prolongs each backup's save-hold /
# auto-save-off consistency window. Schedulers that collide block on this lock
# and run back-to-back instead — the blocked threads are the queue, so none are
# skipped (a stuck backup would hold up the queue, same exposure as before but
# now shared). Only scheduled full backups take it; incrementals and manual
# /backup are unaffected (they gate on the per-server backup_lock as before).
_scheduled_backup_lock = threading.Lock()


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

            # Wait our turn: run one scheduled backup at a time process-wide.
            # Try without blocking first only so we can log when we're actually
            # queued behind another server's backup; then block until it's ours.
            if not _scheduled_backup_lock.acquire(blocking=False):
                logger.info("[%s] Scheduled %s backup queued behind another "
                            "server's backup", server.config.name, schedule)
                _scheduled_backup_lock.acquire()
            try:
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
            finally:
                _scheduled_backup_lock.release()

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
        on_unclaimed=make_unclaimed_handler(auth),
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
