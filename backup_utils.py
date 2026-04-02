"""Shared utilities for backup chain tracking (used by bot.py and restore.py)."""

import os
import re
import secrets
from pathlib import Path

CHAIN_MARKER_NAME = ".mcnotifier_chain"
META_FILES = {"_deletions.json", "_meta.json"}

RE_FULL = re.compile(r'^(.+?)_(\d{8}_\d{6})\.zip$')
RE_INCR = re.compile(r'^(.+?)_incr_([0-9a-f]{8})_(\d{8}_\d{6})\.zip$')


def scan_existing_chain_ids(backup_dir: Path) -> set:
    """Scan backup directory for chain IDs already used in incremental filenames."""
    ids = set()
    if backup_dir.exists():
        for f in backup_dir.iterdir():
            m = RE_INCR.match(f.name)
            if m:
                ids.add(m.group(2))
    return ids


def new_chain_id(backup_dir: Path) -> str:
    """Generate a unique 8-char hex chain ID."""
    existing = scan_existing_chain_ids(backup_dir)
    chain_id = secrets.token_hex(4)
    while chain_id in existing:
        chain_id = secrets.token_hex(4)
    return chain_id


def build_file_manifest(root_dir: Path, backup_dir: Path | None = None) -> dict:
    """Walk root_dir and return {relative_path: mtime} dict.

    Skips CHAIN_MARKER_NAME and anything under backup_dir.
    """
    backup_dir_resolved = backup_dir.resolve() if backup_dir else None
    files = {}
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        dp = Path(dirpath)
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
                rel = str(fp.relative_to(root_dir)).replace("\\", "/")
                files[rel] = fp.stat().st_mtime
            except OSError:
                pass
    return files
