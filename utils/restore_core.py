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
import logging
import os
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .backup_utils import (
    CHAIN_MARKER_NAME, META_FILES, RE_FULL, RE_INCR,
    backup_tmp_path, build_file_manifest, finalize_backup_zip, new_chain_id,
    run_copy_command, validate_backup_zip,
)

logger = logging.getLogger("diamondsign")


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
def scan_backups(backup_dir: Path, server_name: str | None = None) -> tuple:
    """Scan ``backup_dir`` for full and incremental backup zips.

    Returns ``(fulls, incrs)`` where ``fulls`` is ``{filename: entry}`` and
    ``incrs`` is a list of entries. Each entry has path/server/timestamp/size,
    and incrementals also carry ``chain_id`` parsed from the filename.

    When ``server_name`` is given, only zips whose filename's server-name
    component matches it EXACTLY are included. The patterns' leading wildcard
    otherwise swallows any prefix — a quarantined
    ``corrupt_mc-bedrock_incr_<chain>_<ts>.zip`` parses as server
    ``corrupt_mc-bedrock`` and, since chains group by chain ID alone, would
    silently rejoin the chain it was renamed to escape. Skips are logged so a
    deliberately renamed file is visibly excluded, not silently.
    """
    def name_mismatch(f, parsed: str) -> bool:
        if server_name is not None and parsed != server_name:
            logger.info("Ignoring %s: server-name prefix %r does not match %r",
                        f.name, parsed, server_name)
            return True
        return False

    fulls = {}
    incrs = []
    for f in backup_dir.iterdir():
        if not f.is_file() or f.suffix != ".zip":
            continue
        # Incremental pattern first — it's more specific (RE_FULL would also
        # match an incremental filename without this ordering).
        m = RE_INCR.match(f.name)
        if m:
            if name_mismatch(f, m.group(1)):
                continue
            incrs.append({
                "path": f, "server": m.group(1), "chain_id": m.group(2),
                "timestamp": m.group(3), "size": f.stat().st_size,
            })
            continue
        m = RE_FULL.match(f.name)
        if m:
            if name_mismatch(f, m.group(1)):
                continue
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


def discover_chains(backup_dir: Path, server_name: str | None = None) -> list:
    """Convenience: ``scan_backups`` + ``group_by_chain`` for ``backup_dir``.

    Callers with server context pass ``server_name`` (the server directory's
    basename — the same name backups are created with) so foreign or
    quarantined zips can't join the chain; see ``scan_backups``.
    """
    fulls, incrs = scan_backups(backup_dir, server_name)
    return group_by_chain(fulls, incrs)


def validate_chain_files(chain: dict, point_idx: int) -> list[str]:
    """CRC-verify every zip needed to restore ``chain`` up to ``point_idx``.

    Returns ``["<filename>: <problem>", ...]`` — empty when the whole segment
    is healthy. Run BEFORE touching the world: a truncated incremental
    discovered mid-restore (after the wipe) once left a server running on a
    4-day-old partial world.
    """
    to_check = [chain["full"]["path"]]
    if point_idx >= 0:
        to_check += [i["path"] for i in chain["incrementals"][:point_idx + 1]]
    problems = []
    for path in to_check:
        problem = validate_backup_zip(path)
        if problem is not None:
            problems.append(f"{path.name}: {problem}")
    return problems


def estimate_restore_bytes(chain: dict, point_idx: int) -> tuple[int, int]:
    """Estimate the disk space a restore needs, from zip central directories.

    Returns ``(world_bytes, merge_bytes)``: the uncompressed size of the full
    backup plus every incremental up to ``point_idx`` (an upper bound on the
    restored world — overwrites make the true size smaller), and the
    uncompressed incrementals alone (the merged-incremental staging tree).
    Cheap: reads only each zip's central directory, no data.
    """
    def uncompressed(path: Path) -> int:
        with zipfile.ZipFile(path, "r") as zf:
            return sum(i.file_size for i in zf.infolist())

    incrs = 0 if point_idx == -1 else sum(
        uncompressed(i["path"]) for i in chain["incrementals"][:point_idx + 1])
    return uncompressed(chain["full"]["path"]) + incrs, incrs


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
class PreflightError(RuntimeError):
    """A restore refused to start — raised BEFORE any world file is touched
    (corrupt zip in the chain, not enough disk space). Callers can treat this
    as fully safe: the world is coherent and the server may be brought back
    up, unlike a mid-restore failure after the wipe."""


