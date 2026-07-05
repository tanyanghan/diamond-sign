#!/usr/bin/env python3
"""One-line installer for the Diamond Sign Bedrock events behavior pack.

Reads the server(s) from ``diamondsign.json``. If more than one Bedrock server
is configured, it lists them and asks which to act on (or pass ``--server
<name>``). Then, for the chosen server:
  1. copies the pack into ``<minecraft_dir>/behavior_packs/diamondsign_events/``,
  2. activates it in the world's ``world_behavior_packs.json`` (create/append by
     the pack's header UUID, never clobbering other packs),
  3. (unless ``--deaths-only``) enables the Beta APIs / GameTest experiment in
     ``level.dat`` — but only after confirming the **server is not running**
     (a console ``list`` probe over the server's tmux/screen session; a live
     server answers into console.log).

The experiment toggle lives in ``level.dat`` — a little-endian NBT file with an
8-byte header — so this uses ``amulet-nbt`` (from
``requirements-bedrock-restore.txt``). It does not touch the world LevelDB.

Run it with the server stopped:

    python install_bedrock_pack.py                     # pick a server if >1, then install
    python install_bedrock_pack.py --server square     # act on a specific server
    python install_bedrock_pack.py --deaths-only       # skip the experiment (deaths only)
    python install_bedrock_pack.py --force             # bypass the running check
    python install_bedrock_pack.py --uninstall         # reverse it (the experiment can't be undone)

It sets ``content-log-console-output-enabled=true`` in server.properties and the
chosen server's ``bedrock_script_events: true`` (and ``chat_relay: true``) in
``diamondsign.json`` for you; then start the server and restart the bot. See
bedrock_pack/INSTALL.md for details.
"""

import argparse
import json
import os
import shutil
import struct
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent         # repo root (this file's dir)
_PACK_DIR = _REPO_ROOT / "bedrock_pack"
# Pack files that belong in the world (everything else there is tooling/docs).
_PACK_CONTENTS = ("manifest.json", "scripts")
_INSTALLED_NAME = "diamondsign_events"


def _fail(msg: str):
    sys.exit(f"error: {msg}")


def _server_running(server) -> "bool | None":
    """Best-effort, LevelDB-free check for a live BDS before we edit its world.

    Sends ``list`` on the server's console (via its tmux/screen session) and
    watches whether the server answers into ``console.log``. Returns True
    (responded -> running), False (silent -> looks stopped), or None (couldn't
    probe -- no matching mux session).

    Deliberately NOT the world LevelDB lock: this BDS build never takes an fcntl
    lock on the world db (there isn't even a ``LOCK`` file while it runs), so a
    lock check both misreports a running server as stopped AND opens the live
    world db (a second LevelDB instance against a db the server is writing) to
    find out -- the exact corruption this guard exists to prevent.
    """
    import time
    if not server.mux_session:
        return None            # no explicit session -> don't guess a pane
    sys.path.insert(0, str(_REPO_ROOT))
    from backends.mux import detect
    mux = detect(server.mux_session)
    if mux is None:
        return None
    log_path = server.log_path

    def size():
        try:
            return log_path.stat().st_size
        except OSError:
            return -1

    before = size()
    mux.send("list")            # a running server echoes an online-count line
    time.sleep(2.0)             # mux settle (~0.3s) + server response margin
    return size() != before


def _confirm_stopped(assume_yes: bool):
    """Ask the operator to confirm the server is stopped before any edits.
    ``--yes``/``--force`` skip the prompt; a non-interactive shell must pass one."""
    if assume_yes:
        return
    print("The Minecraft server must be STOPPED before this runs "
          "(it edits world and server files).")
    try:
        ans = input("Is the server stopped? [y/N] ").strip().lower()
    except EOFError:
        # No interactive input available (piped / redirected / no TTY).
        _fail("no input available — stop the server, then re-run with --yes "
              "(or --force) to confirm it's stopped")
    if ans not in ("y", "yes"):
        _fail("aborted — stop the server, then re-run")


