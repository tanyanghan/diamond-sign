"""Self-contained tests for Bedrock LevelDB key lookups.

Run with plain ``python tests/test_bedrock_player.py`` -- no pytest, and no
amulet wheels: the db is a stub that behaves like amulet-leveldb, whose
``get`` raises KeyError for a missing key rather than returning None.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import bedrock_player

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


class StubDB:
    """amulet-leveldb's get(): KeyError for a missing key."""

    def __init__(self, data):
        self.data = dict(data)

    def get(self, key):
        return self.data[key]

    def close(self):
        pass


IDENT = "697f095d-d606-3494-a367-d8a295e0dde3"


def test_lookup_missing_mapping():
    print("lookup_server_key")
    try:
        got = bedrock_player.lookup_server_key(StubDB({}), IDENT)
    except KeyError as e:
        got = f"raised KeyError {e}"
    check(got is None, f"missing mapping -> None (got {got!r})")


def test_backup_missing_value():
    print("backup_player_value")
    key = "player_server_5f0c9a3e-0000-0000-0000-000000000000"
    orig = bedrock_player.open_db
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "undo.nbt"
        try:
            bedrock_player.open_db = lambda _p: StubDB({})
            try:
                got = bedrock_player.backup_player_value(Path(tmp), key, dest)
            except KeyError as e:
                got = f"raised KeyError {e}"
            check(got is None and not dest.exists(),
                  f"missing value -> None, no undo file (got {got!r})")

            bedrock_player.open_db = lambda _p: StubDB(
                {key.encode("latin1"): b"\x0a\x00\x00"})
            got = bedrock_player.backup_player_value(Path(tmp), key, dest)
            check(got == b"\x0a\x00\x00" and dest.read_bytes() == got,
                  "present value -> bytes, saved to the undo file")
        finally:
            bedrock_player.open_db = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all passed")