def check_disk_space(chain: dict, point_idx: int, target_dir: Path,
                     backup_dir: Path, establish_chain: bool, log) -> None:
    """Abort (raise PreflightError) if the restore can't fit on disk.

    The restore writes the uncompressed world into ``target_dir`` and — when
    establishing a chain from an incremental point — an uncompressed staging
    tree plus the merged zip under ``backup_dir``. Requirements on the same
    filesystem (st_dev) are summed. Estimates are upper bounds (overwrites
    across incrementals shrink the real footprint) and the check runs before
    the wipe frees the old world's space, so it is conservative in the safe
    direction. Note: ``disk_usage`` sees filesystem free space, not user
    quotas — a quota can still fail a restore this check passed.
    """
    world_bytes, merge_bytes = estimate_restore_bytes(chain, point_idx)
    # Staging tree + merged zip (zip ≤ tree; deflate only shrinks).
    staging = merge_bytes * 2 if (point_idx >= 0 and establish_chain) else 0

    def existing(path: Path) -> Path:
        # The target dir may not exist yet (restore into a fresh directory
        # creates it later); measure its nearest existing ancestor's fs.
        path = path.resolve()
        while not path.exists() and path.parent != path:
            path = path.parent
        return path

    need = {}  # st_dev -> [bytes, [labels]]
    for path, size, label in ((target_dir, world_bytes, "world"),
                              (backup_dir, staging, "merge staging")):
        if size <= 0:
            continue
        path = existing(path)
        dev = os.stat(path).st_dev
        entry = need.setdefault(dev, [0, [], path])
        entry[0] += size
        entry[1].append(label)
    for size, labels, path in need.values():
        free = shutil.disk_usage(path).free
        if free < size + 128 * 1024 * 1024:  # headroom for margin of error
            raise PreflightError(
                f"not enough disk space for {' + '.join(labels)} on "
                f"{path}: need ~{format_size(size)}, "
                f"{format_size(free)} free — world untouched. Free up space "
                "and retry (the estimate is an upper bound).")
    log(f"Disk space OK (world ~{format_size(world_bytes)}, "
        f"staging ~{format_size(staging)})")


def _tree_bytes(root: Path) -> int:
    """Total bytes of all files under ``root`` (the merge staging tree)."""
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _world_walk(target_dir: Path, backup_dir: Path, exclude_names):
    """Yield the restored world's files — the set a full backup would zip:
    skips backup-format entries (META_FILES), the chain marker, per-server
    excludes, and the backup_dir subtree if it lives inside the world dir.
    Mirrors the skip set of ``build_file_manifest``/``run_backup``."""
    skip = {CHAIN_MARKER_NAME} | META_FILES | set(exclude_names or ())
    backup_resolved = backup_dir.resolve() if backup_dir else None
    for dirpath, _dirnames, filenames in os.walk(target_dir):
        dp = Path(dirpath)
        if backup_resolved is not None:
            try:
                dp.resolve().relative_to(backup_resolved)
                continue
            except ValueError:
                pass
        for fn in filenames:
            if fn in skip:
                continue
            yield dp / fn


def _world_bytes(target_dir: Path, backup_dir: Path, exclude_names) -> int:
    """Total bytes a full backup of the restored world would contain."""
    total = 0
    for fp in _world_walk(target_dir, backup_dir, exclude_names):
        try:
            total += fp.stat().st_size
        except OSError:
            pass
    return total