def _select_server(server_name):
    """Return the chosen Bedrock ``ServerConfig`` from diamondsign.json.

    With one Bedrock server it's used directly; with several, ``--server <name>``
    picks one, else the user is shown a numbered list and prompted."""
    sys.path.insert(0, str(_REPO_ROOT))
    from utils.config import load_config, ConfigError, EDITION_BEDROCK
    try:
        app = load_config()
    except ConfigError as e:
        _fail(str(e))
    bedrock = [s for s in app.all_servers() if s.edition == EDITION_BEDROCK]
    if not bedrock:
        _fail("no Bedrock servers configured in diamondsign.json "
              "(this pack is Bedrock-only)")

    if server_name:
        matches = [s for s in bedrock
                   if server_name in (s.name, s.key)]
        if not matches:
            names = ", ".join(s.name for s in bedrock)
            _fail(f"no Bedrock server named '{server_name}'. "
                  f"Bedrock servers: {names}")
        return matches[0]

    if len(bedrock) == 1:
        return bedrock[0]

    print("Multiple Bedrock servers are configured:")
    for i, s in enumerate(bedrock, 1):
        print(f"  {i}. {s.name}   ({s.minecraft_dir})")
    try:
        raw = input(f"Choose a server [1-{len(bedrock)}]: ").strip()
    except EOFError:
        _fail("multiple Bedrock servers — pass --server <name> in a "
              "non-interactive shell")
    try:
        idx = int(raw)
        if not (1 <= idx <= len(bedrock)):
            raise ValueError
    except ValueError:
        _fail(f"invalid choice: '{raw}'")
    return bedrock[idx - 1]


def _read_manifest():
    data = json.loads((_PACK_DIR / "manifest.json").read_text())
    return data["header"]["uuid"], data["header"]["version"]


def _set_kv(path: Path, key: str, value: str) -> bool:
    """Set ``key=value`` in a key=value file (.env / server.properties), in place.
    Replaces an existing line (commented or not), else appends. Returns True if
    the content changed. Other lines (incl. secrets like rcon.password) untouched."""
    import re
    lines = path.read_text().splitlines() if path.exists() else []
    pat = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    line = f"{key}={value}"
    changed = True
    for i, ln in enumerate(lines):
        if pat.match(ln):
            changed = ln != line
            lines[i] = line
            break
    else:
        lines.append(line)
    if changed:
        path.write_text("\n".join(lines) + "\n")
    return changed


# Flags that live under the server's nested "edition" object rather than at the
# server top level (see utils/config.py). chat_relay stays top-level (shared).
_EDITION_FLAGS = {"bedrock_script_events", "console_log"}


def _set_server_flags(server, **flags) -> bool:
    """Set boolean fields on ``server``'s entry in diamondsign.json, in place.

    The entry is matched by its resolved ``minecraft_dir`` (unique per server),
    so it works whether or not the entry has an explicit ``name``. Edition-scoped
    flags (bedrock_script_events) are written under the entry's "edition" object;
    shared ones (chat_relay) at the top level. Other fields and formatting of
    siblings are preserved (json re-dumped with indent=2). Returns True if
    anything changed."""
    cfg_path = _REPO_ROOT / "diamondsign.json"
    if not cfg_path.exists():
        print(f"warning: {cfg_path.name} not found; set "
              + ", ".join(f"{k}={str(v).lower()}" for k, v in flags.items())
              + " on this server yourself")
        return False
    target = server.minecraft_dir.resolve()
    doc = json.loads(cfg_path.read_text())
    changed = False
    for bot in doc.get("bots", []):
        for s in bot.get("servers", []):
            raw_dir = (s.get("minecraft_dir") or "").strip()
            if not raw_dir:
                continue
            if Path(os.path.expanduser(raw_dir)).resolve() != target:
                continue
            for k, v in flags.items():
                if k in _EDITION_FLAGS:
                    ed = s.get("edition")
                    if not isinstance(ed, dict):
                        # Defensive: upgrade a bare string/absent edition to the
                        # object form, preserving any existing type.
                        ed = {"type": ed} if isinstance(ed, str) else {}
                        s["edition"] = ed
                    container = ed
                else:
                    container = s
                if container.get(k) != v:
                    container[k] = v
                    changed = True
    if changed:
        cfg_path.write_text(json.dumps(doc, indent=2) + "\n")
    return changed


def _copy_pack(mc_dir: Path) -> Path:
    dest = mc_dir / "behavior_packs" / _INSTALLED_NAME
    dest.mkdir(parents=True, exist_ok=True)
    for name in _PACK_CONTENTS:
        src = _PACK_DIR / name
        target = dest / name
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)
    return dest


def _activate(world_dir: Path, uuid: str, version) -> bool:
    """Add the pack to world_behavior_packs.json. Returns True if it was added
    (False if already present). Preserves any other packs."""
    wbp = world_dir / "world_behavior_packs.json"
    entries = []
    if wbp.exists():
        try:
            entries = json.loads(wbp.read_text()) or []
        except Exception:
            _fail(f"{wbp} is not valid JSON; fix or remove it and re-run")
    if any(isinstance(e, dict) and e.get("pack_id") == uuid for e in entries):
        return False
    entries.append({"pack_id": uuid, "version": version})
    wbp.write_text(json.dumps(entries, indent=2))
    return True


def _remove_pack(mc_dir: Path) -> bool:
    """Delete the installed pack directory. Returns True if it existed."""
    dest = mc_dir / "behavior_packs" / _INSTALLED_NAME
    if dest.is_dir():
        shutil.rmtree(dest)
        return True
    return False


