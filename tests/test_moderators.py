"""Self-contained tests for moderators: per-server command access for users
other than the admin.

Run with plain ``python tests/test_moderators.py`` (needs the bot's
requirements: it imports bot.py, which loads a config -- a throwaway one is
generated from diamondsign.example.json, and auth.json writes go to a temp
file, so nothing in the repo is touched).
"""
import json
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


_TMP = Path(tempfile.mkdtemp(prefix="mods_test_"))


def _import_bot():
    """Import bot.py against a generated config, with auth.json in _TMP."""
    import utils.config as cfg
    example = json.loads((ROOT / "diamondsign.example.json").read_text())

    def fix(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("minecraft_dir", "dir") and isinstance(v, str):
                    d = _TMP / k
                    d.mkdir(exist_ok=True)
                    o[k] = str(d)
                else:
                    fix(v)
        elif isinstance(o, list):
            for x in o:
                fix(x)
    fix(example)
    conf = _TMP / "diamondsign.json"
    conf.write_text(json.dumps(example))
    cfg._CONFIG_PATH = conf
    cfg._EXAMPLE_PATH = _TMP / "example.json"
    import bot as botmod
    import core.commands as cmds
    botmod.AUTH_PATH = cmds.AUTH_PATH = _TMP / "auth.json"
    return botmod


botmod = _import_bot()
import core.commands as cmds
from core.auth import (
    _normalize_ns, auth_ns, is_authorized, is_moderator, moderated_servers,
)
from chat.base import CommandRouter

ADMIN, MOD, STRANGER = "100", "200", "300"


def make_auth(mods=None):
    return {"telegram": _normalize_ns({
        "admin_user_id": ADMIN, "authorized_chat_ids": ["-5"],
        "chat_servers": {"-5": "b"}, "moderators": mods or {}})}


def make_bot(auth, keys=("a", "b", "c")):
    servers = [types.SimpleNamespace(config=types.SimpleNamespace(
        key=k, name=k.upper())) for k in keys]
    b = botmod.Bot(types.SimpleNamespace(name="bot"), servers)
    b.auth = auth
    b.auth_doc = {"bot": auth}
    return b


def ctx_for(user, private=True, chat=None, args=None, text=""):
    replies = []
    c = types.SimpleNamespace(
        platform="telegram", user_id=user, chat_id=chat or user,
        is_private=private, args=args or [], text=text, sender_label=f"u{user}",
        chat_label="direct", chat_name=None, server=None, reply=replies.append,
        adapter=types.SimpleNamespace(send=lambda *a, **k: None),
        bot=types.SimpleNamespace(
            config=types.SimpleNamespace(name="bot"),
            log=types.SimpleNamespace(info=lambda *a, **k: None)))
    c.replies = replies
    return c


# --- core/auth ---------------------------------------------------------------
def test_auth():
    print("auth: namespace and checks")
    ns = _normalize_ns({"admin_user_id": 1,
                        "moderators": {2: ["a"], 3: [], 4: "x"},
                        "moderator_names": {2: "Al", 3: "gone"}})
    check(ns["moderators"] == {"2": ["a"]},
          "ids stringified; empty / malformed grants dropped")
    check(ns["moderator_names"] == {"2": "Al"},
          "labels kept only for current moderators")
    fresh = auth_ns({}, "slack")
    check(fresh["moderators"] == {} and fresh["moderator_names"] == {},
          "a new platform namespace has both keys")
    old = {"telegram": {"admin_user_id": "1", "authorized_chat_ids": []}}
    check("moderators" in auth_ns(old, "telegram"),
          "an older on-disk namespace gains the keys")

    auth = make_auth({MOD: ["a", "c"]})
    check(moderated_servers("telegram", MOD, auth) == {"a", "c"}, "granted set")
    check(is_moderator("telegram", MOD, auth), "moderator of some server")
    check(is_moderator("telegram", MOD, auth, "a"), "...of a")
    check(not is_moderator("telegram", MOD, auth, "b"), "...not of b")
    check(not is_moderator("telegram", STRANGER, auth), "stranger is not")
    check(not is_moderator("slack", MOD, auth), "grants are per platform")
    check(is_authorized("telegram", MOD, MOD, True, auth), "moderator DM allowed")
    check(is_authorized("telegram", ADMIN, ADMIN, True, auth), "admin DM allowed")
    check(not is_authorized("telegram", STRANGER, STRANGER, True, auth),
          "stranger DM ignored")
    check(not is_authorized("telegram", "-9", MOD, False, auth),
          "a moderator gains nothing in an unauthorized group")
    check(is_authorized("telegram", "-5", STRANGER, False, auth),
          "authorized group unchanged")


# --- chat/base.CommandRouter --------------------------------------------------
def test_router():
    print("router: who may run what")
    auth = make_auth({MOD: ["a"]})
    ran = []
    stub_bot = types.SimpleNamespace(note_chat_name=lambda ctx: None,
                                     note_moderator_name=lambda ctx: None)
    r = CommandRouter(
        is_admin=lambda p, u: u == ADMIN,
        is_moderator=lambda p, u: is_moderator(p, u, auth),
        is_authorized=lambda p, c, u, priv: is_authorized(p, c, u, priv, auth),
        bot=stub_bot, resolve=lambda ctx: True)
    r.register("adm", lambda c: ran.append(("adm", c.user_id)),
               private_only=True, admin_only=True)
    r.register("modcmd", lambda c: ran.append(("modcmd", c.user_id)),
               private_only=True, admin_only=True, moderator=True)
    for user in (ADMIN, MOD, STRANGER):
        for cmd in ("adm", "modcmd"):
            r.dispatch(ctx_for(user, text=f"/{cmd}"))
    check(("adm", ADMIN) in ran and ("modcmd", ADMIN) in ran, "admin runs both")
    check(("modcmd", MOD) in ran, "moderator runs a moderator command")
    check(("adm", MOD) not in ran, "moderator is ignored for an admin-only one")
    check(not any(u == STRANGER for _, u in ran), "stranger runs nothing")
    ran.clear()
    r.dispatch(ctx_for(MOD, private=False, chat="-5", text="/modcmd"))
    check(not ran, "moderator commands stay DM-only, even in an authorized group")
    try:
        r.register("bad", lambda c: None, admin_only=True, moderator=True)
        check(False, "moderator without private_only is rejected")
    except AssertionError:
        check(True, "moderator without private_only is rejected")


# --- bot.Bot: server resolution and /use --------------------------------------
def test_resolution():
    print("resolution: a DM only reaches allowed servers")
    auth = make_auth({MOD: ["a"]})
    b = make_bot(auth)
    c = ctx_for(MOD)
    check(b.resolve_target_server(c).config.key == "a",
          "one granted server resolves without /use")
    check(b.resolve_command(c) and c.server.config.key == "a",
          "resolve_command sets it")

    auth["telegram"]["moderators"][MOD] = ["a", "c"]
    c = ctx_for(MOD)
    check(not b.resolve_command(c), "two grants and no /use: must pick")
    menu = c.replies[-1]
    check(" 1. A" in menu and " 3. C" in menu and " 2. B" not in menu,
          f"the prompt lists only their servers, with global numbers ({menu!r})")

    c = ctx_for(MOD, args=["2"])
    b.set_use(c)
    check("not a moderator of B" in c.replies[-1], "/use 2 (B) is refused")
    c = ctx_for(MOD, args=["3"])
    b.set_use(c)
    check(b.resolve_target_server(ctx_for(MOD)).config.key == "c",
          "/use 3 (C) is accepted and used")

    # Partial revoke leaves a stale selection on C: every command, including
    # read-only ones, must now refuse it.
    auth["telegram"]["moderators"][MOD] = ["a"]
    auth["telegram"]["moderators"]["400"] = ["b", "c"]
    c = ctx_for("400", args=["3"])
    b.set_use(c)
    auth["telegram"]["moderators"]["400"] = ["b", "a"]
    c = ctx_for("400")
    ran = []
    r = CommandRouter(
        is_admin=lambda p, u: u == ADMIN,
        is_moderator=lambda p, u: is_moderator(p, u, auth),
        is_authorized=lambda p, cid, u, priv: is_authorized(p, cid, u, priv, auth),
        bot=types.SimpleNamespace(note_chat_name=lambda x: None,
                                  note_moderator_name=lambda x: None),
        resolve=b.resolve_command)
    r.register("list", lambda x: ran.append(x.server.config.key))
    c.text = "/list"
    r.dispatch(c)
    check(not ran and "not a moderator of C" in c.replies[-1],
          "a read command on a server no longer granted is refused")

    check(b.resolve_target_server(ctx_for(STRANGER)) is None,
          "no grant, no server")
    c = ctx_for(ADMIN, args=["2"])
    b.set_use(c)
    check(b.resolve_target_server(ctx_for(ADMIN)).config.key == "b",
          "the admin can still pick any server")
    g = ctx_for(STRANGER, private=False, chat="-5")
    check(b.resolve_target_server(g).config.key == "b",
          "a bound group still resolves through its binding")


# --- /mods, /chat_id and the claim hook ---------------------------------------
class _Router:
    def __init__(self):
        self.handlers, self.specs = {}, {}

    def register(self, names, fn, **kw):
        for n in (names if isinstance(names, list) else [names]):
            self.handlers[n], self.specs[n] = fn, kw


def test_mods_command():
    print("/mods: add, list, remove")
    auth = make_auth()
    b = make_bot(auth)
    r = _Router()
    cmds.register_commands(r, auth)
    mods = r.handlers["mods"]
    check(r.specs["mods"].get("admin_only") and not r.specs["mods"].get("moderator"),
          "/mods is admin-only")
    for name in ("update_server", "chats"):
        check(not r.specs[name].get("moderator"), f"/{name} stays admin-only")
    for name in ("backup", "allowlist", "restore_player", "restore", "start",
                 "scan_achievements", "scan_deaths", "use"):
        check(r.specs[name].get("moderator"), f"/{name} is a moderator command")

    def run(*args):
        c = ctx_for(ADMIN, args=list(args))
        c.bot = b
        mods(c)
        return c.replies[-1]

    check("Usage: /mods add <user_id> <server>" in run("add", MOD),
          "multi-server bot: add needs a server")
    check("Unknown server" in run("add", MOD, "zz"), "unknown server refused")
    check("now a moderator of A" in run("add", MOD, "a"), "add to A")
    check("now a moderator of C" in run("add", MOD, "C"), "add to C (by name)")
    check("already" in run("add", MOD, "a"), "repeat add is a no-op")
    check("That's the admin" in run("add", ADMIN, "a"), "the admin can't be added")
    saved = json.loads((_TMP / "auth.json").read_text())
    check(saved["bot"]["telegram"]["moderators"] == {MOD: ["a", "c"]},
          "grants are saved to auth.json")
    auth["telegram"]["moderator_names"][MOD] = "Alice"
    listing = run()
    check("Alice (200) -> A, C" in listing, f"listing ({listing!r})")

    b._admin_session["telegram"] = {MOD: "c"}
    check("no longer a moderator of C" in run("remove", MOD, "c"), "remove C only")
    check(auth["telegram"]["moderators"][MOD] == ["a"], "A is kept")
    check(MOD not in b._admin_session["telegram"], "their /use selection is cleared")
    check("not a moderator of C" in run("remove", MOD, "c"), "removing twice")
    check("no longer a moderator of all servers" in run("remove", MOD),
          "remove entirely")
    check(MOD not in auth["telegram"]["moderators"]
          and MOD not in auth["telegram"]["moderator_names"],
          "grant and label both gone")

    single = make_bot(make_auth(), keys=("only",))
    c = ctx_for(ADMIN, args=["add", MOD])
    c.bot = single
    r2 = _Router()
    cmds.register_commands(r2, single.auth)
    r2.handlers["mods"](c)
    check("now a moderator of ONLY" in c.replies[-1],
          "one-server bot: add needs no server")

    c = ctx_for(MOD)
    c.bot = b
    r.handlers["chat_id"](c)
    check(f"Your user ID: {MOD}" in c.replies[-1], "/chat_id shows the user ID")


def test_claim_hook():
    print("admin-claim hook")
    hook = cmds.make_unclaimed_handler
    auth = {"telegram": _normalize_ns({"moderators": {MOD: ["a"]}})}
    c = ctx_for(STRANGER)
    c.bot = types.SimpleNamespace(auth_doc={"bot": auth}, log=b_log())
    check(not hook(auth)(c) and auth["telegram"]["admin_user_id"] is None,
          "a namespace with moderators but no admin is not claimable")
    fresh = {}
    c = ctx_for(STRANGER)
    c.bot = types.SimpleNamespace(auth_doc={"bot": fresh}, log=b_log())
    check(hook(fresh)(c) and fresh["telegram"]["admin_user_id"] == STRANGER,
          "a fresh platform is still claimed by the first DM")


def b_log():
    return types.SimpleNamespace(info=lambda *a, **k: None)


# --- concurrency: /start, /allowlist, scans, /restore_player ----------------
def _server(online=True):
    calls = []
    backend = types.SimpleNamespace(
        is_online=lambda: online, can_restart=True, is_available=lambda: True,
        ALLOWLIST_VERB="allowlist",
        allowlist_command=lambda args: calls.append(("allowlist", args)) or "ok",
        relaunch=lambda say: calls.append("relaunch") or True)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(key="a", name="A", mux_start_cmd="x"),
        backend=backend, backup_lock=threading.Lock(),
        lifecycle_lock=threading.Lock(),
        log=types.SimpleNamespace(info=lambda *a, **k: None,
                                  warning=lambda *a, **k: None,
                                  exception=lambda *a, **k: None)), calls


