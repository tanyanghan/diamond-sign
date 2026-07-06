"""Online-presence reconciliation for a running server.

The in-memory online set is normally maintained from parsed join/leave lines,
but it drifts when players leave without a clean disconnect (e.g. a restore
stop/restart). ``reconcile_online`` re-queries the server and repairs the set;
``recover_online_identities`` back-fills Bedrock xuids for players who were
already online when the bot started. Both operate on a ``server`` object (no
``Server`` import → no cycle).
"""

from core.state import uuid_by_name, register_player
from core.logparse import RE_BEDROCK_CONNECT, RE_BEDROCK_DISCONNECT
from utils.config import EDITION_BEDROCK


def recover_online_identities(server, online_names: list) -> None:
    """Recover xuids for already-online Bedrock players whose connect line the
    bot missed because it started mid-session.

    The Bedrock registry is keyed by xuid, but ``list`` only returns usernames —
    so a player already online at startup has no registry entry (absent from
    ``/list``, stats, identity-learning) until they leave. Bedrock's console.log
    is appended (never rotated), so their ``Player connected: <name>, xuid: <id>``
    line is still on disk: scan it and register the ones we can resolve. No-op on
    Java (its name registry is recovered from world data, not this log)."""
    if server.config.edition != EDITION_BEDROCK:
        return
    missing = [n for n in online_names if uuid_by_name(n, server.names) is None]
    if not missing:
        return
    found: dict = {}
    try:
        with open(server.config.log_path, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                m = (RE_BEDROCK_CONNECT.search(line)
                     or RE_BEDROCK_DISCONNECT.search(line))
                if m and m.group(1).strip() in missing:
                    found[m.group(1).strip()] = m.group(2).strip()  # latest wins
    except FileNotFoundError:
        return
    except Exception:
        server.log.exception("Failed to scan log for online-player identities")
        return
    for name in missing:
        if name in found:
            register_player(server, found[name], name)
    recovered = [n for n in missing if n in found]
    if recovered:
        server.log.info("Recovered identity from log for %d already-online "
                    "player(s): %s", len(recovered), ", ".join(recovered))
    unresolved = [n for n in missing if n not in found]
    if unresolved:
        server.log.info("No identity in log yet for: %s (will resolve on leave)",
                    ", ".join(unresolved))


def reconcile_online(server, *, reason: str = "") -> list | None:
    """Reconcile ``server``'s in-memory online set with what it actually reports.

    The set is normally kept current from parsed join/leave lines, but it goes
    stale when players leave without a clean disconnect line — e.g. a restore
    stop/restart kicks everyone yet BDS emits no "Player disconnected" lines, so
    the bot would keep believing they're online (and never stop the incremental
    cycle). This queries the server, then adds players it missed and drops ones
    that are gone, recording the matching session boundaries and starting or
    stopping the incremental cycle to match the new count.

    Returns the reconciled online list, or None if the server couldn't be
    queried (in which case the in-memory set is left untouched — best-effort).
    """
    with server.reconcile_lock:
        try:
            actual = server.backend.query_online_players()
        except Exception as e:
            server.log.warning("Reconcile online failed%s: %s",
                           f" ({reason})" if reason else "", e)
            return None
        actual_set = set(actual)
        before = set(server.get_online_players())
        joined = actual_set - before
        left = before - actual_set
        if joined or left:
            server.log.info("Reconcile online%s: +%s -%s",
                        f" ({reason})" if reason else "",
                        sorted(joined) or "none", sorted(left) or "none")
        for name in joined:
            server.player_join(name)
            pid = uuid_by_name(name, server.names)
            if pid:
                server.backend.record_player_session("join", pid)
        for name in left:
            server.player_leave(name)
            pid = uuid_by_name(name, server.names)
            if pid:
                server.backend.record_player_session("leave", pid)
        # Match the incremental cycle to reality: running iff someone is online.
        if actual_set:
            server.start_incremental_cycle()
        else:
            server.stop_incremental_cycle(final=bool(left))
        return sorted(actual_set)