def _deactivate(world_dir: Path, uuid: str) -> bool:
    """Remove the pack's entry from world_behavior_packs.json (preserving any
    other packs). Returns True if an entry was removed."""
    wbp = world_dir / "world_behavior_packs.json"
    if not wbp.exists():
        return False
    try:
        entries = json.loads(wbp.read_text()) or []
    except Exception:
        _fail(f"{wbp} is not valid JSON; fix or remove it and re-run")
    kept = [e for e in entries
            if not (isinstance(e, dict) and e.get("pack_id") == uuid)]
    if len(kept) == len(entries):
        return False
    wbp.write_text(json.dumps(kept, indent=2))
    return True


def _load_amulet_nbt():
    try:
        import amulet_nbt
        from amulet_nbt import CompoundTag, ByteTag
        return amulet_nbt, CompoundTag, ByteTag
    except Exception as e:
        _fail("amulet-nbt is required for the experiment step. Install it with:\n"
              "  pip install -r requirements-bedrock-restore.txt\n"
              f"(import failed: {e})")


def _build_enabled_level_dat(raw: bytes, keys, CompoundTag, ByteTag, amulet_nbt):
    """Return (new_level_dat_bytes, after_experiments_dict). Sets each experiment
    key plus the two meta flags in the ``experiments`` compound, preserving the
    8-byte header (storage version + recomputed body length)."""
    storage_version = struct.unpack_from("<i", raw, 0)[0]
    nt = amulet_nbt.load(raw[8:], little_endian=True, compressed=False)
    comp = nt.compound
    exp = comp.get("experiments")
    if not isinstance(exp, CompoundTag):
        exp = CompoundTag()
        comp["experiments"] = exp
    for k in keys:
        exp[k] = ByteTag(1)
    exp["experiments_ever_used"] = ByteTag(1)
    exp["saved_with_toggled_experiments"] = ByteTag(1)
    after = {k: int(exp[k]) for k in exp}
    body = nt.to_nbt(compressed=False, little_endian=True)
    new = struct.pack("<i", storage_version) + struct.pack("<i", len(body)) + body
    return new, after


def _enable_experiment(world_dir: Path):
    """Enable the Beta APIs / GameTest experiment in the world's level.dat.

    "gametest" is the key current versions honor (shows as 'gtst' in the
    server's active-experiments line); "beta_api" is the historical name. We set
    both — unknown keys are ignored. Backs up level.dat and writes atomically."""
    amulet_nbt, CompoundTag, ByteTag = _load_amulet_nbt()
    level_dat = world_dir / "level.dat"
    if not level_dat.is_file():
        _fail(f"no level.dat in {world_dir}")
    raw = level_dat.read_bytes()
    new, after = _build_enabled_level_dat(
        raw, ["beta_api", "gametest"], CompoundTag, ByteTag, amulet_nbt)
    if new == raw:
        print("  experiment already enabled")
        return
    from datetime import datetime
    bak = level_dat.with_name(f"level.dat.bak-{datetime.now():%Y%m%d_%H%M%S}")
    bak.write_bytes(raw)
    tmp = level_dat.with_name("level.dat.tmp")
    tmp.write_bytes(new)
    tmp.replace(level_dat)
    print(f"  enabled experiments {after} (backup: {bak.name})")
    print("  note: enabling an experiment is IRREVERSIBLE for this world")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true",
                    help="reverse the install: remove the pack, deactivate it, "
                         "turn the content-log + config flags back off (the Beta "
                         "APIs experiment can't be undone — see the note)")
    ap.add_argument("--deaths-only", action="store_true",
                    help="install the pack but skip the Beta APIs experiment "
                         "(deaths work; chat needs the experiment)")
    ap.add_argument("--force", action="store_true",
                    help="skip the 'server not running' check before editing "
                         "level.dat (only if you have already stopped it)")
    ap.add_argument("--server", metavar="NAME",
                    help="which Bedrock server (name/key from diamondsign.json) "
                         "to act on; if omitted and several exist, you're prompted")
    ap.add_argument("--no-config", action="store_true",
                    help="don't touch diamondsign.json (otherwise the chosen "
                         "server's bedrock_script_events, and chat_relay unless "
                         "--deaths-only, are set to true)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip the interactive 'server stopped?' confirmation "
                         "(for non-interactive use)")
    args = ap.parse_args()

    server = _select_server(args.server)
    mc_dir = server.minecraft_dir
    if not mc_dir.is_dir():
        _fail(f"minecraft_dir does not exist: {mc_dir}")
    # Sanity-check the directory layout (edition is already 'bedrock' from config).
    looks_bedrock = ((mc_dir / "bedrock_server").exists()
                     or (mc_dir / "bedrock_server.exe").exists()
                     or (mc_dir / "worlds").is_dir())
    if not looks_bedrock:
        _fail(f"{mc_dir} doesn't look like a Bedrock server (no bedrock_server "
              "binary or worlds/ directory). Check minecraft_dir for "
              f"'{server.name}'.")

    # Level name comes from server.properties (get_level_name), so no path arg.
    sys.path.insert(0, str(_REPO_ROOT))
    from utils.config import get_level_name
    level_name = get_level_name(mc_dir)
    world_dir = mc_dir / "worlds" / level_name
    if not world_dir.is_dir():
        _fail(f"world directory not found: {world_dir} "
              f"(level-name from server.properties: '{level_name}')")

    uuid, version = _read_manifest()
    print(f"Server:  {server.name}  ({mc_dir})")
    print(f"World:   {world_dir}")

    # The server should be stopped: this edits world/server files (and install
    # irreversibly enables a world experiment). Confirm before touching anything.
    _confirm_stopped(args.yes or args.force)

    if args.uninstall:
        _do_uninstall(server, mc_dir, world_dir, uuid, args)
    else:
        _do_install(server, mc_dir, world_dir, uuid, version, args)


