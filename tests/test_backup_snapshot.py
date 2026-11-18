"""Self-contained tests for Bedrock incremental-backup change detection.

Run with plain ``python tests/test_backup_snapshot.py`` (needs the bot's
requirements, as it imports core.server). Replays the 2026-11-17 19:17 failure:
a table that existed on disk but was not yet in the `save query` snapshot was
recorded in the manifest as backed up, so no later backup ever copied it, and a
MANIFEST zipped truncated at one backup was never re-copied after it grew.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.server import _RECOPY, _diff_manifest, _snapshot_manifest
from utils.backup_utils import build_file_manifest

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


DB = "worlds/Square Friends/db/"


def write(mc, rel, size, mtime):
    p = mc / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))


def test_unlisted_table_is_captured_later():
    print("table on disk but outside the snapshot")
    with tempfile.TemporaryDirectory() as tmp:
        mc = Path(tmp)
        write(mc, DB + "099140.ldb", 50, 1000)
        write(mc, DB + "MANIFEST-093212", 100, 1000)
        write(mc, "server.properties", 10, 1000)
        old = _snapshot_manifest(build_file_manifest(mc), {
            DB + "099140.ldb": 50, DB + "MANIFEST-093212": 100}, mc)

        # 19:17:17 -- compaction has written 099148.ldb and appended to the
        # MANIFEST, but the snapshot predates both.
        write(mc, DB + "099148.ldb", 70, 1100)
        write(mc, DB + "MANIFEST-093212", 120, 1100)
        ready = {DB + "099140.ldb": 50, DB + "MANIFEST-093212": 100}
        new = _snapshot_manifest(build_file_manifest(mc), ready, mc)
        changed, deleted = _diff_manifest(old, new)
        check(DB + "099148.ldb" not in new,
              "unlisted table is not recorded as backed up")
        check(new[DB + "MANIFEST-093212"] == _RECOPY,
              "MANIFEST zipped truncated is marked for re-copy")
        check(DB + "099140.ldb" not in changed,
              "complete, unchanged table is not re-copied")
        check(new["server.properties"] == 1000, "non-world file keeps its mtime")

        # 19:17:54 -- both are now in the snapshot; nothing changed on disk.
        ready = {DB + "099140.ldb": 50, DB + "099148.ldb": 70,
                 DB + "MANIFEST-093212": 120}
        newer = _snapshot_manifest(build_file_manifest(mc), ready, mc)
        changed, deleted = _diff_manifest(new, newer)
        check(DB + "099148.ldb" in changed, "table is copied once it is listed")
        check(DB + "MANIFEST-093212" in changed, "grown MANIFEST is copied again")
        check(not deleted, "nothing reported deleted")

        # The old behaviour (raw mtimes) would have skipped both.
        raw_old = build_file_manifest(mc)
        raw_changed, _ = _diff_manifest(raw_old, build_file_manifest(mc))
        check(not raw_changed, "raw mtimes miss both (the original bug)")


def test_obsolete_table_is_deleted():
    print("table dropped from the snapshot")
    with tempfile.TemporaryDirectory() as tmp:
        mc = Path(tmp)
        write(mc, DB + "099143.ldb", 40, 1000)
        old = _snapshot_manifest(build_file_manifest(mc),
                                 {DB + "099143.ldb": 40}, mc)
        # Compacted away in the snapshot; the file lingers on disk briefly.
        new = _snapshot_manifest(build_file_manifest(mc), {}, mc)
        _changed, deleted = _diff_manifest(old, new)
        check(DB + "099143.ldb" in deleted, "unlisted old table is deleted")


def test_partial_table_is_recopied():
    print("table longer on disk than in the snapshot")
    with tempfile.TemporaryDirectory() as tmp:
        mc = Path(tmp)
        write(mc, DB + "099150.ldb", 80, 1000)
        new = _snapshot_manifest(build_file_manifest(mc),
                                 {DB + "099150.ldb": 60}, mc)
        check(new[DB + "099150.ldb"] == _RECOPY, "truncated table re-copied")


def test_java_unchanged():
    print("java")
    files = {"world/region/r.0.0.mca": 1000.0}
    check(_snapshot_manifest(files, None, Path(".")) is files,
          "no snapshot map -> manifest untouched")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all passed")
