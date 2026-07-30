"""Shared utilities for backup chain tracking (used by bot.py and restore.py).

Backup Strategy Overview
========================

The backup system uses a "full + incremental chain" approach:

  1. FULL BACKUP: A complete zip of the entire Minecraft server directory.
     Created on a configurable schedule (daily/weekly/monthly) or via /backup.
     Filename format: servername_YYYYMMDD_HHMMSS.zip

  2. INCREMENTAL BACKUP: A small zip containing only files that changed since
     the last backup (full or incremental). Created automatically while players
     are online, at a configurable interval.
     Filename format: servername_incr_CHAINID_YYYYMMDD_HHMMSS.zip

Chain Tracking
--------------
Each full backup starts a new "chain" identified by a unique 8-character hex ID
(e.g., "a1b2c3d4"). All incremental backups that follow belong to that chain,
identified by the chain ID embedded in their filename and in a _meta.json file
inside the zip.

Why chains? After a restore, the server state is different from before. Without
chains, new incremental backups would be mixed with old ones from before the
restore, and the restore tool couldn't tell them apart. The chain ID makes each
sequence of full + incrementals uniquely identifiable.

Chain Marker (.diamondsign_chain)
---------------------------------
A small file placed in the Minecraft server directory containing the current
chain ID. On startup, the bot compares this marker against the manifest to
detect if the server state was replaced while the bot was offline (e.g., manual
restore, files copied from another server). If they don't match, the bot skips
incremental backups until a new full backup establishes a fresh chain.

Manifest (backup_manifest.json)
-------------------------------
Tracks the current chain state: which chain we're in, which full backup it's
based on, and the mtime of every file in the server directory. The incremental
backup process compares the current file mtimes against the manifest to detect
which files changed. After each backup (full or incremental), the manifest is
updated to reflect the new state.
"""

import ctypes
import ctypes.util
import gc
import os
import re
import secrets
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Memory diagnostics
# ---------------------------------------------------------------------------
# Added after repeated OOM kills (2026-07-11/16/18) traced to the Bedrock
# player-sidecar build (fixed: PR #14 isolated it in a subprocess) and a
# slower, still-unexplained multi-day RSS creep that survived that fix. The
# existing single before/after-sidecar RSS line can't say WHICH phase of a
# backup is responsible, or whether growth is live Python objects (a real
# reference leak, fixable in this code) vs. native/allocator memory (e.g. an
# un-isolated zipfile/subprocess buffer that glibc's malloc never hands back
# to the OS — fixable with malloc_trim or subprocess isolation instead).
# log_mem() answers both questions cheaply (RSS + live object count, always
# on) and, when DIAMONDSIGN_MEMTRACE=1 is set before the bot starts, points
# at the exact allocating source lines via tracemalloc (opt-in: tracemalloc
# has real overhead, so it must never run by default).
_MEMTRACE = os.environ.get("DIAMONDSIGN_MEMTRACE") == "1"
if _MEMTRACE:
    import tracemalloc
    tracemalloc.start(10)


