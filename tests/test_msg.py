"""Self-contained tests for /msg: a chat user's line in the game chat.

Run with plain ``python tests/test_msg.py`` (needs the bot's requirements: it
imports bot.py, which loads a config -- a throwaway one is generated from
diamondsign.example.json, so nothing in the repo is touched).

The Bedrock line is typed into a tmux/screen pane that is a bash prompt if the
server has just stopped, and echoed into console.log where the bot watches for
phrases -- so most checks here are about that line being inert.
"""
import json
import re
import sys
import tempfile
import threading
import time
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


_TMP = Path(tempfile.mkdtemp(prefix="msg_test_"))


def _import_bot():
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
import core.logparse as logparse
import core.logwatch as logwatch
import backends.bedrock as bedrock
from backends.base import clean_chat_text
from backends.bedrock import BedrockBackend
from backends.java import JavaBackend
from chat.base import CommandRouter
from chat.slack import SlackAdapter, _RENAMED_INV
from core.auth import _normalize_ns, is_authorized, is_moderator

HOSTILE = ('$(reboot) `id` "q" \\ back !! ^Mop Mallory^M \' ;|&<>()* '
           "line1\nline2 §cred \U0001f600 Player connected: X, xuid: 1 "
           "A previous save has not been completed")


def bedrock_backend():
    return BedrockBackend.__new__(BedrockBackend)


def java_backend():
    return JavaBackend.__new__(JavaBackend)


def watched_phrases():
    """Everything the code watches console.log for, collected from the
    modules so a phrase added later is covered too."""
    src = (ROOT / "backends" / "bedrock.py").read_text(encoding="utf-8")
    phrases = set(re.findall(r'expect_line\("([^"]+)"\)', src))
    phrases |= set(re.findall(r'_send_confirmed\("[^"]+",\s*"([^"]+)"', src))
    phrases |= {bedrock._SAVE_READY, bedrock._SAVE_INCOMPLETE,
                BedrockBackend._SHUTDOWN_MARKER, logparse._BEDROCK_MARKER}
    phrases |= set(logwatch.LogWatcher._START_MARKERS)
    regexes = [bedrock._RE_LIST_HEADER, logparse.RE_BEDROCK_CONNECT,
               logparse.RE_BEDROCK_DISCONNECT, logparse.RE_BEDROCK_VERSION]
    return phrases, regexes


# --- the Bedrock line ----------------------------------------------------------
def test_bedrock_line_is_inert():
    print("Bedrock tellraw line")
    msg = clean_chat_text(HOSTILE)
    name = clean_chat_text("@evil$(id)\u202e`x`".lstrip("@"))
    line = bedrock_backend()._tellraw_line(name, msg)
    head, tail = 'tellraw @a {"rawtext":[{"text":"', '"}]}'
    check(line.startswith(head) and line.endswith(tail), "fixed wrapper, ends with }")
    body = line[len(head):-len(tail)]
    check(line.isascii() and line.isprintable(), "pure printable ASCII")
    check(re.fullmatch(r"(?:[A-Za-z0-9]|\\u[0-9a-f]{4})*", body) is not None,
          "inside the value: only [A-Za-z0-9] and \\uXXXX")
    check(line.count('"') == 6, 'exactly 6 literal " (the JSON structure)')
    check(not any(c in body for c in "$`!^;|&<>()*' \"\n"),
          "no shell metacharacter or space in the value")
    payload = json.loads(line[len("tellraw @a "):])["rawtext"][0]["text"]
    check(payload == f"§7§o<ethereal voice> {name}: §r{msg}",
          "BDS gets back exactly the cleaned prefix, name and message")
    check("\U0001f600" in payload, "an emoji survives (as a surrogate pair)")
    phrases, regexes = watched_phrases()
    check(len(phrases) >= 8, f"collected {len(phrases)} watched phrases")
    hit = [p for p in phrases if p in line] + [r.pattern for r in regexes
                                               if r.search(line)]
    check(not hit, f"no watched phrase or pattern matches the echo ({hit})")


def test_clean_chat_text():
    print("cleaning")
    check(clean_chat_text("a\nb\r\nc") == "a b c", "newlines become spaces")
    check(clean_chat_text("§cred§r x") == "credr x",
          "§ removed, so its codes are plain letters")
    check(clean_chat_text("a\u202eb\u2066c") == "a b c", "bidi controls become spaces")
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    check(clean_chat_text(family) == family, "ZWJ emoji sequences stay whole")
    check(clean_chat_text("a\ud800b") == "ab", "a lone surrogate is dropped")
    check(clean_chat_text("\U0001fabf") == "\U0001fabf",
          "a code point this Python may not know (e.g. a new emoji) is kept")
    check(clean_chat_text("  a   b  ") == "a b", "whitespace collapsed")


