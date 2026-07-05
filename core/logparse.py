"""Parse Minecraft server log/console lines into ``(event_type, payload)`` tuples.

``parse_line(line, server)`` dispatches to the Java or Bedrock parser by the
server's edition. Join/leave/uuid parsing has the side effect of registering
players (via ``core.state.register_player``) and updating the server's online
set; death/achievement/chat return payloads for the notification layer to
deliver. The death phrase table and ``categorize_death`` are also used by the
``/death_summary`` command and the log-history scanners.
"""

import json
import re
from datetime import datetime

from core.state import register_player
from utils.config import EDITION_BEDROCK

RE_JOIN = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) joined the game')
RE_LEAVE = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) left the game')
RE_UUID = re.compile(r'^\[[\d:]+\] \[User Authenticator #\d+/INFO\]: UUID of player (\w+) is ([0-9a-f-]+)')
RE_ACHIEVEMENT = re.compile(
    r'^\[([\d:]+)\] \[Server thread/INFO\]: (\w+) has '
    r'(made the advancement|reached the goal|completed the challenge) '
    r'\[(.+?)\]'
)
ACH_TYPE_MAP = {
    "made the advancement": "advancement",
    "reached the goal": "goal",
    "completed the challenge": "challenge",
}
ACH_VERB_MAP = {v: k for k, v in ACH_TYPE_MAP.items()}

RE_SERVER_MSG = re.compile(r'^\[([\d:]+)\] \[Server thread/INFO\]: (\w+) (.+)$')
# Player chat, e.g. "[12:34:56] [Server thread/INFO]: <Steve> hello" (or a
# Paper-style "[Async Chat Thread - #0/INFO]:"). The <> brackets distinguish it
# from join/leave/death lines (which start with a bare \w name).
RE_CHAT = re.compile(r'^\[[\d:]+\] \[[^\]]*/INFO\]: <([^>]+)> (.+)$')
DEATH_PHRASES = (
    "was slain by", "was shot by", "was killed",
    "was blown up by", "was squashed by", "was fireballed by",
    "was pummeled by", "was stung by", "was impaled",
    "was skewered by", "was struck by lightning",
    "was burnt to", "was frozen to death", "was pricked to death",
    "was poked to death", "was doomed to fall",
    "was roasted in dragon", "was obliterated by",
    "was squished",
    "drowned", "suffocated", "starved to death",
    "burned to death",
    "fell from", "fell off", "fell out of", "fell into", "fell while",
    "hit the ground too hard",
    "tried to swim in lava",
    "walked into",
    "froze to death", "withered away",
    "experienced kinetic energy",
    "went up in flames", "went off with a bang",
    "died", "didn't want to live",
    "discovered the floor was lava",
    "blew up",
    "left the confines of this world",
)


DEATH_CATEGORIES = [
    ("Combat (was slain by)", ["was slain by"]),
    ("Shot by", ["was shot by"]),
    ("Blown up", ["was blown up by"]),
    ("Falls", ["fell from", "fell off", "fell out of", "fell into",
               "fell while", "hit the ground too hard"]),
    ("Lava", ["tried to swim in lava"]),
    ("Fire", ["burned to death", "was burnt to", "went up in flames",
              "walked into fire"]),
    ("Drowning", ["drowned"]),
    ("Withered away", ["withered away"]),
    ("Impaled", ["was impaled"]),
    ("Frozen", ["froze to death", "was frozen to death"]),
    ("Lightning", ["was struck by lightning"]),
    ("Kinetic energy", ["experienced kinetic energy"]),
    ("Suffocation", ["suffocated"]),
    ("Starvation", ["starved to death"]),
    ("Cactus", ["walked into a cactus", "was pricked to death",
                "was poked to death"]),
    ("Dragon", ["was doomed to fall", "was roasted in dragon"]),
    ("Sonic shriek", ["was obliterated by"]),
    ("Explosions", ["blew up", "went off with a bang"]),
    ("Void", ["left the confines of this world"]),
    ("Magic", ["was killed by magic", "was killed by even more magic"]),
]


def categorize_death(message: str) -> str:
    for category, phrases in DEATH_CATEGORIES:
        if any(message.startswith(p) for p in phrases):
            return category
    return "Other"


# The name->uuid pending map lives on the Server (server.pending_uuids).


def _parse_line_java(line: str, server) -> tuple:
    """Return (event_type, payload) or (None, None).

    For join/leave, payload is the player name string.
    For achievement, payload is a dict with player, achievement, type, time.
    """
    line = line.strip()

    m = RE_UUID.match(line)
    if m:
        name, uuid = m.group(1), m.group(2)
        server.pending_uuids[name] = uuid
        register_player(server, uuid, name)
        return None, None

    m = RE_JOIN.match(line)
    if m:
        name = m.group(1)
        uuid = server.pending_uuids.pop(name, None)
        if uuid:
            register_player(server, uuid, name)
        server.player_join(name)
        return "join", name

    m = RE_LEAVE.match(line)
    if m:
        name = m.group(1)
        server.player_leave(name)
        return "leave", name

    m = RE_ACHIEVEMENT.match(line)
    if m:
        time_str, name, ach_type_full, achievement = m.groups()
        return "achievement", {
            "player": name,
            "achievement": achievement,
            "type": ACH_TYPE_MAP[ach_type_full],
            "time": time_str,
        }

    m = RE_SERVER_MSG.match(line)
    if m:
        time_str, name, msg = m.groups()
        if any(msg.startswith(p) for p in DEATH_PHRASES):
            return "death", {
                "player": name,
                "message": msg,
                "time": time_str,
            }

    if server.config.chat_relay:
        m = RE_CHAT.match(line)
        if m:
            return "chat", {"player": m.group(1), "message": m.group(2)}

    return None, None