def process_rss_mb() -> float | None:
    """This process's resident set size in MB (Linux /proc; None elsewhere)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except OSError:
        pass
    return None


def log_mem(tag: str, log_fn=None) -> None:
    """Log a named memory checkpoint: RSS, live Python object count, and (only
    with DIAMONDSIGN_MEMTRACE=1) the top-3 allocating source lines by current
    traced size.

    How to read a sequence of these across one backup: if RSS climbs while
    ``objects`` stays flat, the growth is native/allocator memory — a buffer
    (zipfile deflate, a captured subprocess pipe) that Python freed but glibc
    kept mapped; malloc_trim or moving the work to a subprocess is the fix. If
    ``objects`` climbs in step with RSS, something in THIS process is holding
    Python references — enable DIAMONDSIGN_MEMTRACE and restart to see exactly
    which source line via the ``top=`` field.
    """
    if log_fn is None:
        return
    parts = [f"[{tag}]"]
    rss = process_rss_mb()
    if rss is not None:
        parts.append(f"RSS={rss:.0f}MB")
    parts.append(f"objects={len(gc.get_objects())}")
    if _MEMTRACE and tracemalloc.is_tracing():
        top = tracemalloc.take_snapshot().statistics("lineno")[:3]
        if top:
            top_str = ", ".join(
                f"{Path(s.traceback[0].filename).name}:{s.traceback[0].lineno}="
                f"{s.size / 1024:.0f}KB" for s in top)
            parts.append(f"top=[{top_str}]")
    log_fn(" ".join(parts))


def _load_malloc_trim():
    """Return glibc's malloc_trim as a bound ctypes function, or None if this
    platform doesn't have it (macOS, Windows, musl libc) — any failure mode
    (no libc, CDLL open failure, symbol absent) collapses to None so callers
    never need their own try/except.

    malloc_trim is glibc-specific, so this only even attempts a load on
    POSIX — on Windows, ``ctypes.CDLL(None)`` (the POSIX "no path" fallback
    that opens the main program's own symbol table via dlopen(NULL)) raises
    TypeError rather than a clean OSError, since LoadLibrary has no
    equivalent "give me myself" call.
    """
    if os.name != "posix":
        return None
    try:
        path = ctypes.util.find_library("c")
        lib = ctypes.CDLL(path) if path else ctypes.CDLL(None)
        _ = lib.malloc_trim  # raises AttributeError if the symbol is absent
        return lib
    except (OSError, AttributeError, TypeError):
        return None


_libc = _load_malloc_trim()


def trim_memory(log_fn=None) -> None:
    """Force a GC pass, then ask glibc to return freed heap pages to the OS
    (``gc.collect()`` + ``malloc_trim(0)``).

    2026-07-29 investigation: a Bedrock full backup's CRC-verification step
    (``finalize_backup_zip``'s ``testzip()``, which opens and decompresses
    many small zip entries — Bedrock zips have far more, smaller files than
    Java's) left a reproducible +5 to +7 MB RSS increase per call, three
    times in one test. ``log_mem``'s object count proved this was NOT
    retained Python references — the count returned to baseline by the very
    next backup call each time — so the growth is native allocator memory
    that Python freed but glibc never handed back to the OS. This asks it
    to, right after that step.

    2026-07-29 follow-up: the first ``malloc_trim`` call after a fresh bot
    start reclaimed nothing (0 MB) on every server; the SECOND call for the
    same server (minutes later, same process) reclaimed 2-3 MB every time.
    ``malloc_trim`` can only return memory that's already free — the
    zipfile/decompressor machinery's first run through a fresh process
    likely leaves reference CYCLES that only Python's cyclic GC (not plain
    refcounting) breaks, and that collector runs on its own schedule, not
    synchronously when a function returns. A second call to the same code
    path found that first run's garbage already swept and had real free
    space to hand back. ``gc.collect()`` forces that sweep immediately
    instead of waiting for a second call — sub-100ms on a process this
    size, negligible next to a backup that already takes seconds.

    No-op on platforms without ``malloc_trim`` (macOS, Windows, musl): the
    growth this targets is glibc-arena-specific, so there's nothing to trim
    there and this safely does nothing (gc.collect() still runs — cheap and
    harmless everywhere, but skipped too since it only matters paired with
    the trim it enables). Always logs (when given a log_fn), not just on a
    detected change, so a backup log directly answers "did this help"
    without needing DIAMONDSIGN_MEMTRACE.
    """
    if _libc is None:
        return
    before = process_rss_mb()
    gc.collect()
    _libc.malloc_trim(ctypes.c_int(0))
    if log_fn is None:
        return
    after = process_rss_mb()
    if before is not None and after is not None:
        log_fn(f"malloc_trim: RSS {before:.0f}->{after:.0f} MB")


# Name of the chain marker file placed in the Minecraft server directory.
# This file is excluded from all backup zips — it's metadata about the
# backup process, not part of the server data.
CHAIN_MARKER_NAME = ".diamondsign_chain"

# Internal metadata files stored inside incremental backup zips.
# These are skipped when extracting files during restore.
#   _meta.json:       {"chain_id": "...", "base_full": "..."} — identifies
#                     which chain this incremental belongs to and which full
#                     backup it builds upon.
#   _deletions.json:  ["path/to/deleted/file", ...] — files that were deleted
#                     since the previous backup in the chain.
META_FILES = {"_deletions.json", "_meta.json", "_players.json"}

# Regex for full backup filenames: servername_YYYYMMDD_HHMMSS.zip
# Captures: (1) server name, (2) timestamp
RE_FULL = re.compile(r'^(.+?)_(\d{8}_\d{6})\.zip$')

# Regex for incremental backup filenames: servername_incr_CHAINID_YYYYMMDD_HHMMSS.zip
# Captures: (1) server name, (2) chain ID (8 hex chars), (3) timestamp
RE_INCR = re.compile(r'^(.+?)_incr_([0-9a-f]{8})_(\d{8}_\d{6})\.zip$')


def scan_existing_chain_ids(backup_dir: Path) -> set:
    """Scan backup directory for chain IDs already used in incremental filenames.

    Used to avoid generating a duplicate chain ID when creating a new chain.
    """
    ids = set()
    if backup_dir.exists():
        for f in backup_dir.iterdir():
            m = RE_INCR.match(f.name)
            if m:
                ids.add(m.group(2))
    return ids


def new_chain_id(backup_dir: Path) -> str:
    """Generate a unique 8-char hex chain ID.

    Scans existing incremental filenames in backup_dir to avoid collisions.
    With 4 bytes (8 hex chars) and typically few chains, collision is extremely
    unlikely, but we check anyway.
    """
    existing = scan_existing_chain_ids(backup_dir)
    chain_id = secrets.token_hex(4)
    while chain_id in existing:
        chain_id = secrets.token_hex(4)
    return chain_id


def build_file_manifest(root_dir: Path, backup_dir: Path | None = None,
                        exclude_names: set | None = None) -> dict:
    """Walk root_dir and return {relative_path: mtime} dict.

    This is the core of the incremental backup change-detection system.
    By recording the mtime of every file, we can later compare against a
    new walk to find which files changed, were added, or were deleted.

    Skips:
    - CHAIN_MARKER_NAME: backup metadata, not server data
    - META_FILES (_meta.json / _deletions.json / _players.json): backup-format
      entries the bot writes into each zip. If a copy of one lingers in the
      world dir (e.g. extracted by a restore), it must not be treated as world
      data — otherwise the backup zips it AND re-writes it (a "Duplicate name"
      warning) and it shows as perpetually changed in incrementals.
    - Any basename in exclude_names: bot infrastructure that lives in the
      server directory but isn't server data (e.g. the Bedrock console.log
      the bot tails). Must match the set excluded from the backup zips, or
      excluded files would show up as perpetually changed/deleted.
    - Anything under backup_dir: if the backup output directory happens to
      be inside the server directory, we don't want to back up backups
    """
    skip = {CHAIN_MARKER_NAME} | META_FILES | (exclude_names or set())
    backup_dir_resolved = backup_dir.resolve() if backup_dir else None
    files = {}
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        dp = Path(dirpath)
        # Skip the backup directory if it's inside the server directory
        if backup_dir_resolved:
            try:
                dp.resolve().relative_to(backup_dir_resolved)
                continue
            except ValueError:
                pass
        for fn in filenames:
            if fn in skip:
                continue
            fp = dp / fn
            try:
                # Use forward slashes for cross-platform consistency in manifests
                rel = str(fp.relative_to(root_dir)).replace("\\", "/")
                files[rel] = fp.stat().st_mtime
            except OSError:
                pass
    return files


def wait_for_settle(root_dir: Path, backup_dir: Path | None = None,
                    settle_seconds: int = 5, max_attempts: int = 12,
                    log_fn=None, exclude_names: set | None = None) -> dict:
    """Wait until no files in root_dir change for settle_seconds, then return
    the final manifest.

    After RCON save-all, the server may still be flushing data to disk.  This
    function polls the filesystem by building file manifests and comparing
    consecutive snapshots.  Once two snapshots taken settle_seconds apart are
    identical, the filesystem is considered settled.

    Args:
        root_dir:        Directory to monitor (the Minecraft server directory).
        backup_dir:      Passed through to build_file_manifest (excluded from scan).
        settle_seconds:  Seconds to wait between snapshots (default 5).
        max_attempts:    Maximum polling iterations before giving up (default 12,
                         i.e. ~60 s total).
        log_fn:          Callback for status messages.  If None, silent.

    Returns:
        The final file manifest {relative_path: mtime}.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    manifest = build_file_manifest(root_dir, backup_dir, exclude_names)
    for attempt in range(max_attempts):
        time.sleep(settle_seconds)
        check = build_file_manifest(root_dir, backup_dir, exclude_names)
        if check == manifest:
            log(f"Filesystem settled after {settle_seconds * (attempt + 1)} s")
            return manifest
        else:
            manifest = check
            log(f"Files still changing, re-scanning "
                f"(attempt {attempt + 1}/{max_attempts})")
    log(f"Filesystem did not settle after {settle_seconds * max_attempts} s, "
        f"proceeding with current state")
    return manifest


