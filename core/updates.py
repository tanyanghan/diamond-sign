"""Server-version updates: what's available, and installing it.

Kept out of ``core/server.py`` (already ~1000 lines) the same way
``utils/restore_core.py`` is, but the install path deliberately mirrors
``Server.restore_world`` step for step -- probe, back up, warn, stop with
Ctrl-C escalation, replace, relaunch, and the same "left deliberately
STOPPED" failure posture. That sequence is the hard-won part; this reuses it
rather than inventing a second one.

Two editions, two very different swaps:

  Java     one jar. Copy the cached artifact in beside the live one and
           ``os.replace`` onto it -- atomic, and the outgoing jar is kept so a
           bad build can be reverted by hand.
  Bedrock  a whole directory. The BDS zip ships server.properties,
           permissions.json and allowlist.json, so unpacking it over an
           install would overwrite the operator's config. Instead it is
           extracted to a staging sibling, the files worth keeping are moved
           across, and the directory is swapped in atomically via
           ``restore_core.swap_in_staging``.
"""

import logging
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

from core.presence import reconcile_online
from utils import mc_versions, restore_core
from utils.config import EDITION_BEDROCK
from utils.download import DownloadError, download_file
from utils.restore_core import _apply_zip_mode

logger = logging.getLogger("diamondsign")

# Bot infrastructure and operator config that a Bedrock release must never
# clobber. worlds/ is the world itself; the three json/properties files are
# shipped in the zip as DEFAULTS and would otherwise silently replace the
# operator's; console.log is the stream the bot tails.
#
# The chain marker is deliberately NOT here. An update makes the old chain
# meaningless: its base full holds the previous binary and a pre-migration
# world, and after a Bedrock swap every file in the directory has a fresh
# mtime, so the next incremental would diff against the stale manifest and
# re-capture the entire server directory (the pathology behind the 1.6 GB
# merged incremental this project already fought once). Letting the marker
# go leaves the chain correctly marked invalid, and the post-update full
# backup below rebases everything on the updated server.
_BEDROCK_PRESERVE = {
    "worlds", "server.properties", "permissions.json", "allowlist.json",
    "console.log",
}
_PACK_DIRS = ("behavior_packs", "resource_packs")


class UpdateError(RuntimeError):
    """The update could not proceed. The server was not touched."""


# --- what's available ------------------------------------------------------
def available_update(server):
    """Newest release for ``server``, or None if it is already current.

    Metadata only: no download, no locks. Safe to call from a poll thread.
    """
    cfg = server.config
    if not cfg.updates_enabled:
        return None
    installed = server.load_installed_version()
    if cfg.edition == EDITION_BEDROCK:
        release = mc_versions.latest_bedrock()
    else:
        # Configured flavour wins; otherwise believe what the startup banner
        # said, and fall back to vanilla until something says otherwise.
        flavor = cfg.java_flavor or installed.get("source") or "vanilla"
        if flavor == mc_versions.SOURCE_PAPER:
            pin = (installed.get("mc_version")
                   if cfg.updates_pin_mc_version else None)
            release = mc_versions.latest_paper(pin)
        else:
            release = mc_versions.latest_vanilla()
    if release is None or release.same_build_as(installed):
        return None
    return release


def is_major(release, installed: dict) -> bool:
    """Whether this changes the Minecraft version itself.

    That migrates the world format and generally cannot be undone -- an
    upgraded world will not load on the older server -- so it is worth saying
    out loud every time, separately from a routine build bump.
    """
    current = installed.get("mc_version")
    return bool(current) and current != release.mc_version


def describe_update(server, release) -> str:
    installed = server.load_installed_version()
    current = (installed.get("mc_version") or "unknown")
    if installed.get("build"):
        current += f" build {installed['build']}"
    line = (f"{server.config.name}: {release.source} {release.describe()} "
            f"available (installed: {current}).")
    if is_major(release, installed):
        line += ("\n⚠️ This changes the Minecraft version. It migrates "
                 "the world format and CANNOT be undone -- the upgraded world "
                 "will not load on the old version. A full backup runs first.")
    return line


# --- download --------------------------------------------------------------
def ensure_downloaded(release, log=None) -> Path:
    """Fetch ``release`` into the shared cache if it isn't there already."""
    def say(msg):
        if log:
            log(msg)

    dest = mc_versions.cache_dir(release.edition) / release.filename
    if dest.exists():
        say(f"{release.filename} already downloaded.")
        return dest
    say(f"Downloading {release.filename} ...")
    try:
        return download_file(release.url, dest, sha256=release.sha256,
                             sha1=release.sha1, expected_size=release.size,
                             log_fn=say)
    except DownloadError as e:
        raise UpdateError(f"download failed: {e}") from e