def _wait_free(lock):
    for _ in range(100):
        if not lock.locked():
            return True
        threading.Event().wait(0.02)
    return False


def _handlers():
    r = _Router()
    cmds.register_commands(r, make_auth())
    return r.handlers


def test_start_lock():
    print("/start and lifecycle_lock")
    start = _handlers()["start"]
    srv, calls = _server(online=False)
    srv.lifecycle_lock.acquire()
    c = ctx_for(MOD)
    c.server = srv
    start(c)
    check("A restore, update or start" in c.replies[-1] and not calls,
          "refused while a restore/update/start holds the lock")
    srv.lifecycle_lock.release()

    srv, calls = _server(online=False)
    srv.backup_lock.acquire()          # a backup is running
    c = ctx_for(MOD)
    c.server = srv
    real = cmds.reconcile_online
    cmds.reconcile_online = lambda *a, **k: None
    try:
        start(c)
        check(_wait_free(srv.lifecycle_lock) and calls == ["relaunch"],
              "a running backup doesn't block /start, and /start doesn't take "
              "backup_lock (so it can't make a scheduled backup skip)")
    finally:
        cmds.reconcile_online = real
        srv.backup_lock.release()

    srv, calls = _server(online=True)
    c = ctx_for(MOD)
    c.server = srv
    start(c)
    check("already running" in c.replies[-1] and not srv.lifecycle_lock.locked(),
          "already running: lock released")

    srv, calls = _server(online=False)
    srv.backend.is_online = lambda: 1 / 0
    c = ctx_for(MOD)
    c.server = srv
    try:
        start(c)
    except ZeroDivisionError:
        pass
    check(not srv.lifecycle_lock.locked(),
          "a failing online check doesn't leave the lock held")