def _do_install(server, mc_dir, world_dir, uuid, version, args):
    dest = _copy_pack(mc_dir)
    print(f"Copied:  {dest}")

    added = _activate(world_dir, uuid, version)
    print(f"Activated in world_behavior_packs.json "
          f"({'added' if added else 'already present'})")

    # Mirror the content log (incl. script console output) to stdout/console.log —
    # required for the bot to see the pack's markers at all.
    props = mc_dir / "server.properties"
    if props.is_file():
        ch = _set_kv(props, "content-log-console-output-enabled", "true")
        print("server.properties: content-log-console-output-enabled=true "
              f"({'set' if ch else 'already enabled'})")
    else:
        print(f"warning: no server.properties at {props}; set "
              "content-log-console-output-enabled=true yourself")

    if args.deaths_only:
        print("Skipped the Beta APIs experiment (--deaths-only); chat disabled.")
    else:
        if not args.force:
            # Confirm the server is really down before editing level.dat. A
            # console `list` probe (not the LevelDB lock — see _server_running):
            # a live server answers, closing the false-negative the lock check
            # has on this BDS build without opening the live world db.
            running = _server_running(server)
            if running is True:
                _fail("the server answered a console command — it's still "
                      "running. Stop it and re-run (or --force after stopping "
                      "it yourself).")
            elif running is None:
                print("warning: couldn't probe the server (no tmux/screen "
                      "session matched this server's mux.session) — proceeding "
                      "on your confirmation that it's stopped. Pass --force to "
                      "silence this.")
        print("Enabling Beta APIs experiment (chat)...")
        _enable_experiment(world_dir)

    # Turn on the per-server flags so the events are actually ingested.
    if args.no_config:
        print("Left diamondsign.json untouched (--no-config). Set this server's "
              "bedrock_script_events: true"
              + ("" if args.deaths_only else " and chat_relay: true") + " yourself.")
    else:
        flags = {"bedrock_script_events": True}
        if not args.deaths_only:
            flags["chat_relay"] = True
        _set_server_flags(server, **flags)
        shown = ", ".join(f"{k}: true" for k in flags)
        print(f"diamondsign.json updated for '{server.name}': {shown}")

    print("\nDone. Start the server, then restart the bot.")


def _do_uninstall(server, mc_dir, world_dir, uuid, args):
    removed = _remove_pack(mc_dir)
    print(f"Removed pack: {'yes' if removed else 'not present'}")

    deact = _deactivate(world_dir, uuid)
    print(f"Deactivated in world_behavior_packs.json "
          f"({'removed' if deact else 'not present'})")

    props = mc_dir / "server.properties"
    if props.is_file():
        ch = _set_kv(props, "content-log-console-output-enabled", "false")
        print("server.properties: content-log-console-output-enabled=false "
              f"({'set' if ch else 'already false'})")

    if args.no_config:
        print("Left diamondsign.json untouched (--no-config). Set this server's "
              "bedrock_script_events: false and chat_relay: false yourself.")
    else:
        _set_server_flags(server, bedrock_script_events=False, chat_relay=False)
        print(f"diamondsign.json updated for '{server.name}': "
              "bedrock_script_events: false, chat_relay: false")

    # The Beta APIs experiment is intentionally left on: Bedrock permanently
    # flags a world once an experiment is used, so it can't be cleanly disabled.
    # With the pack gone it simply produces nothing.
    print("\nNote: the world's Beta APIs experiment is left enabled — Bedrock "
          "can't undo it once a world has used it. level.dat is untouched.")
    print("Done. Restart the server.")


if __name__ == "__main__":
    main()
