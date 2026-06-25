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


def open_db(db_path, writable: bool = False):
    """Open a Bedrock world LevelDB. Raises if another process holds the lock
    (i.e. the server is still running) when writable."""
    leveldb = _leveldb()
    return leveldb.LevelDB(str(db_path), bool(writable))


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
    """Overwrite one player's value in a writable (server-stopped) db."""
    db = open_db(db_path, writable=True)
    try:
        db.put(key.encode("latin1"), value)
    finally:
        db.close()


def backup_player_value(db_path, key: str, dest: Path) -> bytes | None:
    """Save the current value of ``key`` to ``dest`` (undo blob). Returns the
    bytes, or None if the key is absent. Opens read-only."""
    db = open_db(db_path, writable=False)
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