def test_allowlist_input_and_lock():
    print("/allowlist: names and lifecycle_lock")
    allow = _handlers()["allowlist"]

    def send(srv, *args):
        c = ctx_for(MOD, args=list(args))
        c.server = srv
        sent = []
        c.adapter = types.SimpleNamespace(send=lambda cid, m, **k: sent.append(m))
        allow(c)
        _wait_free(srv.lifecycle_lock)
        threading.Event().wait(0.05)
        return c.replies, sent

    for evil in ("$(reboot)", "a;rm", "`id`", "x|y", "a&b", "a>b", "../x"):
        srv, calls = _server()
        replies, _ = send(srv, "add", evil)
        check(not calls and "Invalid player name" in replies[-1],
              f"refused: {evil!r}")
    def line(calls):
        return " ".join(["allowlist"] + calls[0][1]) if calls else None

    srv, calls = _server()
    send(srv, "add", "Lean", "Beetle_24102")
    check(line(calls) == 'allowlist add "Lean Beetle_24102"',
          f"a gamertag with a space is sent quoted, as BDS needs ({line(calls)})")
    srv, calls = _server()
    send(srv, "add", '"Lean', 'Beetle_24102"')
    check(line(calls) == 'allowlist add "Lean Beetle_24102"',
          "...also when the user typed the quotes themselves")
    srv, calls = _server()
    send(srv, "remove", "Steve_1")
    check(line(calls) == "allowlist remove Steve_1",
          "a one-word name is sent as-is")
    srv, calls = _server()
    replies, _ = send(srv, "add", "Lean", '$(id)')
    check(not calls and "Invalid player name" in replies[-1],
          "a shell metacharacter in any word is refused")
    srv, calls = _server()
    replies, _ = send(srv, "add", "x" * 33)
    check(not calls and "Invalid player name" in replies[-1],
          "an over-long name is refused")
    srv, calls = _server()
    replies, _ = send(srv, "list", "extra")
    check(not calls and "takes no player name" in replies[-1],
          "extra words after list are refused")

    srv, calls = _server()
    srv.lifecycle_lock.acquire()
    _, sent = send(srv, "add", "Steve")
    srv.lifecycle_lock.release()
    check(not calls and sent and "try again" in sent[-1],
          "nothing is sent while a restore/update/start is running")
    srv, calls = _server()
    srv.backup_lock.acquire()
    send(srv, "add", "Steve")
    srv.backup_lock.release()
    check(calls == [("allowlist", ["add", "Steve"])],
          "a plain backup no longer blocks /allowlist")
    srv, calls = _server(online=False)
    _, sent = send(srv, "add", "Steve")
    check(not calls and sent and "went offline" in sent[-1],
          "server stopped before the send: nothing is typed into the pane")