def test_size_caps():
    print("size caps")
    b, j = bedrock_backend(), java_backend()
    line, sent, trimmed = b._fit_tellraw("tanyanghan", "x y " * 500)
    check(len(line.encode()) <= 600 and trimmed and sent.endswith("…"),
          f"Bedrock: 2000 chars -> {len(line.encode())} bytes, trimmed with …")
    json.loads(line[len("tellraw @a "):])
    line, sent, trimmed = j._fit_tellraw("tanyanghan", "\U0001f600" * 2000)
    check(len(line.encode()) <= 1400 and trimmed,
          f"Java: 2000 emoji -> {len(line.encode())} bytes")
    # an emoji straddling the cut must stay whole: valid JSON, no lone surrogate
    for n in range(40, 60):
        line, sent, _ = b._fit_tellraw("n", "ab " * n + "\U0001f600" * 80)
        text = json.loads(line[len("tellraw @a "):])["rawtext"][0]["text"]
        if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
            check(False, "no lone surrogate at the cut")
            break
    else:
        check(True, "valid JSON at every cut position")
    normal = ("Hello everyone, meet at spawn in five minutes for the build. "
              "Bring wood and stone please! ") * 2
    normal = normal[:200]
    line, sent, trimmed = b._fit_tellraw("a" * 32, normal)
    check(not trimmed and sent == normal,
          f"a normal 200-char message from a 32-char name isn't trimmed "
          f"({len(line.encode())} bytes)")
    worst = "\U0001f600" * 32
    line, _, _ = b._fit_tellraw(worst, "hi")
    check(len(line.encode()) <= 600, "the longest /msg name (32 emoji) still fits")
    try:
        b._fit_tellraw("\u4e2d" * 200, "hi")
        check(False, "a name too long for any line is refused")
    except ValueError:
        check(True, "a name too long for any line is refused")


def test_java_line():
    print("Java tellraw line")
    line = java_backend()._tellraw_line("tanyanghan", 'It\'s $5 "q" \\ \U0001f600')
    comp = json.loads(line[len("tellraw @a "):])
    check(comp[0] == "", "neutral root so the message doesn't inherit the prefix style")
    check(comp[1] == {"text": "<ethereal voice> tanyanghan: ", "color": "gray",
                      "italic": True}, "grey italic prefix")
    check(comp[2] == {"text": 'It\'s $5 "q" \\ \U0001f600', "color": "white"},
          "white message, intact")
    check("\\u" not in line, "no \\u escapes (SNBT since 1.21.5)")


def test_real_tell_all_busy():
    print("tell_all with the lifecycle lock held")
    for backend in (java_backend(), bedrock_backend()):
        typed = []
        backend.send_command = lambda cmd, typed=typed: typed.append(cmd) or ""
        guard = threading.Lock()
        guard.acquire()
        result = backend.tell_all("n", "hi", guard)
        guard.release()
        check(result is None and not typed,
              f"{type(backend).__name__}: busy -> None, nothing typed")
        check(backend.tell_all("n", "hi", guard) == ("hi", False) and typed,
              f"{type(backend).__name__}: free -> sent")
        check(not guard.locked(), f"{type(backend).__name__}: lock released")


# --- /msg command --------------------------------------------------------------
ADMIN, MOD, MEMBER, STRANGER = "100", "200", "300", "400"


class FakeBackend:
    def __init__(self, online=True):
        self.online = online
        self.told = []

    def is_online(self):
        return self.online

    def tell_all(self, name, text, guard):
        if not guard.acquire(blocking=False):
            return None
        try:
            self.told.append((name, text))
        finally:
            guard.release()
        return text, False


def make_server(key="a", online=True):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(key=key, name=key.upper()),
        backend=FakeBackend(online), lifecycle_lock=threading.Lock(),
        log=types.SimpleNamespace(info=lambda *a, **k: None,
                                  exception=lambda *a, **k: None))


