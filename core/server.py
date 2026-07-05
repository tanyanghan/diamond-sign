"""The per-server runtime object.

``Server`` owns a Minecraft server's config, backend, and mutable state (online
players, Bedrock session identities, backup chain state) and orchestrates its
backups and whole-world restore. The two module-private helpers at the bottom
(``_add_world_file_to_zip``, ``_diff_manifest``) are the backup file mechanics
used only by ``Server``.
"""

import json
import os
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from watchdog.observers import Observer

from core.logutil import TagLogAdapter
from core.state import load_achievements, load_deaths, uuid_by_name
from core.presence import reconcile_online
from core.logwatch import LogWatcher
from utils.backup_utils import (
    CHAIN_MARKER_NAME, META_FILES,
    build_file_manifest, new_chain_id, run_copy_command, wait_for_settle,
)
from utils.config import backup_exclude_names, EDITION_BEDROCK
from utils import restore_core

# ---------------------------------------------------------------------------
# Server runtime object (per-server state)
# ---------------------------------------------------------------------------
class Server:
    """One Minecraft server's runtime: its config, backend, and per-server
    mutable state (online players, session xuids, pending UUID correlation, and
    — added in later sub-steps — backup state). main() builds one of these
    today and will loop over several once multi-server lands; the module-level
    aliases below keep existing call sites working in the meantime.
    """

    def __init__(self, config):
        self.config = config
        self.log = TagLogAdapter(logger, {"tag": config.name})
        self.backend = None  # set in main() after make_backend
        self.watcher = None  # LogWatcher, set when the server is brought up
        self.observer = None  # watchdog Observer, stopped on shutdown
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
        """Resolve a per-server state file under data/<key>/."""
        return self.data_dir / filename

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
        """
        try:
            with open(self.chain_marker_path, "w") as f:
                f.write(chain_id)
        except Exception:
            self.log.exception("Failed to write chain marker")

    def read_chain_marker(self) -> str:
        """Read the chain ID from the chain marker; '' if it doesn't exist."""
        try:
            return self.chain_marker_path.read_text().strip()
        except FileNotFoundError:
            return ""
        except Exception:
            self.log.exception("Failed to read chain marker")
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
        online_xuids = {uuid_by_name(n, names) for n in self.get_online_players()}
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
                        # Chain marker, backup-format entries (META_FILES — the
                        # sidecar/meta/deletions the bot writes into the zip
                        # itself), and bot infrastructure are not server data.
                        # Skipping META_FILES avoids a "Duplicate name" if a copy
                        # lingers in the world dir (e.g. from a restore extraction).
                        if fn == CHAIN_MARKER_NAME \
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

    def reattach_log_watch(self) -> None:
        """Re-establish the log-directory watch after a restore replaced the
        server directory. A world restore wipes minecraft_dir, which for Java
        deletes the logs/ dir that inotify was watching — so the observer keeps
        watching the now-gone directory and never sees the new latest.log (join/
        leave, save-off/save-all confirmations, RCON readiness all go unseen).
        Point a fresh observer at the recreated log dir and re-seek the watcher.
        Bedrock tails console.log at the server root (not wiped), so this is a
        no-op there in practice, but it's safe to call for either edition."""
        if self.watcher is None:
            return
        log_dir = self.config.log_path.parent
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.watcher.reset()  # re-seek to the (new) latest.log
            observer = Observer()
            observer.schedule(self.watcher, path=str(log_dir), recursive=False)
            observer.start()
            old = self.observer
            self.observer = observer
            if old is not None:
                old.stop()
                old.join()
            self.log.info("Re-attached log watch on %s (server dir was replaced)",
                          log_dir)
        except Exception:
            self.log.exception("Failed to re-attach log watch after restore")

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
        down = relaunched = False
        try:
            # 1. In-game warning + countdown — best-effort, and only when the
            #    server is actually running (is_online, not is_available: a
            #    down server can't be broadcast to, and a failed broadcast must
            #    never abort the restore we're about to do anyway).
            if warn > 0 and backend.is_online():
                try:
                    backend.broadcast(f"Server restoring in {warn}s — you will "
                                      "be disconnected. Reconnect shortly.")
                    self.log.info("World restore: warned players, %ds countdown",
                                  warn)
                    time.sleep(warn)
                    backend.broadcast("Restoring now — disconnecting...")
                except Exception:
                    self.log.warning("World restore: in-game warning failed "
                                     "(continuing)")

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
                # The wipe replaced the server dir; rebind the log watcher to the
                # recreated log dir before reconciling / awaiting future events.
                self.reattach_log_watch()
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
                    self.reattach_log_watch()
                    reconcile_online(self, reason="after world restore")
                else:
                    say("Could not relaunch. Start the server manually:\n  "
                        f"{self.config.mux_start_cmd}")



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