# --- who is playing --------------------------------------------------------
def require_empty_server(server) -> None:
    """Refuse to update while anyone is playing.

    An update disconnects everyone, and on a Minecraft version bump migrates
    the world on the way back up — not something to do out from under
    someone mid-session.

    It also happens to be what makes skipping the pre-update backup sound: an
    empty server means the incremental cycle already ran its final pass as the
    last player left, so the chain is a current rollback point rather than one
    that stops several minutes ago.

    A query that fails counts as "someone might be on". Being unable to ask is
    not evidence that nobody is playing.
    """
    online = reconcile_online(server, reason="before update")
    if online is None:
        raise UpdateError(
            "could not confirm whether anyone is online. Try again shortly, "
            "or stop the server first and re-run /update.")
    if online:
        names = ", ".join(sorted(online))
        raise UpdateError(
            f"{len(online)} player(s) online ({names}). An update disconnects "
            f"everyone \u2014 wait until the server is empty and run /update "
            f"again.")


# --- rollback point --------------------------------------------------------
def has_rollback_point(server, say) -> bool:
    """Whether the existing backup chain can serve as the update's rollback.

    A valid chain -- a full backup plus a manifest that still matches the
    on-disk marker, the same test bot.py makes at startup -- already IS a
    restorable copy of the world. With the incremental cycle running it is
    also current: it captures the world every few minutes while players are
    online, and once more as the last one leaves. Taking another full backup
    on top of that mostly duplicates it, and on a multi-GB world that is
    minutes of extra downtime for no extra safety.

    Incrementals being disabled is the exception. The chain is then only as
    fresh as the last SCHEDULED full, which on a weekly schedule can be days
    old — not something anyone wants to roll back to — so a fresh full
    backup runs instead.
    """
    chain_id, base_full, _ = server.load_manifest()
    if not chain_id:
        say("No backup chain established \u2014 taking a full backup first.")
        return False
    if server.read_chain_marker() != chain_id:
        say("Backup chain is invalid \u2014 taking a full backup first.")
        return False
    if not server.config.incremental_enabled:
        say(f"Backup chain {chain_id} is valid but incrementals are disabled "
            "for this server, so it may be days old \u2014 taking a fresh "
            "full backup first.")
        return False
    say(f"Backup chain {chain_id} is valid (base: {base_full}) and "
        "incrementals are current \u2014 using it as the rollback point "
        "instead of taking another full backup.")
    return True


# --- Bedrock helpers -------------------------------------------------------
def _extract_bds(artifact: Path, staging: Path, log) -> None:
    """Unpack a BDS zip into ``staging``, preserving Unix modes.

    Per-member extraction with ``_apply_zip_mode`` is not optional:
    ``zipfile`` drops the mode bits, which would leave ``bedrock_server``
    non-executable and the server unable to start at all. restore_core learned
    this the same way; see its ``_apply_zip_mode`` docstring.
    """
    problem = None
    with zipfile.ZipFile(artifact, "r") as zf:
        problem = zf.testzip()
        if problem is not None:
            raise UpdateError(f"downloaded zip is corrupt at '{problem}'")
        for info in zf.infolist():
            out = zf.extract(info, staging)
            _apply_zip_mode(info, Path(out))

    binary = staging / "bedrock_server"
    if not binary.exists():
        raise UpdateError("the downloaded zip has no bedrock_server binary")
    # Belt and braces: some zips carry no mode bits at all, in which case
    # _apply_zip_mode has nothing to apply.
    if not os.access(binary, os.X_OK):
        binary.chmod(0o755)
        log("Marked bedrock_server executable")

    if (staging / "worlds").exists():
        # swap_in_staging moves preserved entries with os.replace, which
        # cannot rename a directory onto a non-empty one. BDS has never
        # shipped worlds/, but if that ever changes, stop here rather than
        # discovering it mid-swap with the server already down.
        raise UpdateError("this release ships a worlds/ directory, which would "
                          "collide with the live world -- update by hand")


def custom_pack_names(mc_dir: Path, staging: Path) -> set:
    """Pack directories the operator added, which the release does not ship.

    Diffed rather than hardcoded: the set of vanilla packs changes between
    releases, and the operator may have packs of their own beside this bot's
    ``diamondsign_events``. Anything the new release did not supply is theirs
    and has to be carried across, or the swap would silently delete it.
    """
    names = set()
    for packs in _PACK_DIRS:
        old_dir, new_dir = mc_dir / packs, staging / packs
        if not old_dir.is_dir():
            continue
        shipped = ({p.name for p in new_dir.iterdir()}
                   if new_dir.is_dir() else set())
        extra = [e.name for e in old_dir.iterdir() if e.name not in shipped]
        if extra and not new_dir.is_dir():
            # Nothing to merge into: the move below needs the parent to exist.
            new_dir.mkdir(parents=True, exist_ok=True)
        names.update(f"{packs}/{name}" for name in extra)
    return names


