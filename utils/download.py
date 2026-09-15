"""Stdlib-only HTTP helpers for the server-version updater.

Deliberately built on ``urllib.request`` rather than ``requests``: this project
keeps its dependency list short on purpose (``amulet`` is left out of
requirements.txt because it has no Linux wheels), and fetching a JSON document
and streaming a file to disk need nothing a third-party client would add.

Every download follows the same contract the backup writer uses
(``utils.backup_utils.finalize_backup_zip``): stream to a ``.part`` file,
verify it, and only then ``os.replace`` it into its final name. A download
killed halfway through therefore leaves debris that is obviously debris, never
a truncated file masquerading as a good one.
"""

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("diamondsign")

# PaperMC's API rejects generic User-Agents (curl/wget/python-urllib defaults):
# it requires one that identifies the software and carries a contact URL. The
# project URL is deliberately used as that contact -- never an operator's email
# address, which has no business being sent to a third-party service in a
# header.
USER_AGENT = ("diamond-sign/1.0 "
              "(+https://github.com/tanyanghan/diamond-sign)")

# The two upstreams want OPPOSITE things, which is why the User-Agent is
# per-request rather than a single constant.
#
# Paper rejects generic agents outright. minecraft.net's CDN does the reverse:
# it accepts the connection from a non-browser agent and then simply never
# sends the body, so the download dies on a read timeout rather than a clean
# error. Measured against the live URL: the descriptive agent above times out
# after 25s, this one returns 206 in 0.1s.
BROWSER_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_CHUNK = 256 * 1024


class DownloadError(RuntimeError):
    """A fetch or download failed, or its content did not verify."""


def fetch_json(url: str, *, timeout: float = 15,
               user_agent: str = USER_AGENT) -> dict:
    """GET ``url`` and parse the response as JSON.

    Raises ``DownloadError`` on any network, HTTP or decode failure, so a
    caller polling several providers can treat one bad source as "no news"
    rather than letting it take the bot down.
    """
    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise DownloadError(f"{url}: HTTP {e.code} {e.reason}") from e
    except Exception as e:
        raise DownloadError(f"{url}: {e}") from e
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise DownloadError(f"{url}: not valid JSON ({e})") from e


def _hasher(sha256: str | None, sha1: str | None):
    if sha256:
        return hashlib.sha256(), sha256.lower(), "sha256"
    if sha1:
        return hashlib.sha1(), sha1.lower(), "sha1"
    return None, None, None


def download_file(url: str, dest: Path, *, sha256: str | None = None,
                  sha1: str | None = None, expected_size: int | None = None,
                  timeout: float = 60, retries: int = 3,
                  user_agent: str = USER_AGENT, log_fn=None) -> Path:
    """Download ``url`` to ``dest``, verifying it before it takes that name.

    Hashes incrementally while streaming rather than reading the file into
    memory -- these are 60-120 MB artifacts and this process has been OOM-killed
    before (see utils/backup_utils.py's memory notes).

    ``sha256``/``sha1``: whichever the provider publishes. Paper publishes
    sha256, Mojang sha1; Bedrock publishes neither, so only ``expected_size``
    can be checked there.

    Retries a few times on network failure. There is deliberately no resume:
    nothing else in this codebase resumes a partial write either (stale ``.tmp``
    files are swept and the work redone), and for artifacts this size a clean
    retry is simpler than getting range requests right.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last = None

    for attempt in range(1, retries + 1):
        digest, expected_hex, algo = _hasher(sha256, sha1)
        written = 0
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout) as resp, \
                    open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if digest is not None:
                        digest.update(chunk)

            if expected_size is not None and written != expected_size:
                raise DownloadError(
                    f"size mismatch: got {written} bytes, expected "
                    f"{expected_size}")
            if digest is not None and digest.hexdigest() != expected_hex:
                raise DownloadError(
                    f"{algo} mismatch: got {digest.hexdigest()}, expected "
                    f"{expected_hex}")

            os.replace(tmp, dest)
            log(f"Downloaded {dest.name} ({written / (1024 * 1024):.1f} MB"
                + (f", {algo} verified)" if digest is not None else ")"))
            return dest
        except Exception as e:
            last = e
            # Never leave a partial or unverified file behind: the next caller
            # must not be able to mistake it for a complete download.
            try:
                tmp.unlink()
            except OSError:
                pass
            if attempt < retries:
                log(f"Download failed ({e}) — retrying {attempt + 1}/{retries}")
                time.sleep(2 * attempt)

    raise DownloadError(f"{url}: gave up after {retries} attempts ({last})")