def run_copy_command(file_path: Path, cmd_template: str, log_fn=None) -> None:
    """Run a copy command to upload a backup file to off-server storage.

    ``cmd_template`` is the per-server ``backup.copy_cmd`` from the config; the
    placeholder {file} in it is replaced with the full path to the backup zip.
    Does nothing if ``cmd_template`` is empty.

    Args:
        file_path:    Path to the backup zip file to copy.
        cmd_template: The shell command template (with an optional {file}).
        log_fn:       Callback for status messages, e.g. logger.info or print.
                      If None, messages are silently discarded.
    """
    if not cmd_template:
        return

    def log(msg):
        if log_fn:
            log_fn(msg)

    copy_cmd = cmd_template.replace("{file}", str(file_path))
    log("Running copy command...")
    try:
        # capture_output=True buffers stdout/stderr FULLY in memory before
        # subprocess.run returns — a copy command that prints a lot (e.g.
        # rsync --progress over many files) can allocate a large temporary
        # string here. Logged so a memory-growth investigation can see
        # whether this is a contributor without guessing.
        result = subprocess.run(copy_cmd, shell=True, capture_output=True,
                                text=True, timeout=600)
        out_bytes = len(result.stdout) + len(result.stderr)
        if result.returncode == 0:
            log(f"Copy command completed successfully "
                f"({out_bytes} byte(s) of output captured)")
        else:
            log(f"Copy command failed (rc={result.returncode}): "
                f"{result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log("Copy command timed out after 10 minutes")
    except Exception as e:
        log(f"Copy command error: {e}")


# ---------------------------------------------------------------------------
# Zip integrity (atomic creation + verification)
# ---------------------------------------------------------------------------
# A bot process killed mid-write (OOM, reboot) used to leave a truncated file
# under its final .zip name, silently poisoning the chain until a restore hit
# it — after the world was already wiped. Backups are therefore built at
# <name>.zip.tmp and only renamed into place after a full verification pass.
# .zip.tmp files are invisible to chain discovery (it requires the .zip
# suffix), so a crash leaves harmless debris instead of a bad backup.

TMP_SUFFIX = ".tmp"  # appended to the final .zip name during creation


def backup_tmp_path(final_path: Path) -> Path:
    """The in-progress path a backup zip is built at before finalize."""
    return final_path.with_name(final_path.name + TMP_SUFFIX)


def validate_backup_zip(path: Path) -> str | None:
    """Fully verify a backup zip. Returns a problem description, or None.

    Opening the archive catches a missing/garbled central directory (the
    signature of a truncated write); ``testzip`` then CRC-checks every entry,
    catching corruption inside the data itself.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                return f"CRC check failed on entry '{bad}'"
    except zipfile.BadZipFile as e:
        return f"not a valid zip ({e})"
    except OSError as e:
        return f"unreadable ({e})"
    return None


def finalize_backup_zip(tmp_path: Path, final_path: Path, log_fn=None) -> None:
    """Verify a just-written backup zip and atomically move it into place.

    Every backup writer funnels through here, so a zip either passes a full
    CRC pass and appears under its final name, or the backup fails loudly now
    — never a bad zip discovered days later by a restore.
    """
    problem = validate_backup_zip(tmp_path)
    if problem is not None:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"backup verification failed for {final_path.name}: {problem}")
    os.replace(tmp_path, final_path)
    if log_fn:
        log_fn(f"Verified {final_path.name} (CRC pass)")


def clean_stale_tmp(backup_dir: Path, log_fn=None) -> None:
    """Remove crash debris from the backup dir: leftover *.zip.tmp files and
    orphaned mcn_sidecar_* temp-db dirs (the sidecar build stages its db copy
    here — real disk, not the tmpfs system tmp).

    Called at the start of each backup; the caller holds the per-server
    backup lock, so no live writer's tmp file can be swept here.
    """
    if not backup_dir.exists():
        return
    for f in backup_dir.glob(f"*.zip{TMP_SUFFIX}"):
        try:
            f.unlink()
            if log_fn:
                log_fn(f"Removed stale partial backup {f.name}")
        except OSError:
            pass
    for d in backup_dir.glob("mcn_sidecar_*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            if log_fn:
                log_fn(f"Removed stale sidecar temp dir {d.name}")
