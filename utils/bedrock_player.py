"""Bedrock per-player data: read/write the world LevelDB and the backup sidecar.

Bedrock has no per-player files — every remote player's data is a single
little-endian-NBT value in the world LevelDB under the key
``player_server_<uuid>`` (the uuid is a stable per-server identity, unrelated to
the player's xuid). Restoring one player = overwriting that one key's raw bytes.

Because the live db is locked while the server runs, the bot reads players only
from a **consistent copy** (the save-query-truncated db materialised during a
backup) and writes only when the server is stopped.

The heavy deps (``leveldb`` = amulet-leveldb, ``amulet_nbt``) are imported
lazily so this module imports cleanly on Java-only hosts / Pythons without the
wheels; only the functions that touch the db require them.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Sidecar file embedded in each Bedrock backup zip (excluded from restore via
# backup_utils.META_FILES). Maps player_server key -> value + hash + identity.
SIDECAR_NAME = "_players.json"

PLAYER_SERVER_PREFIX = b"player_server_"
# Mapping keys (player_<MsaId>/<SelfSignedId>) carry {MsaId, SelfSignedId,
# ServerId}; ServerId points at the data key. Kept for diagnostics — the xuid
# is not present anywhere in the db, so these don't yield a name->key link.
_MAPPING_PREFIX = b"player_"


def _leveldb():
    import leveldb  # amulet-leveldb
    return leveldb


def world_db_path(minecraft_dir) -> Path:
    """The Bedrock world LevelDB directory: ``<dir>/worlds/<level-name>/db``."""
    from .config import get_level_name
    return Path(minecraft_dir) / "worlds" / get_level_name(minecraft_dir) / "db"


# NOTE: there used to be an is_db_locked() here that inferred "server running"
# from the world LevelDB lock. It was removed: BDS (at least this build) never
# takes an fcntl lock on the world db — there is no LOCK file even while the
# server runs — so the check always reported "unlocked", AND it opened the live
# world db via amulet (a second LevelDB instance against a db the server is
# writing) just to find out, risking corruption. Detect liveness from the
# console instead (BedrockBackend.is_online / wait_until_stopped send `list` and
# watch console.log); never open the world db to probe a running server.


def open_db(db_path, create: bool = False):
    """Open a Bedrock world LevelDB (always read-write — amulet-leveldb has no
    read-only mode, so it acquires the LOCK and raises if another process, i.e.
    the running server, holds it). ``create`` only controls create-if-missing;
    callers operate on backups/copies or the stopped live db, so it stays False.
    """
    leveldb = _leveldb()
    return leveldb.LevelDB(str(db_path), bool(create))


def value_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_player_values(db) -> dict:
    """Return {key_str: value_bytes} for every player_server_* key."""
    out = {}
    for k in db.keys():
        if k.startswith(PLAYER_SERVER_PREFIX):
            out[k.decode("latin1")] = db.get(k)
    return out


def read_mappings(db) -> list:
    """Return [{key, MsaId, SelfSignedId, ServerId}] for the player_<id> mapping
    keys. Diagnostic only — not used for name resolution (no xuid inside)."""
    import amulet_nbt
    rows = []
    for k in db.keys():
        if k.startswith(_MAPPING_PREFIX) and not k.startswith(PLAYER_SERVER_PREFIX):
            try:
                comp = amulet_nbt.load(db.get(k), little_endian=True,
                                       compressed=False).compound
                rows.append({
                    "key": k.decode("latin1"),
                    "MsaId": str(comp.get("MsaId")),
                    "SelfSignedId": str(comp.get("SelfSignedId")),
                    "ServerId": str(comp.get("ServerId")),
                })
            except Exception:
                pass
    return rows


def lookup_server_key(db, identity_uuid: str) -> str | None:
    """Resolve an MsaId (or SelfSignedId) to its ``player_server_<uuid>`` data
    key by reading the ``player_<identity_uuid>`` mapping's ``ServerId`` field.

    This is the in-db half of the chain. The xuid->MsaId half is not in the db
    (MsaId is account-stable but not derivable), so it's learned once and kept
    in bedrock_players.json. Returns the data key string, or None if no mapping.
    """
    import amulet_nbt
    raw = db.get(("player_" + identity_uuid).encode("latin1"))
    if raw is None:
        return None
    try:
        comp = amulet_nbt.load(raw, little_endian=True, compressed=False).compound
        server = comp.get("ServerId")
    except Exception:
        return None
    return str(server) if server is not None else None


def write_player_value(db_path, key: str, value: bytes) -> None:
    """Overwrite one player's value in the (server-stopped) live db."""
    db = open_db(db_path)
    try:
        db.put(key.encode("latin1"), value)
    finally:
        db.close()


def backup_player_value(db_path, key: str, dest: Path) -> bytes | None:
    """Save the current value of ``key`` to ``dest`` (undo blob). Returns the
    bytes, or None if the key is absent."""
    db = open_db(db_path)
    try:
        cur = db.get(key.encode("latin1"))
    finally:
        db.close()
    if cur is None:
        return None
    Path(dest).write_bytes(cur)
    return cur


def read_identity_map(db) -> dict:
    """{identity_uuid: 'player_server_<uuid>'} for every player_<identity>
    mapping key (both the MsaId and SelfSignedId aliases). This is the in-db
    half of the resolution chain (MsaId -> data key)."""
    return {m["key"][len("player_"):]: m["ServerId"] for m in read_mappings(db)}


