"""Self-contained tests for Java log-line parsing.

Run with plain ``python tests/test_logparse.py`` -- no pytest, no dependencies
beyond the stdlib. Every line below was taken verbatim from a real server log
(Paper 26.2 and Paper 26.3), not written by hand: 26.3 started prefixing
server-broadcast lines with "System chat: ", and both shapes must parse.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import logparse

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


class StubServer:
    def __init__(self, chat_relay=True):
        self.config = SimpleNamespace(edition="java", chat_relay=chat_relay)
        self.pending_uuids = {}
        self.online = set()
        self.registered = {}

    def player_join(self, name):
        self.online.add(name)

    def player_leave(self, name):
        self.online.discard(name)


def _stub_register(server, uuid, name):
    server.registered[uuid] = name


logparse.register_player = _stub_register

UUID = "6ba93be8-9ee0-4883-87a6-2a703d615ce8"
PREFIX = {"26.2": "", "26.3": "System chat: "}


def test_join_leave_both_versions():
    print("join / leave")
    for ver, sys_chat in PREFIX.items():
        srv = StubServer()
        logparse.parse_line(
            f"[09:59:43] [User Authenticator #0/INFO]: UUID of player "
            f"Kamion_of_Riva is {UUID}", srv)
        check(srv.registered.get(UUID) == "Kamion_of_Riva",
              f"{ver}: UUID line registers the player")
        ev = logparse.parse_line(
            f"[09:59:45] [Server thread/INFO]: {sys_chat}Kamion_of_Riva "
            f"joined the game", srv)
        check(ev == ("join", "Kamion_of_Riva"), f"{ver}: join is parsed")
        check(srv.online == {"Kamion_of_Riva"}, f"{ver}: join marks them online")
        ev = logparse.parse_line(
            f"[10:29:53] [Server thread/INFO]: {sys_chat}Kamion_of_Riva "
            f"left the game", srv)
        check(ev == ("leave", "Kamion_of_Riva"), f"{ver}: leave is parsed")
        check(srv.online == set(), f"{ver}: leave marks them offline")


def test_achievements_both_versions():
    print("achievements")
    for ver, sys_chat in PREFIX.items():
        for verb, kind in (("made the advancement", "advancement"),
                           ("reached the goal", "goal"),
                           ("completed the challenge", "challenge")):
            ev = logparse.parse_line(
                f"[15:57:32] [Server thread/INFO]: {sys_chat}erny1816 has "
                f"{verb} [Not Today, Thank You]", StubServer())
            check(ev == ("achievement", {
                "player": "erny1816", "achievement": "Not Today, Thank You",
                "type": kind, "time": "15:57:32"}), f"{ver}: {kind} is parsed")


def test_deaths_both_versions():
    print("deaths")
    for ver, sys_chat in PREFIX.items():
        ev = logparse.parse_line(
            f"[20:49:39] [Server thread/INFO]: {sys_chat}SebastianYong was "
            f"shot by Skeleton", StubServer())
        check(ev == ("death", {"player": "SebastianYong",
                               "message": "was shot by Skeleton",
                               "time": "20:49:39"}), f"{ver}: death is parsed")


def test_chat_and_non_events():
    print("chat and non-events")
    ev = logparse.parse_line(
        "[16:18:15] [Async Chat Thread - #5/INFO]: <Kamion_of_Riva> sleep please",
        StubServer())
    check(ev == ("chat", {"player": "Kamion_of_Riva",
                          "message": "sleep please"}), "chat is parsed")
    ev = logparse.parse_line(
        "[16:18:15] [Async Chat Thread - #5/INFO]: <Kamion_of_Riva> sleep please",
        StubServer(chat_relay=False))
    check(ev == (None, None), "chat is ignored when chat_relay is off")
    for line in (
        "[09:48:50] [Server thread/INFO]: System chat: [Rcon: Saved the game]",
        "[09:48:49] [Server thread/INFO]: System chat: [Rcon: Automatic saving "
        "is now disabled]",
        "[10:29:53] [Server thread/INFO]: Kamion_of_Riva lost connection: "
        "Disconnected",
        "[09:59:46] [Server thread/INFO]: Kamion_of_Riva[/192.168.87.60:55404] "
        "logged in with entity id 52 at ([minecraft:overworld]997.1, 62.1, -1884.6)",
        "[16:18:15] [Async Chat Thread - #5/INFO]: <Kamion_of_Riva> System chat: "
        "erny1816 joined the game",
    ):
        srv = StubServer(chat_relay=False)
        check(logparse.parse_line(line, srv) == (None, None)
              and not srv.online, f"no event: {line[34:74]}")


def test_death_cause():
    print("death cause")
    for message, cause in (
        ("was slain by Iron Golem", "Iron Golem"),
        ("was shot by Skeleton", "Skeleton"),
        ("was shot by Skeleton using [Bow]", "Skeleton"),
        ("was blown up by Creeper", "Creeper"),
        ("was killed by Witch using magic", "Witch"),
        ("was killed by magic", "Magic"),
        ("was killed by even more magic", "Magic"),
        ("was slain by Kamion_of_Riva using [Diamond Sword]", "Kamion_of_Riva"),
        ("tried to swim in lava", "Lava"),
        ("tried to swim in lava to escape Zombie", "Lava"),
        ("fell from a high place", "Falls"),
        ("drowned", "Drowning"),
        ("was slain", "Other"),
        ("did something unheard of", "Other"),
    ):
        got = logparse.death_cause(message)
        check(got == cause, f"{message!r} -> {cause} (got {got})")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all passed")
