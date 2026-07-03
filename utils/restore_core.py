"""Headless backup-restore core, shared by the bot (``/restore``) and the
standalone ``restore.py`` CLI.

Backups are organised into **chains**: one full backup + zero or more incremental
backups that build on it. Restoring to a point in time means: extract the chain's
full backup, then apply each incremental in order up to the selected point
(overwriting changed files, removing files listed in ``_deletions.json``).

This module is deliberately free of UI and environment access — no curses, no
``input()``, no ``os.environ``. Callers pass in the target/backup directories,
the per-server exclude set, the offsite copy command, and the manifest/marker
paths. ``restore.py`` wraps this with a curses selector + prompts; the bot wraps
it with a chat list/select/confirm flow and its own stop/restart orchestration.

Chain discovery is self-contained: it parses full/incremental filenames and reads
``_meta.json`` from inside each incremental zip (chain_id + base_full) — it does
not depend on ``backup_manifest.json`` or the chain marker.
"""

import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from .backup_utils import (
    CHAIN_MARKER_NAME, CHAIN_MARKER_NAME_LEGACY, META_FILES, RE_FULL, RE_INCR,
    build_file_manifest, new_chain_id, run_copy_command,
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def parse_timestamp(ts: str) -> str:
    """Format '20260401_040000' as '2026-04-01 04:00:00'."""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"


def format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.1f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    return f"{size_bytes / 1024:.1f} KB"


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


# ---------------------------------------------------------------------------
# Chain discovery
# ---------------------------------------------------------------------------
def scan_backups(backup_dir: Path) -> tuple:
    """Scan ``backup_dir`` for full and incremental backup zips.

    Returns ``(fulls, incrs)`` where ``fulls`` is ``{filename: entry}`` and
    ``incrs`` is a list of entries. Each entry has path/server/timestamp/size,
    and incrementals also carry ``chain_id`` parsed from the filename.
    """
    fulls = {}
    incrs = []
    for f in backup_dir.iterdir():
        if not f.is_file() or f.suffix != ".zip":
            continue
        # Incremental pattern first — it's more specific (RE_FULL would also
        # match an incremental filename without this ordering).
        m = RE_INCR.match(f.name)
        if m:
            incrs.append({
                "path": f, "server": m.group(1), "chain_id": m.group(2),
                "timestamp": m.group(3), "size": f.stat().st_size,
            })
            continue
        m = RE_FULL.match(f.name)
        if m:
            fulls[f.name] = {
                "path": f, "server": m.group(1), "timestamp": m.group(2),
                "size": f.stat().st_size,
            }
    return fulls, incrs


def read_incr_meta(zip_path: Path) -> dict:
    """Read ``_meta.json`` (``{chain_id, base_full}``) from an incremental zip;
    ``{}`` on failure."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if "_meta.json" in zf.namelist():
                return json.loads(zf.read("_meta.json"))
    except Exception:
        pass
    return {}


def group_by_chain(fulls: dict, incrs: list) -> list:
    """Group incrementals by chain ID and resolve each chain's base full backup.

    Returns a list of ``{chain_id, full, incrementals}`` dicts (incrementals
    sorted chronologically). Chains whose base full is missing are skipped;
    standalone fulls appear with ``chain_id=None`` and no incrementals. Sorted by
    full timestamp then first-incremental timestamp.
    """
    chain_map = {}
    for incr in incrs:
        chain_map.setdefault(incr["chain_id"], []).append(incr)
    for chain_id in chain_map:
        chain_map[chain_id].sort(key=lambda e: e["timestamp"])

    chains = []
    for chain_id, chain_incrs in chain_map.items():
        meta = read_incr_meta(chain_incrs[0]["path"])
        full_entry = fulls.get(meta.get("base_full", ""))
        if not full_entry:
            continue  # base full deleted/moved — chain unusable
        chains.append({"chain_id": chain_id, "full": full_entry,
                       "incrementals": chain_incrs})

    referenced = {c["full"]["path"].name for c in chains}
    for name, entry in fulls.items():
        if name not in referenced:
            chains.append({"chain_id": None, "full": entry, "incrementals": []})

    def sort_key(c):
        ts = c["full"]["timestamp"]
        return (ts, c["incrementals"][0]["timestamp"] if c["incrementals"] else "")

    chains.sort(key=sort_key)
    return chains


def discover_chains(backup_dir: Path) -> list:
    """Convenience: ``scan_backups`` + ``group_by_chain`` for ``backup_dir``."""
    fulls, incrs = scan_backups(backup_dir)
    return group_by_chain(fulls, incrs)


def list_restore_points(chains: list) -> list:
    """Flatten chains into a newest-first list of restore points for display.

    Each point: ``{n, kind ('FULL'|'INCR'), timestamp, pretty_ts, size,
    pretty_size, chain_id, chain_idx, point_idx, base_full}``. ``point_idx`` is
    -1 for the full backup, else the incremental's index within its chain. The
    ordering (chains latest-first; within a chain, incrementals newest-first then
    the full) matches the CLI selector so numbering is consistent.
    """
    points = []
    n = 1
    for ci in reversed(range(len(chains))):
        chain = chains[ci]
        full = chain["full"]
        for ii in reversed(range(len(chain["incrementals"]))):
            incr = chain["incrementals"][ii]
            points.append({
                "n": n, "kind": "INCR", "timestamp": incr["timestamp"],
                "pretty_ts": parse_timestamp(incr["timestamp"]),
                "size": incr["size"], "pretty_size": format_size(incr["size"]),
                "chain_id": chain["chain_id"], "chain_idx": ci, "point_idx": ii,
                "base_full": full["path"].name,
            })
            n += 1
        points.append({
            "n": n, "kind": "FULL", "timestamp": full["timestamp"],
            "pretty_ts": parse_timestamp(full["timestamp"]),
            "size": full["size"], "pretty_size": format_size(full["size"]),
            "chain_id": chain["chain_id"], "chain_idx": ci, "point_idx": -1,
            "base_full": full["path"].name,
        })
        n += 1
    return points


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def _wipe_target(target_dir: Path, preserve_names, backup_dir: Path,
                 log) -> None:
    """Clear ``target_dir`` before extracting a full snapshot, keeping bot
    infrastructure that isn't world data: top-level entries whose basename is in
    ``preserve_names`` (e.g. the Bedrock ``console.log`` the bot tails) and the
    ``backup_dir`` subtree if it happens to live inside the server directory."""
    backup_resolved = backup_dir.resolve() if backup_dir else None
    for entry in target_dir.iterdir():
        if entry.name in preserve_names:
            continue
        if backup_resolved is not None:
            try:
                entry.resolve().relative_to(backup_resolved)
                continue  # inside the backup dir — never delete backups
            except ValueError:
                pass
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    log("Cleared existing world files")


def restore_chain(chain: dict, point_idx: int, target_dir: Path, *,
                  backup_dir: Path, exclude_names, copy_cmd: str = "",
                  manifest_path: Path = None, marker_dir: Path = None,
                  establish_chain: bool = True, preserve_names=frozenset(),
                  wipe: bool = True, log_fn=None, dry_run: bool = False) -> dict:
    """Restore ``chain`` up to ``point_idx`` into ``target_dir`` (in-place).

    ``point_idx`` == -1 restores the full backup only; >= 0 applies incrementals
    up to and including that index. The server must be stopped by the caller.

    When ``establish_chain`` is True (an in-place restore over the live server
    dir), a fresh chain is set up so the bot can resume incrementals: a merged
    incremental zip is written to ``backup_dir`` (for an incremental point),
    the chain marker is written into ``marker_dir``, and ``manifest_path`` is
    rebuilt. ``exclude_names`` (the server's ``backup_exclude_names``) and
    ``copy_cmd`` (its ``backup.copy_cmd``) come from the caller's config.

    Returns a summary dict ``{full, incrementals, chain_id, merged, files}``.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    full_zip = chain["full"]["path"]
    incrementals = [] if point_idx == -1 else chain["incrementals"][:point_idx + 1]

    if dry_run:
        with zipfile.ZipFile(full_zip, "r") as zf:
            full_n = len(zf.namelist())
        incr_summary = []
        for incr in incrementals:
            with zipfile.ZipFile(incr["path"], "r") as zf:
                names = zf.namelist()
                changed = len([n for n in names if n not in META_FILES])
                dels = (len(json.loads(zf.read("_deletions.json")))
                        if "_deletions.json" in names else 0)
            incr_summary.append({"name": incr["path"].name,
                                 "changed": changed, "deletions": dels})
        log(f"[dry-run] full {full_zip.name}: {full_n} files; "
            f"{len(incrementals)} incremental(s)")
        return {"full": full_zip.name, "incrementals": incr_summary,
                "chain_id": None, "merged": None, "files": full_n}

    target_dir.mkdir(parents=True, exist_ok=True)
    if wipe and any(target_dir.iterdir()):
        _wipe_target(target_dir, set(preserve_names), backup_dir, log)

    # Step 1: extract the full snapshot. Per-member so the stored Unix mode is
    # reapplied (extractall drops it → non-executable bedrock_server). Skip
    # META_FILES so a backup-format file that ever slipped into a full zip is
    # never written into the world directory.
    log(f"Extracting full backup {full_zip.name} ...")
    with zipfile.ZipFile(full_zip, "r") as zf:
        for info in zf.infolist():
            if info.filename in META_FILES:
                continue
            extracted = zf.extract(info, target_dir)
            _apply_zip_mode(info, Path(extracted))

    # Step 2: apply incrementals in order. When establishing a chain, also
    # accumulate changed files in a temp tree to build one merged incremental.
    tmp = Path(tempfile.mkdtemp()) if (incrementals and establish_chain) else None
    merged_deletions: list = []
    re_added: set = set()
    try:
        for incr in incrementals:
            log(f"Applying incremental {incr['path'].name} ...")
            with zipfile.ZipFile(incr["path"], "r") as zf:
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
                if "_deletions.json" in zf.namelist():
                    for rel_path in json.loads(zf.read("_deletions.json")):
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

        if not establish_chain:
            log(f"Restored {full_zip.name} + {len(incrementals)} incremental(s) "
                f"(files only; no chain established)")
            return {"full": full_zip.name,
                    "incrementals": [i["path"].name for i in incrementals],
                    "chain_id": None, "merged": None, "files": None}

        # Step 3: establish a fresh chain so the bot can resume incrementals.
        chain_id = new_chain_id(backup_dir)
        merged_name = None
        if tmp is not None:
            final_deletions = [p for p in merged_deletions if p not in re_added]
            restore_ts = incrementals[point_idx]["timestamp"]
            merged_name = f"{chain['full']['server']}_incr_{chain_id}_{restore_ts}.zip"
            merged_path = backup_dir / merged_name
            log(f"Creating merged incremental {merged_name} ...")
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
            run_copy_command(merged_path, copy_cmd, log_fn=log_fn)

        # Chain marker in the world dir (new name; drop a stale legacy marker).
        marker_root = marker_dir or target_dir
        try:
            (marker_root / CHAIN_MARKER_NAME).write_text(chain_id)
            legacy = marker_root / CHAIN_MARKER_NAME_LEGACY
            if legacy.exists():
                legacy.unlink()
        except OSError:
            log("Warning: could not write chain marker")

        # Rebuild the manifest so the next incremental diffs against this state.
        files = build_file_manifest(target_dir, backup_dir, exclude_names)
        if manifest_path is not None:
            manifest_path.write_text(json.dumps(
                {"chain_id": chain_id, "base_full": full_zip.name, "files": files}))
        log(f"New chain {chain_id} established ({len(files)} files, "
            f"base {full_zip.name})")
        return {"full": full_zip.name,
                "incrementals": [i["path"].name for i in incrementals],
                "chain_id": chain_id, "merged": merged_name, "files": len(files)}
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