# --- sidecar: stored in each backup zip, values hex-encoded to stay JSON-safe.
# Structure: {"players": {server_key: {sha256, hex}}, "mappings": {ident: server_key}}
def build_player_sidecar(db) -> dict:
    """Build the full sidecar (player values + identity mappings) from an open
    db, so a restore resolves entirely offline: MsaId -> mapping -> value."""
    players = read_player_values(db)
    return {
        "players": {k: {"sha256": value_hash(v), "hex": v.hex()}
                    for k, v in players.items()},
        "mappings": read_identity_map(db),
    }


def build_sidecar_from_files(db_files, tmp_dir=None) -> dict:
    """Build the sidecar from a save-query file set.

    ``db_files`` is an iterable of ``(abs_path, max_bytes)`` for the world db
    files (the live db is locked, so we copy each truncated to its snapshot
    length into a temp dir — a consistent, openable db — then read it).

    ``tmp_dir`` hosts that temp copy. Callers should pass a real-disk
    directory (e.g. the backup dir): the default system tmp is a tmpfs on
    typical hosts, where every byte written is RAM taken at exactly the
    backup's peak-memory moment (the OOM kills hit this step).
    """
    tmp = Path(tempfile.mkdtemp(
        prefix="mcn_sidecar_",
        dir=str(tmp_dir) if tmp_dir is not None else None))
    try:
        for path, max_bytes in db_files:
            with open(path, "rb") as src:
                (tmp / Path(path).name).write_bytes(src.read(max_bytes))
        db = open_db(tmp)
        try:
            return build_player_sidecar(db)
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Command that launches the sidecar-build worker subprocess (module runs
# itself; see __main__ at the bottom). A module constant so tests can swap in
# a stub worker.
_WORKER_ARGS = [sys.executable, "-m", "utils.bedrock_player"]


def build_sidecar_subprocess(db_files, tmp_dir=None, timeout: float = 180) -> dict:
    """Build the sidecar in a short-lived subprocess and return it.

    Isolation, not parallelism: the amulet/LevelDB native layer retains
    memory across in-process open/close cycles — after ~26h of 5-minute
    incremental backups the bot reached ~5.5 GB of anonymous memory and was
    OOM-killed (2026-07-18; the 07-11 and 07-16 kills were the same growth).
    A child process gives every build a fresh address space and returns all
    of it to the OS on exit, whatever the native layer leaks.

    Same inputs/outputs as ``build_sidecar_from_files`` (which the worker
    calls); raises RuntimeError on worker failure or timeout.
    """
    req = json.dumps({"files": [[str(p), int(n)] for p, n in db_files],
                      "tmp_dir": str(tmp_dir) if tmp_dir is not None else None})
    try:
        res = subprocess.run(
            _WORKER_ARGS, input=req, capture_output=True, text=True,
            timeout=timeout, cwd=str(Path(__file__).resolve().parent.parent))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"sidecar worker timed out after {timeout:.0f}s")
    if res.returncode != 0:
        raise RuntimeError("sidecar worker failed (rc="
                           f"{res.returncode}): {res.stderr.strip()[-500:]}")
    try:
        return json.loads(res.stdout)
    except ValueError:
        raise RuntimeError("sidecar worker produced unparseable output: "
                           f"{res.stdout[:200]!r}")


def _sidecar_worker_main() -> None:
    """Worker entry (``python -m utils.bedrock_player``): read the request
    JSON ({files: [[path, max_bytes], ...], tmp_dir}) on stdin, build the
    sidecar in THIS process, write it as JSON to stdout, exit. Any exception
    escapes to a non-zero exit with the traceback on stderr."""
    req = json.load(sys.stdin)
    files = [(Path(p), int(n)) for p, n in req["files"]]
    tmp_dir = req.get("tmp_dir")
    json.dump(build_sidecar_from_files(files, tmp_dir=tmp_dir), sys.stdout)


def filter_sidecar_changed(sidecar: dict, prev_hashes: dict) -> tuple:
    """Return ``(filtered_sidecar, new_hashes)``.

    ``filtered_sidecar`` keeps only players whose value hash differs from
    ``prev_hashes`` (pass ``{}`` to keep everyone, e.g. for a full backup), plus
    the mapping entries pointing at the kept players. ``new_hashes`` is the
    complete current ``{server_key: sha256}`` for the caller to persist as the
    new dedup state.
    """
    players = sidecar.get("players", {})
    mappings = sidecar.get("mappings", {})
    new_hashes = {k: e["sha256"] for k, e in players.items()}
    changed = {k: e for k, e in players.items()
               if prev_hashes.get(k) != e["sha256"]}
    filt_maps = {ident: sk for ident, sk in mappings.items() if sk in changed}
    return {"players": changed, "mappings": filt_maps}, new_hashes


def read_sidecar(zip_path) -> dict:
    """Read SIDECAR_NAME from a backup zip; {} if absent/unreadable."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if SIDECAR_NAME in zf.namelist():
                return json.loads(zf.read(SIDECAR_NAME))
    except Exception:
        pass
    return {}


def sidecar_value(entry: dict) -> bytes:
    """Decode a sidecar player entry's value bytes."""
    return bytes.fromhex(entry["hex"])


def resolve_from_sidecar(sidecar: dict, identity_uuid: str):
    """Given a sidecar and a player's MsaId/SelfSignedId, return
    (server_key, value_bytes) or None. Pure offline resolution."""
    server_key = sidecar.get("mappings", {}).get(identity_uuid)
    if not server_key:
        return None
    entry = sidecar.get("players", {}).get(server_key)
    if not entry:
        return None
    return server_key, sidecar_value(entry)


if __name__ == "__main__":
    _sidecar_worker_main()
