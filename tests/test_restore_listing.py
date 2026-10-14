"""Self-contained tests for /restore's listing, ordering and selection.

Run with plain ``python tests/test_restore_listing.py`` -- no pytest, no
dependencies beyond the stdlib.

Covers the two ways a numbered restore list can lie to the operator: ordering
that contradicts its own "latest first" header, and a number that means a
different row by the time it is sent.
"""
import json
import os
import sys
import tempfile
import threading
import types
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import restore_core

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


SRV = "world"
FULL = f"{SRV}_20261014_002135.zip"


def _zip(path: Path, entries: dict, mtime: float | None = None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _full(d: Path, mtime=1000.0):
    return _zip(d / FULL, {"world/level.dat": "FULL"}, mtime)


def _incr(d: Path, chain: str, ts: str, mtime: float):
    """An incremental whose NAME says ts but whose file was written at mtime."""
    return _zip(d / f"{SRV}_incr_{chain}_{ts}.zip",
                {"world/level.dat": f"INCR {chain} {ts}",
                 "_meta.json": json.dumps({"chain_id": chain,
                                           "base_full": FULL})},
                mtime)


def test_chain_order_follows_when_it_was_written():
    print("chain ordering:")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _full(d)
        # The real sequence: chain "old" took a genuine incremental at
        # 11:21:54, then a restore to the 11:20:57 point ran at 11:24 and cut
        # chain "new", whose merged incremental is NAMED after the point it
        # restored (11:20:57) though it was written at 11:24.
        _incr(d, "8843f224", "20261014_112154", mtime=2000.0)
        _incr(d, "bdd96488", "20261014_112057", mtime=3000.0)

        points = restore_core.list_restore_points(
            restore_core.discover_chains(d, SRV))
        order = [p["chain_id"] for p in points]
        check(order[0] == "bdd96488",
              f"the chain the restore just created is listed first ({order}) "
              "-- its merged incremental carries the RESTORED point's "
              "timestamp, so sorting on the name put the superseded chain on "
              "top under a header promising 'latest first'")

        # And a chain that merely started earlier but is still active stays on
        # top of one that went stale -- the case the previous fix addressed.
        _incr(d, "8843f224", "20261014_113045", mtime=4000.0)
        order = [p["chain_id"] for p in restore_core.list_restore_points(
            restore_core.discover_chains(d, SRV))]
        check(order[0] == "8843f224",
              f"and real new activity puts a chain back on top ({order[:2]})")


def test_locate_point_matches_identity_not_position():
    print("point identity:")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _full(d)
        _incr(d, "8843f224", "20261014_112057", mtime=2000.0)
        _incr(d, "8843f224", "20261014_112154", mtime=2100.0)

        chains = restore_core.discover_chains(d, SRV)
        points = restore_core.list_restore_points(chains)
        chosen = next(p for p in points if "112057" in (p["file"] or ""))
        before = chosen["point_idx"]

        # A scheduled incremental lands. Positions move; identity does not.
        _incr(d, "8843f224", "20261014_113045", mtime=2200.0)
        chains2 = restore_core.discover_chains(d, SRV)
        found = restore_core.locate_point(chains2, chosen)
        check(found is not None, "the point is still found after a new backup")
        chain, idx = found
        check(chain["incrementals"][idx]["path"].name == chosen["file"],
              f"and resolves to the SAME zip ({chosen['file']})")
        check(idx == before,
              f"(index happens to be unchanged here: {before} -> {idx}) "
              "-- identity is what is matched, not the index")

        new_points = restore_core.list_restore_points(chains2)
        moved = next(p for p in new_points if p["file"] == chosen["file"])
        check(moved["n"] != chosen["n"],
              f"while its DISPLAY number did move ({chosen['n']} -> "
              f"{moved['n']}), which is what used to be restored by")

        # A pruned backup is reported, not silently resolved to a neighbour.
        (d / chosen["file"]).unlink()
        check(restore_core.locate_point(
                  restore_core.discover_chains(d, SRV), chosen) is None,
              "a point whose zip is gone resolves to None")


# --- the command flow -----------------------------------------------------
class _Router:
    def __init__(self):
        self.handlers = {}

    def register(self, name, fn, **kw):
        for n in (name if isinstance(name, list) else [name]):
            self.handlers[n] = fn


def _make_ctx(d: Path, replies: list, args):
    server = types.SimpleNamespace(
        config=types.SimpleNamespace(
            key=SRV, name=SRV, minecraft_dir=d / SRV, backup_dir=d,
            pre_restore_backup=False, restore_warning_seconds=15),
        log=types.SimpleNamespace(info=lambda *a: None,
                                  warning=lambda *a: None,
                                  exception=lambda *a: None),
        backup_lock=threading.Lock(),
        restore_world=lambda *a, **k: None)
    return types.SimpleNamespace(
        server=server, args=args, platform="telegram", user_id="u1",
        chat_id="c1", is_private=True, sender_label="op", chat_label="direct",
        bot=types.SimpleNamespace(config=types.SimpleNamespace(name="bot")),
        adapter=types.SimpleNamespace(send=lambda *a: None),
        reply=replies.append)


def test_selection_means_the_row_that_was_shown():
    print("/restore <n> after the directory moves:")
    import core.commands as cmds
    from core.commands import register_commands

    # Pending selections are module-level and keyed by bot:server:platform:
    # user, which these two tests share. Production keys differ per operator.
    cmds._pending_world_restore.clear()

    router = _Router()
    register_commands(router, {})
    cmd = router.handlers["restore"]

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / SRV).mkdir()
        _full(d)
        _incr(d, "8843f224", "20261014_111543", mtime=2000.0)
        _incr(d, "8843f224", "20261014_112057", mtime=2100.0)
        _incr(d, "8843f224", "20261014_112154", mtime=2200.0)

        replies = []
        cmd(_make_ctx(d, replies, []))          # bare /restore
        listing = replies[-1]
        check("11:21:54" in listing and "11:20:57" in listing,
              "the list is shown")
        # Row 2 of what the operator read.
        row2 = [l for l in listing.splitlines() if l.strip().startswith("2.")]
        check(row2 and "11:20:57" in row2[0], f"row 2 is 11:20:57 ({row2})")

        # Exactly the log's race: an incremental lands 3 seconds after the
        # listing, before the operator's selection arrives.
        _incr(d, "8843f224", "20261014_113045", mtime=2300.0)

        replies.clear()
        cmd(_make_ctx(d, replies, ["2"]))
        prompt = replies[-1]
        check("11:20:57" in prompt,
              "/restore 2 still selects 11:20:57 -- the row the operator "
              "counted. Re-deriving the list here moved the chosen row and "
              "confirmed a point they were never shown")
        check("11:30:45" not in prompt,
              "and NOT the backup that arrived after the list was printed")


def test_number_without_a_listing_is_refused():
    print("/restore <n> with no listing:")
    import core.commands as cmds
    from core.commands import register_commands

    # Pending selections are module-level and keyed by bot:server:platform:
    # user, which these two tests share. Production keys differ per operator.
    cmds._pending_world_restore.clear()

    router = _Router()
    register_commands(router, {})
    cmd = router.handlers["restore"]

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / SRV).mkdir()
        _full(d)
        _incr(d, "8843f224", "20261014_112057", mtime=2000.0)

        replies = []
        cmd(_make_ctx(d, replies, ["1"]))
        check("/restore first" in replies[-1],
              "a bare number with no listing to count in is refused rather "
              "than resolved against whatever the directory holds now")
        check("different point" in replies[-1],
              f"and says why ({replies[-1][:60]}...)")


def main():
    for fn in (test_chain_order_follows_when_it_was_written,
               test_locate_point_matches_identity_not_position,
               test_selection_means_the_row_that_was_shown,
               test_number_without_a_listing_is_refused):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): " + "; ".join(_failures))
        return 1
    print("ALL RESTORE LISTING TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
