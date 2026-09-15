"""Self-contained tests for the staged world restore.

Run with plain ``python tests/test_restore_staging.py`` -- no pytest, no
dependencies beyond the stdlib. Exits non-zero on the first failure.

Covers the pieces a whole-world restore can lose data with: the staging
eligibility rules, the two-rename swap and its rollback, the cheap structural
validation, and the ``preflight_done`` short-circuit.
"""
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import restore_core
from utils.restore_core import (
    SwapError, can_stage_swap, swap_in_staging, validate_chain_structure,
    validate_chain_files,
)

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


def make_zip(path: Path, entries: dict):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def make_chain(backup_dir: Path, n_incr=0):
    """A minimal chain dict shaped like discover_chains() returns."""
    full = make_zip(backup_dir / "srv_20260101_000000.zip",
                    {"server.properties": "level-name=world\n",
                     "world/level.dat": "FULL"})
    incrs = []
    for i in range(n_incr):
        p = make_zip(backup_dir / f"srv_incr_aaaabbbb_2026010{i+1}_000000.zip",
                     {"world/level.dat": f"INCR{i}",
                      "_meta.json": '{"chain_id": "aaaabbbb", '
                                    '"base_full": "srv_20260101_000000.zip"}'})
        incrs.append({"path": p, "timestamp": f"2026010{i+1}_000000"})
    return {"chain_id": "aaaabbbb",
            "full": {"path": full, "server": "srv",
                     "timestamp": "20260101_000000"},
            "incrementals": incrs}


# --------------------------------------------------------------------------
def test_can_stage_swap():
    print("can_stage_swap:")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mc = root / "server"; mc.mkdir()
        backups = root / "backups"; backups.mkdir()

        ok, why = can_stage_swap(mc, backups)
        check(ok, f"normal layout is eligible ({why or 'no reason'})")

        # backup_dir nested inside the server dir would be carried away by the
        # rename, orphaning every zip.
        nested = mc / "backups"; nested.mkdir()
        ok, why = can_stage_swap(mc, nested)
        check(not ok and "inside" in why, f"nested backup_dir refused ({why})")

        ok, why = can_stage_swap(root / "missing", backups)
        check(not ok, f"missing server dir refused ({why})")

        if hasattr(os, "symlink"):
            link = root / "link"
            try:
                os.symlink(mc, link, target_is_directory=True)
            except (OSError, NotImplementedError, AttributeError):
                print("  skip symlink case (not permitted on this host)")
            else:
                ok, why = can_stage_swap(link, backups)
                check(not ok and "symlink" in why, f"symlink refused ({why})")


def test_swap_success():
    print("swap_in_staging (success):")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mc = root / "server"; mc.mkdir()
        (mc / "world").mkdir()
        (mc / "world" / "level.dat").write_text("OLD")
        (mc / "console.log").write_text("console history")

        staging = root / "server.staging"; staging.mkdir()
        (staging / "world").mkdir()
        (staging / "world" / "level.dat").write_text("NEW")

        old = swap_in_staging(staging, mc, {"console.log"}, lambda m: None)

        check((mc / "world" / "level.dat").read_text() == "NEW",
              "restored world is in place")
        check((mc / "console.log").read_text() == "console history",
              "preserved console.log carried across the swap")
        check(old.exists() and (old / "world" / "level.dat").read_text() == "OLD",
              "previous world kept aside for rollback")
        check(not staging.exists(), "staging dir consumed by the rename")


