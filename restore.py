"""Interactive CLI tool for restoring Minecraft server backups.

Restoration Strategy
====================

Backups are organised into "chains": one full backup + zero or more incremental
backups that build on it. To restore to a specific point in time:

  1. Extract the chain's full backup into the target directory.
  2. Apply each incremental in chronological order up to the selected point,
     overwriting changed files and removing deleted ones.

After restoration, a new chain is established so the bot can immediately resume
incremental backups without requiring a new full backup. How the new chain is
created depends on the restore point:

  - Restoring to a FULL backup point:
    A new chain ID is generated referencing the original full backup. No
    additional files are created — the full backup is already self-contained.

  - Restoring to an INCREMENTAL point:
    The original full backup alone cannot reconstruct this state (it would need
    the incrementals replayed). To avoid forcing an expensive new full backup,
    we create a single "merged incremental" — a zip that combines all the
    applied incrementals into one file. This merged incremental + the original
    full backup = the complete restored state, making the new chain
    self-contained for future restores.

    The merged incremental is given a new chain ID and the same timestamp as the
    selected restore point. Future incrementals will chain off this new ID.

Chain Discovery
---------------
The restore tool is fully self-contained — it doesn't rely on backup_manifest.json
or .mcnotifier_chain to discover chains. Instead, it:
  - Parses full/incremental backup filenames from the backup directory
  - Reads _meta.json from inside each incremental zip to find its chain ID and
    which full backup it belongs to
  - Groups incrementals by chain ID and sorts them chronologically

Usage:
    python restore.py [--backup-dir PATH] [--target-dir PATH] [--dry-run]
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv

from backup_utils import (
    CHAIN_MARKER_NAME, META_FILES, RE_FULL, RE_INCR,
    build_file_manifest, new_chain_id,
)

# Load .env for defaults (BACKUP_DIR, MINECRAFT_DIR)
load_dotenv(Path(__file__).parent / ".env")


def parse_timestamp(ts: str) -> str:
    """Format '20260401_040000' as '2026-04-01 04:00:00'."""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"


def scan_backups(backup_dir: Path) -> tuple:
    """Scan backup directory for full and incremental backup zips.

    Returns (fulls, incrs) where:
      - fulls: dict of {filename: entry} for full backups
      - incrs: list of entries for incremental backups

    Each entry contains path, server name, timestamp, size, and (for
    incrementals) chain_id parsed from the filename.
    """
    fulls = {}   # filename -> entry
    incrs = []   # list of entries

    for f in backup_dir.iterdir():
        if not f.is_file() or f.suffix != ".zip":
            continue
        # Try incremental pattern first (it's more specific — a full backup
        # regex would also match incremental filenames without this ordering)
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
    """Read _meta.json from an incremental zip. Returns {} on failure.

    _meta.json contains {"chain_id": "...", "base_full": "..."} which tells
    us which chain this incremental belongs to and which full backup it
    builds upon. This makes the restore tool self-contained — it doesn't
    need external state files to reconstruct chains.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if "_meta.json" in zf.namelist():
                return json.loads(zf.read("_meta.json"))
    except Exception:
        pass
    return {}