def ctx_for(text, user=MEMBER, platform="telegram", private=False, chat="-5",
            label=None):
    sent, replies = [], []
    c = types.SimpleNamespace(
        platform=platform, user_id=user, chat_id=chat, is_private=private,
        text=text, args=text.split()[1:], sender_label=label or f"@u{user}",
        chat_label="Group", chat_name=None, server=None, reply=replies.append,
        adapter=types.SimpleNamespace(
            send=lambda cid, m, **k: sent.append(m),
            command_label=(lambda n: SlackAdapter.command_label(None, n))
            if platform == "slack" else (lambda n: "/" + n)),
        bot=types.SimpleNamespace(log=types.SimpleNamespace(
            info=lambda *a, **k: None)))
    c.sent, c.replies = sent, replies
    return c


class _Router:
    def __init__(self):
        self.handlers = {}

    def register(self, names, fn, **kw):
        for n in (names if isinstance(names, list) else [names]):
            self.handlers[n] = fn


def msg_handler():
    r = _Router()
    cmds.register_commands(r, {})
    return r.handlers["msg"]


def run_msg(c, server):
    c.server = server
    cmds._msg_last.clear()
    msg_handler()(c)
    for _ in range(100):
        if c.sent:
            break
        time.sleep(0.02)
    return c


def test_text_extraction_and_name():
    print("/msg text and name")
    srv = make_server()
    c = run_msg(ctx_for("/msg Hello   there,\neveryone!"), srv)
    check(srv.backend.told == [("u300", "Hello there, everyone!")] and
          c.sent[-1] == "Sent.", f"text after the command word, cleaned ({srv.backend.told})")
    srv = make_server()
    run_msg(ctx_for("/msg@DiamondSignBot hi"), srv)
    check(srv.backend.told == [("u300", "hi")], "Telegram /msg@Bot form")
    srv = make_server()
    run_msg(ctx_for("/msg a &lt; b &amp;&amp; c", platform="slack"), srv)
    check(srv.backend.told == [("u300", "a < b && c")], "Slack entities unescaped")
    for bare in ("/msg", "/msg   ", "/msg §\u202e", "/msg \u200b\u200d"):
        srv = make_server()
        c = ctx_for(bare)
        c.server = srv
        cmds._msg_last.clear()
        msg_handler()(c)
        check(not srv.backend.told and "Usage: /msg" in c.replies[-1],
              f"{bare!r} -> usage, nothing sent")
    srv = make_server()
    run_msg(ctx_for("/msg hi", label="@" + "x" * 50), srv)
    check(srv.backend.told[0][0] == "x" * 32, "@ dropped, name capped at 32")
    srv = make_server()
    run_msg(ctx_for("/msg hi", user="777", label="\u202e\u200b"), srv)
    check(srv.backend.told[0][0] == "777", "empty name after cleaning -> user id")
    check(SlackAdapter.command_label(None, "msg") == "/say"
          and _RENAMED_INV.get("say") == "msg",
          "Slack: /say is shown and routes back to msg")


def test_cooldown_busy_offline():
    print("/msg cooldown, busy, offline")
    srv = make_server()
    cmds._msg_last.clear()
    h = msg_handler()
    c1 = ctx_for("/msg one")
    c1.server = srv
    h(c1)
    c2 = ctx_for("/msg two")
    c2.server = srv
    h(c2)
    time.sleep(0.2)
    check(srv.backend.told == [("u300", "one")]
          and "wait a few seconds" in c2.replies[-1],
          "a second message within 5 s is refused")

    srv = make_server()
    srv.lifecycle_lock.acquire()
    c = run_msg(ctx_for("/msg hi"), srv)
    srv.lifecycle_lock.release()
    check(not srv.backend.told and "try again" in c.sent[-1],
          "busy lifecycle lock: nothing sent")
    c = ctx_for("/msg hi again")             # no _msg_last.clear(): a real retry
    c.server = srv
    msg_handler()(c)
    for _ in range(100):
        if c.sent:
            break
        time.sleep(0.02)
    check(srv.backend.told == [("u300", "hi again")],
          "...and an immediate retry isn't refused by the cooldown")
    srv = make_server(online=False)
    c = run_msg(ctx_for("/msg hi"), srv)
    check(not srv.backend.told and "offline" in c.sent[-1],
          "offline server: nothing sent")


