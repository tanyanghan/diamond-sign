#!/usr/bin/env python3
"""Enable the Bedrock "Beta APIs" experiment by editing a world's level.dat.

The mcnotifier events behavior pack needs the **Beta APIs** experiment for chat
capture (deaths work without it). Normally you'd toggle this in the game client;
this script does it directly on a dedicated-server world so you don't need the
client.

The experiment toggle lives in level.dat (a little-endian NBT file with an
8-byte header), NOT in the LevelDB — so this uses amulet-nbt (install via
requirements-bedrock-restore.txt), not amulet-leveldb.

IMPORTANT
  - Stop the server before running (level.dat is rewritten).
  - Enabling an experiment is IRREVERSIBLE for that world and disables
    achievements (moot on a dedicated server). A .bak copy is made first.

Usage:
    python enable_beta_apis.py <path-to-level.dat | world-dir>
    python enable_beta_apis.py worlds/"Bedrock level"
    python enable_beta_apis.py --keys beta_api,gametest level.dat   # try both

The experiment NBT key has been renamed across versions ("gametest" historically,
"beta_api" on current versions). The default sets "beta_api"; if the behavior
pack's chat events don't fire afterwards, rerun with --keys beta_api,gametest.
"""

import argparse
import struct
import sys
from datetime import datetime
from pathlib import Path


def _load_amulet_nbt():
    try:
        import amulet_nbt
        from amulet_nbt import CompoundTag, ByteTag
        return amulet_nbt, CompoundTag, ByteTag
    except Exception as e:
        sys.exit("amulet-nbt is required. Install it with:\n"
                 "  pip install -r requirements-bedrock-restore.txt\n"
                 f"(import failed: {e})")


def _resolve_level_dat(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        cand = p / "level.dat"
        if cand.is_file():
            return cand
        sys.exit(f"No level.dat found in directory: {p}")
    if p.is_file():
        return p
    sys.exit(f"Not found: {p}")


def enable_experiments(raw: bytes, keys, CompoundTag, ByteTag, amulet_nbt):
    """Return (new_bytes, before_dict, after_dict)."""
    storage_version = struct.unpack_from("<i", raw, 0)[0]
    nt = amulet_nbt.load(raw[8:], little_endian=True, compressed=False)
    comp = nt.compound

    exp = comp.get("experiments")
    if not isinstance(exp, CompoundTag):
        exp = CompoundTag()
        comp["experiments"] = exp
    before = {k: int(exp[k]) for k in exp}

    for k in keys:
        exp[k] = ByteTag(1)
    exp["experiments_ever_used"] = ByteTag(1)
    exp["saved_with_toggled_experiments"] = ByteTag(1)
    after = {k: int(exp[k]) for k in exp}

    body = nt.to_nbt(compressed=False, little_endian=True)
    new = struct.pack("<i", storage_version) + struct.pack("<i", len(body)) + body
    return new, before, after


def main():
    ap = argparse.ArgumentParser(description="Enable Bedrock Beta APIs in level.dat")
    ap.add_argument("path", help="path to level.dat, or the world directory")
    ap.add_argument("--keys", default="beta_api,gametest",
                    help="comma-separated experiment keys to enable. Default sets "
                         "both known names for the Beta APIs experiment "
                         "(gametest is the one current versions honor; it shows "
                         "as 'gtst' in the server's active-experiments line).")
    ap.add_argument("--no-backup", action="store_true",
                    help="don't write a .bak copy (not recommended)")
    args = ap.parse_args()

    amulet_nbt, CompoundTag, ByteTag = _load_amulet_nbt()
    level_dat = _resolve_level_dat(args.path)
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]

    raw = level_dat.read_bytes()
    new, before, after = enable_experiments(raw, keys, CompoundTag, ByteTag, amulet_nbt)

    print(f"level.dat: {level_dat}  ({len(raw)} -> {len(new)} bytes)")
    print(f"experiments before: {before or '(none)'}")
    print(f"experiments after:  {after}")
    if new == raw:
        print("Already enabled — nothing to do.")
        return

    if not args.no_backup:
        bak = level_dat.with_name(
            f"level.dat.bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        bak.write_bytes(raw)
        print(f"Backup written: {bak.name}")

    tmp = level_dat.with_name("level.dat.tmp")
    tmp.write_bytes(new)
    tmp.replace(level_dat)
    print("Done. Restart the server (it must have been stopped for this).")
    print("Note: enabling an experiment is irreversible for this world.")


if __name__ == "__main__":
    main()
