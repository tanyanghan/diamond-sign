"""Interactive CLI tool for restoring Minecraft server backups.

Scans the backup directory for full and incremental backups, groups
incrementals by chain ID, and reconstructs the server state by applying
the full backup followed by the correct incremental chain in order.

Usage:
    python restore.py [--backup-dir PATH] [--target-dir PATH] [--dry-run]
"""

import argparse
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from dotenv import load_dotenv

from backup_utils import (
    CHAIN_MARKER_NAME, META_FILES, RE_FULL, RE_INCR,
    build_file_manifest, new_chain_id,
)

# Load .env for defaults
load_dotenv(Path(__file__).parent / ".env")


def parse_timestamp(ts: str) -> str:
    """Format '20260401_040000' as '2026-04-01 04:00:00'."""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"


def scan_backups(backup_dir: Path) -> tuple:
    """Scan backup directory. Returns (full_backups, incremental_backups)."""
    fulls = {}   # filename -> entry
    incrs = []   # list of entries

    for f in backup_dir.iterdir():
        if not f.is_file() or f.suffix != ".zip":
            continue
        m = RE_INCR.match(f.name)
        if m:
            incrs.append({
                "path": f,
                "server": m.group(1),
                "chain_id": m.group(2),
                "timestamp": m.group(3),
                "size": f.stat().st_size,
            })
            continue
        m = RE_FULL.match(f.name)
        if m:
            fulls[f.name] = {
                "path": f,
                "server": m.group(1),
                "timestamp": m.group(2),
                "size": f.stat().st_size,
            }

    return fulls, incrs