def test_scan_lock_and_temp_file():
    print("log scans")
    srv, _ = _server()
    lock = cmds._scan_lock(srv)
    lock.acquire()
    try:
        c = ctx_for(MOD)
        c.server = srv
        check(cmds._run_scan(c, "death", None) is None
              and "already running" in c.replies[-1],
              "a second scan of the same server is refused")
    finally:
        lock.release()

    import gzip
    logs = _TMP / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "latest.log").write_text("live\n")
    with gzip.open(logs / "2026-11-20-1.log.gz", "wt") as f:
        f.write("archived\n")
    (logs / "2026-11-20-1.log").write_text("a real log, not ours\n")
    seen = []
    srv.config.log_path = logs / "latest.log"
    cmds._scan_logs(lambda path, date, s: seen.append(path.read_text()) or 0, srv)
    check("archived\n" in seen, "the archive is scanned")
    check((logs / "2026-11-20-1.log").read_text() == "a real log, not ours\n",
          "a same-named .log beside the archive is left untouched")


def test_restore_player_pin():
    print("/restore_player confirms the version that was selected")
    cmd = _handlers()["restore_player"]
    cmds._pending_player_restore.clear()

    v = lambda ts, src="incremental backup": {
        "timestamp": ts, "source": src, "path": Path(f"/b/{ts}.zip"),
        "kind": "zip", "entry": "playerdata/u-1.dat", "sort_key": 0}
    versions = [v("2026-11-17 18:12:24"), v("2026-11-17 17:00:00")]
    restored = []
    done = threading.Event()
    backend = types.SimpleNamespace(
        resolve_player=lambda name, names: ("Hongy1618", "u-1"),
        list_player_versions=lambda uuid, chain: list(versions),
        restore_player=lambda *a: (restored.append(a[2]), done.set()))
    srv, _ = _server()
    srv.backend = backend
    srv.names, srv.load_manifest = {}, lambda: ("c", "f", {})
    real = cmds.reconcile_online
    cmds.reconcile_online = lambda *a, **k: None

    def send(*args):
        c = ctx_for(MOD, args=list(args))
        c.server = srv
        cmd(c)
        return c.replies[-1]

    try:
        send("Hongy1618")
        check("17:00:00" in send("Hongy1618", "2"), "row 2 selected")
        versions.insert(0, v("2026-11-18 09:46:10"))   # a backup lands
        send("Hongy1618", "2", "confirm")
        done.wait(2)
        check(restored and restored[0]["timestamp"] == "2026-11-17 17:00:00",
              f"the selected version is restored, not the new row 2 ({restored})")

        # A new full backup starts a chain: the same archive's label gains
        # "(old chain)". It's still the selected backup.
        restored.clear()
        done.clear()
        send("Hongy1618")
        send("Hongy1618", "3")
        versions[:] = [dict(x, source=x["source"] + " (old chain)")
                       for x in versions]
        send("Hongy1618", "3", "confirm")
        done.wait(2)
        check(restored and restored[0]["timestamp"] == "2026-11-17 17:00:00",
              "still found after a chain rollover relabels it")

        # A live Java player file is rewritten in place (logout save): same
        # path, new mtime. Restoring it now would restore unseen content.
        restored.clear()
        live = {"timestamp": "2026-11-17 12:00:00", "source": "live .dat_old",
                "path": Path("/w/playerdata/u-1.dat_old"), "kind": "live",
                "entry": None, "sort_key": 0}
        versions[:] = [live]
        send("Hongy1618")
        send("Hongy1618", "1")
        versions[:] = [dict(live, timestamp="2026-11-17 12:30:00")]
        msg = send("Hongy1618", "1", "confirm")
        check("no longer available" in msg and not restored,
              "a live file that changed since it was listed is refused")

        restored.clear()
        send("Hongy1618")
        send("Hongy1618", "1")
        versions.pop(0)                                # that backup vanishes
        msg = send("Hongy1618", "1", "confirm")
        check("no longer available" in msg and not restored,
              "a selected version that disappeared is refused")
    finally:
        cmds.reconcile_online = real


