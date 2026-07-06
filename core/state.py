"""Per-server player state storage: the name registry, achievements, and deaths.

These are free functions operating on a given ``Server`` (for the name registry)
or on a ``(dict, path)`` pair (achievements / deaths), each guarded by its own
lock. A leaf module — depends only on the stdlib and the shared logger.
"""

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger("diamondsign")


# ---------------------------------------------------------------------------
# Player name registry
# ---------------------------------------------------------------------------
# The {player_id: name} registry is owned by the backend (Java: player_names.json
# keyed by UUID; Bedrock: projected from bedrock_players.json keyed by xuid) and
# mirrored in ``server.names``; these helpers operate on a given Server.
_names_lock = threading.Lock()


def refresh_player_names(server) -> None:
    with _names_lock:
        server.names.clear()
        server.names.update(server.backend.load_names())


def register_player(server, uuid: str, name: str) -> None:
    names = server.names
    old = names.get(uuid)
    names[uuid] = name
    changed = server.backend.register_name(uuid, name)
    if old and old != name:
        server.log.info("Player registry: %s renamed %s -> %s", uuid, old, name)
    elif changed and not old:
        server.log.info("Player registry: registered %s (%s)", name, uuid)


def uuid_by_name(player_name: str, names: dict) -> str | None:
    for uuid, name in names.items():
        if name == player_name:
            return uuid
    return None


# ---------------------------------------------------------------------------
# Achievements storage
# ---------------------------------------------------------------------------
_achievements_lock = threading.Lock()


def load_achievements(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load %s", path)
    return {}


def _save_achievements(achievements: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(achievements, f, indent=2)


def record_achievement(uuid: str, achievement: str, ach_type: str,
                       timestamp: str, achievements: dict, path: Path) -> bool:
    with _achievements_lock:
        entries = achievements.setdefault(uuid, [])
        for e in entries:
            if e["achievement"] == achievement and e["timestamp"] == timestamp:
                return False
        entries.append({
            "achievement": achievement,
            "type": ach_type,
            "timestamp": timestamp,
        })
        _save_achievements(achievements, path)
    return True


# ---------------------------------------------------------------------------
# Deaths storage
# ---------------------------------------------------------------------------
_deaths_lock = threading.Lock()


def load_deaths(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load %s", path)
    return {}


def _save_deaths(deaths: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(deaths, f, indent=2)


def record_death(uuid: str, message: str, timestamp: str,
                 deaths: dict, path: Path) -> bool:
    with _deaths_lock:
        entries = deaths.setdefault(uuid, [])
        for e in entries:
            if e["message"] == message and e["timestamp"] == timestamp:
                return False
        entries.append({"message": message, "timestamp": timestamp})
        _save_deaths(deaths, path)
    return True
