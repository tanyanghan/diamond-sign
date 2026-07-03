import argparse
import gzip
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from utils.backup_utils import (
    CHAIN_MARKER_NAME, CHAIN_MARKER_NAME_LEGACY, META_FILES, RE_FULL, RE_INCR,
    build_file_manifest, new_chain_id, run_copy_command, wait_for_settle,
)
from utils.config import (
    load_config, backup_exclude_names, EDITION_BEDROCK, ConfigError,
)
from utils import restore_core
from backends import (
    make_backend, BackendUnavailable, CAP_PLAYER_RESTORE, CAP_STATS,
    EVENT_DEATH, EVENT_ACHIEVEMENT,
)
from chat import make_adapters, CommandRouter

# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
# All config reading lives in config.load_config(), which returns an AppConfig
# (bots -> servers). load_config() reads diamondsign.json, migrates a legacy
# .env, or runs a first-run wizard; on a misconfiguration it raises ConfigError,
# which we surface cleanly and stop.
try:
    APP_CONFIG = load_config()
except ConfigError as e:
    print(f"\n{e}\n", file=sys.stderr)
    sys.exit(1)

# main() builds a Server per server-config and a Bot per bot-config from
# APP_CONFIG and loops over all of them (see main / _bring_up_*). No module-level
# server/bot/backend singletons — every instance carries its own config, backend,
# and state.

# Per-server state lives under data/<server-name>/ so multiple servers in one
# process never collide. (auth.json stays at the repo root — it's per-bot, not
# per-server.) Each Server resolves its own paths under config.data_dir; see
# Server._data_path.


