"""Interactive CLI tool for restoring Minecraft server backups.

Scans the backup directory for full and incremental backups, displays
available restore points, and reconstructs the server state by applying
the full backup followed by incremental backups in order.

Usage:
    python restore.py [--backup-dir PATH] [--target-dir PATH] [--dry-run]
"""

import argparse
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

from dotenv import load_dotenv

# Load .env for defaults
load_dotenv(Path(__file__).parent / ".env")

RE_FULL = re.compile(r'^(.+?)_(\d{8}_\d{6})\.zip$')
RE_INCR = re.compile(r'^(.+?)_incr_(\d{8}_\d{6})\.zip$')


def parse_timestamp(ts: str) -> str:
    """Format '20260401_040000' as '2026-04-01 04:00:00'."""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"


def scan_backups(backup_dir: Path) -> list:
    """Scan backup directory and return sorted list of backup entries."""
    entries = []
    for f in backup_dir.iterdir():
        if not f.is_file() or not f.suffix == ".zip":
            continue
        m = RE_INCR.match(f.name)
        if m:
            entries.append({
                "path": f,
                "type": "incremental",
                "timestamp": m.group(2),
                "server": m.group(1),
                "size": f.stat().st_size,
            })
            continue
        m = RE_FULL.match(f.name)
        if m:
            entries.append({
                "path": f,
                "type": "full",
                "timestamp": m.group(2),
                "server": m.group(1),
                "size": f.stat().st_size,
            })
    entries.sort(key=lambda e: e["timestamp"])
    return entries


def group_by_chain(entries: list) -> list:
    """Group entries into chains, each starting with a full backup."""
    chains = []
    current = None
    for e in entries:
        if e["type"] == "full":
            current = {"full": e, "incrementals": []}
            chains.append(current)
        elif current is not None:
            current["incrementals"].append(e)
        # Skip incrementals before any full backup
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
        print(f"\n  {num:3d}. [FULL] {parse_timestamp(full['timestamp'])}  "
              f"({format_size(full['size'])})")
        points.append((ci, -1))  # -1 means restore to full only
        num += 1

        for ii, incr in enumerate(chain["incrementals"]):
            print(f"  {num:3d}.   └─ [INCR] {parse_timestamp(incr['timestamp'])}  "
                  f"({format_size(incr['size'])})")
            points.append((ci, ii))
            num += 1

    return points


def restore(chains: list, chain_idx: int, point_idx: int,
            target_dir: Path, dry_run: bool = False) -> None:
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

        # Show what the full backup contains
        with zipfile.ZipFile(full_zip, "r") as zf:
            print(f"\n  Full backup contains {len(zf.namelist())} files")

        for incr in incrementals:
            with zipfile.ZipFile(incr["path"], "r") as zf:
                names = zf.namelist()
                file_count = len([n for n in names if n != "_deletions.json"])
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
                if name == "_deletions.json":
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
                        # Clean up empty parent directories
                        parent = del_path.parent
                        while parent != target_dir and not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent

        print(f"  Applied ({len([n for n in zf.namelist() if n != '_deletions.json'])} files)")

    # Rebuild backup_manifest.json so the bot's incremental backups
    # use the restored state as the baseline
    manifest_path = Path(__file__).parent / "backup_manifest.json"
    backup_dir = Path(os.path.expanduser(
        os.environ.get("BACKUP_DIR", "~/minecraft_backup"))).resolve()
    print("Rebuilding backup manifest...")
    manifest = {}
    for dirpath, _dirnames, filenames in os.walk(target_dir):
        dp = Path(dirpath)
        # Skip BACKUP_DIR if it's inside the server directory
        try:
            dp.resolve().relative_to(backup_dir)
            continue
        except ValueError:
            pass
        for fn in filenames:
            fp = dp / fn
            try:
                rel = str(fp.relative_to(target_dir)).replace("\\", "/")
                manifest[rel] = fp.stat().st_mtime
            except OSError:
                pass
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)
    print(f"  Manifest rebuilt with {len(manifest)} files.")

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

    entries = scan_backups(backup_dir)
    if not entries:
        print(f"No backup files found in {backup_dir}")
        sys.exit(1)

    chains = group_by_chain(entries)
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

    restore(chains, chain_idx, point_idx, target_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