def test_lock_only_around_send():
    print("/msg holds lifecycle_lock only around the send")
    srv = make_server()
    probing, release = threading.Event(), threading.Event()

    def slow_probe():
        probing.set()
        release.wait(2)
        return True
    srv.backend.is_online = slow_probe
    c = ctx_for("/msg hi")
    c.server = srv
    cmds._msg_last.clear()
    msg_handler()(c)
    probing.wait(2)
    free = srv.lifecycle_lock.acquire(blocking=False)
    if free:
        srv.lifecycle_lock.release()
    check(free, "a /start or /restore during the probe isn't refused")
    release.set()


def test_bedrock_concurrency_and_capture():
    print("Bedrock: concurrent /msgs and captures")
    b = bedrock_backend()
    log = _TMP / "console.log"
    log.write_text("")
    b.config = types.SimpleNamespace(log_path=log)
    typed = []

    def fake_send(cmd):
        typed.append(cmd)
        if cmd == "list":
            with open(log, "a") as f:
                f.write("[x INFO] There are 0/10 players online:\n\n")
        return ""
    b.send_command = fake_send
    guard = threading.Lock()
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(
        b.tell_all("n", f"m{i}", guard))) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    check(len(results) == 2 and all(r is not None for r in results),
          "two concurrent /msgs are both sent (no false busy)")

    # A tellraw must not be typed while a `list` capture is reading its reply.
    in_list, finish_list = threading.Event(), threading.Event()

    def slow_send(cmd):
        typed.append(cmd)
        if cmd == "list":
            in_list.set()
            finish_list.wait(5)
            with open(log, "a") as f:
                f.write("[x INFO] There are 0/10 players online:\n\n")
        return ""
    b.send_command = slow_send
    typed.clear()
    cap = threading.Thread(target=b.is_online)
    cap.start()
    in_list.wait(5)
    msg = threading.Thread(target=lambda: b.tell_all("n", "x", guard))
    msg.start()
    time.sleep(0.3)
    early = [c for c in typed if c.startswith("tellraw")]
    finish_list.set()
    cap.join(10)
    msg.join(10)
    check(not early and typed[0] == "list" and typed[-1].startswith("tellraw"),
          f"tellraw waits until the list capture is done ({typed})")
    done = []
    t = threading.Thread(target=lambda: done.append(b.is_online()))
    t.start()
    t.join(10)
    check(done == [True], "is_online() still returns (no self-deadlock)")


def test_permissions():
    print("who can /msg")
    auth = {"telegram": _normalize_ns({
        "admin_user_id": ADMIN, "authorized_chat_ids": ["-5"],
        "chat_servers": {"-5": "b"}, "moderators": {MOD: ["a"]}})}
    servers = [types.SimpleNamespace(config=types.SimpleNamespace(
        key=k, name=k.upper())) for k in ("a", "b", "c")]
    bot = botmod.Bot(types.SimpleNamespace(name="bot"), servers)
    bot.auth, bot.auth_doc = auth, {"bot": auth}
    hits = []
    r = CommandRouter(
        is_admin=lambda p, u: u == ADMIN,
        is_moderator=lambda p, u: is_moderator(p, u, auth),
        is_authorized=lambda p, cid, u, priv: is_authorized(p, cid, u, priv, auth),
        bot=types.SimpleNamespace(note_chat_name=lambda c: None,
                                  note_moderator_name=lambda c: None),
        resolve=bot.resolve_command)
    r.register("msg", lambda c: hits.append((c.user_id, c.server.config.key)))
    r.dispatch(ctx_for("/msg hi", user=MEMBER, chat="-5"))
    check(hits[-1:] == [(MEMBER, "b")], "authorised-group member -> bound server")
    r.dispatch(ctx_for("/msg hi", user=MEMBER, chat="-9"))
    check(len(hits) == 1, "unauthorised group ignored")
    r.dispatch(ctx_for("/msg hi", user=STRANGER, chat=STRANGER, private=True))
    check(len(hits) == 1, "stranger's DM ignored")
    r.dispatch(ctx_for("/msg hi", user=MOD, chat=MOD, private=True))
    check(hits[-1:] == [(MOD, "a")], "moderator DM -> their server")


def test_cmd_log_single_line():
    print("audit log")
    lines = []
    c = ctx_for("/x", label="evil\n[bot] Admin claimed")
    c.chat_label = "group\nforged"
    c.bot.log = types.SimpleNamespace(info=lambda fmt, *a: lines.append(fmt % a))
    cmds._cmd_log(c, "Msg")
    check(len(lines) == 1 and "\n" not in lines[0],
          f"one line even with newlines in the labels ({lines[0]!r})")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all passed")