def _install_bedrock(server, artifact: Path, say) -> Path:
    mc_dir = server.config.minecraft_dir
    staging = mc_dir.parent / f"{mc_dir.name}.update-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    say("Unpacking the new server...")
    _extract_bds(artifact, staging, say)

    preserve = set(_BEDROCK_PRESERVE) | custom_pack_names(mc_dir, staging)
    kept = sorted(n for n in preserve if "/" in n)
    if kept:
        say(f"Keeping your packs: {', '.join(kept)}")
    return restore_core.swap_in_staging(staging, mc_dir, preserve, say)


def _install_java(server, artifact: Path, say) -> Path:
    mc_dir = server.config.minecraft_dir
    jar = mc_dir / server.config.server_jar
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    kept = jar.with_name(f"{jar.name}.pre-update-{ts}")
    if jar.exists():
        shutil.copy2(jar, kept)
    tmp = jar.with_name(jar.name + ".new")
    shutil.copyfile(artifact, tmp)
    shutil.copymode(kept if kept.exists() else artifact, tmp)
    os.replace(tmp, jar)        # atomic: the jar is never half-written
    say(f"Installed {release_name(artifact)} as {server.config.server_jar}")
    return kept


def release_name(artifact: Path) -> str:
    return artifact.name


# --- preflight -------------------------------------------------------------
def preflight(server, release) -> None:
    """Refuse an update that cannot work, before anything is touched."""
    cfg = server.config
    if cfg.edition == EDITION_BEDROCK:
        ok, why = restore_core.can_stage_swap(cfg.minecraft_dir,
                                              cfg.backup_dir)
        if not ok:
            # Unlike /restore there is no in-place fallback: the only other
            # way to apply a BDS release is to unzip over the install, which
            # is exactly the config-destroying thing this avoids.
            raise UpdateError(
                f"cannot safely swap this server directory ({why}). Bedrock "
                "updates need a staged swap; fix the layout and retry.")
        return

    jar = cfg.minecraft_dir / cfg.server_jar
    if not jar.exists():
        found = sorted(p.name for p in cfg.minecraft_dir.glob("*.jar"))
        hint = (f" Found: {', '.join(found)}." if found else "")
        raise UpdateError(
            f"no '{cfg.server_jar}' in {cfg.minecraft_dir}.{hint} /update "
            f"overwrites exactly that file, so rename your jar to "
            f"'{cfg.server_jar}' (updating mux.start_cmd and any shell alias "
            f"to match), or set edition.server_jar to the name you use.")


