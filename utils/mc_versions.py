"""Where new Minecraft server releases are discovered, and the download cache.

Three upstreams, all JSON. The minecraft.net download pages the operator sees
are JS/Cloudflare-gated and cannot be scraped, but each has a real API behind
it:

  Paper    fill.papermc.io/v3  -- note the OLD v2 API is retired (HTTP 410),
           and v3 REJECTS generic User-Agents (see utils.download.USER_AGENT).
  Vanilla  Mojang's version manifest -> per-version json -> downloads.server
  Bedrock  the minecraft-services download-links endpoint; the version is only
           available from the zip's FILENAME, and no checksum is published.

Every provider is split into a pure ``_parse_*`` function over an
already-fetched dict and a thin ``latest_*`` wrapper that does the fetching.
That split is what lets the tests cover the parsing against captured fixtures
without touching the network.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .download import BROWSER_USER_AGENT, DownloadError, USER_AGENT, fetch_json

logger = logging.getLogger("diamondsign")

PAPER_PROJECT = "https://fill.papermc.io/v3/projects/paper"
VANILLA_MANIFEST = ("https://launchermeta.mojang.com/mc/game/"
                    "version_manifest_v2.json")
BEDROCK_LINKS = ("https://net-secondary.web.minecraft-services.net/"
                 "api/v1.0/download/links")

# Where downloaded server artifacts live. Deliberately outside the repo (all
# other state is repo-relative: data/<key>/, auth.json, logs/) because these
# are large, shared between servers of the same edition, and worth surviving a
# re-clone of the bot.
CACHE_DIR = Path.home() / ".diamond-sign" / "minecraft_servers"

SOURCE_PAPER = "paper"
SOURCE_VANILLA = "vanilla"
SOURCE_BEDROCK = "bedrock"

# bedrock-server-1.26.45.1.zip -> 1.26.45.1
_RE_BEDROCK_VERSION = re.compile(r'bedrock-server-([\d.]+)\.zip')
# A Paper/Mojang version key that is a real release, not 26.2-rc-2 / 1.21-pre1.
_RE_PRERELEASE = re.compile(r'-(rc|pre|snapshot)', re.IGNORECASE)


@dataclass
class ReleaseInfo:
    """One downloadable server build."""
    source: str                 # SOURCE_PAPER | SOURCE_VANILLA | SOURCE_BEDROCK
    mc_version: str             # "26.2", "1.26.45.1"
    build: int | None           # Paper build number; None for the others
    url: str
    filename: str
    sha256: str | None = None
    sha1: str | None = None
    size: int | None = None
    # Which User-Agent this artifact's host expects. Paper demands a
    # descriptive one; minecraft.net's CDN stalls anything non-browser.
    user_agent: str = USER_AGENT

    @property
    def edition(self) -> str:
        return "bedrock" if self.source == SOURCE_BEDROCK else "java"

    def describe(self) -> str:
        if self.build is not None:
            return f"{self.mc_version} build {self.build}"
        return self.mc_version

    def same_build_as(self, record: dict) -> bool:
        """Whether an installed-version record already describes this build."""
        return (record.get("source") == self.source
                and record.get("mc_version") == self.mc_version
                and record.get("build") == self.build)


def describe_version(record: dict) -> str:
    """Name an installed-version record: '26.2 build 124', or '26.2'.

    One definition because it was previously written three times and two of
    them dropped the build — so a log line read "installed: 26.2" for a
    server whose whole reason to update was that it was on build 123 rather
    than 124, and a build-only bump logged as "26.2 -> 26.2".
    """
    version = record.get("mc_version") or "unknown"
    build = record.get("build")
    return f"{version} build {build}" if build else version


def version_tuple(v: str) -> tuple:
    """Sortable form of a dotted version ('26.2' -> (26, 2)).

    Non-numeric components sort last so a malformed key can never be mistaken
    for the newest release.
    """
    parts = []
    for chunk in str(v).split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, 0))
    return tuple(parts)


def is_prerelease(v: str) -> bool:
    return bool(_RE_PRERELEASE.search(v))


# --- Paper -----------------------------------------------------------------
def parse_paper_versions(doc: dict) -> list[str]:
    """Stable Minecraft versions Paper publishes, newest first."""
    versions = (doc or {}).get("versions") or {}
    stable = [v for v in versions if not is_prerelease(v)]
    return sorted(stable, key=version_tuple, reverse=True)


def parse_paper_build(doc: dict, mc_version: str) -> ReleaseInfo | None:
    """Turn a .../builds/latest document into a ReleaseInfo.

    Only STABLE builds are offered: an experimental Paper build is not
    something to hand an operator as "an update is available".
    """
    doc = doc or {}
    if str(doc.get("channel", "")).upper() != "STABLE":
        return None
    dl = (doc.get("downloads") or {}).get("server:default") or {}
    url = dl.get("url")
    if not url:
        return None
    return ReleaseInfo(
        source=SOURCE_PAPER, mc_version=mc_version, build=doc.get("id"),
        url=url, filename=dl.get("name") or Path(url).name,
        sha256=(dl.get("checksums") or {}).get("sha256"),
        size=dl.get("size"))


def latest_paper(mc_version: str | None = None, *,
                 not_older_than: str | None = None) -> ReleaseInfo | None:
    """Newest STABLE Paper build, or None.

    ``mc_version`` pins the search to one Minecraft version. Otherwise the
    newest version that actually HAS a stable build wins -- which is not the
    same as the newest version key. Paper publishes a key as soon as ALPHA
    builds exist for it, so looking only at the newest one meant reporting
    "up to date" while the version the server really runs had a newer stable
    build waiting. Seen in the wild: 26.3's latest build was ALPHA while 26.2
    had stable build 124, and a server on 26.2 build 121 was told it was
    current.

    ``not_older_than`` stops the walk before it reaches versions older than
    the one installed, so the fallback can never offer a downgrade.
    """
    try:
        if mc_version is not None:
            return parse_paper_build(
                fetch_json(f"{PAPER_PROJECT}/versions/{mc_version}"
                           "/builds/latest"), mc_version)
        floor = version_tuple(not_older_than) if not_older_than else None
        for candidate in parse_paper_versions(fetch_json(PAPER_PROJECT)):
            if floor is not None and version_tuple(candidate) < floor:
                break       # sorted newest-first, so nothing below is newer
            release = parse_paper_build(
                fetch_json(f"{PAPER_PROJECT}/versions/{candidate}"
                           "/builds/latest"), candidate)
            if release is not None:
                return release
            logger.info("Paper %s has no stable build yet; looking further "
                        "back", candidate)
        return None
    except DownloadError as e:
        logger.warning("Paper version check failed: %s", e)
        return None


# --- Vanilla Java ----------------------------------------------------------
def parse_vanilla_manifest(doc: dict) -> tuple[str, str] | None:
    """``(latest_release_id, that version's metadata url)``."""
    doc = doc or {}
    latest = (doc.get("latest") or {}).get("release")
    if not latest:
        return None
    for entry in doc.get("versions") or []:
        if entry.get("id") == latest and entry.get("url"):
            return latest, entry["url"]
    return None


def parse_vanilla_version(doc: dict, mc_version: str) -> ReleaseInfo | None:
    server = ((doc or {}).get("downloads") or {}).get("server") or {}
    if not server.get("url"):
        return None
    return ReleaseInfo(
        source=SOURCE_VANILLA, mc_version=mc_version, build=None,
        url=server["url"], filename=f"minecraft_server-{mc_version}.jar",
        sha1=server.get("sha1"), size=server.get("size"))


def latest_vanilla() -> ReleaseInfo | None:
    try:
        found = parse_vanilla_manifest(fetch_json(VANILLA_MANIFEST))
        if not found:
            return None
        mc_version, url = found
        return parse_vanilla_version(fetch_json(url), mc_version)
    except DownloadError as e:
        logger.warning("Vanilla version check failed: %s", e)
        return None


# --- Bedrock ---------------------------------------------------------------
def parse_bedrock_links(doc: dict) -> ReleaseInfo | None:
    """Pick the stable Linux BDS zip out of the download-links document.

    The version exists only in the filename, and no checksum is published, so
    a downloaded zip is verified by opening it (CRC) rather than by hash.
    """
    links = ((doc or {}).get("result") or {}).get("links") or []
    for link in links:
        if link.get("downloadType") != "serverBedrockLinux":
            continue           # skip Windows and the *Preview* channels
        url = link.get("downloadUrl") or ""
        m = _RE_BEDROCK_VERSION.search(url)
        if not m:
            return None
        return ReleaseInfo(source=SOURCE_BEDROCK, mc_version=m.group(1),
                           build=None, url=url, filename=Path(url).name,
                           user_agent=BROWSER_USER_AGENT)
    return None


def latest_bedrock() -> ReleaseInfo | None:
    try:
        return parse_bedrock_links(fetch_json(BEDROCK_LINKS))
    except DownloadError as e:
        logger.warning("Bedrock version check failed: %s", e)
        return None


# --- Download cache --------------------------------------------------------
def cache_dir(edition: str) -> Path:
    d = CACHE_DIR / ("bedrock" if edition == "bedrock" else "java")
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_cache(edition: str, keep_names) -> list[str]:
    """Delete cached artifacts other than ``keep_names``.

    Retention is "newest download + whatever is currently installed", so the
    running version is always on hand for a manual rollback while the cache
    stays bounded -- Paper alone publishes several builds a week.
    Returns the names removed.
    """
    keep = {n for n in keep_names if n}
    removed = []
    for entry in cache_dir(edition).iterdir():
        if entry.name in keep or not entry.is_file():
            continue
        try:
            entry.unlink()
            removed.append(entry.name)
        except OSError:
            logger.warning("Could not prune cached %s", entry)
    return removed