def test_swap_rollback():
    print("swap_in_staging (rollback):")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mc = root / "server"; mc.mkdir()
        (mc / "world").mkdir()
        (mc / "world" / "level.dat").write_text("OLD")
        (mc / "console.log").write_text("history")
        staging = root / "server.staging"; staging.mkdir()
        (staging / "world").mkdir()
        (staging / "world" / "level.dat").write_text("NEW")

        real_rename, calls = os.rename, {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 2:          # the staging -> target rename
                raise OSError("simulated swap failure")
            return real_rename(a, b)

        os.rename = flaky
        try:
            swap_in_staging(staging, mc, {"console.log"}, lambda m: None)
            check(False, "should have raised SwapError")
        except SwapError as e:
            check(e.world_intact, "reports the world as intact")
        finally:
            os.rename = real_rename

        check(mc.is_dir() and (mc / "world" / "level.dat").read_text() == "OLD",
              "original world rolled back into place")
        check((mc / "console.log").read_text() == "history",
              "preserved file rolled back too")


def test_structural_validation():
    print("validate_chain_structure:")
    with tempfile.TemporaryDirectory() as td:
        backups = Path(td); chain = make_chain(backups, n_incr=2)
        check(validate_chain_structure(chain, 1) == [], "healthy chain passes")

        # Truncation -- the failure mode actually seen in the wild.
        victim = chain["incrementals"][1]["path"]
        raw = victim.read_bytes()
        victim.write_bytes(raw[:len(raw) // 2])
        problems = validate_chain_structure(chain, 1)
        check(len(problems) == 1 and victim.name in problems[0],
              f"truncated zip caught ({problems[0][:44]}...)")
        check(validate_chain_structure(chain, 0) == [],
              "a point that does not need the bad zip still passes")


def test_structural_is_cheap_vs_crc():
    print("structural check does not decompress:")
    with tempfile.TemporaryDirectory() as td:
        backups = Path(td); chain = make_chain(backups, n_incr=1)
        reads = {"n": 0}
        real_open = zipfile.ZipFile.open

        def counting_open(self, *a, **k):
            reads["n"] += 1
            return real_open(self, *a, **k)

        zipfile.ZipFile.open = counting_open
        try:
            validate_chain_structure(chain, 0)
            structural = reads["n"]
            validate_chain_files(chain, 0)
            crc = reads["n"] - structural
        finally:
            zipfile.ZipFile.open = real_open
        check(structural == 0, f"structural opened 0 entries (was {structural})")
        check(crc > 0, f"CRC pass opened {crc} entries")




# ==========================================================================
# restore_world end-to-end, against a fake backend/server
# ==========================================================================
import types

import core.server as server_mod
from core.server import Server


class FakeBackend:
    def __init__(self, online=True):
        self.online = online
        self.events = []
        self._mux = types.SimpleNamespace(
            send=lambda cmd: self.events.append(("mux", cmd)))

    def probe_stopped(self, timeout=10):
        return not self.online

    def is_online(self):
        return self.online

    def broadcast(self, msg):
        self.events.append(("broadcast", msg))

    def stop_server(self, say):
        self.events.append(("stop", None))
        self.online = False

    def wait_until_stopped(self, timeout=120):
        return True

    def force_stop(self, say):
        return True

    def relaunch(self, say):
        self.events.append(("relaunch", None))
        self.online = True
        return True

    def did(self, kind):
        return [e for e in self.events if e[0] == kind]


def make_server(root: Path, *, backup_dir=None, pre_restore=False,
                exclude=frozenset()):
    """A Server-shaped stub good enough to drive restore_world."""
    mc = root / "server"
    (mc / "world").mkdir(parents=True, exist_ok=True)
    (mc / "world" / "level.dat").write_text("LIVE")
    backups = backup_dir or (root / "backups")
    backups.mkdir(parents=True, exist_ok=True)

    srv = types.SimpleNamespace()
    srv.backend = FakeBackend()
    srv.config = types.SimpleNamespace(
        minecraft_dir=mc, backup_dir=backups, restore_warning_seconds=0,
        pre_restore_backup=pre_restore, backup_copy_cmd="",
        mux_start_cmd=f"cd {mc.as_posix()} && ./run", name="srv")
    srv.backup_exclude_names = set(exclude)
    srv.manifest_path = root / "manifest.json"
    srv.log = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        exception=lambda *a, **k: None)
    srv.said = []
    srv.manifest_writes = []
    srv.reattach_log_watch = lambda: None
    srv.save_manifest = lambda files, chain_id="", base_full="": \
        srv.manifest_writes.append(chain_id)
    srv.run_backup = lambda status_cb=None, offline=False: \
        srv.said.append("ran pre-restore backup")
    # bound the real methods onto the stub
    srv.restore_world = types.MethodType(Server.restore_world, srv)
    srv._prepare_relaunch_cwd = types.MethodType(Server._prepare_relaunch_cwd, srv)
    srv._discard_old_world = types.MethodType(Server._discard_old_world, srv)
    return srv, mc, backups


def run_restore(srv, chain, point_idx=0):
    srv.restore_world(chain, point_idx, say=srv.said.append)
    return srv


def test_staged_restore_end_to_end():
    print("restore_world (staged path):")
    _real = server_mod.reconcile_online
    server_mod.reconcile_online = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            srv, mc, backups = make_server(root, exclude={"console.log"})
            (mc / "console.log").write_text("console history")
            chain = make_chain(backups, n_incr=1)

            run_restore(srv, chain, point_idx=0)

            check((mc / "world" / "level.dat").read_text() == "INCR0",
                  "world restored to the chosen point")
            check((mc / "console.log").read_text() == "console history",
                  "Bedrock console.log survived the swap")
            check(srv.backend.did("stop") and srv.backend.did("relaunch"),
                  "server was stopped and relaunched")
            leftovers = [p.name for p in root.iterdir()
                         if "restore-staging" in p.name or "pre-restore" in p.name]
            check(not leftovers, f"no staging/old dirs left behind ({leftovers})")
            check((mc / ".diamondsign_chain").exists(),
                  "chain marker landed in the live server dir")
            check(srv.manifest_writes == [],
                  "chain was NOT invalidated on a clean restore")
            check(any("still running" in m for m in srv.said),
                  "extraction reported as happening with the server up")
            cds = [c for k, c in srv.backend.events
                   if k == "mux" and c.startswith("cd ")]
            check(cds and mc.as_posix() in cds[0],
                  "pane cwd reset to the server dir before relaunch "
                  "(a start_cmd without its own `cd` would otherwise "
                  "relaunch the pre-restore world)")
    finally:
        server_mod.reconcile_online = _real


def test_staged_decompresses_once():
    print("restore_world (staged) decompresses each zip once:")
    _real = server_mod.reconcile_online
    server_mod.reconcile_online = lambda *a, **k: None
    counts = {"n": 0}
    real_open = zipfile.ZipFile.open

    def counting(self, *a, **k):
        counts["n"] += 1
        return real_open(self, *a, **k)

    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            srv, mc, backups = make_server(root)
            chain = make_chain(backups, n_incr=1)
            # entries the restore must read: 2 from the full + 1 from the
            # incremental (_meta.json is skipped on apply but read for
            # deletions handling)
            zipfile.ZipFile.open = counting
            server_mod.reconcile_online = lambda *a, **k: None
            run_restore(srv, chain, point_idx=0)
            staged = counts["n"]

            # Same restore through the in-place path, which still CRC-verifies
            # up front: it must read strictly more.
            counts["n"] = 0
            srv2, mc2, backups2 = make_server(root / "second")
            chain2 = make_chain(backups2, n_incr=1)
            nested = mc2 / "backups"          # forces the fallback
            nested.mkdir()
            srv2.config.backup_dir = backups2
            orig = restore_core.can_stage_swap
            restore_core.can_stage_swap = lambda *a, **k: (False, "forced")
            try:
                run_restore(srv2, chain2, point_idx=0)
            finally:
                restore_core.can_stage_swap = orig
            in_place = counts["n"]
    finally:
        zipfile.ZipFile.open = real_open
        server_mod.reconcile_online = _real
    check(staged < in_place,
          f"staged reads fewer entries than in-place ({staged} vs {in_place})")


def test_fallback_nested_backup_dir():
    print("restore_world (fallback: backup dir nested in server dir):")
    _real = server_mod.reconcile_online
    server_mod.reconcile_online = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mc = root / "server"; mc.mkdir(parents=True)
            nested = mc / "backups"
            srv, mc, backups = make_server(root, backup_dir=nested)
            chain = make_chain(backups, n_incr=0)

            run_restore(srv, chain, point_idx=-1)

            check((mc / "world" / "level.dat").read_text() == "FULL",
                  "world restored via the in-place path")
            check(nested.is_dir() and any(nested.iterdir()),
                  "nested backup dir survived (would be orphaned by a swap)")
            check(not [c for k, c in srv.backend.events
                       if k == "mux" and c.startswith("cd ")],
                  "no cwd injection on the in-place path (inode unchanged)")
    finally:
        server_mod.reconcile_online = _real


def test_extraction_failure_leaves_server_up():
    print("restore_world (corrupt zip, staged): live world untouched:")
    _real = server_mod.reconcile_online
    server_mod.reconcile_online = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            srv, mc, backups = make_server(root)
            chain = make_chain(backups, n_incr=1)
            # Corrupt the incremental so extraction fails. Written STORED so
            # the flip lands in the entry's data and trips the CRC check
            # rather than the deflate decoder -- precisely the claim the
            # staged design rests on: extraction IS the verification.
            victim = chain["incrementals"][0]["path"]
            with zipfile.ZipFile(victim, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr("world/level.dat", "INCR0-PAYLOAD")
                zf.writestr("_meta.json", '{"chain_id": "aaaabbbb", '
                            '"base_full": "srv_20260101_000000.zip"}')
            raw = bytearray(victim.read_bytes())
            raw[raw.find(b"INCR0-PAYLOAD")] = ord("X")   # breaks its CRC
            victim.write_bytes(bytes(raw))

            run_restore(srv, chain, point_idx=0)

            check((mc / "world" / "level.dat").read_text() == "LIVE",
                  "live world completely untouched")
            check(not srv.backend.did("stop"),
                  "server was never stopped")
            check(srv.backend.is_online(), "server still online")
            check(srv.manifest_writes == [],
                  "chain NOT invalidated (nothing was replaced)")
            leftovers = [p.name for p in root.iterdir()
                         if "restore-staging" in p.name]
            check(not leftovers, f"staging tree cleaned up ({leftovers})")
    finally:
        server_mod.reconcile_online = _real


def test_swap_failure_rolls_back_and_relaunches():
    print("restore_world (swap fails): rollback + relaunch:")
    _real = server_mod.reconcile_online
    server_mod.reconcile_online = lambda *a, **k: None
    real_rename, calls = os.rename, {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated swap failure")
        return real_rename(a, b)

    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            srv, mc, backups = make_server(root)
            chain = make_chain(backups, n_incr=0)
            os.rename = flaky
            try:
                run_restore(srv, chain, point_idx=-1)
            finally:
                os.rename = real_rename

            check((mc / "world" / "level.dat").read_text() == "LIVE",
                  "original world rolled back")
            check(srv.backend.did("relaunch"),
                  "server brought back up (world was intact)")
            check(srv.manifest_writes == [""],
                  "chain invalidated so no incremental extends a stale chain")
    finally:
        os.rename = real_rename
        server_mod.reconcile_online = _real


def test_preflight_done_flag():
    print("preflight_done (restore.py must keep verifying):")
    import inspect
    sig = inspect.signature(restore_core.restore_chain)
    check(sig.parameters["preflight_done"].default is False,
          "defaults to False, so the CLI still CRC-verifies")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backups = root / "b"; backups.mkdir()
        target = root / "t"; target.mkdir()
        chain = make_chain(backups, n_incr=0)
        raw = chain["full"]["path"].read_bytes()
        chain["full"]["path"].write_bytes(raw[:len(raw) // 2])   # truncate

        try:
            restore_core.restore_chain(
                chain, -1, target, backup_dir=backups, exclude_names=set(),
                establish_chain=False)
            check(False, "truncated full should have been refused")
        except restore_core.PreflightError:
            check(True, "default path refuses a truncated chain up front")
        except Exception as e:
            check(False, f"expected PreflightError, got {type(e).__name__}")

        # With preflight_done the guard is skipped -- the caller promised it
        # already checked. Extraction then fails on its own, which is the
        # point: corruption is still caught, just not twice.
        try:
            restore_core.restore_chain(
                chain, -1, target, backup_dir=backups, exclude_names=set(),
                establish_chain=False, preflight_done=True)
            check(False, "corrupt zip should still fail during extraction")
        except restore_core.PreflightError:
            check(False, "preflight ran despite preflight_done=True")
        except Exception:
            check(True, "skips the duplicate check; extraction still catches it")


def main():
    for fn in (test_can_stage_swap, test_swap_success, test_swap_rollback,
               test_structural_validation, test_structural_is_cheap_vs_crc,
               test_staged_restore_end_to_end, test_staged_decompresses_once,
               test_fallback_nested_backup_dir,
               test_extraction_failure_leaves_server_up,
               test_swap_failure_rolls_back_and_relaunches,
               test_preflight_done_flag):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): " + "; ".join(_failures))
        return 1
    print("ALL RESTORE STAGING TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