def group_by_chain(fulls: dict, incrs: list) -> list:
    """Group incrementals by chain ID and resolve their base full backup.

    Returns a list of chain dicts, each containing:
      - chain_id: the 8-char hex ID (or None for standalone full backups)
      - full: the full backup entry this chain is based on
      - incrementals: sorted list of incremental entries in this chain

    Also includes standalone full backups (those with no incrementals) so
    they appear as restore points too.
    """
    # Group incrementals by chain_id
    chain_map = {}  # chain_id -> list of incr entries
    for incr in incrs:
        chain_map.setdefault(incr["chain_id"], []).append(incr)

    # Sort each chain's incrementals by timestamp (chronological order)
    for chain_id in chain_map:
        chain_map[chain_id].sort(key=lambda e: e["timestamp"])

    chains = []
    for chain_id, chain_incrs in chain_map.items():
        # Read _meta.json from the first incremental to find which full
        # backup this chain is based on
        meta = read_incr_meta(chain_incrs[0]["path"])
        base_full_name = meta.get("base_full", "")
        full_entry = fulls.get(base_full_name)

        if not full_entry:
            # Can't find the base full backup — skip this chain entirely.
            # This can happen if the full backup was deleted or moved.
            continue

        chains.append({
            "chain_id": chain_id,
            "full": full_entry,
            "incrementals": chain_incrs,
        })

    # Add standalone full backups (those not referenced by any chain)
    # so they still appear as restore points
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
    """Display numbered restore points and return flat list of
    (chain_idx, point_idx) tuples.

    point_idx == -1 means the full backup itself.
    point_idx >= 0 means the incremental at that index within the chain.
    """
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
    """Restore server state from a full backup + incremental chain.

    Args:
        chains:     List of chain dicts from group_by_chain()
        chain_idx:  Index of the selected chain
        point_idx:  -1 for full backup only, or index of incremental to restore to
        target_dir: Directory to restore into (the Minecraft server directory)
        backup_dir: Directory containing the backup zip files
        dry_run:    If True, only preview what would be restored
    """
    chain = chains[chain_idx]
    full_zip = chain["full"]["path"]

    # Determine which incrementals to apply (all up to and including point_idx)
    if point_idx == -1:
        incrementals = []
    else:
        incrementals = chain["incrementals"][:point_idx + 1]

    # Show the restore plan to the user
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

    # Safety: confirm before overwriting an existing directory
    if target_dir.exists() and any(target_dir.iterdir()):
        resp = input(f"\nTarget directory '{target_dir}' already exists and is not empty.\n"
                     f"Overwrite? (yes/no): ").strip().lower()
        if resp != "yes":
            print("Restore cancelled.")
            return
        print("Clearing target directory...")
        shutil.rmtree(target_dir)

    target_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Extract the full backup as the base state
    print(f"Extracting full backup: {full_zip.name} ...")
    with zipfile.ZipFile(full_zip, "r") as zf:
        zf.extractall(target_dir)
    print(f"  Full backup extracted.")

    # Step 2: Apply incrementals in chronological order
    #
    # If restoring to an incremental point, we also extract each incremental
    # into a temporary directory. This temp dir accumulates all changed files
    # across all applied incrementals (later files overwrite earlier ones),
    # producing a "merged delta" — the combined difference between the full
    # backup and the final restored state.
    #
    # Why? After restoration, we need to create a new chain so the bot can
    # resume incremental backups immediately. But the new chain's base_full
    # points to the original full backup, which alone can't reconstruct the
    # restored state (it would need the original incrementals replayed).
    #
    # Rather than creating an expensive new full backup, we zip the temp dir
    # as a single "merged incremental" — one file that, combined with the
    # original full backup, fully reconstructs the restored state. This makes
    # the new chain self-contained for future restores.
    if incrementals:
        tmp = Path(tempfile.mkdtemp())
        # Track deletions across all incrementals for merging
        merged_deletions = []   # all deleted paths, in order encountered
        re_added = set()        # paths that were re-added by a later incremental

        try:
            for incr in incrementals:
                incr_path = incr["path"]
                print(f"Applying incremental: {incr_path.name} ...")
                with zipfile.ZipFile(incr_path, "r") as zf:
                    file_count = 0
                    for name in zf.namelist():
                        if name in META_FILES:
                            continue
                        # Extract changed/added files to both:
                        # - target_dir: the actual restore destination
                        # - tmp: accumulates the merged delta for the merged incremental
                        for dest_root in (target_dir, tmp):
                            dest = dest_root / name
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(name) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                        # Track that this file was added/updated (not deleted)
                        re_added.add(name)
                        file_count += 1

                    # Process deletions: files that existed before but were
                    # removed during this incremental period
                    if "_deletions.json" in zf.namelist():
                        deletions = json.loads(zf.read("_deletions.json"))
                        for rel_path in deletions:
                            merged_deletions.append(rel_path)
                            # If a file was deleted, it's no longer "re-added"
                            # (it might have been added by an earlier incremental
                            # but then deleted by this one)
                            re_added.discard(rel_path)
                            # Apply the deletion to the target directory
                            del_path = target_dir / rel_path
                            if del_path.exists():
                                del_path.unlink()
                                # Clean up empty parent directories
                                parent = del_path.parent
                                while parent != target_dir and not any(parent.iterdir()):
                                    parent.rmdir()
                                    parent = parent.parent

                print(f"  Applied ({file_count} files)")

            # Build the merged deletion list for the merged incremental.
            # Only include paths that were deleted and NOT re-added by a later
            # incremental. Example: if incr1 deletes "a.txt" and incr2 re-adds
            # "a.txt", the merged incremental should contain a.txt (in the zip)
            # but NOT list it in _deletions.json.
            final_deletions = [p for p in merged_deletions if p not in re_added]

            # Create the merged incremental zip.
            # This single zip + the original full backup = the complete state
            # at the selected restore point. Future restores of this chain only
            # need these two files.
            chain_id = new_chain_id(backup_dir)
            # Reuse the timestamp from the selected restore point so the
            # merged incremental sorts correctly alongside other backups
            restore_ts = incrementals[point_idx]["timestamp"]
            server_name = chain["full"]["server"]
            merged_name = f"{server_name}_incr_{chain_id}_{restore_ts}.zip"
            merged_path = backup_dir / merged_name

            print(f"Creating merged incremental: {merged_name} ...")
            with zipfile.ZipFile(merged_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add all changed/added files from the temp dir
                for dirpath, _dirnames, filenames in os.walk(tmp):
                    dp = Path(dirpath)
                    for fn in filenames:
                        fp = dp / fn
                        rel = str(fp.relative_to(tmp)).replace("\\", "/")
                        zf.write(fp, rel)
                # Add merged deletions list
                if final_deletions:
                    zf.writestr("_deletions.json",
                                json.dumps(final_deletions, indent=2))
                # Add chain metadata so the restore tool can discover this
                # incremental's chain membership and base full backup
                zf.writestr("_meta.json", json.dumps({
                    "chain_id": chain_id, "base_full": full_zip.name}))

            print(f"  Merged incremental created ({format_size(merged_path.stat().st_size)})")

        finally:
            # Clean up the temporary directory
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        # Restoring to a full backup only — no incrementals to merge.
        # Just generate a new chain ID.
        chain_id = new_chain_id(backup_dir)

    # Step 3: Rebuild backup_manifest.json and write chain marker
    #
    # The manifest records the mtime of every file in the restored state.
    # The bot uses this as the baseline for detecting changes in the next
    # incremental backup.
    #
    # The chain marker (.mcnotifier_chain) tells the bot which chain is
    # active. On startup, the bot compares this against the manifest to
    # verify the server state hasn't been replaced behind its back.
    manifest_path = Path(__file__).parent / "backup_manifest.json"
    print("Rebuilding backup manifest...")
    files = build_file_manifest(target_dir, backup_dir)
    with open(manifest_path, "w") as f:
        json.dump({"chain_id": chain_id, "base_full": full_zip.name,
                    "files": files}, f)
    marker_path = target_dir / CHAIN_MARKER_NAME
    with open(marker_path, "w") as f:
        f.write(chain_id)
    print(f"  Manifest rebuilt with {len(files)} files (new chain: {chain_id})")
    print(f"  Chain marker written.")

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
        # Server offline warning — restoring while the server is running
        # will cause data corruption because the server holds region files
        # open and writes to them asynchronously
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