# ---------------------------------------------------------------------------
# Logging setup (configured in main, used everywhere via module-level logger)
# ---------------------------------------------------------------------------
logger = logging.getLogger("mcnotifier")


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"log_{timestamp}.txt"

    fmt = logging.Formatter("%(asctime)s  %(name)-12s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    telebot_logger = logging.getLogger("TeleBot")
    telebot_logger.addHandler(file_handler)

    logger.info("Logging started — writing to %s", log_file)




# ---------------------------------------------------------------------------
# 2. Player Name Registry
# ---------------------------------------------------------------------------
# The {player_id: name} registry is owned by the backend (Java: player_names.json
# keyed by UUID; Bedrock: projected from bedrock_players.json keyed by xuid) and
# mirrored in ``server.names``; these helpers operate on a given Server.
_names_lock = threading.Lock()


def refresh_player_names(server) -> None:
    with _names_lock:
        server.names.clear()
        server.names.update(server.backend.load_names())


def register_player(server, uuid: str, name: str) -> None:
    names = server.names
    old = names.get(uuid)
    names[uuid] = name
    changed = server.backend.register_name(uuid, name)
    if old and old != name:
        server.log.info("Player registry: %s renamed %s -> %s", uuid, old, name)
    elif changed and not old:
        server.log.info("Player registry: registered %s (%s)", name, uuid)


def _uuid_by_name(player_name: str, names: dict) -> str | None:
    for uuid, name in names.items():
        if name == player_name:
            return uuid
    return None


# ---------------------------------------------------------------------------
# 2b. Achievements Storage
# ---------------------------------------------------------------------------
_achievements_lock = threading.Lock()


def load_achievements(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load %s", path)
    return {}


def _save_achievements(achievements: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(achievements, f, indent=2)


def record_achievement(uuid: str, achievement: str, ach_type: str,
                       timestamp: str, achievements: dict, path: Path) -> bool:
    with _achievements_lock:
        entries = achievements.setdefault(uuid, [])
        for e in entries:
            if e["achievement"] == achievement and e["timestamp"] == timestamp:
                return False
        entries.append({
            "achievement": achievement,
            "type": ach_type,
            "timestamp": timestamp,
        })
        _save_achievements(achievements, path)
    return True


# ---------------------------------------------------------------------------
# 2c. Deaths Storage
# ---------------------------------------------------------------------------
_deaths_lock = threading.Lock()


def load_deaths(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load %s", path)
    return {}


def _save_deaths(deaths: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(deaths, f, indent=2)


def record_death(uuid: str, message: str, timestamp: str,
                 deaths: dict, path: Path) -> bool:
    with _deaths_lock:
        entries = deaths.setdefault(uuid, [])
        for e in entries:
            if e["message"] == message and e["timestamp"] == timestamp:
                return False
        entries.append({"message": message, "timestamp": timestamp})
        _save_deaths(deaths, path)
    return True


# ---------------------------------------------------------------------------
# 3. Server runtime object (per-server state)
# ---------------------------------------------------------------------------
class _TagLogAdapter(logging.LoggerAdapter):
    """Prefix every record with [<tag>] so interleaved multi-bot / multi-server
    logs stay attributable (per-server backups/notifications, per-bot commands)."""

    def process(self, msg, kwargs):
        return f"[{self.extra['tag']}] {msg}", kwargs


class Server:
    """One Minecraft server's runtime: its config, backend, and per-server
    mutable state (online players, session xuids, pending UUID correlation, and
    — added in later sub-steps — backup state). main() builds one of these
    today and will loop over several once multi-server lands; the module-level
    aliases below keep existing call sites working in the meantime.
    """

    def __init__(self, config, migrate_legacy=False):
        self.config = config
        self.log = _TagLogAdapter(logger, {"tag": config.name})
        self.backend = None  # set in main() after make_backend
        self.watcher = None  # LogWatcher, set when the server is brought up
        self.observer = None  # watchdog Observer, stopped on shutdown
        # Only the historical single-server install has state at the repo root;
        # that server migrates it into data/<key>/ once. Additional servers in a
        # multi-server config start clean and never pull from the root.
        self._migrate_legacy = migrate_legacy
        self.data_dir = config.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.online_players: set = set()
        self.online_lock = threading.Lock()
        # Serializes reconcile_online passes for this server (a /status and an
        # on_server_start resync may race).
        self.reconcile_lock = threading.Lock()

        # Bedrock identity learning: xuids seen online since the last backup
        # (kept even after a player leaves, so a short session that only triggers
        # a post-leave backup is still attributable). Pruned to the still-online
        # set after each learn attempt. See _maybe_learn_player.
        self.session_xuids: set = set()
        self.session_lock = threading.Lock()

        # name -> uuid, populated by the UUID log line, consumed by the join line.
        self.pending_uuids: dict = {}

        # Backup state (per-server). The manifest lives under data/<key>/; the
        # chain marker lives in the world dir (so an offline restore is
        # detectable). incr_timer is the self-rescheduling incremental cycle.
        self.backup_lock = threading.Lock()
        self.incr_lock = threading.Lock()      # protects incr_timer
        self.incr_timer: threading.Timer | None = None
        # Files excluded from backups and the change manifest in addition to the
        # chain marker: bot infrastructure that lives in the server directory but
        # isn't server data (e.g. the captured-stdout console.log the bot tails
        # on Bedrock). Matched by basename anywhere in the tree.
        self.backup_exclude_names = frozenset(backup_exclude_names(config))
        self.manifest_path = self._data_path("backup_manifest.json")
        self.chain_marker_path = self.config.minecraft_dir / CHAIN_MARKER_NAME
        self.chain_marker_path_legacy = (self.config.minecraft_dir
                                         / CHAIN_MARKER_NAME_LEGACY)
        # Bedrock per-player restore reads player data from a sidecar embedded in
        # each backup zip (the live LevelDB is locked while the server runs).
        # Dedup state of {player_server_key: sha256} so incrementals only carry
        # players that changed.
        self.player_state_path = self._data_path("bedrock_player_state.json")

        # Player-facing state (populated by load_state() once the backend is set):
        # the {player_id: name} registry (backend-sourced), plus recorded
        # achievements and deaths keyed by player id.
        self.achievements_path = self._data_path("player_achievements.json")
        self.deaths_path = self._data_path("player_deaths.json")
        self.names: dict = {}
        self.achievements: dict = {}
        self.deaths: dict = {}

    def _data_path(self, filename: str) -> Path:
        """Resolve a per-server state file under data/<key>/. For the migrated
        single-server install (migrate_legacy=True), a legacy repo-root copy is
        moved into place once so its data survives the relocation."""
        target = self.data_dir / filename
        if self._migrate_legacy:
            legacy = Path(__file__).parent / filename
            if not target.exists() and legacy.exists():
                try:
                    shutil.move(str(legacy), str(target))
                except OSError:
                    self.log.warning("Could not migrate %s into %s",
                                   filename, self.data_dir)
        return target

    def load_state(self) -> None:
        """Load this server's player registry, achievements, and deaths. Called
        in main() once the backend exists (the name registry is backend-sourced)."""
        self.names = self.backend.load_names()
        self.achievements = load_achievements(self.achievements_path)
        self.deaths = load_deaths(self.deaths_path)

    def player_join(self, name: str) -> None:
        with self.online_lock:
            self.online_players.add(name)

    def player_leave(self, name: str) -> None:
        with self.online_lock:
            self.online_players.discard(name)

    def get_online_players(self) -> list:
        with self.online_lock:
            return sorted(self.online_players)

    def note_active_xuid(self, xuid: str) -> None:
        if xuid:
            with self.session_lock:
                self.session_xuids.add(xuid)

    # --- Backup manifest + chain marker -------------------------------------

    def load_manifest(self) -> tuple:
        """Load backup_manifest.json. Returns (chain_id, base_full, files_dict).

        Returns ("", "", {}) if the manifest is missing or corrupt, which
        effectively means "no chain established — skip incremental backups".
        """
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path) as f:
                    data = json.load(f)
                return (data.get("chain_id", ""),
                        data.get("base_full", ""),
                        data.get("files", {}))
            except Exception:
                self.log.exception("Failed to load backup_manifest.json")
        return "", "", {}

    def save_manifest(self, files: dict, chain_id: str, base_full: str) -> None:
        """Write the manifest with the current chain state and file mtimes."""
        with open(self.manifest_path, "w") as f:
            json.dump({"chain_id": chain_id, "base_full": base_full,
                       "files": files}, f)

    def write_chain_marker(self, chain_id: str) -> None:
        """Write chain ID to the chain marker (CHAIN_MARKER_NAME) in the world dir.

        This marker file lets the bot detect on startup if the server state
        was replaced while it was offline (e.g., manual restore). If the marker
        doesn't match the manifest's chain_id, the chain is considered invalid.
        A leftover legacy marker is removed so only one marker remains.
        """
        try:
            with open(self.chain_marker_path, "w") as f:
                f.write(chain_id)
            if self.chain_marker_path_legacy.exists():
                self.chain_marker_path_legacy.unlink()
        except Exception:
            self.log.exception("Failed to write chain marker")

    def read_chain_marker(self) -> str:
        """Read the chain ID from the chain marker, falling back to the legacy
        (.mcnotifier_chain) name so pre-rename installs keep their chain. '' if
        neither exists."""
        for path in (self.chain_marker_path, self.chain_marker_path_legacy):
            try:
                return path.read_text().strip()
            except FileNotFoundError:
                continue
            except Exception:
                self.log.exception("Failed to read chain marker")
                return ""
        return ""

    # --- Bedrock per-player sidecar / identity learning ---------------------

    def load_player_state(self) -> dict:
        try:
            return json.loads(self.player_state_path.read_text())
        except FileNotFoundError:
            return {}
        except Exception:
            self.log.exception("Failed to read bedrock_player_state.json")
            return {}

    def save_player_state(self, hashes: dict) -> None:
        try:
            self.player_state_path.write_text(json.dumps(hashes))
        except Exception:
            self.log.exception("Failed to write bedrock_player_state.json")

    def maybe_learn_player(self, sidecar: dict, log) -> None:
        """Bind a player's xuid to their (account-stable) identity uuids by
        process of elimination from a backup's changed-player sidecar.

        The xuid->identity link isn't in the world db, so it's inferred: the
        changed sidecar lists exactly the players whose data changed since the
        last backup, grouped into server keys (each with its MsaId+SelfSignedId).
        If exactly one of those keys is still unattributed AND exactly one xuid
        that was online since the last backup still has no identities, they must
        be the same player — bind them. Handles a lone player (even one who
        already left) and "N online, N-1 already known". Ambiguous cases are
        skipped and retried next backup.

        Lives here (not the backend) because it needs the server's session/online
        state and the name registry; the backend supplies the known-identity
        facts.
        """
        # Changed server keys -> their identity uuids.
        by_key: dict = {}
        for ident, key in sidecar.get("mappings", {}).items():
            by_key.setdefault(key, []).append(ident)
        if not by_key:
            return

        known = self.backend.known_identities()
        unattributed = [k for k, idents in by_key.items()
                        if not any(i in known for i in idents)]

        names = self.backend.load_names()
        with self.session_lock:
            session = list(self.session_xuids)
        unknown_active = [x for x in session if not self.backend.player_identities(x)]

        if len(unattributed) == 1 and len(unknown_active) == 1:
            key = unattributed[0]
            xuid = unknown_active[0]
            name = names.get(xuid, xuid)
            if self.backend.learn_player(name, xuid, by_key[key]):
                log(f"Learned Bedrock identity for {name}")

        # Drop players who have left; keep still-online ones for the next backup.
        online_xuids = {_uuid_by_name(n, names) for n in self.get_online_players()}
        online_xuids.discard(None)
        with self.session_lock:
            self.session_xuids.intersection_update(online_xuids)

    def write_player_sidecar(self, zf, ready, full_backup: bool, log) -> None:
        """Embed the _players.json sidecar into an open Bedrock backup zip.

        No-op for Java or when there's no snapshot file set. Generates the
        sidecar from the snapshot db files, hash-dedups against the persisted
        state (full backup = baseline/everyone + reset state; incremental =
        changed-only), and never fails the backup — if the amulet libs are
        missing it just logs and skips, so per-player restore is unavailable for
        that zip but the backup is otherwise complete.
        """
        if self.config.edition != EDITION_BEDROCK or not ready:
            return
        try:
            from utils import bedrock_player
        except Exception as e:
            log(f"Player sidecar skipped (amulet libs unavailable: {e})")
            return
        db_files = [(p, n) for (p, n) in ready
                    if "/db/" in str(p).replace("\\", "/")]
        if not db_files:
            return
        try:
            sidecar = bedrock_player.build_sidecar_from_files(db_files)
            prev = {} if full_backup else self.load_player_state()
            filtered, new_hashes = bedrock_player.filter_sidecar_changed(sidecar, prev)
            zf.writestr(bedrock_player.SIDECAR_NAME, json.dumps(filtered))
            self.save_player_state(new_hashes)
            log(f"Player sidecar: {len(filtered['players'])} player(s) "
                f"({len(new_hashes)} total)")
            self.maybe_learn_player(filtered, log)
        except Exception as e:
            self.log.exception("Player sidecar generation failed")
            log(f"Player sidecar generation failed: {e}")

    # --- Full backup --------------------------------------------------------

    def run_backup(self, status_cb=None):
        """Run a full server backup.

        Full backup process:
          1. begin_save: freeze the world and flush pending writes
             (Java: save-off + save-all; Bedrock: save hold).
          2. Determine the consistent file set (files_ready):
             - Java returns None -> wait for the filesystem to settle, then zip
               the whole server directory.
             - Bedrock returns [(path, max_bytes), ...] -> zip each file
               truncated to its snapshot length.
          3. end_save: resume normal saving (always, even if zip fails).
          4. Run BACKUP_COPY_CMD if configured (e.g., rsync to remote storage).
          5. Start a new incremental chain: generate a fresh chain ID, rebuild
             the file manifest (mtime baseline), and write the chain marker.
        """
        backup_dir = self.config.backup_dir

        def status(msg):
            self.log.info("Backup: %s", msg)
            if status_cb:
                status_cb(msg)

        if not self.backend.is_available():
            raise RuntimeError("Server backend not available")

        backup_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Freeze the world and flush pending writes (edition-specific).
        self.backend.begin_save(status)

        mc_dir = self.config.minecraft_dir
        # Uses relative paths inside the zip so the restore tool can extract
        # directly into any target directory.
        dir_name = mc_dir.name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_path = backup_dir / f"{dir_name}_{timestamp}.zip"
        backup_dir_resolved = backup_dir.resolve()

        try:
            # Step 2: Get the consistent file set to copy. Java returns None (copy
            # the whole settled directory); Bedrock returns snapshot byte-lengths
            # so the walk truncates listed files and skips stale, unlisted db/
            # files.
            ready = self.backend.files_ready(status)
            ready_map = None
            if ready is None:
                # Java: the server may still be flushing to disk after save-all,
                # so wait for the filesystem to settle before zipping.
                wait_for_settle(mc_dir, backup_dir, log_fn=status,
                                exclude_names=self.backup_exclude_names)
            else:
                ready_map = {str(p.relative_to(mc_dir)).replace("\\", "/"): n
                             for p, n in ready}

            status(f"Zipping {mc_dir} ...")
            with zipfile.ZipFile(final_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for dirpath, _dirnames, filenames in os.walk(mc_dir):
                    dp = Path(dirpath)
                    # Skip the backup directory if it's inside the server dir
                    try:
                        dp.resolve().relative_to(backup_dir_resolved)
                        continue
                    except ValueError:
                        pass
                    for fn in filenames:
                        # Chain marker (new + legacy), backup-format entries
                        # (META_FILES — the sidecar/meta/deletions the bot writes
                        # into the zip itself), and bot infrastructure are not
                        # server data. Skipping META_FILES avoids a "Duplicate
                        # name" if a copy lingers in the world dir (e.g. from a
                        # restore extraction).
                        if fn in (CHAIN_MARKER_NAME, CHAIN_MARKER_NAME_LEGACY) \
                                or fn in META_FILES \
                                or fn in self.backup_exclude_names:
                            continue
                        fp = dp / fn
                        rel = str(fp.relative_to(mc_dir)).replace("\\", "/")
                        _add_world_file_to_zip(zf, fp, rel, ready_map)
                # Bedrock: embed the full player-data sidecar (baseline).
                self.write_player_sidecar(zf, ready, full_backup=True, log=status)
            size_mb = final_path.stat().st_size / (1024 * 1024)
            status(f"Backup saved: {final_path.name} ({size_mb:.1f} MB)")
        finally:
            # Step 3: Always resume normal saving, even if zip fails
            self.backend.end_save(status)

        # Step 4: Copy off-server if configured (e.g., rsync to NAS/cloud)
        run_copy_command(final_path, self.config.backup_copy_cmd, log_fn=status)

        # Step 5: Start a new incremental chain
        # Every full backup starts a fresh chain. The manifest records the mtime
        # of every file, which becomes the baseline for detecting changes in
        # subsequent incremental backups. The chain marker is written to the
        # server directory so the bot can detect if the server state is replaced
        # while it's offline.
        try:
            chain_id = new_chain_id(backup_dir)
            fresh_files = build_file_manifest(mc_dir, backup_dir,
                                              self.backup_exclude_names)
            self.save_manifest(fresh_files, chain_id=chain_id,
                               base_full=final_path.name)
            self.write_chain_marker(chain_id)
            self.log.info("Backup: new chain %s established (base: %s)",
                        chain_id, final_path.name)
        except Exception:
            self.log.exception("Failed to reset incremental manifest after full backup")

        return str(final_path)

    # --- Incremental backup -------------------------------------------------

    def run_incremental_backup(self) -> str | None:
        """Run an incremental backup of changed files. Returns zip path or None.

        Incremental backup process:
          1. Load the manifest to get the current chain_id and file mtime
             baseline.
          2. Walk the server directory and compare mtimes to detect changes.
          3. If changes found: RCON save-off/save-all to flush world data, then
             re-scan to capture any newly flushed changes.
          4. Zip only the changed/added files, plus _deletions.json and
             _meta.json.
          5. Update the manifest with the new file mtimes (same chain_id).
          6. RCON save-on to re-enable auto-save.
          7. Run BACKUP_COPY_CMD if configured.

        Returns None if: no chain established, no changes detected, another
        backup is in progress, or the backup fails.
        """
        backup_dir = self.config.backup_dir

        if not self.backup_lock.acquire(blocking=False):
            self.log.info("Incremental backup skipped: another backup is in progress")
            return None

        try:
            mc_dir = self.config.minecraft_dir
            chain_id, base_full, old_files = self.load_manifest()

            # A chain must be established (by a full backup or restore) before
            # incremental backups can run. Without a chain, we don't know which
            # full backup these incrementals belong to.
            if not chain_id:
                self.log.warning("Incremental backup skipped: no chain established. "
                               "Run a full backup first.")
                return None

            # First pass: quick scan to see if anything changed at all
            new_manifest = build_file_manifest(mc_dir, backup_dir,
                                               self.backup_exclude_names)

            changed, deleted = _diff_manifest(old_files, new_manifest)
            if not changed and not deleted:
                self.log.info("Incremental backup: no changes detected, skipping")
                return None

            self.log.info("Incremental backup: %d changed/added, %d deleted",
                         len(changed), len(deleted))

            if not self.backend.is_available():
                self.log.warning("Incremental backup skipped: server backend not available")
                return None

            backup_dir.mkdir(parents=True, exist_ok=True)
            inc_log = lambda msg: self.log.info("Incremental backup: %s", msg)

            # Freeze the world state and flush pending writes (edition-specific)
            # — ensures we zip consistent file state, not partially-written files.
            self.backend.begin_save(inc_log)

            try:
                # Determine the consistent file set, then re-scan to capture any
                # changes flushed by the save before computing the final diff.
                ready = self.backend.files_ready(inc_log)
                if ready is None:
                    # Java: the server may still be flushing after the save, so
                    # wait for the filesystem to settle before diffing.
                    new_manifest = wait_for_settle(mc_dir, backup_dir, log_fn=inc_log,
                                                   exclude_names=self.backup_exclude_names)
                    ready_map = None
                else:
                    # Bedrock: snapshot lengths are authoritative; no settle needed.
                    new_manifest = build_file_manifest(mc_dir, backup_dir,
                                                       self.backup_exclude_names)
                    ready_map = {str(p.relative_to(mc_dir)).replace("\\", "/"): n
                                 for p, n in ready}
                changed, deleted = _diff_manifest(old_files, new_manifest)

                # Build the incremental zip with chain ID in the filename
                dir_name = mc_dir.name
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                zip_name = f"{dir_name}_incr_{chain_id}_{timestamp}"
                zip_path = backup_dir / f"{zip_name}.zip"

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    # Add changed/added files. On Bedrock, listed files are
                    # truncated to their snapshot length and stale unlisted db/
                    # files skipped.
                    for rel_path in changed:
                        full_path = mc_dir / rel_path
                        if not full_path.exists():
                            continue
                        _add_world_file_to_zip(zf, full_path, rel_path, ready_map)
                    # Record deleted files so restore can remove them too
                    if deleted:
                        zf.writestr("_deletions.json", json.dumps(deleted, indent=2))
                    # Embed chain metadata so restore.py can discover this
                    # incremental's chain membership without external state
                    zf.writestr("_meta.json", json.dumps({
                        "chain_id": chain_id, "base_full": base_full}))
                    # Bedrock: embed the changed-only player-data sidecar.
                    self.write_player_sidecar(zf, ready, full_backup=False, log=inc_log)

                size_mb = zip_path.stat().st_size / (1024 * 1024)
                self.log.info("Incremental backup saved: %s (%.1f MB, %d files)",
                            zip_path.name, size_mb, len(changed))

                # Update the manifest: same chain, but new mtime baseline
                self.save_manifest(new_manifest, chain_id=chain_id, base_full=base_full)

            finally:
                # Always resume normal saving
                self.backend.end_save(inc_log)

            # Checkpoint online-time stats so a crash loses at most one interval
            # of in-progress playtime (no-op on Java).
            self.backend.checkpoint_open_sessions()

            # Copy off-server if configured
            run_copy_command(zip_path, self.config.backup_copy_cmd,
                             log_fn=lambda msg: self.log.info("Incremental backup: %s", msg))

            return str(zip_path)

        except Exception:
            self.log.exception("Incremental backup failed")
            return None
        finally:
            self.backup_lock.release()

    # --- Incremental backup cycle (player-activity-driven) ------------------
    # Uses a threading.Timer to run incremental backups at regular intervals
    # while players are online. The cycle starts when the first player joins
    # and stops when the last player leaves.

    def _incremental_cycle(self):
        """Run one incremental backup, then reschedule if the cycle is still
        active."""
        try:
            self.run_incremental_backup()
        finally:
            with self.incr_lock:
                if self.incr_timer is not None:  # cycle still active (not stopped)
                    self.incr_timer = threading.Timer(
                        self.config.incremental_interval_minutes * 60,
                        self._incremental_cycle)
                    self.incr_timer.daemon = True
                    self.incr_timer.start()

    def start_incremental_cycle(self):
        """Start the incremental backup cycle if not already running.

        Called when the first player joins the server.
        """
        if not self.config.incremental_enabled:
            return
        with self.incr_lock:
            if self.incr_timer is not None:
                return  # already running
            self.log.info("Incremental backup cycle started (every %d min)",
                        self.config.incremental_interval_minutes)
            self.incr_timer = threading.Timer(
                self.config.incremental_interval_minutes * 60,
                self._incremental_cycle)
            self.incr_timer.daemon = True
            self.incr_timer.start()

    def stop_incremental_cycle(self, final: bool = False):
        """Stop the incremental backup cycle.

        Called when the last player leaves the server.
        If final=True, runs one last incremental backup to capture any remaining
        changes from the play session.
        """
        if not self.config.incremental_enabled:
            return
        with self.incr_lock:
            if self.incr_timer is None:
                return
            self.incr_timer.cancel()
            self.incr_timer = None
        self.log.info("Incremental backup cycle stopped")
        if final:
            self.log.info("Running final incremental backup before stop")
            threading.Thread(target=self.run_incremental_backup, daemon=True).start()

    # --- Whole-world restore (stop -> replace -> restart) -------------------

    def restore_world(self, chain: dict, point_idx: int, *, say) -> None:
        """Restore the whole world to a chosen backup point: warn players,
        (optionally) take a pre-restore backup, stop the server, replace its
        files with the restored chain, then relaunch. Assumes the caller holds
        ``backup_lock``. ``say`` reports progress to the chat.

        Fail-safe: if the server is taken down but the restore or relaunch
        errors, the ``finally`` brings it back up (or tells the admin how).
        Nothing is relaunched unless we confirmed the server was fully down, so
        a stuck stop can't cause a double start.
        """
        backend = self.backend
        warn = self.config.restore_warning_seconds
        stopped = down = relaunched = False
        try:
            # 1. In-game warning + countdown (only if the server is reachable).
            if warn > 0 and backend.is_available():
                backend.broadcast(f"Server restoring in {warn}s — you will be "
                                  "disconnected. Reconnect shortly.")
                self.log.info("World restore: warned players, %ds countdown", warn)
                time.sleep(warn)
                backend.broadcast("Restoring now — disconnecting...")

            # 2. Optional pre-restore backup of the CURRENT world.
            if self.config.pre_restore_backup:
                say("Taking a pre-restore backup of the current world...")
                try:
                    self.run_backup(status_cb=say)
                except Exception as e:
                    say(f"Pre-restore backup failed, aborting restore: {e}")
                    return

            # 3. Stop and confirm the server is fully down.
            say("Stopping the server...")
            backend.stop_server(say)
            stopped = True
            if not backend.wait_until_stopped(timeout=120):
                say("Server did not shut down in time — aborting. It was not "
                    "relaunched (avoiding a double start); check it manually.")
                return
            down = True

            # 4. Replace the world with the restored chain while it's down.
            say("Restoring world files...")
            summary = restore_core.restore_chain(
                chain, point_idx, self.config.minecraft_dir,
                backup_dir=self.config.backup_dir,
                exclude_names=self.backup_exclude_names,
                copy_cmd=self.config.backup_copy_cmd,
                manifest_path=self.manifest_path,
                preserve_names=self.backup_exclude_names,
                log_fn=say)

            # 5. Relaunch and confirm ready.
            say("Restarting the server...")
            if backend.relaunch(say):
                relaunched = True
                reconcile_online(self, reason="after world restore")
                chain_note = (f" New chain {summary['chain_id']}."
                              if summary.get("chain_id") else "")
                say(f"World restore complete.{chain_note}")
            else:
                say("Restore applied but relaunch was not confirmed. Start the "
                    f"server manually:\n  {self.config.mux_start_cmd}")
        except Exception as e:
            self.log.exception("World restore failed")
            say(f"World restore failed: {e}")
        finally:
            # If we took the server down but never got it back up (restore error,
            # or a relaunch we didn't confirm), try once more so it isn't left
            # offline. Skip when it never confirmed down (avoids a double start).
            if down and not relaunched:
                say("Bringing the server back up...")
                if backend.relaunch(say):
                    reconcile_online(self, reason="after world restore")
                else:
                    say("Could not relaunch. Start the server manually:\n  "
                        f"{self.config.mux_start_cmd}")


class Bot:
    """One chat identity (its Telegram bot and/or Slack app) fronting a set of
    servers. Owns the adapters, the command router, and this bot's slice of the
    auth doc; delivers announcements only to its own authorized chats. main()
    builds one today and will loop over several once multi-bot lands.

    Announcements are per (bot, server): a single-server bot fans out to every
    authorized chat (today's behavior), while a multi-server bot delivers only
    to chats bound to that server via the ``chat_servers`` auth binding.
    """

    def __init__(self, config, servers):
        self.config = config
        self.log = _TagLogAdapter(logger, {"tag": config.name})
        self.servers = list(servers)
        self.by_name = {s.config.name: s for s in self.servers}
        self.by_key = {s.config.key: s for s in self.servers}
        # Per-admin /use selection, in-memory only: {platform: {user_id: key}}.
        self._admin_session: dict = {}
        # Set in main() once the adapters and auth doc are built.
        self.adapters: list = []
        self.router = None
        self.auth_doc: dict = {}   # whole {bot: {platform: ns}} doc (for saves)
        self.auth: dict = {}       # this bot's slice: {platform: ns}

    def drop_server(self, server) -> None:
        """Remove a server from this bot (e.g. its backend failed to start), so
        resolution and announcements ignore it."""
        self.servers = [s for s in self.servers if s is not server]
        self.by_name.pop(server.config.name, None)
        self.by_key.pop(server.config.key, None)

    def find_server(self, token: str):
        """Resolve a user-typed server token (its name or data key) to a Server,
        or None if it matches neither."""
        if not token:
            return None
        return self.by_name.get(token) or self.by_key.get(token)

    def note_chat_name(self, ctx) -> None:
        """Learn/refresh an authorized chat's human name from an inbound message
        and persist it in auth.json. Called for every dispatched message, so a
        group/channel rename is picked up on its next message. Only authorized
        chats are recorded (keeps the map bounded and relevant)."""
        if ctx.is_private or not ctx.chat_name:
            return
        ns = self.auth.get(ctx.platform)
        if ns is None or ctx.chat_id not in ns.get("authorized_chat_ids", []):
            return
        names = ns.setdefault("chat_names", {})
        if names.get(ctx.chat_id) == ctx.chat_name:
            return  # unchanged — no write
        with _auth_lock:
            names[ctx.chat_id] = ctx.chat_name
            save_auth(self.auth_doc, _AUTH_PATH)
        self.log.info("Chat name learned: %s = [%s]", ctx.chat_id, ctx.chat_name)

    def chat_display(self, platform: str, chat_id: str) -> str:
        """Human label for a chat ID from the persisted names, else the ID —
        for logs/listings that have no live inbound message to read a name from."""
        ns = self.auth.get(platform) or {}
        name = (ns.get("chat_names") or {}).get(str(chat_id))
        return f"{name} ({chat_id})" if name else str(chat_id)

    def resolve_target_server(self, ctx):
        """Pick the Server a server-scoped command should act on, or None if it's
        ambiguous (a multi-server bot with no channel binding and no /use
        selection). Resolution order:
          1. single-server bot -> its only server (implicit);
          2. a bound group/channel -> the server in its chat_servers binding;
          3. an admin DM -> the /use session selection.
        """
        if len(self.servers) == 1:
            return self.servers[0]
        ns = self.auth.get(ctx.platform) or {}
        if not ctx.is_private:
            key = (ns.get("chat_servers") or {}).get(ctx.chat_id)
            return self.by_key.get(key)
        sel = (self._admin_session.get(ctx.platform) or {}).get(ctx.user_id)
        return self.by_key.get(sel)

    def resolve_command(self, ctx) -> bool:
        """CommandRouter resolve hook: set ctx.server, or reply with a
        disambiguation message and return False."""
        server = self.resolve_target_server(ctx)
        if server is None:
            names = ", ".join(sorted(self.by_name)) or "(none)"
            ctx.reply("This bot serves multiple servers. Pick one with "
                      f"/use <server> first.\nServers: {names}")
            return False
        ctx.server = server
        return True

    def set_use(self, ctx) -> None:
        """Handle /use: bare form lists servers + current selection; /use
        <server> sets this admin's session target for subsequent commands."""
        current = (self._admin_session.get(ctx.platform) or {}).get(ctx.user_id)
        if not ctx.args:
            cur = self.by_key.get(current)
            lines = ["Servers:"]
            for s in self.servers:
                mark = "  * " if s is cur else "    "
                lines.append(f"{mark}{s.config.name}")
            lines.append("")
            lines.append(f"Current: {cur.config.name if cur else '(none — /use <server>)'}")
            ctx.reply("\n".join(lines))
            return
        target = self.find_server(ctx.args[0])
        if target is None:
            names = ", ".join(sorted(self.by_name)) or "(none)"
            ctx.reply(f"Unknown server '{ctx.args[0]}'.\nServers: {names}")
            return
        self._admin_session.setdefault(ctx.platform, {})[ctx.user_id] = target.config.key
        ctx.reply(f"Now using {target.config.name} for your commands.")

    def _server_chats(self, adapter, server):
        """Yield the authorized chat IDs on ``adapter`` that should receive
        ``server``'s announcements: all of them for a single-server bot, else
        only those bound to the server's key."""
        ns = self.auth.get(adapter.name) or {}
        chat_ids = ns.get("authorized_chat_ids", [])
        if len(self.servers) == 1:
            yield from chat_ids
            return
        binding = ns.get("chat_servers", {})
        key = server.config.key
        for chat_id in chat_ids:
            if binding.get(chat_id) == key:
                yield chat_id

    def announce(self, server, msg: str) -> int:
        """Send an announcement about ``server`` to its authorized chats on every
        platform. Returns how many chats it reached."""
        sent = 0
        for adapter in self.adapters:
            for chat_id in self._server_chats(adapter, server):
                try:
                    adapter.send(chat_id, msg)
                    sent += 1
                except Exception as e:
                    logger.warning("Announce to %s/%s failed: %s",
                                   adapter.name, chat_id, e)
        return sent

    def alert_admins(self, msg: str) -> None:
        """Send an operational alert to each platform's admin (if claimed)."""
        for adapter in self.adapters:
            admin = (self.auth.get(adapter.name) or {}).get("admin_user_id")
            if admin:
                try:
                    adapter.send(admin, msg)
                except Exception:
                    logger.warning("Failed to alert %s admin", adapter.name)


# ---------------------------------------------------------------------------
# 4. Log Parsing
# ---------------------------------------------------------------------------
RE_JOIN = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) joined the game')
RE_LEAVE = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) left the game')
RE_UUID = re.compile(r'^\[[\d:]+\] \[User Authenticator #\d+/INFO\]: UUID of player (\w+) is ([0-9a-f-]+)')
RE_ACHIEVEMENT = re.compile(
    r'^\[([\d:]+)\] \[Server thread/INFO\]: (\w+) has '
    r'(made the advancement|reached the goal|completed the challenge) '
    r'\[(.+?)\]'
)
_ACH_TYPE_MAP = {
    "made the advancement": "advancement",
    "reached the goal": "goal",
    "completed the challenge": "challenge",
}
_ACH_VERB_MAP = {v: k for k, v in _ACH_TYPE_MAP.items()}

RE_SERVER_MSG = re.compile(r'^\[([\d:]+)\] \[Server thread/INFO\]: (\w+) (.+)$')
# Player chat, e.g. "[12:34:56] [Server thread/INFO]: <Steve> hello" (or a
# Paper-style "[Async Chat Thread - #0/INFO]:"). The <> brackets distinguish it
# from join/leave/death lines (which start with a bare \w name).
RE_CHAT = re.compile(r'^\[[\d:]+\] \[[^\]]*/INFO\]: <([^>]+)> (.+)$')
_DEATH_PHRASES = (
    "was slain by", "was shot by", "was killed",
    "was blown up by", "was squashed by", "was fireballed by",
    "was pummeled by", "was stung by", "was impaled",
    "was skewered by", "was struck by lightning",
    "was burnt to", "was frozen to death", "was pricked to death",
    "was poked to death", "was doomed to fall",
    "was roasted in dragon", "was obliterated by",
    "was squished",
    "drowned", "suffocated", "starved to death",
    "burned to death",
    "fell from", "fell off", "fell out of", "fell into", "fell while",
    "hit the ground too hard",
    "tried to swim in lava",
    "walked into",
    "froze to death", "withered away",
    "experienced kinetic energy",
    "went up in flames", "went off with a bang",
    "died", "didn't want to live",
    "discovered the floor was lava",
    "blew up",
    "left the confines of this world",
)


_DEATH_CATEGORIES = [
    ("Combat (was slain by)", ["was slain by"]),
    ("Shot by", ["was shot by"]),
    ("Blown up", ["was blown up by"]),
    ("Falls", ["fell from", "fell off", "fell out of", "fell into",
               "fell while", "hit the ground too hard"]),
    ("Lava", ["tried to swim in lava"]),
    ("Fire", ["burned to death", "was burnt to", "went up in flames",
              "walked into fire"]),
    ("Drowning", ["drowned"]),
    ("Withered away", ["withered away"]),
    ("Impaled", ["was impaled"]),
    ("Frozen", ["froze to death", "was frozen to death"]),
    ("Lightning", ["was struck by lightning"]),
    ("Kinetic energy", ["experienced kinetic energy"]),
    ("Suffocation", ["suffocated"]),
    ("Starvation", ["starved to death"]),
    ("Cactus", ["walked into a cactus", "was pricked to death",
                "was poked to death"]),
    ("Dragon", ["was doomed to fall", "was roasted in dragon"]),
    ("Sonic shriek", ["was obliterated by"]),
    ("Explosions", ["blew up", "went off with a bang"]),
    ("Void", ["left the confines of this world"]),
    ("Magic", ["was killed by magic", "was killed by even more magic"]),
]


def _categorize_death(message: str) -> str:
    for category, phrases in _DEATH_CATEGORIES:
        if any(message.startswith(p) for p in phrases):
            return category
    return "Other"


# The name->uuid pending map lives on the Server (server.pending_uuids).


def _parse_line_java(line: str, server) -> tuple:
    """Return (event_type, payload) or (None, None).

    For join/leave, payload is the player name string.
    For achievement, payload is a dict with player, achievement, type, time.
    """
    line = line.strip()

    m = RE_UUID.match(line)
    if m:
        name, uuid = m.group(1), m.group(2)
        server.pending_uuids[name] = uuid
        register_player(server, uuid, name)
        return None, None

    m = RE_JOIN.match(line)
    if m:
        name = m.group(1)
        uuid = server.pending_uuids.pop(name, None)
        if uuid:
            register_player(server, uuid, name)
        server.player_join(name)
        return "join", name

    m = RE_LEAVE.match(line)
    if m:
        name = m.group(1)
        server.player_leave(name)
        return "leave", name

    m = RE_ACHIEVEMENT.match(line)
    if m:
        time_str, name, ach_type_full, achievement = m.groups()
        return "achievement", {
            "player": name,
            "achievement": achievement,
            "type": _ACH_TYPE_MAP[ach_type_full],
            "time": time_str,
        }

    m = RE_SERVER_MSG.match(line)
    if m:
        time_str, name, msg = m.groups()
        if any(msg.startswith(p) for p in _DEATH_PHRASES):
            return "death", {
                "player": name,
                "message": msg,
                "time": time_str,
            }

    if server.config.chat_relay:
        m = RE_CHAT.match(line)
        if m:
            return "chat", {"player": m.group(1), "message": m.group(2)}

    return None, None


# Bedrock Dedicated Server console lines (terser than Java's log). Names may
# contain spaces, so capture up to the ", xuid:" delimiter. BDS's own console
# reports only join/leave; death and chat come from the bedrock_pack behavior
# pack as `MCNOTIFIER {json}` marker lines (see below).
RE_BEDROCK_CONNECT = re.compile(r'Player connected:\s*(.+?),\s*xuid:\s*(\d+)')
RE_BEDROCK_DISCONNECT = re.compile(r'Player disconnected:\s*(.+?),\s*xuid:\s*(\d+)')

# Behavior-pack event marker, e.g.
#   [<ts> WARN] [Scripting] MCNOTIFIER {"t":"death","player":"X","cause":"lava"}
_BEDROCK_MARKER = "MCNOTIFIER "

# Bedrock damage cause -> death phrase, worded to mirror Java so the same
# _categorize_death / _DEATH_CATEGORIES logic works for /death_summary. "{by}"
# is filled with the prettified killer entity when present.
_BEDROCK_DEATH_PHRASES = {
    "lava": "tried to swim in lava",
    "fire": "went up in flames",
    "fire_tick": "burned to death",
    "fall": "fell from a high place",
    "drowning": "drowned",
    "suffocation": "suffocated in a wall",
    "starve": "starved to death",
    "freezing": "froze to death",
    "lightning": "was struck by lightning",
    "void": "fell out of the world",
    "contact": "was pricked to death",
    "magma": "discovered the floor was lava",
    "wither": "withered away",
    "anvil": "was squashed by a falling anvil",
    "falling_block": "was squashed by a falling block",
    "magic": "was killed by magic",
    "sonic_boom": "was obliterated by a sonically-charged shriek",
    "block_explosion": "blew up",
    "entity_explosion": "blew up",
    "entity_attack": "was slain",
    "projectile": "was shot",
    "thorns": "was killed trying to hurt",
    "self_destruct": "blew up",
}


def _pretty_entity(type_id: str) -> str:
    """'minecraft:zombie' -> 'Zombie'."""
    return type_id.split(":")[-1].replace("_", " ").title()


def _bedrock_death_message(cause: str, by) -> str:
    """Build a Java-style death message from a Bedrock damage cause + killer."""
    phrase = _BEDROCK_DEATH_PHRASES.get((cause or "").lower())
    killer = _pretty_entity(by) if by else None
    if phrase is None:
        return f"was killed by {killer}" if killer else "died"
    # Causes that read naturally with a "by <killer>" suffix.
    if cause.lower() in ("entity_attack", "projectile", "thorns") and killer:
        verb = {"entity_attack": "was slain by", "projectile": "was shot by",
                "thorns": "was killed trying to hurt"}[cause.lower()]
        return f"{verb} {killer}"
    return phrase


def _parse_line_bedrock(line: str, server) -> tuple:
    """Return (event_type, payload) or (None, None) for a Bedrock console line.

    Join/leave come from BDS itself; death/chat come from the bedrock_pack
    behavior pack's MCNOTIFIER marker lines (gated by config). The player's xuid
    is the registry key (Bedrock has no per-player UUID file).
    """
    line = line.strip()
    m = RE_BEDROCK_CONNECT.search(line)
    if m:
        name, xuid = m.group(1).strip(), m.group(2).strip()
        if xuid:
            register_player(server, xuid, name)
        server.player_join(name)
        return "join", name
    m = RE_BEDROCK_DISCONNECT.search(line)
    if m:
        name, xuid = m.group(1).strip(), m.group(2).strip()
        # The disconnect line carries the xuid too, so register it even if we
        # never saw this player's connect line (e.g. they were already online
        # when the bot started). Otherwise their identity would go unrecorded.
        if xuid:
            register_player(server, xuid, name)
        server.player_leave(name)
        return "leave", name

    # Behavior-pack markers (anywhere after the log/[Scripting] prefixes).
    idx = line.find(_BEDROCK_MARKER)
    if idx != -1:
        try:
            ev = json.loads(line[idx + len(_BEDROCK_MARKER):])
        except (ValueError, TypeError):
            return None, None
        t = ev.get("t")
        if t == "death" and server.config.bedrock_script_events:
            return "death", {
                "player": ev.get("player", "?"),
                "message": _bedrock_death_message(ev.get("cause"), ev.get("by")),
                "time": datetime.now().strftime("%H:%M:%S"),
            }
        if t == "chat" and server.config.chat_relay:
            return "chat", {"player": ev.get("player", "?"),
                            "message": ev.get("msg", "")}
    return None, None


def parse_line(line: str, server) -> tuple:
    """Dispatch line parsing to the edition-specific parser."""
    if server.config.edition == EDITION_BEDROCK:
        return _parse_line_bedrock(line, server)
    return _parse_line_java(line, server)


# ---------------------------------------------------------------------------
# 5. LogWatcher
# ---------------------------------------------------------------------------
class LogLineWaiter:
    """A handle returned by LogWatcher.expect_line().

    Register this BEFORE sending the RCON command that will produce the
    expected log line. Then call wait() to block until the line appears
    or the timeout expires. This eliminates the race condition of capturing
    a file position after the command — the waiter is already listening
    when the command is sent.
    """
    def __init__(self, phrase: str):
        self.phrase = phrase
        self._event = threading.Event()

    def _signal(self):
        """Called by LogWatcher when a matching line is found."""
        self._event.set()

    def wait(self, timeout: float = 60) -> bool:
        """Block until the phrase is seen or timeout. Returns True if found."""
        return self._event.wait(timeout)

    def triggered(self) -> bool:
        """Whether the phrase has been seen (non-blocking)."""
        return self._event.is_set()


class LogWatcher(FileSystemEventHandler):
    """Watches the Minecraft server's latest.log for player events and
    RCON confirmations.

    This is the single point of access for reading latest.log. It handles
    log file rotation (when the server starts, it renames the old log and
    creates a new one) by tracking the file's inode.

    Two consumers:
      - Player event notifications: parsed via parse_line() and forwarded
        to the notify callback (joins, leaves, achievements, deaths).
      - RCON confirmation waiters: registered via expect_line(), which
        returns a LogLineWaiter. The waiter is signalled when a matching
        line appears in the log.
    """
    # Lines that mean the server is (re)started and ready to answer queries.
    # Java logs RCON readiness; Bedrock logs "Server started." (its console.log
    # is appended via tee, so there's no inode rotation to key off).
    _START_MARKERS = ("RCON running on", "Server started")
    _START_DEBOUNCE = 15  # seconds; collapse a burst of start lines into one

    def __init__(self, server, notify_cb, on_server_start=None):
        self._path = server.config.log_path
        self._filename = self._path.name  # "latest.log" (Java) or "console.log" (Bedrock)
        self._server = server  # the Server whose log this tails; parse into it
        self._notify = notify_cb
        self._on_server_start = on_server_start  # called when the server (re)starts
        self._last_start_trigger = 0.0
        self._pos = 0
        self._inode = None
        self._lock = threading.Lock()
        # Waiters: list of LogLineWaiter objects waiting for specific phrases
        self._waiters: list[LogLineWaiter] = []
        self._waiters_lock = threading.Lock()
        self._seek_to_end()

    def _seek_to_end(self) -> None:
        try:
            stat = self._path.stat()
            self._inode = stat.st_ino
            self._pos = stat.st_size
        except FileNotFoundError:
            self._server.log.warning("Log file not found at startup: %s (server may be offline)", self._path)

    def _check_rotation(self) -> bool:
        """Detect if latest.log was replaced (rotated) by checking inode."""
        try:
            inode = self._path.stat().st_ino
            if inode != self._inode:
                self._inode = inode
                self._pos = 0
                self._server.log.info("Log file rotation detected, resetting position")
                return True
        except FileNotFoundError:
            pass
        return False

    def expect_line(self, phrase: str) -> LogLineWaiter:
        """Register a waiter for a log line containing phrase.

        Call this BEFORE sending the RCON command that produces the line.
        The returned LogLineWaiter.wait(timeout) blocks until the line
        appears or times out.
        """
        waiter = LogLineWaiter(phrase)
        with self._waiters_lock:
            self._waiters.append(waiter)
        return waiter

    def cancel(self, waiter: LogLineWaiter) -> None:
        """Deregister a waiter that never fired (e.g. an error-line watcher that
        wasn't triggered), so it doesn't linger in the waiters list."""
        with self._waiters_lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _check_waiters(self, line: str) -> None:
        """Check a log line against all registered waiters."""
        with self._waiters_lock:
            triggered = []
            for waiter in self._waiters:
                if waiter.phrase in line:
                    waiter._signal()
                    triggered.append(waiter)
            # Remove triggered waiters
            for waiter in triggered:
                self._waiters.remove(waiter)

    def _maybe_server_start(self, line: str) -> None:
        """If a line signals the server (re)started, fire the on_server_start
        callback in a background thread (debounced). Runs off-thread so the
        reconcile's live query doesn't stall log reading."""
        if self._on_server_start is None:
            return
        if not any(marker in line for marker in self._START_MARKERS):
            return
        now = time.time()
        if now - self._last_start_trigger < self._START_DEBOUNCE:
            return
        self._last_start_trigger = now
        self._server.log.info("Server start detected in log; resyncing online status")
        threading.Thread(target=self._on_server_start, daemon=True).start()

    def on_modified(self, event):
        if not str(event.src_path).endswith(self._filename):
            return
        with self._lock:
            self._check_rotation()
            try:
                with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._pos)
                    new_data = f.read()
                    self._pos = f.tell()
                for line in new_data.splitlines():
                    # Check RCON confirmation waiters
                    self._check_waiters(line)
                    # Server (re)start -> resync online status (debounced).
                    self._maybe_server_start(line)
                    # Check player event notifications
                    event_type, payload = parse_line(line, self._server)
                    if event_type and payload:
                        self._notify(event_type, payload)
            except FileNotFoundError:
                pass
            except Exception:
                self._server.log.exception("Error reading Minecraft log")


# ---------------------------------------------------------------------------
# 6. Notification Callback
# ---------------------------------------------------------------------------
def make_notify_callback(bot, server):
    """Build the per-(bot, server) event->chat callback. Announcements go out
    through ``bot`` to the chats bound to ``server``; player-session, name
    registry, achievements/deaths, and incremental-backup side effects all
    operate on ``server``."""
    _last_event: dict = {}
    _cooldown = 3

    def _send_to_chats(msg: str) -> int:
        return bot.announce(server, msg)

    def notify(event_type: str, payload) -> None:
        if event_type == "achievement":
            player = payload["player"]
            achievement = payload["achievement"]
            ach_type = payload["type"]
            time_str = payload["time"]
            key = f"{player}-achievement-{achievement}"
            now = time.time()
            if now - _last_event.get(key, 0) < _cooldown:
                return
            _last_event[key] = now

            timestamp = f"{datetime.now().strftime('%Y-%m-%d')} {time_str}"
            uuid = _uuid_by_name(player, server.names)
            if uuid:
                record_achievement(uuid, achievement, ach_type, timestamp,
                                   server.achievements, server.achievements_path)

            verb = _ACH_VERB_MAP[ach_type]
            sent = _send_to_chats(f"{player} has {verb} [{achievement}]")
            server.log.info("Achievement: %s — %s — sent to %d chat(s)",
                            player, achievement, sent)
            return

        if event_type == "death":
            player = payload["player"]
            death_msg = payload["message"]
            time_str = payload["time"]
            timestamp = f"{datetime.now().strftime('%Y-%m-%d')} {time_str}"
            uuid = _uuid_by_name(player, server.names)
            if uuid:
                record_death(uuid, death_msg, timestamp, server.deaths,
                             server.deaths_path)

            sent = _send_to_chats(f"{player} {death_msg}")
            server.log.info("Death: %s %s — sent to %d chat(s)",
                            player, death_msg, sent)
            return

        if event_type == "chat":
            # In-game chat relayed to the platforms (one-way; no cooldown so
            # distinct messages aren't suppressed). Gated by config.chat_relay
            # at the parser; nothing recorded.
            player = payload["player"]
            message = payload["message"]
            sent = _send_to_chats(f"\U0001f4ac {player}: {message}")
            server.log.info("Chat: %s: %s — sent to %d chat(s)",
                            player, message, sent)
            return

        name = payload
        # Online-time accumulation (Bedrock; no-op on Java). Done before the
        # cooldown gate so a quick rejoin still records the session boundary.
        pid = _uuid_by_name(name, server.names)
        if pid:
            server.backend.record_player_session(event_type, pid)
            server.note_active_xuid(pid)  # candidate for identity learning
            if event_type == "leave":
                # Refresh last_seen to the disconnect time (connect already set
                # it on join). No-op on Java for an unchanged name.
                server.backend.register_name(pid, name)

        key = f"{name}-{event_type}"
        now = time.time()
        if now - _last_event.get(key, 0) < _cooldown:
            return
        _last_event[key] = now

        online = server.get_online_players()
        count = len(online)
        names_str = ", ".join(online) if online else "none"

        verb = "joined the game" if event_type == "join" else "left the game"
        status = "online" if event_type == "join" else "offline"
        sent = _send_to_chats(f"{name} {verb}\nPlayers online: {count} ({names_str})")
        server.log.info("Notification: player %s %s — sent to %d chat(s)",
                        name, status, sent)

        # Incremental backup triggers
        if event_type == "join" and count == 1:
            server.start_incremental_cycle()
        elif event_type == "leave" and count == 0:
            server.stop_incremental_cycle(final=True)

    return notify


# ---------------------------------------------------------------------------
# 7. Stats Logic
# ---------------------------------------------------------------------------
# Per-player stat dicts come from the backend (Java: world stat files; Bedrock:
# accumulated online time). Every dict has `name` + `time_played_hours`; only
# Java adds the richer fields, so the formatter shows whatever is present.
_STAT_FIELDS = [
    ("time_played_hours", "Time played", "h"),
    ("sessions", "Sessions", ""),
    ("deaths", "Deaths", ""),
    ("diamonds_mined", "Diamonds mined", ""),
    ("ancient_debris_mined", "Ancient debris mined", ""),
    ("distance_travelled_km", "Distance travelled", " km"),
    ("villager_trades", "Villager trades", ""),
    ("total_mobs_killed", "Mobs killed", ""),
    ("last_seen", "Last seen", ""),
]


def _format_stats(p: dict) -> str:
    lines = [f"Player: {p['name']}"]
    for key, label, unit in _STAT_FIELDS:
        if key in p and p[key] != "":
            lines.append(f"  {label}: {p[key]}{unit}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7b. Achievement Scanning
# ---------------------------------------------------------------------------
RE_GZ_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})-\d+\.log\.gz')


def _scan_log_for_achievements(file_path: Path, date_str: str, server) -> int:
    count = 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = RE_UUID.match(line)
            if m:
                name, uuid = m.group(1), m.group(2)
                register_player(server, uuid, name)
                continue
            m = RE_ACHIEVEMENT.match(line)
            if m:
                time_str, player, ach_type_full, achievement = m.groups()
                ach_type = _ACH_TYPE_MAP[ach_type_full]
                timestamp = f"{date_str} {time_str}"
                uuid = _uuid_by_name(player, server.names)
                if uuid:
                    if record_achievement(uuid, achievement, ach_type,
                                          timestamp, server.achievements,
                                          server.achievements_path):
                        count += 1
                else:
                    server.log.warning("Scan: no UUID for player %s, skipping achievement", player)
    return count


def _scan_log_for_deaths(file_path: Path, date_str: str, server) -> int:
    count = 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = RE_UUID.match(line)
            if m:
                name, uuid = m.group(1), m.group(2)
                register_player(server, uuid, name)
                continue
            m = RE_SERVER_MSG.match(line)
            if m:
                time_str, player, msg = m.groups()
                if any(msg.startswith(p) for p in _DEATH_PHRASES):
                    timestamp = f"{date_str} {time_str}"
                    uuid = _uuid_by_name(player, server.names)
                    if uuid:
                        if record_death(uuid, msg, timestamp, server.deaths,
                                        server.deaths_path):
                            count += 1
                    else:
                        server.log.warning("Scan: no UUID for player %s, skipping death", player)
    return count


def _scan_logs(scan_fn, server) -> int:
    """Run ``scan_fn`` over every rotated ``*.log.gz`` (by embedded date) plus the
    live log, returning the total newly-recorded count. Shared by /scan_deaths
    and /scan_achievements."""
    log_path = server.config.log_path
    logs_dir = log_path.parent
    total = 0
    for gz_path in sorted(logs_dir.glob("*.log.gz")):
        m = RE_GZ_DATE.match(gz_path.name)
        if not m:
            continue
        date_str = m.group(1)
        extracted = gz_path.with_suffix("")  # remove .gz
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as gz_f:
                with open(extracted, "w", encoding="utf-8") as out_f:
                    out_f.write(gz_f.read())
            total += scan_fn(extracted, date_str, server)
        except Exception as e:
            server.log.warning("Scan: failed to process %s: %s", gz_path.name, e)
        finally:
            if extracted.exists():
                extracted.unlink()
    if log_path.exists():
        total += scan_fn(log_path, datetime.now().strftime("%Y-%m-%d"), server)
    return total


# ---------------------------------------------------------------------------
# 7d. RCON & Backup
# ---------------------------------------------------------------------------

# /restore_player pending-state, keyed by admin user_id.
# Forces the admin through the list -> select -> confirm sequence so a typo
# in a single command can't trigger a destructive restore.
_pending_player_restore: dict = {}
_pending_player_lock = threading.Lock()
_PENDING_PLAYER_RESTORE_TTL = 300  # seconds; older entries are treated as missing
_RESTORE_PLAYER_PAGE_SIZE = 10  # versions shown per page in /restore_player listing

# /restore (whole-world) pending-state, keyed like the player restore by
# "{bot}:{server}:{platform}:{user}". Same list -> select -> confirm gate so an
# accidental /restore can't wipe and rebuild the world in one command.
_pending_world_restore: dict = {}
_pending_world_lock = threading.Lock()
_PENDING_WORLD_RESTORE_TTL = 300
_RESTORE_PAGE_SIZE = 10  # restore points shown per page


def _get_pending_world_restore(pkey: str, expected_stage: str | None = None) -> dict | None:
    with _pending_world_lock:
        entry = _pending_world_restore.get(pkey)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _PENDING_WORLD_RESTORE_TTL:
            _pending_world_restore.pop(pkey, None)
            return None
        if expected_stage is not None and entry.get("stage") != expected_stage:
            return None
        return entry


def _set_pending_world_restore(pkey: str, **fields) -> None:
    with _pending_world_lock:
        existing = _pending_world_restore.get(pkey, {})
        existing.update(fields)
        existing["ts"] = time.time()
        _pending_world_restore[pkey] = existing


def _clear_pending_world_restore(pkey: str) -> None:
    with _pending_world_lock:
        _pending_world_restore.pop(pkey, None)


def _format_restore_points(points: list, offset: int = 0) -> str:
    """Render one page of the numbered restore-point list for /restore."""
    if not points:
        return ("No backups found for this server.\n"
                "Run /backup first, or check the backup directory.")
    page = points[offset:offset + _RESTORE_PAGE_SIZE]
    lines = ["World restore points (latest first).",
             "To select, send: /restore <number>", ""]
    for p in page:
        chain = f"chain {p['chain_id']}" if p['chain_id'] else "standalone"
        lines.append(f"  {p['n']:3d}.  [{p['kind']}] {p['pretty_ts']}   "
                     f"({p['pretty_size']}, {chain})")
    remaining = len(points) - (offset + _RESTORE_PAGE_SIZE)
    if remaining > 0:
        lines.append(f"\n{remaining} more. Send: /restore more")
    return "\n".join(lines)


def _add_world_file_to_zip(zf, fp: Path, rel: str, ready_map: dict | None) -> None:
    """Add ``fp`` to the zip under ``rel``, honouring an optional snapshot map.

    ``ready_map`` is None for Java (copy the file whole). For Bedrock it maps a
    relative path to the byte length reported by ``save query``:
      - listed file  -> copied truncated to that length (the consistent snapshot;
        Bedrock keeps appending past it),
      - unlisted file under a world ``db/`` directory -> skipped (a stale LevelDB
        fragment not part of the snapshot),
      - anything else (server.properties, packs, level.dat, ...) -> copied whole.
    """
    if ready_map is not None:
        if rel in ready_map:
            # Truncate to the snapshot length. Build the entry from the file so
            # its mode/mtime are preserved (writestr with a bare name drops the
            # Unix mode, which restore needs e.g. for executables).
            zi = zipfile.ZipInfo.from_file(fp, rel)
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(fp, "rb") as src:
                zf.writestr(zi, src.read(ready_map[rel]))
            return
        if "/db/" in rel:
            return
    zf.write(fp, rel)


# ---------------------------------------------------------------------------
# 7e. Incremental Backup
# ---------------------------------------------------------------------------
# Incremental backups capture only files that changed since the last backup
# (full or incremental). They are triggered automatically while players are
# online, on a configurable interval (config.incremental_interval_minutes).
#
# How change detection works:
#   - The manifest (backup_manifest.json) stores {relative_path: mtime} for
#     every file in the server directory at the time of the last backup.
#   - Before each incremental, we walk the server directory again and compare
#     mtimes against the manifest. Files with different mtimes or new files
#     are "changed"; files in the manifest but missing from disk are "deleted".
#   - Only changed/added files are zipped. Deleted paths are recorded in a
#     _deletions.json file inside the zip.
#
# Each incremental zip also contains _meta.json with the chain_id and
# base_full filename, making it self-describing for the restore tool.
#
# The incremental cycle is player-activity-driven:
#   - Starts when the first player joins the server
#   - Runs every config.incremental_interval_minutes while players are online
#   - Stops when the last player leaves (with one final backup)
# ---------------------------------------------------------------------------

def _diff_manifest(old: dict, new: dict) -> tuple:
    """Compare two file manifests and return (changed_or_added, deleted).

    Each manifest is {relative_path: mtime}. A file is "changed" if its mtime
    differs or it's new. A file is "deleted" if it was in old but not in new.
    """
    changed = []
    for path, mtime in new.items():
        if path not in old or old[path] != mtime:
            changed.append(path)
    deleted = [path for path in old if path not in new]
    return changed, deleted


# ---------------------------------------------------------------------------
# Player-data restore helpers (used by /restore_player)
# ---------------------------------------------------------------------------


def _format_versions_reply(username: str, uuid: str, versions: list,
                           offset: int = 0) -> str:
    """Render one page of the numbered list reply for /restore_player <username>."""
    if not versions:
        return (f"No player data found for {username} ({uuid}).\n"
                f"Live file missing and no backups in the active chain.")
    page = versions[offset:offset + _RESTORE_PLAYER_PAGE_SIZE]
    lines = [f"Player data versions for {username}  (UUID: {uuid})",
             "Latest first. To select, send: /restore_player "
             f"{username} <number>", ""]
    for i, v in enumerate(page, offset + 1):
        lines.append(f"  {i:3d}.  {v['timestamp']}   {v['source']}")
    remaining = len(versions) - (offset + _RESTORE_PLAYER_PAGE_SIZE)
    if remaining > 0:
        lines.append(f"\n{remaining} more. Send: /restore_player {username} more")
    return "\n".join(lines)


def _format_confirm_reply(username: str, uuid: str, n: int, version: dict) -> str:
    """Render the step-2 confirmation block."""
    return (
        "Confirm restore:\n"
        f"  Player:    {username}\n"
        f"  UUID:      {uuid}\n"
        f"  Timestamp: {version['timestamp']}\n"
        f"  Source:    {version['source']}\n"
        "\n"
        "  To proceed, send:\n"
        f"    /restore_player {username} {n} confirm"
    )


def _get_pending_player_restore(user_id: int,
                                expected_username: str | None = None,
                                expected_stage: str | None = None) -> dict | None:
    """Lookup pending player restore for an admin, with TTL and match checks.

    Returns the pending entry if it exists, hasn't expired, matches the
    expected username (case-insensitive), and is in the expected stage.
    Otherwise returns None and (if expired) discards the stale entry.
    """
    with _pending_player_lock:
        entry = _pending_player_restore.get(user_id)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _PENDING_PLAYER_RESTORE_TTL:
            _pending_player_restore.pop(user_id, None)
            return None
        if (expected_username is not None
                and entry["username"].lower() != expected_username.lower()):
            return None
        if expected_stage is not None and entry["stage"] != expected_stage:
            return None
        return entry


def _set_pending_player_restore(user_id: int, **fields) -> None:
    """Store/update a pending player restore entry with the current timestamp."""
    with _pending_player_lock:
        existing = _pending_player_restore.get(user_id, {})
        existing.update(fields)
        existing["ts"] = time.time()
        _pending_player_restore[user_id] = existing


def _clear_pending_player_restore(user_id: int) -> None:
    with _pending_player_lock:
        _pending_player_restore.pop(user_id, None)


def _recover_online_identities(server, online_names: list) -> None:
    """Recover xuids for already-online Bedrock players whose connect line the
    bot missed because it started mid-session.

    The Bedrock registry is keyed by xuid, but ``list`` only returns usernames —
    so a player already online at startup has no registry entry (absent from
    ``/list``, stats, identity-learning) until they leave. Bedrock's console.log
    is appended (never rotated), so their ``Player connected: <name>, xuid: <id>``
    line is still on disk: scan it and register the ones we can resolve. No-op on
    Java (its name registry is recovered from world data, not this log)."""
    if server.config.edition != EDITION_BEDROCK:
        return
    missing = [n for n in online_names if _uuid_by_name(n, server.names) is None]
    if not missing:
        return
    found: dict = {}
    try:
        with open(server.config.log_path, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                m = (RE_BEDROCK_CONNECT.search(line)
                     or RE_BEDROCK_DISCONNECT.search(line))
                if m and m.group(1).strip() in missing:
                    found[m.group(1).strip()] = m.group(2).strip()  # latest wins
    except FileNotFoundError:
        return
    except Exception:
        server.log.exception("Failed to scan log for online-player identities")
        return
    for name in missing:
        if name in found:
            register_player(server, found[name], name)
    recovered = [n for n in missing if n in found]
    if recovered:
        server.log.info("Recovered identity from log for %d already-online "
                    "player(s): %s", len(recovered), ", ".join(recovered))
    unresolved = [n for n in missing if n not in found]
    if unresolved:
        server.log.info("No identity in log yet for: %s (will resolve on leave)",
                    ", ".join(unresolved))


def reconcile_online(server, *, reason: str = "") -> list | None:
    """Reconcile ``server``'s in-memory online set with what it actually reports.

    The set is normally kept current from parsed join/leave lines, but it goes
    stale when players leave without a clean disconnect line — e.g. a restore
    stop/restart kicks everyone yet BDS emits no "Player disconnected" lines, so
    the bot would keep believing they're online (and never stop the incremental
    cycle). This queries the server, then adds players it missed and drops ones
    that are gone, recording the matching session boundaries and starting or
    stopping the incremental cycle to match the new count.

    Returns the reconciled online list, or None if the server couldn't be
    queried (in which case the in-memory set is left untouched — best-effort).
    """
    with server.reconcile_lock:
        try:
            actual = server.backend.query_online_players()
        except Exception as e:
            server.log.warning("Reconcile online failed%s: %s",
                           f" ({reason})" if reason else "", e)
            return None
        actual_set = set(actual)
        before = set(server.get_online_players())
        joined = actual_set - before
        left = before - actual_set
        if joined or left:
            server.log.info("Reconcile online%s: +%s -%s",
                        f" ({reason})" if reason else "",
                        sorted(joined) or "none", sorted(left) or "none")
        for name in joined:
            server.player_join(name)
            pid = _uuid_by_name(name, server.names)
            if pid:
                server.backend.record_player_session("join", pid)
        for name in left:
            server.player_leave(name)
            pid = _uuid_by_name(name, server.names)
            if pid:
                server.backend.record_player_session("leave", pid)
        # Match the incremental cycle to reality: running iff someone is online.
        if actual_set:
            server.start_incremental_cycle()
        else:
            server.stop_incremental_cycle(final=bool(left))
        return sorted(actual_set)


# ---------------------------------------------------------------------------
# 8. Authorization System
# ---------------------------------------------------------------------------
_AUTH_PATH = Path(__file__).parent / "auth.json"
_auth_lock = threading.Lock()

# The whole bot-namespaced auth doc ({bot_name: {platform: ns}}) for the process,
# set in main(). Each Bot's `auth` is its own slice (the {platform: ns} value for
# its name), sharing the same dict objects — so mutating a bot's slice and
# persisting the doc (save_auth(_AUTH_DOC)) stay consistent.
_AUTH_DOC: dict = {}


def _normalize_ns(ns: dict) -> dict:
    """Normalize one platform's auth namespace: string IDs throughout.

    ``chat_servers`` binds an authorized chat to a specific server key (used
    once a bot fronts several servers); single-server bots ignore it and
    announce to every authorized chat. ``chat_names`` records each authorized
    chat's human name (group/channel title) — learned + refreshed from inbound
    messages — so logs and /listchats show names, not raw IDs.
    """
    admin = ns.get("admin_user_id")
    return {
        "admin_user_id": str(admin) if admin is not None else None,
        "authorized_chat_ids": [str(c) for c in ns.get("authorized_chat_ids", [])],
        "chat_servers": {str(c): str(s)
                         for c, s in (ns.get("chat_servers") or {}).items()},
        "chat_names": {str(c): str(n)
                       for c, n in (ns.get("chat_names") or {}).items()},
    }


def load_auth(path: Path, bots: list) -> dict:
    """Load the whole bot-namespaced auth doc for the configured ``bots`` (a list
    of BotConfig):
    ``{bot_name: {platform: {admin_user_id, authorized_chat_ids, chat_servers}}}``.

    Chains the legacy migrations so any older on-disk shape upgrades in place:
      1. flat ``{admin_user_id, authorized_chat_ids}`` -> ``{telegram: …}``
         (pre-multi-platform installs);
      2. platform-level ``{platform: ns}`` -> ``{<first bot>: {platform: ns}}``
         (pre-multi-bot installs — the legacy doc belonged to the sole bot, so
         it's folded under the first configured bot's name);
      3. every configured bot gets a namespace, and any bot that fronts exactly
         one server has its existing authorized chats bound to that server's key
         so a later multi-server config keeps delivering to them.

    Migration is in-memory only; the new shape is persisted on the next auth
    mutation (admin claim / authorize / revoke).
    """
    data = {}
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            logger.exception("Failed to load auth.json")
    # 1. flat -> telegram namespace
    if "admin_user_id" in data or "authorized_chat_ids" in data:
        data = {"telegram": data}
    # 2. platform-level -> bot-namespaced. A platform-level doc has ns dicts
    #    (with admin_user_id/authorized_chat_ids) as its top-level values; a
    #    bot-namespaced doc has platform maps there instead. Fold a legacy
    #    platform-level doc under the first configured bot (the historical one).
    if any(isinstance(v, dict)
           and ("admin_user_id" in v or "authorized_chat_ids" in v)
           for v in data.values()):
        data = {bots[0].name: data}
    # Ensure every configured bot has a namespace and normalize all of them.
    for b in bots:
        data.setdefault(b.name, {})
    out = {bname: {p: _normalize_ns(ns) for p, ns in bns.items()}
           for bname, bns in data.items()}
    # 3. bind pre-existing authorized chats to the sole server (per single-server
    #    bot), so an eventual multi-server upgrade keeps delivering to them.
    for b in bots:
        if len(b.servers) == 1:
            key = b.servers[0].key
            for ns in out[b.name].values():
                for cid in ns["authorized_chat_ids"]:
                    ns["chat_servers"].setdefault(cid, key)
    return out


def save_auth(auth: dict, path: Path) -> None:
    # Write-then-rename so a crash mid-write can't corrupt auth.json.
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(auth, f, indent=2)
    os.replace(tmp, path)


def _auth_ns(auth: dict, platform: str) -> dict:
    ns = auth.setdefault(
        platform,
        {"admin_user_id": None, "authorized_chat_ids": [], "chat_servers": {},
         "chat_names": {}})
    ns.setdefault("chat_names", {})  # older docs predate chat_names
    return ns


def is_admin(platform: str, user_id, auth: dict) -> bool:
    admin = (auth.get(platform) or {}).get("admin_user_id")
    return admin is not None and str(admin) == str(user_id)


def is_authorized(platform: str, chat_id, user_id, is_private: bool,
                  auth: dict) -> bool:
    """A command is processed only from the platform admin (in private) or an
    authorized chat (in a group/channel)."""
    ns = auth.get(platform) or {}
    if is_private:
        admin = ns.get("admin_user_id")
        return admin is not None and str(admin) == str(user_id)
    return str(chat_id) in ns.get("authorized_chat_ids", [])


# ---------------------------------------------------------------------------
# 9. Bot Commands
# ---------------------------------------------------------------------------
def _cmd_log(ctx, action: str, extra: str = "") -> None:
    """Uniform command audit line, prefixed with the handling bot:
    ``[<bot>] <action>: requested by [<sender>] on [<chat>]<extra>`` — where
    <chat> is the group/channel name (or 'direct' for a DM)."""
    ctx.bot.log.info("%s: requested by [%s] on [%s]%s",
                     action, ctx.sender_label, ctx.chat_label, extra)


def register_commands(router, auth: dict) -> None:
    """Register every command on the platform-agnostic router. Handlers take a
    Context and reply via it, so the same logic serves any chat platform. Each
    server-scoped handler acts on ``ctx.server`` (set by the router's resolve
    hook) — its backend, name registry, achievements, and deaths."""

    # --- /start, /help ---
    def cmd_help(ctx):
        _cmd_log(ctx, "Help")
        backend = ctx.server.backend
        lines = [
            "Available commands:",
            f"{ctx.adapter.command_label('status')} — show online players",
            "/list — list all known players",
        ]
        if backend.supports(CAP_STATS):
            lines += [
                "/stats [player] — player statistics",
                "/playtime — playtime leaderboard",
            ]
        if backend.supports(EVENT_ACHIEVEMENT):
            lines.append("/achievements [player] — player achievements")
        if backend.supports(EVENT_DEATH):
            lines += [
                "/deaths [player] — death history",
                "/death_summary — deaths grouped by cause",
            ]
        lines.append("/chat_id — show this chat's ID")
        if ctx.is_private and is_admin(ctx.platform, ctx.user_id, auth):
            if len(ctx.bot.servers) > 1:
                lines.append("/use <server> — pick the server your commands act on")
            lines += [
                "/authorize <chat_id> — whitelist a chat",
                "/revoke <chat_id> — remove a chat from whitelist",
                "/listchats — list authorized chats",
            ]
            if backend.supports(EVENT_ACHIEVEMENT):
                lines.append("/scan_achievements — scan all logs for achievements")
            if backend.supports(EVENT_DEATH):
                lines.append("/scan_deaths — scan all logs for deaths")
            lines.append("/backup — trigger a server backup now")
            if backend.supports(CAP_PLAYER_RESTORE):
                lines.append("/restore_player <username> — restore one player's data")
            if backend.can_restart:
                lines.append("/restore [<N>] — restore the whole world (stops + "
                             "restarts the server)")
            lines.append(f"/allowlist <on|off|add|remove|list|reload> [player] "
                         f"— server {backend.ALLOWLIST_VERB}")
        ctx.reply("\n".join(lines))
    router.register(["start", "help"], cmd_help)

    # --- /status ---
    def cmd_status(ctx):
        _cmd_log(ctx, "Status")
        # Query the server live (and reconcile the in-memory set) so the answer
        # reflects reality, not just what the bot last parsed from the log.
        online = reconcile_online(ctx.server, reason="/status")
        suffix = ""
        if online is None:  # server unreachable — fall back to last-known set
            online = ctx.server.get_online_players()
            suffix = " (server unreachable; last known)"
        if online:
            reply = f"Players online: {len(online)} ({', '.join(online)}){suffix}"
        else:
            reply = f"No players currently online.{suffix}"
        ctx.reply(reply)
        ctx.bot.log.info("Status: replied to [%s] — %s", ctx.sender_label, reply)
    router.register("status", cmd_status)

    # --- /stats ---
    def cmd_stats(ctx):
        refresh_player_names(ctx.server)
        target = ctx.args[0].lower() if ctx.args else None
        _cmd_log(ctx, "Stats", f" (player={target or 'all'})")
        all_stats = ctx.server.backend.player_stats(ctx.server.names)
        if not all_stats:
            ctx.reply("No player statistics recorded yet.")
            return
        if target:
            matches = [p for p in all_stats if p["name"].lower() == target]
            if not matches:
                ctx.reply(f"No player found matching '{target}'.")
                return
            ctx.reply(_format_stats(matches[0]))
        else:
            lines = [_format_stats(p) for p in sorted(all_stats, key=lambda p: p["name"].lower())]
            ctx.reply("\n\n".join(lines))
    router.register("stats", cmd_stats,
                    cap=lambda ctx: ctx.server.backend.supports(CAP_STATS),
                    cap_message="Player statistics are not available on this server edition.")

    # --- /playtime ---
    def cmd_playtime(ctx):
        refresh_player_names(ctx.server)
        _cmd_log(ctx, "Playtime")
        all_stats = ctx.server.backend.player_stats(ctx.server.names)
        if not all_stats:
            ctx.reply("No player statistics recorded yet.")
            return
        ranked = sorted(all_stats, key=lambda p: p["time_played_hours"], reverse=True)
        lines = [f"{i+1}. {p['name']} — {p['time_played_hours']}h" for i, p in enumerate(ranked)]
        ctx.reply("Playtime leaderboard:\n" + "\n".join(lines))
    router.register("playtime", cmd_playtime,
                    cap=lambda ctx: ctx.server.backend.supports(CAP_STATS),
                    cap_message="Playtime is not available on this server edition.")

    # --- /list ---
    def cmd_list(ctx):
        refresh_player_names(ctx.server)
        _cmd_log(ctx, "List")
        entries = ctx.server.backend.list_known_players(ctx.server.names)
        if not entries:
            ctx.reply("No players found.")
            ctx.bot.log.info("List: replied to [%s] — no known players", ctx.sender_label)
            return
        ctx.reply("Known players:\n" + "\n".join(entries))
        ctx.bot.log.info("List: replied to [%s] — %d known player(s)",
                         ctx.sender_label, len(entries))
    router.register("list", cmd_list)

    # --- /achievements ---
    def cmd_achievements(ctx):
        refresh_player_names(ctx.server)
        names = ctx.server.names
        achievements = ctx.server.achievements
        target = ctx.args[0].lower() if ctx.args else None
        _cmd_log(ctx, "Achievements", f" (player={target or 'all'})")
        if not achievements:
            ctx.reply("No achievements recorded yet.")
            return
        if target:
            uuid = next((u for u, n in names.items() if n.lower() == target), None)
            if not uuid or uuid not in achievements:
                ctx.reply(f"No achievements found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            lines = [f"Achievements for {player_name}:"]
            current_date = None
            for e in sorted(achievements[uuid], key=lambda x: x["timestamp"]):
                date_part, time_part = e["timestamp"].split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_part[:5]} | {e['type']:<11} | {e['achievement']}")
            ctx.reply("\n".join(lines), monospace=True)
        else:
            lines = []
            for uuid, entries in sorted(achievements.items(),
                                        key=lambda x: names.get(x[0], x[0]).lower()):
                lines.append(f"{names.get(uuid, uuid)}: {len(entries)} achievement(s)")
            ctx.reply("Achievements summary:\n" + "\n".join(lines))
    router.register("achievements", cmd_achievements,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_ACHIEVEMENT),
                    cap_message="Achievements are not tracked on this server edition.")

    # --- /scan_achievements ---
    def cmd_scan_achievements(ctx):
        _cmd_log(ctx, "ScanAchievements")
        refresh_player_names(ctx.server)
        ctx.reply("Scanning log files for achievements...")
        total = _scan_logs(_scan_log_for_achievements, ctx.server)
        ctx.reply(f"Scan complete. {total} new achievement(s) recorded.")
        ctx.bot.log.info("ScanAchievements: %d new achievement(s) found", total)
    router.register("scan_achievements", cmd_scan_achievements,
                    private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_ACHIEVEMENT),
                    cap_message="Achievements are not tracked on this server edition.")

    # --- /deaths ---
    def cmd_deaths(ctx):
        refresh_player_names(ctx.server)
        names = ctx.server.names
        deaths = ctx.server.deaths
        target = ctx.args[0].lower() if ctx.args else None
        _cmd_log(ctx, "Deaths", f" (player={target or 'all'})")
        if not deaths:
            ctx.reply("No deaths recorded yet.")
            return
        if target:
            uuid = next((u for u, n in names.items() if n.lower() == target), None)
            if not uuid or uuid not in deaths:
                ctx.reply(f"No deaths found for '{target}'.")
                return
            player_name = names.get(uuid, uuid)
            entries = deaths[uuid]
            lines = [f"Deaths for {player_name} ({len(entries)} total):"]
            current_date = None
            for e in sorted(entries, key=lambda x: x["timestamp"]):
                date_part, time_part = e["timestamp"].split(" ", 1)
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%b-%Y")
                except ValueError:
                    formatted_date = date_part
                if formatted_date != current_date:
                    current_date = formatted_date
                    lines.append(f"\n{formatted_date}")
                lines.append(f"  {time_part[:5]} | {e['message']}")
            ctx.reply("\n".join(lines), monospace=True)
        else:
            ranked = sorted(((names.get(u, u), len(e)) for u, e in deaths.items()),
                            key=lambda x: x[1], reverse=True)
            lines = ["Death summary:"]
            for player_name, count in ranked:
                lines.append(f"  {player_name}: {count} death(s)")
            ctx.reply("\n".join(lines), monospace=True)
    router.register("deaths", cmd_deaths,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /death_summary ---
    def cmd_death_summary(ctx):
        refresh_player_names(ctx.server)
        names = ctx.server.names
        deaths = ctx.server.deaths
        _cmd_log(ctx, "DeathSummary")
        if not deaths:
            ctx.reply("No deaths recorded yet.")
            return
        categories = {}
        grand_total = 0
        for uuid, entries in deaths.items():
            player_name = names.get(uuid, uuid)
            for e in entries:
                cat = _categorize_death(e["message"])
                counts = categories.setdefault(cat, {})
                counts[player_name] = counts.get(player_name, 0) + 1
                grand_total += 1
        lines = [f"Death Summary ({grand_total} total)", ""]
        ordered = [cat for cat, _ in _DEATH_CATEGORIES]
        for cat in ordered:
            if cat not in categories:
                continue
            counts = categories.pop(cat)
            lines.append(f"{cat}: {sum(counts.values())}")
            for player_name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")
        if "Other" in categories:
            counts = categories["Other"]
            lines.append(f"Other: {sum(counts.values())}")
            for player_name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {player_name:<16} {count}")
            lines.append("")
        ctx.reply("\n".join(lines).rstrip(), monospace=True)
    router.register("death_summary", cmd_death_summary,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /scan_deaths ---
    def cmd_scan_deaths(ctx):
        _cmd_log(ctx, "ScanDeaths")
        refresh_player_names(ctx.server)
        ctx.reply("Scanning log files for deaths...")
        total = _scan_logs(_scan_log_for_deaths, ctx.server)
        ctx.reply(f"Scan complete. {total} new death(s) recorded.")
        ctx.bot.log.info("ScanDeaths: %d new death(s) found", total)
    router.register("scan_deaths", cmd_scan_deaths,
                    private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.supports(EVENT_DEATH),
                    cap_message="Deaths are not tracked on this server edition.")

    # --- /backup ---
    def cmd_backup(ctx):
        server = ctx.server
        if not server.backup_lock.acquire(blocking=False):
            ctx.reply("A backup is already in progress.")
            return
        server.log.info("Backup: manually triggered by [%s] on [%s]",
                        ctx.sender_label, ctx.chat_label)
        ctx.reply("Starting backup...")
        say = lambda m: ctx.adapter.send(ctx.chat_id, m)

        def run():
            try:
                path = server.run_backup(status_cb=say)
                say(f"Backup complete: {Path(path).name}")
            except Exception as e:
                server.log.exception("Backup failed")
                say(f"Backup failed: {e}")
            finally:
                server.backup_lock.release()

        threading.Thread(target=run, daemon=True).start()
    router.register("backup", cmd_backup, private_only=True, admin_only=True)

    # --- /allowlist (server whitelist/allowlist passthrough) ---
    _ALLOWLIST_SUBS = {"on", "off", "add", "remove", "list", "reload"}

    def cmd_allowlist(ctx):
        backend = ctx.server.backend
        verb = backend.ALLOWLIST_VERB
        usage = (f"Usage: /allowlist <on|off|add|remove|list|reload> [player]\n"
                 f"(runs the server '{verb}' command)")
        if not ctx.args:
            ctx.reply(usage)
            return
        sub = ctx.args[0].lower()
        if sub not in _ALLOWLIST_SUBS:
            ctx.reply(f"Unknown subcommand '{ctx.args[0]}'.\n{usage}")
            return
        if sub in ("add", "remove") and len(ctx.args) < 2:
            ctx.reply(f"Usage: /allowlist {sub} <player>")
            return
        if not backend.is_available():
            ctx.reply("Server is not reachable right now — try again once it's up.")
            return
        logger.info("Allowlist: %s ran '%s %s'", ctx.sender_label, verb,
                    " ".join(ctx.args))

        # Run in a thread: Bedrock capture polls the log for up to a few seconds.
        def run():
            try:
                resp = backend.allowlist_command(ctx.args)
            except Exception as e:
                logger.exception("Allowlist command failed")
                ctx.adapter.send(ctx.chat_id, f"{verb} command failed: {e}")
                return
            ctx.adapter.send(ctx.chat_id,
                             resp.strip() or "(server returned no output)",
                             monospace=True)

        threading.Thread(target=run, daemon=True).start()
    router.register("allowlist", cmd_allowlist, private_only=True, admin_only=True)

    # --- /restore_player ---
    def cmd_restore_player(ctx):
        server = ctx.server
        backend = server.backend
        # Pending state is scoped to (bot, server, platform, admin): the
        # list -> select -> confirm sequence must not survive a /use switch (or
        # a same-admin second bot), or the confirm would restore onto a
        # different server with an index chosen from another server's list.
        pkey = (f"{ctx.bot.config.name}:{server.config.key}:"
                f"{ctx.platform}:{ctx.user_id}")
        if not ctx.args:
            ctx.reply("Usage:\n"
                      "  /restore_player <username>\n"
                      "  /restore_player <username> more\n"
                      "  /restore_player <username> <N>\n"
                      "  /restore_player <username> <N> confirm")
            return
        typed_name = ctx.args[0]
        typed_n = ctx.args[1] if len(ctx.args) >= 2 else None
        typed_confirm = ctx.args[2] if len(ctx.args) >= 3 else None
        if typed_confirm is not None and typed_confirm.lower() != "confirm":
            ctx.reply(f"Unexpected argument: '{typed_confirm}' (did you mean 'confirm'?)")
            return
        resolved = backend.resolve_player(typed_name, server.names)
        if resolved is None:
            ctx.reply(f"Unknown player: {typed_name}")
            return
        canonical, uuid = resolved

        if typed_n is None:
            versions = backend.list_player_versions(uuid, server.load_manifest()[:2])
            if not versions:
                _clear_pending_player_restore(pkey)
                ctx.reply(_format_versions_reply(canonical, uuid, versions))
                return
            _set_pending_player_restore(
                pkey, stage="listed", username=canonical, uuid=uuid,
                versions=versions, selected_n=None, page_offset=0)
            server.log.info("RestorePlayer: [%s] on [%s] listed %d version(s) for %s",
                        ctx.sender_label, ctx.chat_label, len(versions), canonical)
            ctx.reply(_format_versions_reply(canonical, uuid, versions, offset=0))
            return

        if typed_n.lower() == "more":
            entry = _get_pending_player_restore(pkey, expected_username=canonical)
            if entry is None:
                ctx.reply(f"Run /restore_player {canonical} first to see the list.")
                return
            new_offset = entry.get("page_offset", 0) + _RESTORE_PLAYER_PAGE_SIZE
            versions = entry["versions"]
            if new_offset >= len(versions):
                ctx.reply(f"No more versions for {canonical}.")
                return
            _set_pending_player_restore(pkey, page_offset=new_offset)
            server.log.info("RestorePlayer: [%s] on [%s] paged to offset %d for %s",
                        ctx.sender_label, ctx.chat_label, new_offset, canonical)
            ctx.reply(_format_versions_reply(canonical, uuid, versions, offset=new_offset))
            return

        try:
            n = int(typed_n)
        except ValueError:
            ctx.reply(f"Invalid selection: '{typed_n}'. Use a number or 'more'.")
            return

        if typed_confirm is not None:
            entry = _get_pending_player_restore(
                pkey, expected_username=canonical, expected_stage="selected")
            if entry is None or entry.get("selected_n") != n:
                ctx.reply(f"You must select a timestamp first with "
                          f"/restore_player {canonical} {n}")
                return
            versions = backend.list_player_versions(uuid, server.load_manifest()[:2])
            if not (1 <= n <= len(versions)):
                _clear_pending_player_restore(pkey)
                ctx.reply(f"Selection {n} is no longer valid (only "
                          f"{len(versions)} version(s) available). "
                          f"Run /restore_player {canonical} again.")
                return
            version = versions[n - 1]
            server.log.info("RestorePlayer: [%s] on [%s] confirmed restore of %s "
                        "to %s (source: %s)", ctx.sender_label, ctx.chat_label,
                        canonical, version["timestamp"], version["source"])
            ctx.reply(f"Starting restore of {canonical} to {version['timestamp']}...")
            _clear_pending_player_restore(pkey)
            say = lambda m: ctx.adapter.send(ctx.chat_id, m)

            def run():
                if not server.backup_lock.acquire(blocking=False):
                    say("A backup or restore is in progress.")
                    return
                try:
                    backend.restore_player(canonical, uuid, version, say)
                    # A Bedrock restore restarts the server (stop -> edit ->
                    # start), kicking everyone with no "disconnect" lines, so the
                    # in-memory online set would be left stale. Resync it against
                    # the freshly-restarted server (no-op on Java — no restart).
                    reconcile_online(server, reason="after restore")
                finally:
                    server.backup_lock.release()

            threading.Thread(target=run, daemon=True).start()
            return

        entry = _get_pending_player_restore(pkey, expected_username=canonical)
        if entry is None:
            ctx.reply(f"Run /restore_player {canonical} first to see the list.")
            return
        versions = entry["versions"]
        if not (1 <= n <= len(versions)):
            ctx.reply(f"Invalid selection: {n}. Choose 1-{len(versions)}.")
            return
        version = versions[n - 1]
        _set_pending_player_restore(
            pkey, stage="selected", username=canonical, uuid=uuid,
            versions=versions, selected_n=n)
        server.log.info("RestorePlayer: [%s] on [%s] selected version %d (%s) for %s",
                    ctx.sender_label, ctx.chat_label, n, version["source"], canonical)
        ctx.reply(_format_confirm_reply(canonical, uuid, n, version))
    router.register("restore_player", cmd_restore_player,
                    private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.supports(CAP_PLAYER_RESTORE),
                    cap_message="Per-player restore is not available on this server edition.")

    # --- /restore (whole-world restore: stop -> replace -> restart) ---
    def cmd_restore(ctx):
        server = ctx.server
        pkey = (f"{ctx.bot.config.name}:{server.config.key}:"
                f"{ctx.platform}:{ctx.user_id}")
        sub = ctx.args[0] if ctx.args else None

        def discover():
            return restore_core.list_restore_points(
                restore_core.discover_chains(server.config.backup_dir))

        # Bare /restore or "/restore more": (re)show the paged list.
        if sub is None or sub.lower() == "more":
            points = discover()
            if sub is None:
                offset = 0
                _set_pending_world_restore(pkey, stage="listed", offset=0)
            else:
                entry = _get_pending_world_restore(pkey)
                if entry is None:
                    ctx.reply("Send /restore first to see the list.")
                    return
                offset = entry.get("offset", 0) + _RESTORE_PAGE_SIZE
                if offset >= len(points):
                    ctx.reply("No more restore points.")
                    return
                _set_pending_world_restore(pkey, offset=offset)
            server.log.info("Restore: [%s] on [%s] listed %d point(s)",
                            ctx.sender_label, ctx.chat_label, len(points))
            ctx.reply(_format_restore_points(points, offset=offset))
            return

        # "/restore <N> [confirm]"
        try:
            n = int(sub)
        except ValueError:
            ctx.reply("Usage: /restore  |  /restore <N>  |  /restore <N> confirm")
            return
        confirm = len(ctx.args) >= 2 and ctx.args[1].lower() == "confirm"
        points = discover()
        if not (1 <= n <= len(points)):
            ctx.reply(f"Invalid selection: {n}. Choose 1-{len(points)}.")
            return
        point = points[n - 1]
        chains = restore_core.discover_chains(server.config.backup_dir)

        if not confirm:
            _set_pending_world_restore(pkey, stage="selected", selected_n=n)
            pre = ("A fresh full backup of the current world will be taken first."
                   if server.config.pre_restore_backup
                   else "No pre-restore backup will be taken (backup.pre_restore_"
                        "backup is off).")
            ctx.reply(
                "Confirm WORLD restore — this stops the server, replaces the "
                "world, and restarts it:\n"
                f"  Point:  [{point['kind']}] {point['pretty_ts']}\n"
                f"  Chain:  {point['chain_id'] or 'standalone'}\n"
                f"  {pre}\n"
                f"  Players will be warned {server.config.restore_warning_seconds}s "
                "in-game, then disconnected.\n\n"
                f"  To proceed, send:  /restore {n} confirm")
            return

        entry = _get_pending_world_restore(pkey, expected_stage="selected")
        if entry is None or entry.get("selected_n") != n:
            ctx.reply(f"Select first: /restore {n}")
            return
        _clear_pending_world_restore(pkey)
        server.log.info("Restore: [%s] on [%s] confirmed world restore to %s (%s)",
                        ctx.sender_label, ctx.chat_label,
                        point["pretty_ts"], point["kind"])
        ctx.reply(f"Starting world restore to {point['pretty_ts']}...")
        say = lambda m: ctx.adapter.send(ctx.chat_id, m)

        def run():
            if not server.backup_lock.acquire(blocking=False):
                say("A backup or restore is already in progress.")
                return
            try:
                server.restore_world(chains[point["chain_idx"]],
                                     point["point_idx"], say=say)
            finally:
                server.backup_lock.release()

        threading.Thread(target=run, daemon=True).start()
    router.register("restore", cmd_restore, private_only=True, admin_only=True,
                    cap=lambda ctx: ctx.server.backend.can_restart,
                    cap_message="World restore needs a restart transport — set "
                                "mux.session + mux.start_cmd for this server.")

    # --- /chat_id (public — lets an unauthorized chat learn its ID) ---
    def cmd_chat_id(ctx):
        _cmd_log(ctx, "ChatID", f" (chat_id={ctx.chat_id})")
        ctx.reply(f"Chat ID: {ctx.chat_id}")
    router.register("chat_id", cmd_chat_id, public=True, needs_server=False)

    # --- /use (multi-server: pick this admin's target server) ---
    def cmd_use(ctx):
        _cmd_log(ctx, "Use", f" (args={ctx.args})")
        ctx.bot.set_use(ctx)
    router.register("use", cmd_use, private_only=True, admin_only=True,
                    needs_server=False)

    # --- /authorize ---
    def cmd_authorize(ctx):
        multi = len(ctx.bot.servers) > 1
        usage = ("Usage: /authorize <chat_id> <server>" if multi
                 else "Usage: /authorize <chat_id>")
        if not ctx.args:
            ctx.reply(usage)
            return
        target_id = str(ctx.args[0]).strip()
        # Resolve the target server: single-server bots auto-bind to the sole
        # server; multi-server bots require an explicit <server> arg so the
        # channel's events aren't silently misrouted.
        if len(ctx.args) >= 2:
            server = ctx.bot.find_server(ctx.args[1])
            if server is None:
                names = ", ".join(sorted(ctx.bot.by_name)) or "(none)"
                ctx.reply(f"Unknown server '{ctx.args[1]}'.\nServers: {names}")
                return
        elif multi:
            names = ", ".join(sorted(ctx.bot.by_name))
            ctx.reply(f"{usage}\nServers: {names}")
            return
        else:
            server = ctx.bot.servers[0]
        with _auth_lock:
            ns = _auth_ns(auth, ctx.platform)
            if target_id not in ns["authorized_chat_ids"]:
                ns["authorized_chat_ids"].append(target_id)
            ns["chat_servers"][target_id] = server.config.key
            save_auth(_AUTH_DOC, _AUTH_PATH)
        ctx.bot.log.info("Authorize: chat %s -> %s by [%s] on [%s]",
                         ctx.bot.chat_display(ctx.platform, target_id),
                         server.config.name, ctx.sender_label, ctx.chat_label)
        ctx.reply(f"Chat {target_id} is now authorized for "
                  f"{server.config.name}.")
    router.register("authorize", cmd_authorize, private_only=True,
                    admin_only=True, needs_server=False)

    # --- /revoke ---
    def cmd_revoke(ctx):
        if not ctx.args:
            ctx.reply("Usage: /revoke <chat_id>")
            return
        target_id = str(ctx.args[0]).strip()
        with _auth_lock:
            ns = _auth_ns(auth, ctx.platform)
            was_bound = ns["chat_servers"].pop(target_id, None) is not None
            if target_id in ns["authorized_chat_ids"]:
                display = ctx.bot.chat_display(ctx.platform, target_id)
                ns["authorized_chat_ids"].remove(target_id)
                ns.get("chat_names", {}).pop(target_id, None)
                save_auth(_AUTH_DOC, _AUTH_PATH)
                ctx.bot.log.info("Revoke: chat %s by [%s] on [%s]",
                                 display, ctx.sender_label, ctx.chat_label)
                ctx.reply(f"Chat {target_id} has been revoked.")
            elif was_bound:
                save_auth(_AUTH_DOC, _AUTH_PATH)
                ctx.reply(f"Chat {target_id} has been revoked.")
            else:
                ctx.reply(f"Chat {target_id} was not authorized.")
    router.register("revoke", cmd_revoke, private_only=True,
                    admin_only=True, needs_server=False)

    # --- /listchats ---
    def cmd_listchats(ctx):
        _cmd_log(ctx, "ListChats")
        ns = auth.get(ctx.platform) or {}
        ids = ns.get("authorized_chat_ids", [])
        if not ids:
            ctx.reply("No authorized chats.")
            return
        binding = ns.get("chat_servers", {})
        names = ns.get("chat_names", {})
        multi = len(ctx.bot.servers) > 1
        lines = ["Authorized chats:"]
        for cid in ids:
            name = names.get(cid)
            who = f"{name} ({cid})" if name else cid
            if multi:
                srv = ctx.bot.by_key.get(binding.get(cid))
                label = srv.config.name if srv else (binding.get(cid) or "(unbound)")
                lines.append(f"  {who} -> {label}")
            else:
                lines.append(f"  {who}")
        ctx.reply("\n".join(lines))
    router.register("listchats", cmd_listchats, private_only=True,
                    admin_only=True, needs_server=False)


def _make_unclaimed_handler(auth: dict):
    """Build the admin-claim hook: the first private message on a platform with
    no admin yet claims that platform's admin."""
    def on_unclaimed(ctx) -> bool:
        ns = auth.get(ctx.platform) or {}
        if ns.get("admin_user_id") is not None or not ctx.is_private:
            return False
        with _auth_lock:
            if _auth_ns(auth, ctx.platform).get("admin_user_id") is not None:
                return False
            _auth_ns(auth, ctx.platform)["admin_user_id"] = ctx.user_id
            save_auth(_AUTH_DOC, _AUTH_PATH)
        ctx.bot.log.info("Admin claimed on %s by [%s] (id=%s)",
                         ctx.platform, ctx.sender_label, ctx.user_id)
        ctx.reply("You are now the admin.")
        return True
    return on_unclaimed


# ---------------------------------------------------------------------------
# 10. Main
# ---------------------------------------------------------------------------
def _capture_initial_online(server, bot) -> None:
    """On startup, capture players already online (the bot may have restarted
    mid-session) and open their online-time sessions. Alerts the bot's admins if
    the server can't be reached at all."""
    backend = server.backend
    if not backend.is_available(log_warnings=True):
        return
    online = None
    try:
        online = backend.query_online_players()
    except Exception as e:
        logger.warning("[%s] Online query failed, server may still be starting: %s",
                       server.config.name, e)
        if backend.wait_for_ready(timeout=120):
            logger.info("[%s] Server is now ready, retrying online query",
                        server.config.name)
            try:
                online = backend.query_online_players()
            except Exception as e2:
                logger.warning("[%s] Online query retry failed: %s",
                               server.config.name, e2)

    if online is None:
        # Never connected — server is likely not running. Alert the bot's admins.
        logger.error("[%s] Could not connect to the Minecraft server. "
                     "Server may not be running.", server.config.name)
        bot.alert_admins(
            f"⚠️ Could not connect to {server.config.name}.\n"
            "The server may not be running or not ready yet.\n"
            "Backups and /list will not work until the server is up.")
        return

    # Online-time bookkeeping: clear any sessions left open by a prior crash, then
    # open a session for each currently-online player (their join predates us).
    backend.reset_open_sessions()
    for name in online:
        server.player_join(name)
    current = server.get_online_players()
    if not current:
        logger.info("[%s] No players online", server.config.name)
        return
    logger.info("[%s] %d player(s) already online: %s", server.config.name,
                len(current), ", ".join(current))
    # The bot missed these players' connect lines (started mid-session), so
    # recover their xuids from the log to register them (Bedrock; no-op on Java).
    _recover_online_identities(server, current)
    for name in current:
        pid = _uuid_by_name(name, server.names)
        if pid:
            backend.record_player_session("join", pid)
    # The join notify callback never fired for these players, so start the cycle.
    server.start_incremental_cycle()


def _validate_chain(server) -> None:
    """Validate the incremental backup chain against the on-disk marker; clear a
    stale chain so incrementals are skipped until the next full backup."""
    chain_id, base_full, _ = server.load_manifest()
    if not chain_id:
        logger.info("[%s] No backup chain established. Run /backup to start one.",
                    server.config.name)
        return
    marker = server.read_chain_marker()
    if marker == chain_id:
        logger.info("[%s] Backup chain %s valid (base: %s)",
                    server.config.name, chain_id, base_full)
    else:
        logger.warning("[%s] Backup chain invalid: manifest chain %s does not "
                       "match marker %s. Incremental backups will be skipped "
                       "until a full backup is run.",
                       server.config.name, chain_id, marker or "(missing)")
        server.save_manifest({}, chain_id="", base_full="")


def _start_scheduled_backup(server, bot) -> None:
    """Start the per-server scheduled full-backup thread. Status is reported to
    the owning bot's admins."""
    hour = server.config.backup_hour
    schedule = server.config.backup_schedule

    def _next_backup_time(now: datetime) -> datetime:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if schedule == "weekly":
            days_ahead = (7 - now.weekday()) % 7  # Monday = 0
            target = target + timedelta(days=days_ahead)
            if target <= now:
                target = target + timedelta(weeks=1)
        elif schedule == "monthly":
            target = target.replace(day=1)
            if target <= now:
                if now.month == 12:
                    target = target.replace(year=now.year + 1, month=1)
                else:
                    target = target.replace(month=now.month + 1)
        else:  # daily (default)
            if target <= now:
                target = target + timedelta(days=1)
        return target

    def _loop():
        while True:
            now = datetime.now()
            target = _next_backup_time(now)
            wait = (target - now).total_seconds()
            logger.info("[%s] Next %s backup in %.0f seconds (at %s)",
                        server.config.name, schedule, wait,
                        target.strftime("%Y-%m-%d %H:%M"))
            time.sleep(wait)

            if not server.backend.is_available():
                logger.warning("[%s] Scheduled backup skipped: backend not available",
                               server.config.name)
                continue

            # Same exclusivity as /backup and the incremental cycle: never
            # overlap another backup's save-hold on this server.
            if not server.backup_lock.acquire(blocking=False):
                logger.warning("[%s] Scheduled backup skipped: another backup "
                               "is in progress", server.config.name)
                continue

            logger.info("[%s] Scheduled %s backup starting",
                        server.config.name, schedule)

            def status_cb(msg):
                bot.alert_admins(f"[Backup {server.config.name}] {msg}")

            try:
                path = server.run_backup(status_cb=status_cb)
                status_cb(f"Complete: {Path(path).name}")
            except Exception as e:
                logger.exception("[%s] Scheduled backup failed", server.config.name)
                status_cb(f"Failed: {e}")
            finally:
                server.backup_lock.release()

    threading.Thread(target=_loop, daemon=True,
                     name=f"backup-{server.config.key}").start()


def _bring_up_server(server, bot) -> bool:
    """Construct one server's backend and start watching + backing it up. Returns
    True on success; on backend failure alerts the bot's admins and returns False
    (the caller drops the server so the bot skips it)."""
    try:
        server.backend = make_backend(server.config,
                                      migrate_legacy=server._migrate_legacy)
    except BackendUnavailable as e:
        logger.error("[%s] Cannot start backend (edition '%s'): %s",
                     server.config.name, server.config.edition, e)
        bot.alert_admins(f"⚠️ Diamond Sign cannot start "
                         f"{server.config.name}: {e}")
        return False
    logger.info("[%s] Server edition: %s", server.config.name, server.config.edition)

    # Player registry, achievements, deaths (registry is backend-sourced).
    server.load_state()

    notify = make_notify_callback(bot, server)
    log_path = server.config.log_path
    watcher = LogWatcher(server, notify,
                         on_server_start=lambda: reconcile_online(
                             server, reason="server start"))
    server.watcher = watcher
    server.backend.attach_watcher(watcher)
    observer = Observer()
    observer.schedule(watcher, path=str(log_path.parent), recursive=False)
    observer.start()
    server.observer = observer
    logger.info("[%s] Watching %s for join/leave events",
                server.config.name, log_path)

    _capture_initial_online(server, bot)
    _validate_chain(server)
    _start_scheduled_backup(server, bot)
    return True


def _bring_up_bot(bot) -> None:
    """Build a bot's command router and start its adapter threads. Its adapters
    and servers must already be built (backends ready)."""
    auth = bot.auth
    logger.info("[%s] Chat platforms: %s", bot.config.name,
                ", ".join(a.name for a in bot.adapters))

    # Command router shared by this bot's adapters; replies go to the originating
    # chat. bot + resolve give handlers ctx.bot and the resolved ctx.server.
    router = CommandRouter(
        is_admin=lambda platform, uid: is_admin(platform, uid, auth),
        is_authorized=lambda platform, cid, uid, priv:
            is_authorized(platform, cid, uid, priv, auth),
        on_unclaimed=_make_unclaimed_handler(auth),
        logger=logger,
        bot=bot,
        resolve=bot.resolve_command,
    )
    bot.router = router
    register_commands(router, auth)

    for adapter in bot.adapters:
        threading.Thread(target=adapter.start, args=(router.dispatch,),
                         name=f"chat-{bot.config.name}-{adapter.name}",
                         daemon=True).start()
        logger.info("[%s] Started %s adapter", bot.config.name, adapter.name)


def _shutdown(bots) -> None:
    """Stop every bot's adapters and every server's watcher + flush its sessions."""
    for bot in bots:
        for adapter in bot.adapters:
            try:
                adapter.stop()
            except Exception:
                logger.exception("[%s] Failed to stop %s adapter",
                                 bot.config.name, adapter.name)
        for server in bot.servers:
            server.stop_incremental_cycle()
            # Flush open online-time sessions so playtime isn't lost on a clean
            # stop (a hard crash still loses the in-progress session).
            try:
                server.backend.close_open_sessions()
            except Exception:
                logger.exception("[%s] Failed to close open sessions on shutdown",
                                 server.config.name)
            if server.observer is not None:
                server.observer.stop()
                server.observer.join()
    logger.info("Diamond Sign stopped")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="diamondsign",
        description="Diamond Sign - Minecraft chat notifier + backups")
    parser.add_argument("--only", metavar="BOT",
                        help="run only the bot with this name (per-process "
                             "isolation); default runs every configured bot")
    return parser.parse_args(argv)


def main():
    args = _parse_args()
    setup_logging(Path(__file__).parent / "logs")

    global _AUTH_DOC

    bots_cfg = APP_CONFIG.bots
    if args.only:
        bots_cfg = [b for b in APP_CONFIG.bots if b.name == args.only]
        if not bots_cfg:
            names = ", ".join(b.name for b in APP_CONFIG.bots)
            print(f"--only: no bot named '{args.only}'. Configured bots: {names}",
                  file=sys.stderr)
            sys.exit(1)

    # Legacy repo-root state is migrated into data/<key>/ only for the classic
    # single-server install; a multi-server config can't unambiguously claim it.
    migrate_legacy = len(APP_CONFIG.all_servers()) == 1

    # One auth.json for the whole process; each bot operates on its own slice
    # (shared dict objects, so save_auth(_AUTH_DOC) persists in-place mutations).
    # Always pass the FULL bot list (not the --only-filtered one): load_auth
    # folds a legacy platform-level doc under the first bot, and filtering could
    # misattribute the historical admin/chats to whichever bot --only selected.
    _AUTH_DOC = load_auth(_AUTH_PATH, APP_CONFIG.bots)

    bots = []
    for bcfg in bots_cfg:
        servers = [Server(scfg, migrate_legacy=migrate_legacy)
                   for scfg in bcfg.servers]
        bot = Bot(bcfg, servers)
        bot.auth_doc = _AUTH_DOC
        bot.auth = _AUTH_DOC[bcfg.name]
        # Adapters first so a server backend failure can still alert the admins.
        bot.adapters = make_adapters(bcfg)
        # Bring up each server; drop any whose backend won't start.
        for server in list(bot.servers):
            if not _bring_up_server(server, bot):
                bot.drop_server(server)
        if not bot.servers:
            logger.error("[%s] No servers came up; skipping this bot", bcfg.name)
            for adapter in bot.adapters:
                try:
                    adapter.stop()
                except Exception:
                    pass
            continue
        bots.append(bot)

    if not bots:
        logger.error("No bots could be started (no reachable servers). Exiting.")
        return

    # Now that every server is up, wire each bot's router + start its adapters.
    for bot in bots:
        _bring_up_bot(bot)

    logger.info("Diamond Sign running: %d bot(s), %d server(s)",
                len(bots), sum(len(b.servers) for b in bots))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown(bots)


if __name__ == "__main__":
    main()
