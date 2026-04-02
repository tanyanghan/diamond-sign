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

Chain Marker (.mcnotifier_chain)
--------------------------------
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

import os
import re
import secrets
from pathlib import Path

# Name of the chain marker file placed in the Minecraft server directory.
# This file is excluded from all backup zips — it's metadata about the
# backup process, not part of the server data.
CHAIN_MARKER_NAME = ".mcnotifier_chain"

# Internal metadata files stored inside incremental backup zips.
# These are skipped when extracting files during restore.
#   _meta.json:       {"chain_id": "...", "base_full": "..."} — identifies
#                     which chain this incremental belongs to and which full
#                     backup it builds upon.
#   _deletions.json:  ["path/to/deleted/file", ...] — files that were deleted
#                     since the previous backup in the chain.
META_FILES = {"_deletions.json", "_meta.json"}

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


def build_file_manifest(root_dir: Path, backup_dir: Path | None = None) -> dict:
    """Walk root_dir and return {relative_path: mtime} dict.

    This is the core of the incremental backup change-detection system.
    By recording the mtime of every file, we can later compare against a
    new walk to find which files changed, were added, or were deleted.

    Skips:
    - CHAIN_MARKER_NAME: backup metadata, not server data
    - Anything under backup_dir: if the backup output directory happens to
      be inside the server directory, we don't want to back up backups
    """
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
            if fn == CHAIN_MARKER_NAME:
                continue
            fp = dp / fn
            try:
                # Use forward slashes for cross-platform consistency in manifests
                rel = str(fp.relative_to(root_dir)).replace("\\", "/")
                files[rel] = fp.stat().st_mtime
            except OSError:
                pass
    return files