# --- the update ------------------------------------------------------------
def update_server(server, release, *, say) -> None:
    """Install ``release``. Assumes the caller holds ``server.backup_lock``.

    Mirrors Server.restore_world's shape and failure posture: nothing is
    touched until the artifact is downloaded and verified, a full backup runs
    first (the only way back from a world migration), and a failure after the
    stop leaves the server deliberately down rather than running on a
    half-updated install.
    """
    backend = server.backend
    cfg = server.config
    warn = cfg.restore_warning_seconds
    down = safe_abort = installed_ok = relaunched = False
    kept = None

    try:
        # 0. Everything that can fail harmlessly, with the server still up.
        preflight(server, release)
        artifact = ensure_downloaded(release, say)

        already_down = backend.probe_stopped(timeout=10) is True
        if already_down:
            say("Server is already stopped — updating directly.")
        else:
            require_empty_server(server)

        # 1. Make sure a rollback point exists before anything is touched. A
        #    Minecraft version upgrade migrates the world irreversibly, so a
        #    backup is the only route back -- but it does not have to be a
        #    NEW one. See has_rollback_point().
        if not has_rollback_point(server, say):
            say("Taking a full backup before updating...")
            try:
                server.run_backup(status_cb=say, offline=already_down)
            except Exception as e:
                say(f"Pre-update backup failed, aborting update: {e}")
                return

        # 2. Warn players, immediately before the stop.
        if warn > 0 and not already_down and backend.is_online():
            try:
                backend.broadcast(f"Server updating in {warn}s — you will be "
                                  "disconnected. Reconnect shortly.")
                time.sleep(warn)
                backend.broadcast("Updating now — disconnecting...")
            except Exception:
                logger.warning("[%s] Update warning failed (continuing)",
                               cfg.name)

        # 3. Stop, with the same Ctrl-C escalation /restore uses.
        if already_down:
            down = True
        else:
            say("Stopping the server...")
            backend.stop_server(say)
            if not backend.wait_until_stopped(timeout=120):
                say("Server did not shut down in time — interrupting it "
                    "(Ctrl-C)...")
                if not backend.force_stop(say):
                    say("Server still did not shut down — aborting. It was "
                        "not relaunched; check it manually.")
                    return
                say("Server exited after interrupt")
            down = True

        # 4. Swap the binary in.
        if cfg.edition == EDITION_BEDROCK:
            kept = _install_bedrock(server, artifact, say)
        else:
            kept = _install_java(server, artifact, say)
        installed_ok = True
        server.save_installed_version(release.source, release.mc_version,
                                      release.build, filename=release.filename)
        _invalidate_chain(server)

        # 5. Relaunch.
        say("Restarting the server...")
        server._prepare_relaunch_cwd(cfg.edition == EDITION_BEDROCK)
        if backend.relaunch(say):
            relaunched = True
            server.reattach_log_watch()
            reconcile_online(server, reason="after server update")
            _cleanup(server, release, kept, say)
            say(f"Update complete — now running {release.source} "
                f"{release.describe()}.")
            _rebase_backup_chain(server, say)
        else:
            say("Update applied but relaunch was not confirmed. Start the "
                f"server manually:\n  {cfg.mux_start_cmd}")
    except UpdateError as e:
        safe_abort = True
        say(f"Update aborted (server untouched): {e}")
    except restore_core.SwapError as e:
        logger.exception("[%s] Update swap failed", cfg.name)
        say(f"Update failed during the swap: {e}")
        safe_abort = e.world_intact
    except Exception as e:
        logger.exception("[%s] Update failed", cfg.name)
        say(f"Update failed: {e}")
    finally:
        if down and not relaunched:
            if installed_ok or safe_abort:
                say("Bringing the server back up...")
                server._prepare_relaunch_cwd(cfg.edition == EDITION_BEDROCK)
                if backend.relaunch(say):
                    server.reattach_log_watch()
                    reconcile_online(server, reason="after server update")
                else:
                    say("Could not relaunch. Start the server manually:\n  "
                        f"{cfg.mux_start_cmd}")
            else:
                say("⚠️ The server was left STOPPED: the update did not "
                    "complete. Fix the problem and run /update again, or "
                    "/start to bring it up on the old version.")
        if kept is not None and not relaunched:
            say(f"Previous version kept at {kept.name} — restore it by hand "
                "if the new one will not start.")


def _invalidate_chain(server) -> None:
    """Retire the backup chain that described the PREVIOUS version.

    An update makes the old chain meaningless: its base full holds the old
    binary and a pre-migration world. On Bedrock the marker is already gone
    (the swap simply does not carry it across); on Java only the jar changed,
    so the marker is still sitting there and would otherwise keep a stale
    chain looking valid. Clearing both here means the state is honest in the
    window before the post-update backup — and stays honest if that backup
    never happens.
    """
    try:
        server.save_manifest({}, chain_id="", base_full="")
        marker = server.chain_marker_path
        if marker.exists():
            marker.unlink()
    except OSError:
        server.log.warning("Could not invalidate the backup chain after the "
                           "update; run /backup to re-establish it")


def _rebase_backup_chain(server, say) -> None:
    """Start a fresh backup chain on the updated server.

    Deliberately AFTER the relaunch: the new server migrates the world when it
    first opens it, so a backup taken while it was still stopped would pair
    the new binary with a pre-migration world. run_backup() generates a new
    chain id, rebuilds the manifest from the files as they now are, and writes
    the marker — so every later incremental is based on the updated server
    instead of on a full backup holding the previous version.

    This matters most on Bedrock, where the swap gives every file in the
    directory a fresh mtime: without re-basing, the next incremental would
    diff against the old manifest and re-capture the whole server directory.

    A failure here does not undo the update. It leaves incrementals suspended,
    which is the correct fail-safe, until the operator runs /backup.
    """
    say("Taking a post-update backup to re-base the backup chain...")
    try:
        server.run_backup(status_cb=say)
        say("Backup chain re-based on the updated server.")
    except Exception as e:
        server.log.exception("Post-update backup failed")
        say(f"Post-update backup failed: {e}\nThe update itself succeeded, "
            "but incremental backups stay suspended until you run /backup.")


def _cleanup(server, release, kept, say) -> None:
    """Drop superseded artifacts once the new version is confirmed running."""
    try:
        removed = mc_versions.prune_cache(release.edition, {release.filename})
        if removed:
            say(f"Pruned {len(removed)} superseded download(s) from the cache.")
    except OSError:
        logger.warning("[%s] Cache prune failed", server.config.name)
    # The Bedrock swap leaves the whole previous server dir aside; the Java
    # swap only a jar. Both are the rollback path, so they are only dropped
    # after the relaunch is confirmed.
    if kept is not None and kept.is_dir():
        server._discard_old_world(kept)
