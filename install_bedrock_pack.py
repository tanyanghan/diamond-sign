#!/usr/bin/env python3
"""One-line installer for the Diamond Sign Bedrock events behavior pack.

Reads the server location from the bot's ``.env`` (``MINECRAFT_DIR``), then:
  1. copies the pack into ``<MINECRAFT_DIR>/behavior_packs/diamondsign_events/``,
  2. activates it in the world's ``world_behavior_packs.json`` (create/append by
     the pack's header UUID, never clobbering other packs),
  3. (unless ``--deaths-only``) enables the Beta APIs / GameTest experiment in
     ``level.dat`` — but only after confirming the **server is not running**
     (the world LevelDB must be unlocked).

It also enables the Bedrock **Beta APIs** experiment in ``level.dat`` for chat
(deaths don't need it). The experiment toggle lives in ``level.dat`` — a
little-endian NBT file with an 8-byte header — so this uses ``amulet-nbt`` (from
``requirements-bedrock-restore.txt``), not the LevelDB.

Run it with the server stopped:

    python install_bedrock_pack.py                # pack + activate + experiment (chat & deaths)
    python install_bedrock_pack.py --deaths-only  # skip the experiment (deaths only; no amulet libs)
    python install_bedrock_pack.py --force        # bypass the running check (you stopped it yourself)
    python install_bedrock_pack.py --uninstall    # reverse it (the experiment can't be undone)

It sets ``content-log-console-output-enabled=true`` in server.properties and
``BEDROCK_SCRIPT_EVENTS=true`` (and ``CHAT_RELAY=true``) in ``.env`` for you; then
start the server and restart the bot. See bedrock_pack/INSTALL.md for details.
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


def _load_env():
    """Return (minecraft_dir: Path, edition: str) from the bot's .env."""
    try:
        from dotenv import load_dotenv
    except Exception:
        _fail("python-dotenv is required (pip install -r requirements.txt)")
    load_dotenv(_REPO_ROOT / ".env")
    mc = os.environ.get("MINECRAFT_DIR", "").strip()
    if not mc:
        _fail("MINECRAFT_DIR is not set in .env")
    mc_dir = Path(os.path.expanduser(mc))
    if not mc_dir.is_dir():
        _fail(f"MINECRAFT_DIR does not exist: {mc_dir}")
    edition = os.environ.get("SERVER_EDITION", "java").strip().lower()
    return mc_dir, edition


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


def _set_env_var(key: str, value: str):
    _set_kv(_REPO_ROOT / ".env", key, value)


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
                         "turn the content-log + .env flags back off (the Beta "
                         "APIs experiment can't be undone — see the note)")
    ap.add_argument("--deaths-only", action="store_true",
                    help="install the pack but skip the Beta APIs experiment "
                         "(deaths work; chat needs the experiment)")
    ap.add_argument("--force", action="store_true",
                    help="skip the 'server not running' check before editing "
                         "level.dat (only if you have already stopped it)")
    ap.add_argument("--no-env", action="store_true",
                    help="don't touch .env (otherwise BEDROCK_SCRIPT_EVENTS, and "
                         "CHAT_RELAY unless --deaths-only, are set to true)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip the interactive 'server stopped?' confirmation "
                         "(for non-interactive use)")
    args = ap.parse_args()

    mc_dir, edition = _load_env()
    # Confirm this is a Bedrock server two ways: the configured edition, and the
    # directory layout (a bedrock_server binary or a worlds/ folder). The pack and
    # the experiment edit are Bedrock-only and would be meaningless on Java.
    if edition != "bedrock":
        _fail(f"SERVER_EDITION is '{edition}', not 'bedrock' — set it to bedrock; "
              "this pack is Bedrock-only")
    looks_bedrock = ((mc_dir / "bedrock_server").exists()
                     or (mc_dir / "bedrock_server.exe").exists()
                     or (mc_dir / "worlds").is_dir())
    if not looks_bedrock:
        _fail(f"{mc_dir} doesn't look like a Bedrock server (no bedrock_server "
              "binary or worlds/ directory). Check MINECRAFT_DIR.")

    # Level name comes from server.properties (get_level_name), so no path arg.
    sys.path.insert(0, str(_REPO_ROOT))
    from utils.config import get_level_name
    level_name = get_level_name(mc_dir)
    world_dir = mc_dir / "worlds" / level_name
    if not world_dir.is_dir():
        _fail(f"world directory not found: {world_dir} "
              f"(level-name from server.properties: '{level_name}')")

    uuid, version = _read_manifest()
    print(f"Server:  {mc_dir}")
    print(f"World:   {world_dir}")

    # The server should be stopped: this edits world/server files (and install
    # irreversibly enables a world experiment). Confirm before touching anything.
    _confirm_stopped(args.yes or args.force)

    if args.uninstall:
        _do_uninstall(mc_dir, world_dir, uuid, args)
    else:
        _do_install(mc_dir, world_dir, uuid, version, args)


def _do_install(mc_dir, world_dir, uuid, version, args):
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
            # Probe the LevelDB lib directly so a missing dependency isn't
            # mistaken for "server running" (is_db_locked treats any open
            # failure — lock, absent, or ImportError — as locked).
            try:
                import leveldb  # noqa: F401  (amulet-leveldb)
            except Exception as e:
                _fail("amulet-leveldb is needed to verify the server is stopped "
                      "(pip install -r requirements-bedrock-restore.txt), or pass "
                      f"--force after stopping the server yourself. ({e})")
            from utils import bedrock_player
            if bedrock_player.is_db_locked(bedrock_player.world_db_path(mc_dir)):
                _fail("the world database is locked — the server appears to be "
                      "running (or another tool has it open). Stop it and re-run.")
        print("Enabling Beta APIs experiment (chat)...")
        _enable_experiment(world_dir)

    # Turn on the bot-side flags so the events are actually ingested.
    if args.no_env:
        print("Left .env untouched (--no-env). Set BEDROCK_SCRIPT_EVENTS=true"
              + ("" if args.deaths_only else " and CHAT_RELAY=true") + " yourself.")
    else:
        _set_env_var("BEDROCK_SCRIPT_EVENTS", "true")
        flags = "BEDROCK_SCRIPT_EVENTS=true"
        if not args.deaths_only:
            _set_env_var("CHAT_RELAY", "true")
            flags += ", CHAT_RELAY=true"
        print(f".env updated: {flags}")

    print("\nDone. Start the server, then restart the bot.")


def _do_uninstall(mc_dir, world_dir, uuid, args):
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

    if args.no_env:
        print("Left .env untouched (--no-env). Set BEDROCK_SCRIPT_EVENTS=false "
              "and CHAT_RELAY=false yourself.")
    else:
        _set_env_var("BEDROCK_SCRIPT_EVENTS", "false")
        _set_env_var("CHAT_RELAY", "false")
        print(".env updated: BEDROCK_SCRIPT_EVENTS=false, CHAT_RELAY=false")

    # The Beta APIs experiment is intentionally left on: Bedrock permanently
    # flags a world once an experiment is used, so it can't be cleanly disabled.
    # With the pack gone it simply produces nothing.
    print("\nNote: the world's Beta APIs experiment is left enabled — Bedrock "
          "can't undo it once a world has used it. level.dat is untouched.")
    print("Done. Restart the server.")


if __name__ == "__main__":
    main()