def test_mods_save_failure_rolls_back():
    print("/mods when auth.json can't be saved")
    auth = make_auth({MOD: ["a"]})
    b = make_bot(auth)
    r = _Router()
    cmds.register_commands(r, auth)
    real = cmds.save_auth

    def boom(*a, **k):
        raise OSError("disk full")
    cmds.save_auth = boom
    try:
        for args in (["remove", MOD], ["add", MOD, "b"]):
            c = ctx_for(ADMIN, args=args)
            c.bot = b
            b.log = types.SimpleNamespace(info=lambda *a, **k: None,
                                          exception=lambda *a, **k: None)
            r.handlers["mods"](c)
            check("Could not save auth.json" in c.replies[-1]
                  and auth["telegram"]["moderators"] == {MOD: ["a"]},
                  f"/mods {args[0]}: in-memory grants unchanged")
    finally:
        cmds.save_auth = real


def test_mods_publish_after_save():
    print("/mods publishes a grant only once it's saved")
    auth = make_auth()
    b = make_bot(auth)
    r = _Router()
    cmds.register_commands(r, auth)
    real = cmds.save_auth
    seen = []

    def spy(doc, path):
        seen.append((is_moderator("telegram", MOD, auth),
                     doc["bot"]["telegram"]["moderators"].get(MOD)))
        return real(doc, path)
    cmds.save_auth = spy
    try:
        c = ctx_for(ADMIN, args=["add", MOD, "a"])
        c.bot = b
        r.handlers["mods"](c)
    finally:
        cmds.save_auth = real
    check(seen == [(False, ["a"])],
          f"saved with the grant, but not live during the save ({seen})")
    check(is_moderator("telegram", MOD, auth, "a"), "live once saved")


