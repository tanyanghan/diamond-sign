"""The per-(bot, server) event -> chat notification callback.

``make_notify_callback`` builds the ``notify(event_type, payload)`` function the
log watcher calls for each parsed event: it records achievements/deaths + player
sessions on the ``server`` and announces to the chats bound to it via ``bot``.
"""

import time
from datetime import datetime

from core.state import uuid_by_name, record_achievement, record_death
from core.logparse import ACH_VERB_MAP


def make_notify_callback(bot, server):
    """Build the per-(bot, server) event->chat callback. Announcements go out
    through ``bot`` to the chats bound to ``server``; player-session, name
    registry, achievements/deaths, and incremental-backup side effects all
    operate on ``server``."""
    _last_event: dict = {}
    _cooldown = 3

    def _send_to_chats(msg: str) -> int:
        return bot.announce(server, msg)

    def notify(event_type: str, payload) -> None:
        if event_type == "achievement":
            player = payload["player"]
            achievement = payload["achievement"]
            ach_type = payload["type"]
            time_str = payload["time"]
            key = f"{player}-achievement-{achievement}"
            now = time.time()
            if now - _last_event.get(key, 0) < _cooldown:
                return
            _last_event[key] = now

            timestamp = f"{datetime.now().strftime('%Y-%m-%d')} {time_str}"
            uuid = uuid_by_name(player, server.names)
            if uuid:
                record_achievement(uuid, achievement, ach_type, timestamp,
                                   server.achievements, server.achievements_path)

            verb = ACH_VERB_MAP[ach_type]
            sent = _send_to_chats(f"{player} has {verb} [{achievement}]")
            server.log.info("Achievement: %s — %s — sent to %d chat(s)",
                            player, achievement, sent)
            return

        if event_type == "death":
            player = payload["player"]
            death_msg = payload["message"]
            time_str = payload["time"]
            timestamp = f"{datetime.now().strftime('%Y-%m-%d')} {time_str}"
            uuid = uuid_by_name(player, server.names)
            if uuid:
                record_death(uuid, death_msg, timestamp, server.deaths,
                             server.deaths_path)

            sent = _send_to_chats(f"{player} {death_msg}")
            server.log.info("Death: %s %s — sent to %d chat(s)",
                            player, death_msg, sent)
            return

        if event_type == "chat":
            # In-game chat relayed to the platforms (one-way; no cooldown so
            # distinct messages aren't suppressed). Gated by config.chat_relay
            # at the parser; nothing recorded.
            player = payload["player"]
            message = payload["message"]
            sent = _send_to_chats(f"\U0001f4ac {player}: {message}")
            server.log.info("Chat: %s: %s — sent to %d chat(s)",
                            player, message, sent)
            return

        name = payload
        # Online-time accumulation (Bedrock; no-op on Java). Done before the
        # cooldown gate so a quick rejoin still records the session boundary.
        pid = uuid_by_name(name, server.names)
        if pid:
            server.backend.record_player_session(event_type, pid)
            server.note_active_xuid(pid)  # candidate for identity learning
            if event_type == "leave":
                # Refresh last_seen to the disconnect time (connect already set
                # it on join). No-op on Java for an unchanged name.
                server.backend.register_name(pid, name)

        key = f"{name}-{event_type}"
        now = time.time()
        if now - _last_event.get(key, 0) < _cooldown:
            return
        _last_event[key] = now

        online = server.get_online_players()
        count = len(online)
        names_str = ", ".join(online) if online else "none"

        verb = "joined the game" if event_type == "join" else "left the game"
        status = "online" if event_type == "join" else "offline"
        sent = _send_to_chats(f"{name} {verb}\nPlayers online: {count} ({names_str})")
        server.log.info("Notification: player %s %s — sent to %d chat(s)",
                        name, status, sent)

        # Incremental backup triggers
        if event_type == "join" and count == 1:
            server.start_incremental_cycle()
        elif event_type == "leave" and count == 0:
            server.stop_incremental_cycle(final=True)

    return notify
