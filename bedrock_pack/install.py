#!/usr/bin/env python3
"""One-line installer for the mcnotifier Bedrock events behavior pack.

Reads the server location from the bot's ``.env`` (``MINECRAFT_DIR``), then:
  1. copies the pack into ``<MINECRAFT_DIR>/behavior_packs/mcnotifier_events/``,
  2. activates it in the world's ``world_behavior_packs.json`` (create/append by
     the pack's header UUID, never clobbering other packs),
  3. (unless ``--deaths-only``) enables the Beta APIs / GameTest experiment in
     ``level.dat`` — but only after confirming the **server is not running**
     (the world LevelDB must be unlocked).

Run it with the server stopped:

    python bedrock_pack/install.py                # pack + activate + experiment (chat & deaths)
    python bedrock_pack/install.py --deaths-only  # skip the experiment (deaths only; no amulet libs)
    python bedrock_pack/install.py --force        # bypass the running check (you stopped it yourself)

Afterwards: set ``content-log-console-output-enabled=true`` in server.properties,
set ``BEDROCK_SCRIPT_EVENTS=true`` (and ``CHAT_RELAY=true`` for chat) in ``.env``,
then start the server. See bedrock_pack/INSTALL.md for the manual steps.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

_PACK_DIR = Path(__file__).resolve().parent          # bedrock_pack/
_REPO_ROOT = _PACK_DIR.parent
# Pack files that belong in the world (everything else here is tooling/docs).
_PACK_CONTENTS = ("manifest.json", "scripts")
_INSTALLED_NAME = "mcnotifier_events"


def _fail(msg: str):
    sys.exit(f"error: {msg}")


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


def _set_env_var(key: str, value: str):
    """Set KEY=value in the bot's .env, in place. Replaces an existing line
    (commented or not), else appends. Other lines (incl. secrets) untouched."""
    import re
    env = _REPO_ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    pat = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    line = f"{key}={value}"
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = line
            break
    else:
        lines.append(line)
    env.write_text("\n".join(lines) + "\n")


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


def _enable_experiment(world_dir: Path):
    """Enable the Beta APIs experiment via the sibling enable_beta_apis module."""
    sys.path.insert(0, str(_PACK_DIR))
    import enable_beta_apis as eba
    amulet_nbt, CompoundTag, ByteTag = eba._load_amulet_nbt()
    level_dat = world_dir / "level.dat"
    if not level_dat.is_file():
        _fail(f"no level.dat in {world_dir}")
    raw = level_dat.read_bytes()
    new, before, after = eba.enable_experiments(
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
    ap.add_argument("--deaths-only", action="store_true",
                    help="install the pack but skip the Beta APIs experiment "
                         "(deaths work; chat needs the experiment)")
    ap.add_argument("--force", action="store_true",
                    help="skip the 'server not running' check before editing "
                         "level.dat (only if you have already stopped it)")
    ap.add_argument("--no-env", action="store_true",
                    help="don't touch .env (otherwise BEDROCK_SCRIPT_EVENTS, and "
                         "CHAT_RELAY unless --deaths-only, are set to true)")
    args = ap.parse_args()

    mc_dir, edition = _load_env()
    if edition != "bedrock":
        _fail(f"SERVER_EDITION is '{edition}', not 'bedrock' — this pack is "
              "Bedrock-only")

    # Level name comes from server.properties (get_level_name), so no path arg.
    sys.path.insert(0, str(_REPO_ROOT))
    from config import get_level_name
    level_name = get_level_name(mc_dir)
    world_dir = mc_dir / "worlds" / level_name
    if not world_dir.is_dir():
        _fail(f"world directory not found: {world_dir} "
              f"(level-name from server.properties: '{level_name}')")

    uuid, version = _read_manifest()
    print(f"Server:  {mc_dir}")
    print(f"World:   {world_dir}")

    dest = _copy_pack(mc_dir)
    print(f"Copied:  {dest}")

    added = _activate(world_dir, uuid, version)
    print(f"Activated in world_behavior_packs.json "
          f"({'added' if added else 'already present'})")

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
            import bedrock_player
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

    print("\nDone. Next:")
    print("  1. server.properties: content-log-console-output-enabled=true")
    print("  2. Start the server, then restart the bot.")


if __name__ == "__main__":
    main()