def test_moderator_name_save_is_best_effort():
    print("a moderator's name that can't be saved")
    auth = make_auth({MOD: ["a"]})
    b = make_bot(auth)
    b.log = types.SimpleNamespace(exception=lambda *a, **k: None,
                                  info=lambda *a, **k: None)
    real = botmod.save_auth

    def boom(*a, **k):
        raise OSError("disk full")
    botmod.save_auth = boom
    try:
        c = ctx_for(MOD)
        c.sender_label = "NewName"
        b.note_moderator_name(c)       # must not raise
        check(True, "no exception reaches the command")
        check("NewName" not in auth["telegram"]["moderator_names"].values(),
              "the unsaved name is forgotten, so the next command retries")
    except Exception as e:
        check(False, f"no exception reaches the command ({e!r})")
    finally:
        botmod.save_auth = real


def test_lifecycle_fail_fast():
    print("restore/update don't queue behind /start holding backup_lock")
    srv, _ = _server()
    srv.lifecycle_lock.acquire()       # a /start relaunch in progress
    said = []
    check(not cmds._acquire_both(srv, said.append)
          and not srv.backup_lock.locked() and "being started" in said[-1],
          "refused at once, backup_lock left free for scheduled backups")
    srv.lifecycle_lock.release()
    check(cmds._acquire_both(srv, said.append)
          and srv.backup_lock.locked() and srv.lifecycle_lock.locked(),
          "both taken when free")
    cmds._release_both(srv)
    check(not srv.backup_lock.locked() and not srv.lifecycle_lock.locked(),
          "both released")


def test_admin_with_no_servers_up():
    print("admin on a bot whose servers all failed")
    auth = make_auth()
    b = make_bot(auth, keys=())
    c = ctx_for(ADMIN, text="/list")
    check(not b.resolve_command(c)
          and c.replies[-1] == "No servers are running on this bot.",
          "the admin isn't told they're not a moderator")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all passed")