# Bedrock Dedicated Server console lines (terser than Java's log). Names may
# contain spaces, so capture up to the ", xuid:" delimiter. BDS's own console
# reports only join/leave; death and chat come from the bedrock_pack behavior
# pack as `DIAMONDSIGN {json}` marker lines (see below).
RE_BEDROCK_CONNECT = re.compile(r'Player connected:\s*(.+?),\s*xuid:\s*(\d+)')
RE_BEDROCK_DISCONNECT = re.compile(r'Player disconnected:\s*(.+?),\s*xuid:\s*(\d+)')

# Behavior-pack event marker, e.g.
#   [<ts> WARN] [Scripting] DIAMONDSIGN {"t":"death","player":"X","cause":"lava"}
_BEDROCK_MARKER = "DIAMONDSIGN "

# Bedrock damage cause -> death phrase, worded to mirror Java so the same
# categorize_death / DEATH_CATEGORIES logic works for /death_summary. "{by}"
# is filled with the prettified killer entity when present.
_BEDROCK_DEATH_PHRASES = {
    "lava": "tried to swim in lava",
    "fire": "went up in flames",
    "fire_tick": "burned to death",
    "fall": "fell from a high place",
    "drowning": "drowned",
    "suffocation": "suffocated in a wall",
    "starve": "starved to death",
    "freezing": "froze to death",
    "lightning": "was struck by lightning",
    "void": "fell out of the world",
    "contact": "was pricked to death",
    "magma": "discovered the floor was lava",
    "wither": "withered away",
    "anvil": "was squashed by a falling anvil",
    "falling_block": "was squashed by a falling block",
    "magic": "was killed by magic",
    "sonic_boom": "was obliterated by a sonically-charged shriek",
    "block_explosion": "blew up",
    "entity_explosion": "blew up",
    "entity_attack": "was slain",
    "projectile": "was shot",
    "thorns": "was killed trying to hurt",
    "self_destruct": "blew up",
}


def _pretty_entity(type_id: str) -> str:
    """'minecraft:zombie' -> 'Zombie'."""
    return type_id.split(":")[-1].replace("_", " ").title()


def _bedrock_death_message(cause: str, by) -> str:
    """Build a Java-style death message from a Bedrock damage cause + killer."""
    phrase = _BEDROCK_DEATH_PHRASES.get((cause or "").lower())
    killer = _pretty_entity(by) if by else None
    if phrase is None:
        return f"was killed by {killer}" if killer else "died"
    # Causes that read naturally with a "by <killer>" suffix.
    if cause.lower() in ("entity_attack", "projectile", "thorns") and killer:
        verb = {"entity_attack": "was slain by", "projectile": "was shot by",
                "thorns": "was killed trying to hurt"}[cause.lower()]
        return f"{verb} {killer}"
    return phrase


def _parse_line_bedrock(line: str, server) -> tuple:
    """Return (event_type, payload) or (None, None) for a Bedrock console line.

    Join/leave come from BDS itself; death/chat come from the bedrock_pack
    behavior pack's DIAMONDSIGN marker lines (gated by config). The player's xuid
    is the registry key (Bedrock has no per-player UUID file).
    """
    line = line.strip()
    m = RE_BEDROCK_CONNECT.search(line)
    if m:
        name, xuid = m.group(1).strip(), m.group(2).strip()
        if xuid:
            register_player(server, xuid, name)
        server.player_join(name)
        return "join", name
    m = RE_BEDROCK_DISCONNECT.search(line)
    if m:
        name, xuid = m.group(1).strip(), m.group(2).strip()
        # The disconnect line carries the xuid too, so register it even if we
        # never saw this player's connect line (e.g. they were already online
        # when the bot started). Otherwise their identity would go unrecorded.
        if xuid:
            register_player(server, xuid, name)
        server.player_leave(name)
        return "leave", name

    # Behavior-pack markers (anywhere after the log/[Scripting] prefixes).
    idx = line.find(_BEDROCK_MARKER)
    if idx != -1:
        try:
            ev = json.loads(line[idx + len(_BEDROCK_MARKER):])
        except (ValueError, TypeError):
            return None, None
        t = ev.get("t")
        if t == "death" and server.config.bedrock_script_events:
            return "death", {
                "player": ev.get("player", "?"),
                "message": _bedrock_death_message(ev.get("cause"), ev.get("by")),
                "time": datetime.now().strftime("%H:%M:%S"),
            }
        if t == "chat" and server.config.chat_relay:
            return "chat", {"player": ev.get("player", "?"),
                            "message": ev.get("msg", "")}
    return None, None


def parse_line(line: str, server) -> tuple:
    """Dispatch line parsing to the edition-specific parser."""
    if server.config.edition == EDITION_BEDROCK:
        return _parse_line_bedrock(line, server)
    return _parse_line_java(line, server)
