"""Interactive CLI tool for restoring Minecraft server backups.

Restoration Strategy
====================

Backups are organised into "chains": one full backup + zero or more incremental
backups that build on it. To restore to a specific point in time:

  1. Extract the chain's full backup into the target directory.
  2. Apply each incremental in chronological order up to the selected point,
     overwriting changed files and removing deleted ones.

In-place vs out-of-place restore
--------------------------------
The chain-management work (new chain ID, merged incremental, marker file,
manifest update) only runs when the restore is **in-place** — that is, when
``target_dir`` resolves to the same path as the bot's ``MINECRAFT_DIR``.

  - In-place (target_dir == MINECRAFT_DIR):
    A new chain is established so the bot can resume incremental backups
    without requiring a fresh full backup.
      * Restoring to a FULL point: a new chain ID is generated referencing
        the original full backup; no additional zip is created.
      * Restoring to an INCREMENTAL point: a single "merged incremental" zip
        is created combining all applied incrementals.  Original full +
        merged incremental fully reconstruct the restored state.  The merged
        incremental reuses the selected restore point's timestamp.
    The .mcnotifier_chain marker is written into target_dir and the bot's
    backup_manifest.json is refreshed.

  - Out-of-place (target_dir != MINECRAFT_DIR):
    Files are extracted only.  No new chain is established, no merged
    incremental zip is written, no marker file is created, and the bot's
    backup_manifest.json is left untouched — the bot continues tracking
    MINECRAFT_DIR's existing chain.  If the user later promotes target_dir
    by replacing MINECRAFT_DIR's contents with it, the absent
    .mcnotifier_chain forces the bot to take a fresh full backup, which
    cleanly starts a new chain on the live server.

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
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
import curses
from dotenv import load_dotenv

from backup_utils import (
    CHAIN_MARKER_NAME, META_FILES, RE_FULL, RE_INCR,
    build_file_manifest, new_chain_id, run_copy_command,
)
from config import backup_exclude_names

# Load .env for defaults (BACKUP_DIR, MINECRAFT_DIR)
load_dotenv(Path(__file__).parent / ".env")


def parse_timestamp(ts: str) -> str:
    """Format '20260401_040000' as '2026-04-01 04:00:00'."""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"


def _apply_zip_mode(info: zipfile.ZipInfo, path: Path) -> None:
    """Reapply the Unix mode stored in a zip entry to the extracted file.

    Python's zipfile records the source file's mode in external_attr on write
    but does NOT restore it on extract, so executables (e.g. the Bedrock
    ``bedrock_server`` binary) come out non-executable. Reapply it here.
    """
    mode = info.external_attr >> 16
    if mode:
        try:
            os.chmod(path, stat.S_IMODE(mode))
        except OSError:
            pass


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


def _build_display(chains: list) -> tuple:
    """Build display lines and points list in reverse chronological order.

    Returns (display_lines, points) where:
      - display_lines: list of (is_selectable, text, point_idx) tuples.
        Separator lines have is_selectable=False and point_idx=None.
      - points: flat list of (chain_idx, point_idx) tuples.  point_idx is
        an index into this list, stored in the display_lines tuple so the
        curses selector can return it directly.

    Chains are ordered latest-first. Within each chain, incrementals appear
    in reverse chronological order followed by the full backup.
    """
    points = []
    display_lines = []
    num = 1

    for rev_idx, ci in enumerate(reversed(range(len(chains)))):
        chain = chains[ci]
        full = chain["full"]
        chain_id = chain["chain_id"]
        chain_label = f"Chain {chain_id}" if chain_id else "Standalone"

        if rev_idx > 0:
            display_lines.append((False, "", None))
        display_lines.append(
            (False, f"  {chain_label} (from {full['path'].name})", None))

        # Incrementals in reverse chronological order (latest first)
        for ii in reversed(range(len(chain["incrementals"]))):
            incr = chain["incrementals"][ii]
            label = (f"  {num:3d}.  [INCR] {parse_timestamp(incr['timestamp'])}"
                     f"  ({format_size(incr['size'])})")
            display_lines.append((True, label, len(points)))
            points.append((ci, ii))
            num += 1

        # Full backup shown last within its chain
        label = (f"  {num:3d}.  [FULL] {parse_timestamp(full['timestamp'])}"
                 f"  ({format_size(full['size'])})")
        display_lines.append((True, label, len(points)))
        points.append((ci, -1))
        num += 1

    return display_lines, points


def _curses_select(stdscr, header_text: str, display_lines: list,
                   selectable_indices: list) -> int | None:
    """Interactive curses-based paginated selector with highlight.

    Navigation:
        Up / Down       — move highlight between selectable restore points
        Right / n       — next page  (highlight → first item on new page)
        Left  / p       — previous page
        Enter           — select the highlighted item (or submit typed number)
        0-9             — accumulate a number; Enter jumps to that item
        Backspace       — delete last digit from number buffer
        Esc             — clear number buffer
        q               — cancel

    Returns the point_idx stored in the selected display_lines entry, or None.
    """
    curses.curs_set(0)
    curses.use_default_colors()

    header_lines = header_text.splitlines() if header_text else []

    def recalc():
        """Recompute layout after a terminal resize."""
        my, mx = stdscr.getmaxyx()
        ps = max(3, my - len(header_lines) - 2)  # -2: blank + status bar
        tp = max(1, (len(display_lines) + ps - 1) // ps)
        return my, mx, ps, tp

    max_y, max_x, page_size, total_pages = recalc()

    hi = 0 if selectable_indices else -1
    page = (selectable_indices[0] // page_size) if selectable_indices else 0
    number_buf = ""

    def first_sel_on_page(p):
        lo, hi_bound = p * page_size, (p + 1) * page_size
        for i, di in enumerate(selectable_indices):
            if lo <= di < hi_bound:
                return i
        return None

    while True:
        stdscr.erase()
        row = 0

        # -- header (fixed at top of every page) --
        for hl in header_lines:
            if row >= max_y:
                break
            try:
                stdscr.addnstr(row, 0, hl, max_x - 1)
            except curses.error:
                pass
            row += 1

        # -- current page of display lines --
        start = page * page_size
        end = min(start + page_size, len(display_lines))
        for di in range(start, end):
            if row >= max_y - 1:
                break
            _sel, text, _pidx = display_lines[di]
            try:
                if _sel and hi >= 0 and di == selectable_indices[hi]:
                    stdscr.addnstr(row, 0, text.ljust(max_x - 1),
                                   max_x - 1, curses.A_REVERSE)
                else:
                    stdscr.addnstr(row, 0, text, max_x - 1)
            except curses.error:
                pass
            row += 1

        # -- status bar (last row) --
        is_last = end >= len(display_lines)
        nav = []
        if selectable_indices:
            nav.append("Up/Dn highlight")
        if page > 0:
            nav.append("Left/p prev")
        if not is_last:
            nav.append("Right/n next")
        nav.append("Enter select")
        nav.append("# jump")
        nav.append("q quit")
        page_str = f"Page {page + 1}/{total_pages}"
        nav_str = "  ".join(nav)
        if number_buf:
            status = f"  {page_str}  |  Select: {number_buf}_  ({nav_str})"
        else:
            status = f"  {page_str}  |  {nav_str}"
        try:
            stdscr.addnstr(max_y - 1, 0, status, max_x - 1, curses.A_BOLD)
        except curses.error:
            pass

        stdscr.refresh()
        key = stdscr.getch()

        # -- handle input --
        if key == ord('q'):
            return None

        elif key == curses.KEY_UP:
            number_buf = ""
            if selectable_indices and hi > 0:
                hi -= 1
                page = selectable_indices[hi] // page_size

        elif key == curses.KEY_DOWN:
            number_buf = ""
            if selectable_indices and hi < len(selectable_indices) - 1:
                hi += 1
                page = selectable_indices[hi] // page_size

        elif key in (curses.KEY_RIGHT, ord('n'), ord('N')):
            number_buf = ""
            if page < total_pages - 1:
                page += 1
                fp = first_sel_on_page(page) if selectable_indices else None
                if fp is not None:
                    hi = fp

        elif key in (curses.KEY_LEFT, ord('p'), ord('P'), ord('b'), ord('B')):
            number_buf = ""
            if page > 0:
                page -= 1
                fp = first_sel_on_page(page) if selectable_indices else None
                if fp is not None:
                    hi = fp

        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            if number_buf:
                try:
                    n = int(number_buf) - 1
                    if 0 <= n < len(selectable_indices):
                        return display_lines[selectable_indices[n]][2]
                except ValueError:
                    pass
                number_buf = ""
            elif selectable_indices and hi >= 0:
                return display_lines[selectable_indices[hi]][2]

        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if number_buf:
                number_buf = number_buf[:-1]

        elif key == 27:  # Escape
            number_buf = ""

        elif 48 <= key <= 57:  # digits 0-9
            number_buf += chr(key)

        elif key == curses.KEY_RESIZE:
            max_y, max_x, page_size, total_pages = recalc()
            page = min(page, total_pages - 1)


def display_restore_points(chains: list, header: str | None = None) -> tuple:
    """Display restore points interactively with curses and return the selection.

    Returns (points, selected_idx) where:
      - points: flat list of (chain_idx, point_idx) tuples
      - selected_idx: index into points chosen by the user, or None if cancelled

    point_idx == -1 selects the full backup only; >= 0 selects the
    incremental at that index within the chain.
    """
    display_lines, points = _build_display(chains)
    selectable_indices = [
        i for i, (is_sel, _, _) in enumerate(display_lines) if is_sel]

    def _run(stdscr):
        return _curses_select(stdscr, header or "", display_lines,
                              selectable_indices)

    try:
        selected = curses.wrapper(_run)
    except KeyboardInterrupt:
        selected = None

    return points, selected


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

    # Step 1: Extract the full backup as the base state. Extract per-member so
    # the stored Unix mode can be reapplied (extractall drops it, which would
    # leave the Bedrock server binary non-executable).
    print(f"Extracting full backup: {full_zip.name} ...")
    with zipfile.ZipFile(full_zip, "r") as zf:
        for info in zf.infolist():
            extracted = zf.extract(info, target_dir)
            _apply_zip_mode(info, Path(extracted))
    print(f"  Full backup extracted.")

    # Determine whether this is an in-place restore (overwriting the live
    # MINECRAFT_DIR the bot tracks) or an out-of-place restore (any other
    # target).  Chain-management work — new chain ID, merged incremental,
    # marker file, manifest update — only makes sense in-place.  Out-of-place
    # restores just extract the files; the bot keeps tracking MINECRAFT_DIR
    # with its existing chain.  If the user later replaces MINECRAFT_DIR with
    # the target_dir contents, the absence of a marker forces the bot to
    # take a fresh full backup, which is exactly what we want.
    mc_dir_env = os.environ.get("MINECRAFT_DIR", "")
    in_place = bool(mc_dir_env) and Path(mc_dir_env).resolve() == target_dir.resolve()

    # Step 2: Apply incrementals in chronological order.
    #
    # In-place: also accumulate all changed files in a temp directory so we
    # can build a single "merged incremental" zip after the loop.  This makes
    # the new chain self-contained — original full + one merged incremental
    # reconstructs the restored state without needing the original individual
    # incrementals.
    #
    # Out-of-place: just apply each incremental to target_dir and move on.
    # No new chain is established for this target.
    tmp = Path(tempfile.mkdtemp()) if (incrementals and in_place) else None
    merged_deletions: list = []
    re_added: set = set()

    try:
        for incr in incrementals:
            incr_path = incr["path"]
            print(f"Applying incremental: {incr_path.name} ...")
            with zipfile.ZipFile(incr_path, "r") as zf:
                file_count = 0
                dest_roots = (target_dir, tmp) if tmp is not None else (target_dir,)
                for name in zf.namelist():
                    if name in META_FILES:
                        continue
                    info = zf.getinfo(name)
                    for dest_root in dest_roots:
                        dest = dest_root / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        _apply_zip_mode(info, dest)
                    if tmp is not None:
                        re_added.add(name)
                    file_count += 1

                # Apply deletions to target_dir (and track them for the
                # merged incremental, when in-place).
                if "_deletions.json" in zf.namelist():
                    deletions = json.loads(zf.read("_deletions.json"))
                    for rel_path in deletions:
                        if tmp is not None:
                            merged_deletions.append(rel_path)
                            re_added.discard(rel_path)
                        del_path = target_dir / rel_path
                        if del_path.exists():
                            del_path.unlink()
                            parent = del_path.parent
                            while parent != target_dir and not any(parent.iterdir()):
                                parent.rmdir()
                                parent = parent.parent

            print(f"  Applied ({file_count} files)")

        # Step 3 (in-place only): create merged incremental + marker + manifest.
        if in_place:
            if tmp is not None:
                # Merged deletions = deleted-and-not-re-added later
                final_deletions = [p for p in merged_deletions if p not in re_added]
                chain_id = new_chain_id(backup_dir)
                restore_ts = incrementals[point_idx]["timestamp"]
                server_name = chain["full"]["server"]
                merged_name = f"{server_name}_incr_{chain_id}_{restore_ts}.zip"
                merged_path = backup_dir / merged_name

                print(f"Creating merged incremental: {merged_name} ...")
                with zipfile.ZipFile(merged_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for dirpath, _dirnames, filenames in os.walk(tmp):
                        dp = Path(dirpath)
                        for fn in filenames:
                            fp = dp / fn
                            rel = str(fp.relative_to(tmp)).replace("\\", "/")
                            zf.write(fp, rel)
                    if final_deletions:
                        zf.writestr("_deletions.json",
                                    json.dumps(final_deletions, indent=2))
                    zf.writestr("_meta.json", json.dumps({
                        "chain_id": chain_id, "base_full": full_zip.name}))

                print(f"  Merged incremental created "
                      f"({format_size(merged_path.stat().st_size)})")
                run_copy_command(merged_path, log_fn=print)
            else:
                # Restoring to a full-only point: no incrementals applied,
                # just give the existing full backup a new chain ID.
                chain_id = new_chain_id(backup_dir)

            marker_path = target_dir / CHAIN_MARKER_NAME
            with open(marker_path, "w") as f:
                f.write(chain_id)

            manifest_path = Path(__file__).parent / "backup_manifest.json"
            print("Rebuilding backup manifest...")
            files = build_file_manifest(target_dir, backup_dir,
                                        backup_exclude_names())
            with open(manifest_path, "w") as f:
                json.dump({"chain_id": chain_id, "base_full": full_zip.name,
                            "files": files}, f)
            print(f"  Manifest rebuilt with {len(files)} files "
                  f"(new chain: {chain_id})")
            print(f"  Chain marker written to {marker_path}")
        else:
            print(f"\n  NOTE: Restored to {target_dir}, which is not the bot's")
            print(f"        MINECRAFT_DIR ({mc_dir_env or 'unset'}).")
            print(f"        No new chain was established and the bot's")
            print(f"        backup_manifest.json was not modified — the bot")
            print(f"        continues tracking MINECRAFT_DIR's existing chain.")
            print(f"        If you later replace MINECRAFT_DIR with this")
            print(f"        target_dir, the missing .mcnotifier_chain will")
            print(f"        force a fresh full backup, starting a new chain.")

    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

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

    header = "\n".join([
        "=" * 60,
        "  Minecraft Server Backup Restore Tool",
        "=" * 60,
        f"  Backup directory: {backup_dir}",
        f"  Target directory: {target_dir}",
        "",
        "  Available restore points:",
    ])

    points, selected_idx = display_restore_points(chains, header=header)

    if selected_idx is None:
        print("Cancelled.")
        return

    chain_idx, point_idx = points[selected_idx]

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