def _write_full_of_restored(target_dir: Path, backup_dir: Path, server: str,
                            restore_ts: str, exclude_names, log) -> str:
    """Zip the restored world as a NEW full backup and return its filename.

    Used when the merged incremental would rival a full in size (Bedrock's
    LevelDB churn): the chain re-bases on this zip instead. Named with the
    restore point's timestamp so /restore listings show the point in time it
    represents (falls back to now on a name collision). Atomic + verified
    like every backup zip. A Bedrock player sidecar is embedded when possible
    — the restored db files are quiescent at full length — via the
    memory-isolated worker; failure to build one (e.g. amulet absent on a
    bare restore.py host) is logged and never fails the restore, matching
    write_player_sidecar's contract.
    """
    full_name = f"{server}_{restore_ts}.zip"
    if (backup_dir / full_name).exists():
        full_name = f"{server}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    full_path = backup_dir / full_name
    full_tmp = backup_tmp_path(full_path)
    log(f"Creating full backup {full_name} from the restored world ...")
    with zipfile.ZipFile(full_tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in _world_walk(target_dir, backup_dir, exclude_names):
            rel = str(fp.relative_to(target_dir)).replace("\\", "/")
            zf.write(fp, rel)
        try:
            sidecar = _restored_world_sidecar(target_dir, backup_dir)
            if sidecar is not None:
                zf.writestr("_players.json", json.dumps(sidecar))
                log(f"Player sidecar: {len(sidecar.get('players', {}))} "
                    "player(s)")
        except Exception as e:
            log(f"Player sidecar skipped ({e})")
    finalize_backup_zip(full_tmp, full_path, log_fn=log)
    return full_name


def _restored_world_sidecar(target_dir: Path, backup_dir: Path):
    """Build the _players.json sidecar from the restored world's LevelDB, or
    None when there is no Bedrock world layout (Java). The server is stopped
    and the files are quiescent at full length, so unlike a live backup no
    save-query truncation is needed."""
    db_files = []
    worlds = target_dir / "worlds"
    if worlds.is_dir():
        for db in worlds.glob("*/db"):
            db_files += [(p, p.stat().st_size)
                         for p in db.iterdir() if p.is_file()]
    if not db_files:
        return None
    from . import bedrock_player
    return bedrock_player.build_sidecar_subprocess(db_files,
                                                   tmp_dir=backup_dir)


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

    if not dry_run:
        # Never touch the world on a chain that can't complete. Both checks
        # run BEFORE the wipe: a truncated zip or full disk discovered
        # mid-apply once left a server on a days-old partial world.
        problems = validate_chain_files(chain, point_idx)
        if problems:
            raise PreflightError(
                "backup validation failed — world untouched:\n  "
                + "\n  ".join(problems))
        check_disk_space(chain, point_idx, target_dir, backup_dir,
                         establish_chain, log)

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
    # The tree holds every distinct changed file across ALL applied
    # incrementals uncompressed — multi-GB for a long chain — so it lives
    # under backup_dir (big disk, already excluded from backups/manifest),
    # NOT the system tmp dir: /tmp is a small tmpfs on typical hosts and a
    # long chain overflowed it in the wild.
    tmp = (Path(tempfile.mkdtemp(prefix=".restore_merge_", dir=str(backup_dir)))
           if (incrementals and establish_chain) else None)
    merged_deletions: list = []
    re_added: set = set()
    try:
        total = len(incrementals)
        report_step = max(1, (total + 9) // 10)  # ceil(total/10)
        for i, incr in enumerate(incrementals, 1):
            # Per-file detail goes to the bot log only (it's the forensic
            # trail that identified a corrupt zip in the wild); the chat sees
            # ~10% milestones — one message per zip flooded Telegram/Slack on
            # long chains.
            logger.info("Applying incremental %s (%d/%d)",
                        incr["path"].name, i, total)
            if i == 1 or i == total or i % report_step == 0:
                log(f"Applying incremental {i} of {total} ...")
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
                        # Remove from the restored world AND the merge staging
                        # tree. Staging must mirror the NET state: a deleted
                        # file's content left in the tree rides into the merged
                        # zip as dead weight — on Bedrock (LevelDB renames
                        # everything on compaction) that once compounded
                        # hundreds of 10-15 MB incrementals into a 1.6 GB
                        # merged zip for a ~100 MB world. A file deleted then
                        # re-added later is rewritten into staging by the later
                        # incremental, so only genuinely-dead content is lost.
                        roots = ((target_dir, tmp) if tmp is not None
                                 else (target_dir,))
                        for root in roots:
                            del_path = root / rel_path
                            if del_path.exists():
                                del_path.unlink()
                                parent = del_path.parent
                                while parent != root and not any(parent.iterdir()):
                                    parent.rmdir()
                                    parent = parent.parent

        if not establish_chain:
            log(f"Restored {full_zip.name} + {len(incrementals)} incremental(s) "
                f"(files only; no chain established)")
            return {"full": full_zip.name,
                    "incrementals": [i["path"].name for i in incrementals],
                    "chain_id": None, "merged": None, "files": None}

        # Step 3: establish a fresh chain so the bot can resume incrementals.
        # The new chain's base artifact is EITHER a merged incremental (riding
        # on the old full) or a brand-new full backup of the restored world —
        # whichever is smaller. The merged form exists to keep future restores
        # small (Java: ~100 MB merged vs a 3.5 GB full), but on Bedrock LevelDB
        # renames every file within days, so the trimmed merge converges to the
        # whole world and a restore through it does strictly more work than one
        # fresh full. Decided by measured size, not edition, so it adapts.
        chain_id = new_chain_id(backup_dir)
        merged_name = new_full_name = None
        base_full_name = full_zip.name
        if tmp is not None:
            restore_ts = incrementals[point_idx]["timestamp"]
            staging_bytes = _tree_bytes(tmp)
            world_bytes = _world_bytes(target_dir, backup_dir, exclude_names)
            if staging_bytes >= 0.5 * world_bytes:
                # Merged would save less than half a full while forcing a
                # two-zip restore: re-base the chain on a fresh full instead.
                log(f"Merged incremental would be ~{format_size(staging_bytes)} "
                    f"vs ~{format_size(world_bytes)} world — re-basing on a "
                    "new full backup instead")
                new_full_name = _write_full_of_restored(
                    target_dir, backup_dir, chain["full"]["server"],
                    restore_ts, exclude_names, log)
                base_full_name = new_full_name
                run_copy_command(backup_dir / new_full_name, copy_cmd,
                                 log_fn=log_fn)
            else:
                final_deletions = [p for p in merged_deletions
                                   if p not in re_added]
                merged_name = (f"{chain['full']['server']}_incr_{chain_id}_"
                               f"{restore_ts}.zip")
                merged_path = backup_dir / merged_name
                merged_tmp = backup_tmp_path(merged_path)
                log(f"Creating merged incremental {merged_name} ...")
                with zipfile.ZipFile(merged_tmp, "w", zipfile.ZIP_DEFLATED) as zf:
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
                finalize_backup_zip(merged_tmp, merged_path, log_fn=log)
                run_copy_command(merged_path, copy_cmd, log_fn=log_fn)

        # Chain marker in the world dir.
        marker_root = marker_dir or target_dir
        try:
            (marker_root / CHAIN_MARKER_NAME).write_text(chain_id)
        except OSError:
            log("Warning: could not write chain marker")

        # Rebuild the manifest so the next incremental diffs against this state.
        files = build_file_manifest(target_dir, backup_dir, exclude_names)
        if manifest_path is not None:
            manifest_path.write_text(json.dumps(
                {"chain_id": chain_id, "base_full": base_full_name,
                 "files": files}))
        log(f"New chain {chain_id} established ({len(files)} files, "
            f"base {base_full_name})")
        return {"full": full_zip.name,
                "incrementals": [i["path"].name for i in incrementals],
                "chain_id": chain_id, "merged": merged_name,
                "new_full": new_full_name, "files": len(files)}
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
