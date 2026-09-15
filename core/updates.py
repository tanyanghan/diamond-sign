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
from utils.backup_utils import CHAIN_MARKER_NAME
from utils.config import EDITION_BEDROCK
from utils.download import DownloadError, download_file
from utils.restore_core import _apply_zip_mode

logger = logging.getLogger("diamondsign")

# Bot infrastructure and operator config that a Bedrock release must never
# clobber. worlds/ is the world itself; the three json/properties files are
# shipped in the zip as DEFAULTS and would otherwise silently replace the
# operator's; console.log is the stream the bot tails; the chain marker must
# survive or the next startup invalidates the backup chain.
_BEDROCK_PRESERVE = {
    "worlds", "server.properties", "permissions.json", "allowlist.json",
    "console.log", CHAIN_MARKER_NAME,
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

        # 1. Always back up first. A Minecraft version upgrade migrates the
        #    world irreversibly, so this is the only route back.
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