def read_incr_meta(zip_path: Path) -> dict:
    """Read _meta.json from an incremental zip. Returns {} on failure."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if "_meta.json" in zf.namelist():
                return json.loads(zf.read("_meta.json"))
    except Exception:
        pass
    return {}


def group_by_chain(fulls: dict, incrs: list) -> list:
    """Group incrementals by chain ID, resolve base full backup for each chain."""
    # Group incrementals by chain_id
    chain_map = {}  # chain_id -> list of incr entries
    for incr in incrs:
        chain_map.setdefault(incr["chain_id"], []).append(incr)

    # Sort each chain's incrementals by timestamp
    for chain_id in chain_map:
        chain_map[chain_id].sort(key=lambda e: e["timestamp"])

    chains = []
    for chain_id, chain_incrs in chain_map.items():
        # Read _meta.json from first incremental to find base_full
        meta = read_incr_meta(chain_incrs[0]["path"])
        base_full_name = meta.get("base_full", "")
        full_entry = fulls.get(base_full_name)

        if not full_entry:
            # Can't find the base full backup — skip this chain
            continue

        chains.append({
            "chain_id": chain_id,
            "full": full_entry,
            "incrementals": chain_incrs,
        })

    # Also add standalone full backups (those not referenced by any chain)
    referenced_fulls = {c["full"]["path"].name for c in chains}
    for name, entry in fulls.items():
        if name not in referenced_fulls:
            chains.append({
                "chain_id": None,
                "full": entry,
                "incrementals": [],
            })

    # Sort chains by full backup timestamp, then by first incremental timestamp
    def chain_sort_key(c):
        ts = c["full"]["timestamp"]
        if c["incrementals"]:
            return (ts, c["incrementals"][0]["timestamp"])
        return (ts, "")

    chains.sort(key=chain_sort_key)
    return chains


def format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.1f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.1f} MB"
    return f"{size_bytes / 1024:.1f} KB"


def display_restore_points(chains: list) -> list:
    """Display numbered restore points and return flat list of (chain_idx, point_idx) tuples."""
    points = []
    num = 1

    for ci, chain in enumerate(chains):
        full = chain["full"]
        chain_id = chain["chain_id"]
        header = f"Chain {chain_id}" if chain_id else "Standalone"
        print(f"\n  {header} (from {full['path'].name})")

        print(f"  {num:3d}. [FULL] {parse_timestamp(full['timestamp'])}  "
              f"({format_size(full['size'])})")
        points.append((ci, -1))
        num += 1

        for ii, incr in enumerate(chain["incrementals"]):
            print(f"  {num:3d}.   └─ [INCR] {parse_timestamp(incr['timestamp'])}  "
                  f"({format_size(incr['size'])})")
            points.append((ci, ii))
            num += 1

    return points



def restore(chains: list, chain_idx: int, point_idx: int,
            target_dir: Path, backup_dir: Path, dry_run: bool = False) -> None:
    """Restore from a full backup + incremental chain up to point_idx."""
    chain = chains[chain_idx]
    full_zip = chain["full"]["path"]

    # Determine which incrementals to apply
    if point_idx == -1:
        incrementals = []
    else:
        incrementals = chain["incrementals"][:point_idx + 1]

    print(f"\nRestore plan:")
    print(f"  1. Extract full backup: {full_zip.name}")
    for i, incr in enumerate(incrementals, 2):
        print(f"  {i}. Apply incremental: {incr['path'].name}")

    print(f"  Target directory: {target_dir}")

    if dry_run:
        print("\n[DRY RUN] No files will be written.")

        with zipfile.ZipFile(full_zip, "r") as zf:
            print(f"\n  Full backup contains {len(zf.namelist())} files")

        for incr in incrementals:
            with zipfile.ZipFile(incr["path"], "r") as zf:
                names = zf.namelist()
                file_count = len([n for n in names if n not in META_FILES])
                has_deletions = "_deletions.json" in names
                print(f"  Incremental {incr['path'].name}: "
                      f"{file_count} changed files", end="")
                if has_deletions:
                    deletions = json.loads(zf.read("_deletions.json"))
                    print(f", {len(deletions)} deletions", end="")
                print()
        return

    # Confirm target directory
    if target_dir.exists() and any(target_dir.iterdir()):
        resp = input(f"\nTarget directory '{target_dir}' already exists and is not empty.\n"
                     f"Overwrite? (yes/no): ").strip().lower()
        if resp != "yes":
            print("Restore cancelled.")
            return
        print("Clearing target directory...")
        shutil.rmtree(target_dir)

    target_dir.mkdir(parents=True, exist_ok=True)

    # Extract full backup
    print(f"Extracting full backup: {full_zip.name} ...")
    with zipfile.ZipFile(full_zip, "r") as zf:
        zf.extractall(target_dir)
    print(f"  Full backup extracted.")

    # Apply incrementals in order
    for incr in incrementals:
        incr_path = incr["path"]
        print(f"Applying incremental: {incr_path.name} ...")
        with zipfile.ZipFile(incr_path, "r") as zf:
            # Apply changed/added files
            for name in zf.namelist():
                if name in META_FILES:
                    continue
                dest = target_dir / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)

            # Apply deletions
            if "_deletions.json" in zf.namelist():
                deletions = json.loads(zf.read("_deletions.json"))
                for rel_path in deletions:
                    del_path = target_dir / rel_path
                    if del_path.exists():
                        del_path.unlink()
                        parent = del_path.parent
                        while parent != target_dir and not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent

        file_count = len([n for n in zf.namelist() if n not in META_FILES])
        print(f"  Applied ({file_count} files)")

    # Rebuild backup_manifest.json
    manifest_path = Path(__file__).parent / "backup_manifest.json"
    print("Rebuilding backup manifest...")
    files = build_file_manifest(target_dir, backup_dir)
    marker_path = target_dir / CHAIN_MARKER_NAME

    if point_idx == -1:
        # Restoring to full backup only — chain is self-contained
        chain_id = new_chain_id(backup_dir)
        with open(manifest_path, "w") as f:
            json.dump({"chain_id": chain_id, "base_full": full_zip.name,
                        "files": files}, f)
        with open(marker_path, "w") as f:
            f.write(chain_id)
        print(f"  Manifest rebuilt with {len(files)} files (new chain: {chain_id})")
        print(f"  Chain marker written.")
    else:
        # Restoring to incremental point — the original full backup alone
        # is NOT sufficient to reconstruct this state. Invalidate the chain
        # so the bot is forced to take a new full backup before incrementals
        # can resume.
        with open(manifest_path, "w") as f:
            json.dump({"chain_id": "", "base_full": "", "files": files}, f)
        if marker_path.exists():
            marker_path.unlink()
        print(f"  Manifest rebuilt with {len(files)} files")
        print(f"  No chain established — a full backup is required before")
        print(f"  incremental backups can resume.")

    print(f"\nRestore complete. Server files are in: {target_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="Restore Minecraft server from full + incremental backup chain")
    parser.add_argument("--backup-dir", type=str,
                        default=os.path.expanduser(
                            os.environ.get("BACKUP_DIR", "~/minecraft_backup")),
                        help="Directory containing backup zip files")
    parser.add_argument("--target-dir", type=str, default=None,
                        help="Directory to restore into (default: MINECRAFT_DIR from .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview restore without writing files")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir)
    if not backup_dir.exists():
        print(f"Error: Backup directory not found: {backup_dir}")
        sys.exit(1)

    target_dir = Path(args.target_dir) if args.target_dir else None
    if target_dir is None:
        mc_dir = os.environ.get("MINECRAFT_DIR")
        if mc_dir:
            target_dir = Path(mc_dir)
        else:
            print("Error: No --target-dir specified and MINECRAFT_DIR not set in .env")
            sys.exit(1)

    fulls, incrs = scan_backups(backup_dir)
    if not fulls and not incrs:
        print(f"No backup files found in {backup_dir}")
        sys.exit(1)

    chains = group_by_chain(fulls, incrs)
    if not chains:
        print("No full backups found. Cannot restore from incremental backups alone.")
        sys.exit(1)

    print("=" * 60)
    print("  Minecraft Server Backup Restore Tool")
    print("=" * 60)
    print(f"\nBackup directory: {backup_dir}")
    print(f"Target directory: {target_dir}")
    print(f"\nAvailable restore points:")

    points = display_restore_points(chains)

    print(f"\n  Enter a number (1-{len(points)}) to select a restore point, or 'q' to quit.")
    choice = input("\n  Selection: ").strip()

    if choice.lower() == "q":
        print("Cancelled.")
        return

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(points):
            raise ValueError
    except ValueError:
        print("Invalid selection.")
        sys.exit(1)

    chain_idx, point_idx = points[idx]

    if not args.dry_run:
        # Server offline warning
        print("\n" + "!" * 60)
        print("  WARNING: The Minecraft server MUST be stopped before")
        print("  restoring a backup. Restoring while the server is")
        print("  running will cause data corruption!")
        print("!" * 60)
        confirm = input("\n  Is the Minecraft server stopped? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("\nPlease stop the Minecraft server first, then run this tool again.")
            return

    restore(chains, chain_idx, point_idx, target_dir, backup_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
