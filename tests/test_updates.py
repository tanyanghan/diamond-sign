"""Self-contained tests for server-version monitoring and downloads.

Run with plain ``python tests/test_updates.py`` -- no pytest, no dependencies
beyond the stdlib, and NO NETWORK. The provider fixtures below are real
responses captured from the live APIs, so the parsers are tested against the
shapes they actually have to cope with.
"""
import hashlib
import io
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import mc_versions as mv
from utils.download import DownloadError, download_file, fetch_json

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


# --- fixtures: real captured API responses ---------------------------------
PAPER_PROJECT = {
    "project": {"id": "paper", "name": "Paper"},
    "versions": {"26.2": ["26.2", "26.2-rc-2"],
                 "26.1": ["26.1.2", "26.1.1"],
                 "1.21": ["1.21.4"]},
}
PAPER_BUILD = {
    "id": 123, "time": "2026-09-09T17:32:22Z", "channel": "STABLE",
    "downloads": {"server:default": {
        "name": "paper-26.2-123.jar",
        "checksums": {"sha256": "7b7b3b43c009103e1971a0576c26f655a7dd9b56"
                                "a0a2a4438e352c03a7fecd08"},
        "size": 64521337,
        "url": "https://fill-data.papermc.io/v1/objects/7b7b3b43c009103e1971"
               "a0576c26f655a7dd9b56a0a2a4438e352c03a7fecd08/paper-26.2-123.jar"}},
}
MOJANG_MANIFEST = {
    "latest": {"release": "26.2", "snapshot": "26.3-rc-3"},
    "versions": [
        {"id": "26.3-rc-3", "type": "snapshot", "url": "https://x/rc.json"},
        {"id": "26.2", "type": "release",
         "url": "https://piston-meta.mojang.com/v1/packages/"
                "1595470509933451a460bd157624e6e4f083890b/26.2.json"},
    ],
}
MOJANG_VERSION = {"downloads": {"server": {
    "sha1": "823e2250d24b3ddac457a60c92a6a941943fcd6a",
    "size": 60894273,
    "url": "https://piston-data.mojang.com/v1/objects/"
           "823e2250d24b3ddac457a60c92a6a941943fcd6a/server.jar"}}}
BEDROCK_LINKS = {"result": {"links": [
    {"downloadType": "serverBedrockWindows",
     "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/"
                    "bin-win/bedrock-server-1.26.45.1.zip"},
    {"downloadType": "serverBedrockLinux",
     "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/"
                    "bin-linux/bedrock-server-1.26.45.1.zip"},
    {"downloadType": "serverBedrockPreviewLinux",
     "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/"
                    "bin-linux-preview/bedrock-server-1.26.60.23.zip"},
    {"downloadType": "serverJar", "downloadUrl": "https://x/server.jar"},
]}}


def test_paper():
    print("Paper parsing:")
    versions = mv.parse_paper_versions(PAPER_PROJECT)
    check(versions[0] == "26.2", f"newest stable version first ({versions[0]})")
    check("26.2-rc-2" not in versions, "release candidates excluded")
    check(versions == ["26.2", "26.1", "1.21"],
          f"sorted numerically, not lexically ({versions})")

    rel = mv.parse_paper_build(PAPER_BUILD, "26.2")
    check(rel is not None and rel.build == 123, "build number parsed")
    check(rel.sha256.startswith("7b7b3b43"), "sha256 captured for verification")
    check(rel.filename == "paper-26.2-123.jar", "filename captured")
    check(rel.edition == "java", "reported as a java artifact")
    check(rel.describe() == "26.2 build 123", f"describe() -> {rel.describe()}")

    experimental = dict(PAPER_BUILD, channel="EXPERIMENTAL")
    check(mv.parse_paper_build(experimental, "26.2") is None,
          "experimental builds are never offered")
    check(mv.parse_paper_build({}, "26.2") is None, "empty doc -> None")


def test_paper_falls_back_past_alpha_versions():
    print("Paper version fallback:")
    import json as _json

    # The exact shape seen in the wild on 2026-09-16: Paper had published a
    # 26.3 key whose latest build was ALPHA, while the version the server ran
    # (26.2) had stable build 124 available.
    project = {"versions": {"26.3": ["26.3"], "26.2": ["26.2"],
                            "26.1": ["26.1.2"]}}
    alpha = {"id": 5, "channel": "ALPHA", "downloads": {"server:default": {
        "name": "paper-26.3-5.jar", "url": "https://x/paper-26.3-5.jar",
        "checksums": {"sha256": "a" * 64}, "size": 1}}}
    stable_262 = {"id": 124, "channel": "STABLE", "downloads": {
        "server:default": {"name": "paper-26.2-124.jar",
                           "url": "https://x/paper-26.2-124.jar",
                           "checksums": {"sha256": "b" * 64}, "size": 1}}}
    stable_261 = {"id": 99, "channel": "STABLE", "downloads": {
        "server:default": {"name": "paper-26.1-99.jar",
                           "url": "https://x/paper-26.1-99.jar",
                           "checksums": {"sha256": "c" * 64}, "size": 1}}}

    routes = {mv.PAPER_PROJECT: project,
              mv.PAPER_PROJECT + "/versions/26.3/builds/latest": alpha,
              mv.PAPER_PROJECT + "/versions/26.2/builds/latest": stable_262,
              mv.PAPER_PROJECT + "/versions/26.1/builds/latest": stable_261}
    fetched = []

    real = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: (
        fetched.append(req.full_url),
        _FakeResponse(_json.dumps(routes[req.full_url]).encode()))[1]
    try:
        rel = mv.latest_paper(not_older_than="26.2")
        check(rel is not None, "a release is found despite 26.3 being ALPHA")
        check(rel.mc_version == "26.2" and rel.build == 124,
              f"falls back to the newest STABLE build ({rel and rel.describe()}) "
              "-- previously this returned None and the server was told it "
              "was up to date while build 124 was waiting")
        check(any("26.3" in u for u in fetched),
              "it did look at 26.3 first")

        # The floor stops the walk before it can offer a downgrade.
        fetched.clear()
        rel = mv.latest_paper(not_older_than="26.3")
        check(rel is None,
              "with 26.3 installed and only an ALPHA available, nothing is "
              "offered -- it does NOT fall back to 26.2 and propose a "
              "downgrade")
        check(not any("26.1" in u for u in fetched),
              "and it stops walking rather than scanning every old version")

        # Pinning still queries exactly one version.
        fetched.clear()
        rel = mv.latest_paper("26.1")
        check(rel is not None and rel.build == 99, "pinned lookup unaffected")
        check(len(fetched) == 1, "a pinned lookup makes exactly one request")
    finally:
        urllib.request.urlopen = real


def test_vanilla():
    print("Vanilla parsing:")
    found = mv.parse_vanilla_manifest(MOJANG_MANIFEST)
    check(found is not None and found[0] == "26.2",
          "latest.release picked, not the snapshot")
    check(found[1].endswith("26.2.json"), "metadata url resolved")

    rel = mv.parse_vanilla_version(MOJANG_VERSION, "26.2")
    check(rel is not None and rel.sha1.startswith("823e2250"),
          "sha1 captured (Mojang publishes sha1, not sha256)")
    check(rel.size == 60894273, "size captured")
    check(rel.build is None, "vanilla has no build number")
    check(mv.parse_vanilla_manifest({}) is None, "empty manifest -> None")


def test_bedrock():
    print("Bedrock parsing:")
    rel = mv.parse_bedrock_links(BEDROCK_LINKS)
    check(rel is not None and rel.mc_version == "1.26.45.1",
          f"version parsed out of the filename ({rel.mc_version})")
    check("bin-linux/" in rel.url, "picked Linux, not Windows")
    check("bin-win" not in rel.url, "Windows build rejected")
    check("preview" not in rel.url, "picked stable, not the Preview channel")
    # Three near-misses sit alongside it in the same response; the match is on
    # exact downloadType, so none of them can be picked up by accident.
    only_preview = {"result": {"links": [
        l for l in BEDROCK_LINKS["result"]["links"]
        if l["downloadType"] != "serverBedrockLinux"]}}
    check(mv.parse_bedrock_links(only_preview) is None,
          "with no serverBedrockLinux present it returns None rather than "
          "falling back to Windows or Preview")
    check(rel.sha256 is None and rel.sha1 is None,
          "no checksum published -> none claimed")
    check(rel.edition == "bedrock", "reported as a bedrock artifact")
    check(mv.parse_bedrock_links({}) is None, "empty doc -> None")


def test_version_ordering():
    print("version ordering:")
    check(mv.version_tuple("26.10") > mv.version_tuple("26.9"),
          "26.10 > 26.9 (numeric, not lexical)")
    check(mv.version_tuple("1.21.4") > mv.version_tuple("1.21"),
          "1.21.4 > 1.21")
    check(mv.is_prerelease("26.2-rc-2") and mv.is_prerelease("1.21-pre1"),
          "rc/pre detected")
    check(not mv.is_prerelease("26.2"), "plain release is not a prerelease")


class _FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _with_fake_urlopen(payload, fn):
    real = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _FakeResponse(payload)
    try:
        return fn()
    finally:
        urllib.request.urlopen = real


def test_download_verification():
    print("download_file verification:")
    body = b"minecraft server bytes" * 1000
    good = hashlib.sha256(body).hexdigest()

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "server.jar"

        _with_fake_urlopen(body, lambda: download_file(
            "https://x/server.jar", dest, sha256=good, retries=1))
        check(dest.exists() and dest.read_bytes() == body,
              "good download lands at its final name")
        check(not (dest.parent / "server.jar.part").exists(),
              ".part cleaned up on success")

        bad = Path(td) / "bad.jar"
        try:
            _with_fake_urlopen(body, lambda: download_file(
                "https://x/bad.jar", bad, sha256="0" * 64, retries=1))
            check(False, "checksum mismatch should raise")
        except DownloadError as e:
            check("sha256 mismatch" in str(e), f"raises on bad checksum")
        check(not bad.exists(),
              "NO file left behind on checksum mismatch (cannot be mistaken "
              "for a good download)")
        check(not (bad.parent / "bad.jar.part").exists(), ".part removed too")

        sized = Path(td) / "sized.jar"
        try:
            _with_fake_urlopen(body, lambda: download_file(
                "https://x/s.jar", sized, expected_size=999, retries=1))
            check(False, "size mismatch should raise")
        except DownloadError as e:
            check("size mismatch" in str(e), "raises on wrong size")
        check(not sized.exists(), "no file left behind on size mismatch")

        # Bedrock publishes no checksum: size-only must still work.
        plain = Path(td) / "bds.zip"
        _with_fake_urlopen(body, lambda: download_file(
            "https://x/bds.zip", plain, retries=1))
        check(plain.exists(), "download with no checksum still works")


def test_fetch_json():
    print("fetch_json:")
    out = _with_fake_urlopen(b'{"a": 1}',
                             lambda: fetch_json("https://x/j"))
    check(out == {"a": 1}, "parses JSON")
    try:
        _with_fake_urlopen(b'not json', lambda: fetch_json("https://x/j"))
        check(False, "malformed JSON should raise DownloadError")
    except DownloadError:
        check(True, "malformed JSON -> DownloadError (not a bare ValueError)")


def test_prune():
    print("cache retention:")
    with tempfile.TemporaryDirectory() as td:
        real = mv.CACHE_DIR
        mv.CACHE_DIR = Path(td)
        try:
            d = mv.cache_dir("java")
            for n in ("paper-26.2-121.jar", "paper-26.2-122.jar",
                      "paper-26.2-123.jar"):
                (d / n).write_text("x")
            removed = mv.prune_cache(
                "java", {"paper-26.2-123.jar", "paper-26.2-122.jar"})
            check(removed == ["paper-26.2-121.jar"],
                  f"prunes only the superseded build ({removed})")
            check((d / "paper-26.2-123.jar").exists()
                  and (d / "paper-26.2-122.jar").exists(),
                  "latest + currently-installed both kept")
        finally:
            mv.CACHE_DIR = real




# ==========================================================================
# Bedrock install: the parts that can destroy a server
# ==========================================================================
import os
import stat
import types
import json as _json
import zipfile as _zf

import core.updates as up
from utils.restore_core import swap_in_staging


# Pack UUIDs are stable across releases; the directory NAME is not. These
# stand in for the real ones, which is all the diff cares about.
UUID_VANILLA_BEH = "11111111-1111-4111-8111-111111111111"
UUID_VANILLA_RES = "22222222-2222-4222-8222-222222222222"
UUID_CHEMISTRY = "33333333-3333-4333-8333-333333333333"


def _manifest(uuid: str, name: str = "pack") -> str:
    return _json.dumps({"format_version": 2,
                        "header": {"uuid": uuid, "version": [1, 0, 0],
                                   "name": name}})


def _make_bds_zip(path: Path, *, with_worlds=False, exec_bit=True, extra=(),
                  version="1.26.51"):
    """A miniature BDS release zip, shaped like the real one.

    Note the version-stamped pack directory names: this is what BDS actually
    ships (``behavior_packs/vanilla_1.26.51``), and getting it wrong in this
    fixture is what let a name-only pack diff look correct.
    """
    with _zf.ZipFile(path, "w", _zf.ZIP_DEFLATED) as z:
        info = _zf.ZipInfo("bedrock_server")
        # external_attr is where the Unix mode lives; 0o755 << 16 is what a
        # real BDS zip carries for the binary.
        info.external_attr = (0o755 << 16) if exec_bit else (0o644 << 16)
        z.writestr(info, "#!binary\n")
        z.writestr("server.properties", "level-name=DEFAULT\ngamemode=survival\n")
        z.writestr("permissions.json", "[]")
        z.writestr("allowlist.json", "[]")
        # The real zip ships the whole historical chain, every entry under the
        # SAME uuid -- not just the current version. Getting this wrong in the
        # fixture is what made a name-only diff look correct.
        for old_version in ("1.26.30", "1.26.40"):
            z.writestr(f"behavior_packs/vanilla_{old_version}/manifest.json",
                       _manifest(UUID_VANILLA_BEH, old_version))
        z.writestr(f"behavior_packs/vanilla_{version}/manifest.json",
                   _manifest(UUID_VANILLA_BEH, version))
        z.writestr("behavior_packs/chemistry/manifest.json",
                   _manifest(UUID_CHEMISTRY))
        z.writestr(f"resource_packs/vanilla_{version}/manifest.json",
                   _manifest(UUID_VANILLA_RES, version))
        if with_worlds:
            z.writestr("worlds/Bedrock level/level.dat", "NEW")
        for name in extra:
            z.writestr(name, "x")
    return path


def _make_install(root: Path) -> Path:
    """An existing Bedrock server dir with config, world and custom packs."""
    mc = root / "server"
    (mc / "worlds" / "Bedrock level").mkdir(parents=True)
    (mc / "worlds" / "Bedrock level" / "level.dat").write_text("MYWORLD")
    (mc / "worlds" / "Bedrock level" / "world_behavior_packs.json").write_text("[]")
    (mc / "server.properties").write_text("level-name=Bedrock level\nallow-list=true\n")
    (mc / "permissions.json").write_text('[{"permission":"operator"}]')
    (mc / "allowlist.json").write_text('[{"name":"Kamion"}]')
    (mc / "console.log").write_text("history")
    (mc / ".diamondsign_chain").write_text("abcd1234")
    # What 1.26.45 left behind: its own version-stamped packs, plus the
    # operator's. A real install looks exactly like this.
    for pack, uuid in (("vanilla_1.26.30", UUID_VANILLA_BEH),
                       ("vanilla_1.26.40", UUID_VANILLA_BEH),
                       ("vanilla_1.26.45", UUID_VANILLA_BEH),
                       ("chemistry", UUID_CHEMISTRY),
                       ("diamondsign_events", "44444444-4444-4444-8444-4444"),
                       ("my_custom_pack", "55555555-5555-4555-8555-5555")):
        (mc / "behavior_packs" / pack).mkdir(parents=True)
        (mc / "behavior_packs" / pack / "manifest.json").write_text(
            _manifest(uuid))
    (mc / "resource_packs" / "vanilla_1.26.45").mkdir(parents=True)
    (mc / "resource_packs" / "vanilla_1.26.45" / "manifest.json").write_text(
        _manifest(UUID_VANILLA_RES))
    (mc / "resource_packs" / "my_textures").mkdir(parents=True)
    # No manifest at all: unreadable identity must not mean "discardable".
    (mc / "resource_packs" / "my_textures" / "textures.json").write_text("mine")
    return mc


def test_extract_preserves_exec_bit():
    print("BDS extract:")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        z = _make_bds_zip(root / "bds.zip")
        staging = root / "staging"; staging.mkdir()
        up._extract_bds(z, staging, lambda m: None)
        binary = staging / "bedrock_server"
        check(binary.exists(), "binary extracted")
        if os.name == "posix":
            mode = binary.stat().st_mode
            check(bool(mode & stat.S_IXUSR),
                  "bedrock_server is EXECUTABLE (zipfile drops mode bits; a "
                  "naive extract would make the server unable to start)")
        else:
            check(os.access(binary, os.X_OK) or True,
                  "exec-bit check skipped on non-POSIX (chmod is a no-op)")
        check((staging / "server.properties").exists(),
              "release's default config extracted (to be overwritten by ours)")


def test_extract_rejects_bad_zips():
    print("BDS extract guards:")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        z = _make_bds_zip(root / "worlds.zip", with_worlds=True)
        try:
            up._extract_bds(z, root / "s1", lambda m: None)
            check(False, "a zip shipping worlds/ should be refused")
        except up.UpdateError as e:
            check("worlds/" in str(e), "refuses a release that ships worlds/")

        with _zf.ZipFile(root / "nobin.zip", "w") as zz:
            zz.writestr("server.properties", "x")
        try:
            up._extract_bds(root / "nobin.zip", root / "s2", lambda m: None)
            check(False, "a zip with no binary should be refused")
        except up.UpdateError as e:
            check("bedrock_server" in str(e), "refuses a zip with no binary")

        # Defence in depth: the WINDOWS build ships bedrock_server.exe, so even
        # if the wrong download were ever selected it is refused here -- before
        # the server is stopped, not after.
        with _zf.ZipFile(root / "win.zip", "w") as zz:
            zz.writestr("bedrock_server.exe", "MZ")
            zz.writestr("server.properties", "x")
        try:
            up._extract_bds(root / "win.zip", root / "s3", lambda m: None)
            check(False, "a Windows zip should be refused")
        except up.UpdateError as e:
            check("bedrock_server" in str(e),
                  "a Windows build (bedrock_server.exe) is refused before "
                  "anything is stopped")


def test_custom_pack_diff():
    print("custom pack detection:")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mc = _make_install(root)
        staging = root / "staging"; staging.mkdir()
        _make_bds_zip(root / "bds.zip")
        up._extract_bds(root / "bds.zip", staging, lambda m: None)

        said = []
        names = up.custom_pack_names(mc, staging, said.append)
        check("behavior_packs/diamondsign_events" in names,
              "this bot's own pack is carried across")
        check("behavior_packs/my_custom_pack" in names,
              "an operator's own pack is carried across too")
        check("resource_packs/my_textures" in names,
              "a pack with no readable manifest is kept, not discarded")
        check("behavior_packs/chemistry" not in names,
              "a pack the release ships under the SAME name is not carried")
        check("behavior_packs/vanilla_1.26.40" not in names,
              "a historical pack the release still ships is left alone")
        check("behavior_packs/vanilla_1.26.45" not in names
              and "resource_packs/vanilla_1.26.45" not in names,
              "a pack the release DROPPED from its chain is not carried "
              "across as if it were the operator's -- it shares a uuid with "
              "the chain 1.26.51 does ship, and carrying it kept the outgoing "
              "release's own pack for good, under the operator's name")
        check(said and "vanilla_1.26.45" in said[0],
              f"and it says which packs the release replaced ({said})")


def test_bedrock_swap_end_to_end():
    print("Bedrock swap end-to-end:")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mc = _make_install(root)
        staging = root / "server.update-staging"; staging.mkdir()
        _make_bds_zip(root / "bds.zip", extra=("definitions/new_thing.json",))
        up._extract_bds(root / "bds.zip", staging, lambda m: None)

        preserve = set(up._BEDROCK_PRESERVE) | up.custom_pack_names(mc, staging)
        swap_in_staging(staging, mc, preserve, lambda m: None)

        check((mc / "server.properties").read_text().startswith("level-name=Bedrock level"),
              "OPERATOR's server.properties survived (not the zip's default)")
        check("Kamion" in (mc / "allowlist.json").read_text(),
              "allowlist survived")
        check("operator" in (mc / "permissions.json").read_text(),
              "permissions survived")
        check((mc / "worlds" / "Bedrock level" / "level.dat").read_text() == "MYWORLD",
              "world survived untouched")
        check(not (mc / ".diamondsign_chain").exists(),
              "backup-chain marker deliberately NOT carried across (the old "
              "chain describes the previous version; the post-update backup "
              "re-bases it)")
        check((mc / "console.log").read_text() == "history",
              "console.log survived")
        check((mc / "behavior_packs" / "diamondsign_events").is_dir(),
              "diamondsign behavior pack survived")
        check((mc / "behavior_packs" / "my_custom_pack").is_dir(),
              "operator's custom pack survived")
        check((mc / "resource_packs" / "my_textures").is_dir(),
              "manifest-less operator pack survived")
        check((mc / "behavior_packs" / "vanilla_1.26.51").is_dir()
              and (mc / "resource_packs" / "vanilla_1.26.51").is_dir(),
              "the new release's vanilla packs are installed")
        check(not (mc / "behavior_packs" / "vanilla_1.26.45").exists()
              and not (mc / "resource_packs" / "vanilla_1.26.45").exists(),
              "and the one the release dropped is GONE rather than being "
              "kept for good as though the operator had installed it")
        check((mc / "definitions" / "new_thing.json").exists(),
              "new release's files are present")
        check((mc / "bedrock_server").read_text() == "#!binary\n",
              "new binary installed")


def test_chain_is_rebased_not_preserved():
    print("backup chain after an update:")
    check(".diamondsign_chain" not in up._BEDROCK_PRESERVE,
          "chain marker is not in the Bedrock preserve set")

    calls = []
    with tempfile.TemporaryDirectory() as td:
        mc = Path(td) / "srv"; mc.mkdir()
        marker = mc / ".diamondsign_chain"
        marker.write_text("oldchain")
        server = types.SimpleNamespace(
            chain_marker_path=marker,
            log=types.SimpleNamespace(warning=lambda *a: None,
                                      exception=lambda *a: None),
            save_manifest=lambda files, chain_id="", base_full="":
                calls.append(("manifest", chain_id)),
            run_backup=lambda status_cb=None: calls.append(("backup", None)))

        up._invalidate_chain(server)
        check(not marker.exists(),
              "Java's surviving marker is removed too (only the jar changed, "
              "so the swap would otherwise leave a stale chain looking valid)")
        check(("manifest", "") in calls, "manifest cleared to an empty chain")

        up._rebase_backup_chain(server, lambda m: None)
        check(("backup", None) in calls,
              "a full backup runs after the update, re-basing the chain on "
              "the updated server")

    # A failed post-update backup must not look like a failed update.
    said = []
    boom = types.SimpleNamespace(
        log=types.SimpleNamespace(exception=lambda *a: None),
        run_backup=lambda status_cb=None: (_ for _ in ()).throw(
            RuntimeError("disk full")))
    up._rebase_backup_chain(boom, said.append)
    check(any("update itself succeeded" in m for m in said),
          "a failed post-update backup reports the update as still successful")
    check(any("/backup" in m for m in said),
          "and tells the operator how to re-establish the chain")


def test_refuses_while_players_online():
    print("empty-server gate:")
    import core.updates as upd
    real = upd.reconcile_online
    try:
        upd.reconcile_online = lambda srv, reason=None: {"Kamion", "erny1618"}
        try:
            upd.require_empty_server(object())
            check(False, "should refuse while players are online")
        except upd.UpdateError as e:
            msg = str(e)
            check("2 player(s) online" in msg, "reports how many are online")
            check("Kamion" in msg and "erny1618" in msg, "names them")
            check("disconnects everyone" in msg, "explains why it refuses")

        # A failed query is not proof the server is empty.
        upd.reconcile_online = lambda srv, reason=None: None
        try:
            upd.require_empty_server(object())
            check(False, "an unconfirmed query should refuse, not proceed")
        except upd.UpdateError as e:
            check("could not confirm" in str(e),
                  "unknown online state refuses rather than assuming empty")

        upd.reconcile_online = lambda srv, reason=None: set()
        upd.require_empty_server(object())
        check(True, "proceeds when the server is empty")
    finally:
        upd.reconcile_online = real


def test_pre_update_backup_is_conditional():
    print("pre-update backup:")
    import core.updates as upd

    def server(chain, marker, incr=True):
        return types.SimpleNamespace(
            load_manifest=lambda: (chain, "srv_full.zip", {}),
            read_chain_marker=lambda: marker,
            config=types.SimpleNamespace(incremental_enabled=incr))

    said = []
    check(upd.has_rollback_point(server("abcd", "abcd"), said.append),
          "valid chain + incrementals -> reuse it, skip the full backup")
    check(any("as the rollback point" in m for m in said),
          "says why it is skipping")

    said = []
    check(not upd.has_rollback_point(server("abcd", "wxyz"), said.append),
          "marker mismatch -> take a full backup")
    check(any("invalid" in m for m in said), "says the chain is invalid")

    said = []
    check(not upd.has_rollback_point(server("", ""), said.append),
          "no chain at all -> take a full backup")

    said = []
    check(not upd.has_rollback_point(server("abcd", "abcd", incr=False),
                                     said.append),
          "valid chain but incrementals DISABLED -> still take a full backup "
          "(the chain is only as fresh as the last scheduled full)")
    check(any("may be days old" in m for m in said),
          "explains the staleness risk")


def test_paper_startup_does_not_record_vanilla():
    print("Paper startup banner order:")
    from core.server import Server
    from core.logparse import parse_version_line

    with tempfile.TemporaryDirectory() as td:
        vp = Path(td) / "v.json"
        logged = []
        srv = types.SimpleNamespace(
            version_path=vp,
            log=types.SimpleNamespace(info=lambda f, *a: logged.append(f % a),
                                      warning=lambda *a: None,
                                      exception=lambda *a: None))
        for m in ("load_installed_version", "save_installed_version",
                  "record_observed_version"):
            setattr(srv, m, types.MethodType(getattr(Server, m), srv))

        srv.save_installed_version("paper", "26.2", 124)

        # Paper's REAL startup order: the generic vanilla line first, its own
        # banner second. The tailer sees one line at a time and cannot look
        # ahead the way the startup backfill does.
        srv.record_observed_version(parse_version_line(
            "[11:51:52] [Server thread/INFO]: Starting minecraft server "
            "version 26.2"))
        mid = srv.load_installed_version()
        check(mid["source"] == "paper" and mid["build"] == 124,
              "the generic line does NOT downgrade the record to vanilla -- "
              "a check landing in that window would have polled Mojang and "
              "offered a jar that replaces Paper")
        check(not logged, f"and logs nothing spurious ({logged})")

        srv.record_observed_version(parse_version_line(
            "[11:51:52] [Server thread/INFO]: This server is running Paper "
            "version 26.2-124-main@abc (x)"))
        check(srv.load_installed_version()["build"] == 124,
              "the Paper banner that follows is a no-op (unchanged)")

        # A genuine bump still records, and names both builds.
        logged.clear()
        srv.record_observed_version(parse_version_line(
            "[x] [Server thread/INFO]: This server is running Paper version "
            "26.2-125-main@abc (y)"))
        changed = [l for l in logged if "changed" in l]
        check(changed and "26.2 build 124 -> 26.2 build 125" in changed[0],
              f"a build-only bump names the builds, not '26.2 -> 26.2' "
              f"({changed and changed[0][:60]})")

        # A real flavour switch (different version) is still believed.
        srv.record_observed_version(parse_version_line(
            "[x] [Server thread/INFO]: Starting minecraft server version 26.3"))
        check(srv.load_installed_version()["mc_version"] == "26.3",
              "a build-less observation of a DIFFERENT version is still taken")


def test_missing_banner_message_matches_reality():
    print("missing-banner message:")
    import core.updates as upd
    lines = []
    real = upd.logger.info
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "latest.log"
        log.write_text("no banner in here" + chr(10), encoding="utf-8")

        def run(record):
            lines.clear()
            srv = types.SimpleNamespace(
                config=types.SimpleNamespace(edition="java", log_path=log,
                                             name="world",
                                             updates_enabled=True),
                load_installed_version=lambda: record,
                record_observed_version=lambda p: None)
            upd.logger.info = lambda f, *a: lines.append(f % a)
            try:
                upd.recover_server_version(srv)
            finally:
                upd.logger.info = real
            return lines[0]

        msg = run({"mc_version": "26.2", "build": 124})
        check("unknown" not in msg,
              "with a record already on file it does NOT claim the version "
              "is unknown -- that contradicted the '(installed: 26.2)' the "
              "very next check printed")
        check("rotated" in msg, f"it explains the real reason: {msg[-46:]}")

        msg = run({})
        check("unknown" in msg,
              "with genuinely no record it still says so")


def test_version_detection_from_logs():
    print("version detection from real log lines:")
    from core.logparse import parse_bedrock_version_line, parse_version_line
    import core.updates as upd

    # Lines taken verbatim from the operator's own servers.
    paper = parse_version_line(
        "[12:38:49 INFO]: This server is running Paper version "
        "26.2-121-main@a2a42c5 (2026-08-29T11:32:25Z) (Implementing API "
        "version 26.2.build.121-stable)")
    check(paper == {"source": "paper", "software": "Paper",
                    "mc_version": "26.2", "build": 121},
          f"real Paper banner -> {paper}")
    bds = parse_bedrock_version_line(
        "[2026-09-15 12:37:33:754 INFO] Version: 1.26.45.1")
    check(bds and bds["mc_version"] == "1.26.45.1",
          "real BDS banner parsed")

    # Neighbouring BDS lines must not be mistaken for the banner.
    for other in ("[2026-09-15 12:37:33:754 INFO] Build ID: 49559497",
                  "[2026-09-15 12:37:33:754 INFO] Session ID: 62fce78a-2226",
                  "[2026-09-15 16:20 INFO] Kamion: my Version: 9.9.9"):
        check(parse_bedrock_version_line(other) is None,
              "not mistaken for the banner: " + other.split("] ")[-1][:28])

    lf = chr(10)
    with tempfile.TemporaryDirectory() as td:
        # Java: latest.log is recreated per run, and the VANILLA line comes
        # first even on Paper -- first-match-wins would mislabel it.
        jlog = Path(td) / "latest.log"
        jlog.write_text(lf.join([
            "[12:38:49 INFO]: Starting minecraft server version 26.2",
            "[12:38:49 INFO]: This server is running Paper version "
            "26.2-121-main@a2a42c5 (2026-08-29T11:32:25Z)",
            ""]), encoding="utf-8")
        srv = types.SimpleNamespace(
            config=types.SimpleNamespace(edition="java", log_path=jlog,
                                         name="XPS-Java", updates_enabled=True),
            record_observed_version=lambda p: None)
        got = upd.recover_server_version(srv)
        check(got and got["source"] == "paper" and got["build"] == 121,
              "Java backfill prefers the Paper banner over the vanilla line "
              "printed before it")

        # Bedrock: console.log is appended forever, so the LAST banner wins.
        blog = Path(td) / "console.log"
        blog.write_text(lf.join([
            "[2026-08-01 10:00:00:002 INFO] Version: 1.26.44.1",
            "[2026-08-01 10:05:00:000 INFO] Player connected: K, xuid: 1",
            "[2026-09-15 12:37:33:754 INFO] Version: 1.26.45.1",
            "[2026-09-15 12:39:09:506 INFO] There are 0/10 players online:",
            ""]), encoding="utf-8")
        srv2 = types.SimpleNamespace(
            config=types.SimpleNamespace(edition="bedrock", log_path=blog,
                                         name="XPS-Bedrock", updates_enabled=True),
            record_observed_version=lambda p: None)
        got2 = upd.recover_server_version(srv2)
        check(got2 and got2["mc_version"] == "1.26.45.1",
              "Bedrock backfill takes the most recent run's banner, not the "
              "first one in an append-only log")


def test_up_to_date_bedrock_is_quiet():
    print("an up-to-date Bedrock server:")
    import core.updates as upd
    rel = mv.parse_bedrock_links(BEDROCK_LINKS)          # 1.26.45.1
    server = types.SimpleNamespace(
        config=types.SimpleNamespace(updates_enabled=True, edition="bedrock",
                                     name="Square-Friends"),
        load_installed_version=lambda: {
            "source": "bedrock", "mc_version": "1.26.45.1", "build": None})
    real = mv.latest_bedrock
    try:
        mv.latest_bedrock = lambda: rel
        check(upd.available_update(server) is None,
              "a server already on the latest version reports nothing "
              "(this was the false alarm seen in production)")
    finally:
        mv.latest_bedrock = real


def test_backup_status_is_not_logged_twice():
    print("post-update backup logging:")
    import core.updates as upd

    logged, chatted, status_cbs = [], [], []
    srv = types.SimpleNamespace(
        log=types.SimpleNamespace(info=lambda fmt, m: logged.append(m),
                                  exception=lambda *a: None),
        run_backup=lambda status_cb=None: status_cbs.append(status_cb))

    # say() logs and forwards; chat() only forwards. run_backup logs every
    # status line itself, so it must be handed the chat-only one.
    def chat(m):
        chatted.append(m)

    def say(m):
        logged.append(m)
        chat(m)

    upd._rebase_backup_chain(srv, say, chat)
    check(status_cbs and status_cbs[0] is chat,
          "run_backup is given the chat-only callback, not the logging "
          "wrapper (which produced a 'Backup: X' AND an 'Update: X' line "
          "for every single step)")
    check(any("post-update backup" in m for m in logged),
          "the update's own message is still logged")


def test_rollback_phrasing_reads_as_a_sentence():
    print("rollback-point phrasing:")
    import core.updates as upd
    srv = types.SimpleNamespace(
        load_manifest=lambda: ("08d63f2b", "mc-bedrock_20260817.zip", {}),
        read_chain_marker=lambda: "08d63f2b",
        config=types.SimpleNamespace(incremental_enabled=True))

    said = []
    upd.has_rollback_point(srv, said.append)
    check("chain 08d63f2b is valid" not in said[0],
          "not 'the existing backup chain X IS VALID ... as the rollback "
          "point' -- the detail is a noun phrase both callers can slot in")
    check("the existing backup chain 08d63f2b" in said[0]
          and "as the rollback point" in said[0],
          f"reads properly: {said[0][:70]}...")
    plan = upd.backup_plan(srv)
    check("Rollback point: the existing backup chain 08d63f2b" in plan,
          "and the confirm prompt reads properly too")


def test_first_check_is_prompt_and_staggered():
    print("first version check timing:")
    import bot as botmod

    check(botmod._UPDATE_FIRST_CHECK_DELAY <= 5,
          f"first check within a few seconds of start "
          f"({botmod._UPDATE_FIRST_CHECK_DELAY}s) -- it used to be a flat 60s, "
          "so a pending update went unmentioned for a minute after a restart")
    check(botmod._UPDATE_CHECK_STAGGER > 0,
          "servers are still staggered, not fired simultaneously")

    # Each thread takes the next slot, so N servers spread out rather than
    # all waiting the same amount.
    slots = [next(botmod._update_check_slot) for _ in range(4)]
    check(slots == sorted(set(slots)) and len(set(slots)) == 4,
          f"each server gets a distinct, increasing slot ({slots})")

    delays = [botmod._UPDATE_FIRST_CHECK_DELAY
              + botmod._UPDATE_CHECK_STAGGER * i for i in range(4)]
    check(max(delays) < 15,
          f"even the last of four servers checks within {max(delays)}s")
    check(botmod._UPDATE_CHECK_INTERVAL == 6 * 60 * 60,
          "the steady-state interval is unchanged")


def test_restore_downgrade_is_noticed():
    print("a restore that downgrades the binary:")
    from core.server import Server
    from core.logparse import parse_bedrock_version_line
    import core.updates as upd

    # A Bedrock backup contains the WHOLE server directory, bedrock_server
    # included, so restoring an older one downgrades the server. Real numbers
    # from the run: /update_server installed 1.26.45.1, then a restore to an
    # August backup brought back 1.26.40.8.
    banner = "[2026-09-15 19:49:54:793 INFO] Version: 1.26.40.8"

    with tempfile.TemporaryDirectory() as td:
        vp = Path(td) / "installed_version.json"
        log = Path(td) / "console.log"
        log.write_text(banner + chr(10), encoding="utf-8")
        srv = types.SimpleNamespace(
            version_path=vp,
            config=types.SimpleNamespace(edition="bedrock", log_path=log,
                                         name="XPS-Bedrock",
                                         updates_enabled=True),
            log=types.SimpleNamespace(info=lambda *a: None,
                                      warning=lambda *a: None,
                                      exception=lambda *a: None))
        for m in ("load_installed_version", "save_installed_version",
                  "record_observed_version", "forget_installed_version"):
            setattr(srv, m, types.MethodType(getattr(Server, m), srv))

        srv.save_installed_version("bedrock", "1.26.45.1", None)
        check(srv.load_installed_version()["observed"] is False,
              "/update_server's record starts out authoritative")

        srv.record_observed_version(parse_bedrock_version_line(banner))
        check(srv.load_installed_version()["mc_version"] == "1.26.40.8",
              "the running server's banner CORRECTS the authoritative record "
              "-- it was only authoritative until something else changed the "
              "binary, and a restore does exactly that")

        # Same answer via the startup backfill, for a bot restarted later.
        srv.save_installed_version("bedrock", "1.26.45.1", None)
        upd.recover_server_version(srv)
        check(srv.load_installed_version()["mc_version"] == "1.26.40.8",
              "the startup backfill corrects it too")

        # And the downgrade is then reported as an available update.
        rel = mv.parse_bedrock_links(BEDROCK_LINKS)      # 1.26.45.1
        check(not rel.same_build_as(srv.load_installed_version()),
              "so the newer release is seen as available again")

        # forget_installed_version leaves it honestly unknown.
        srv.forget_installed_version()
        check(srv.load_installed_version() == {},
              "a restore can drop the record outright")


def test_chain_rebased_after_a_recovered_relaunch():
    print("chain re-base after an unconfirmed relaunch:")
    import core.updates as upd

    order = []
    rel = mv.parse_bedrock_links(BEDROCK_LINKS)
    attempts = {"n": 0}

    def relaunch(say):
        # First call fails to CONFIRM (the server may well be up); the
        # finally-block retry succeeds. This is exactly what happened when
        # the log watcher was bound to the swapped-away directory.
        attempts["n"] += 1
        order.append(f"relaunch{attempts['n']}")
        return attempts["n"] > 1

    backend = types.SimpleNamespace(
        probe_stopped=lambda timeout=10: True, is_online=lambda: False,
        broadcast=lambda m: None, stop_server=lambda say: None,
        wait_until_stopped=lambda timeout=120: True,
        force_stop=lambda say: True, relaunch=relaunch)

    server = types.SimpleNamespace(
        config=types.SimpleNamespace(
            edition="bedrock", name="XPS-Bedrock", restore_warning_seconds=0,
            minecraft_dir=Path("/srv/b"), backup_dir=Path("/srv/bk"),
            mux_start_cmd="x", incremental_enabled=True),
        backend=backend,
        load_installed_version=lambda: {"source": "bedrock",
                                        "mc_version": "1.26.44.1",
                                        "build": None},
        load_manifest=lambda: ("abcd", "base.zip", {}),
        read_chain_marker=lambda: "abcd",
        save_installed_version=lambda *a, **k: None,
        save_manifest=lambda *a, **k: order.append("invalidate"),
        chain_marker_path=Path("/nonexistent/.diamondsign_chain"),
        run_backup=lambda status_cb=None, offline=False: None,
        reattach_log_watch=lambda *a: None,
        _prepare_relaunch_cwd=lambda staged: None,
        _discard_old_world=lambda p: None,
        log=types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                  exception=lambda *a: None))

    saved = (upd.preflight, upd.ensure_downloaded, upd._install_bedrock,
             upd.reconcile_online, upd._rebase_backup_chain)
    try:
        upd.preflight = lambda s, r: None
        upd.ensure_downloaded = lambda r, log=None: Path("/tmp/b.zip")
        upd._install_bedrock = lambda s, a, say: Path("/srv/old")
        upd.reconcile_online = lambda s, reason=None: set()
        upd._rebase_backup_chain = lambda s, say, chat=None: order.append("rebase")
        upd.update_server(server, rel, say=lambda m: None)
    finally:
        (upd.preflight, upd.ensure_downloaded, upd._install_bedrock,
         upd.reconcile_online, upd._rebase_backup_chain) = saved

    check("invalidate" in order, "the old chain is retired by the install")
    check(order.count("relaunch1") and order.count("relaunch2"),
          f"the first relaunch went unconfirmed and was retried ({order})")
    check("rebase" in order,
          "the chain is re-based once the retry succeeds -- otherwise the "
          "server returns on the new version with NO backup chain, waiting "
          "on a manual /backup nothing asked for")
    check(order.index("invalidate") < order.index("rebase"),
          "and in the right order")


def test_update_progress_is_logged():
    print("update progress reaches the bot log:")
    import core.updates as upd

    logged, chatted = [], []
    srv = types.SimpleNamespace(
        log=types.SimpleNamespace(info=lambda fmt, m: logged.append(m),
                                  exception=lambda *a: None),
        config=types.SimpleNamespace(edition="bedrock", name="XPS-Bedrock",
                                     restore_warning_seconds=0),
        backend=types.SimpleNamespace(probe_stopped=lambda timeout=10: True))

    real = upd.preflight
    try:
        upd.preflight = lambda s, r: (_ for _ in ()).throw(
            upd.UpdateError("staged swap unavailable"))
        upd.update_server(srv, types.SimpleNamespace(
            source="bedrock", mc_version="1", build=None, filename="f.zip"),
            say=chatted.append)
    finally:
        upd.preflight = real

    check(logged == chatted and logged,
          "every chat message is also written to the bot log (an update used "
          "to leave no trace there at all)")
    check("staged swap unavailable" in logged[0],
          "the reason is in the log, not only in the chat")


def test_missing_baseline_is_logged():
    print("missing version baseline is visible:")
    import core.updates as upd
    lines = []
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "console.log"
        log.write_text("nothing resembling a version banner" + chr(10),
                       encoding="utf-8")
        srv = types.SimpleNamespace(
            config=types.SimpleNamespace(edition="bedrock", log_path=log,
                                         name="Square-Friends",
                                         updates_enabled=True),
            load_installed_version=lambda: {},
            record_observed_version=lambda p: None)
        real = upd.logger.info
        try:
            upd.logger.info = lambda fmt, *a: lines.append(fmt % a)
            check(upd.recover_server_version(srv) is None, "finds nothing")
        finally:
            upd.logger.info = real
    check(any("installed version is unknown" in l for l in lines),
          "says so, rather than silently never checking for updates again")


def test_watcher_rebound_before_relaunch():
    print("log watcher rebinding order:")
    import core.updates as upd

    order = []
    rel = mv.parse_bedrock_links(BEDROCK_LINKS)

    backend = types.SimpleNamespace(
        probe_stopped=lambda timeout=10: True,      # already stopped
        is_online=lambda: False,
        broadcast=lambda m: None,
        stop_server=lambda say: None,
        wait_until_stopped=lambda timeout=120: True,
        force_stop=lambda say: True,
        relaunch=lambda say: (order.append("relaunch"), True)[1])

    server = types.SimpleNamespace(
        config=types.SimpleNamespace(
            edition="bedrock", name="XPS-Bedrock", restore_warning_seconds=0,
            minecraft_dir=Path("/srv/bedrock"), backup_dir=Path("/srv/backup"),
            mux_start_cmd="cd /srv/bedrock && ./bedrock_server",
            incremental_enabled=True),
        backend=backend,
        load_installed_version=lambda: {"source": "bedrock",
                                        "mc_version": "1.26.44.1",
                                        "build": None},
        load_manifest=lambda: ("abcd", "base.zip", {}),
        read_chain_marker=lambda: "abcd",
        save_installed_version=lambda *a, **k: None,
        save_manifest=lambda *a, **k: None,
        chain_marker_path=Path("/nonexistent/.diamondsign_chain"),
        run_backup=lambda status_cb=None, offline=False: None,
        reattach_log_watch=lambda *a: order.append("reattach"),
        _prepare_relaunch_cwd=lambda staged: None,
        _discard_old_world=lambda p: None,
        log=types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                  exception=lambda *a: None))

    real_pre, real_dl, real_inst = (upd.preflight, upd.ensure_downloaded,
                                    upd._install_bedrock)
    real_rec, real_rem = upd.reconcile_online, upd._rebase_backup_chain
    try:
        upd.preflight = lambda s, r: None
        upd.ensure_downloaded = lambda r, log=None: Path("/tmp/bds.zip")
        upd._install_bedrock = lambda s, a, say: (order.append("swap"),
                                                  Path("/srv/old"))[1]
        upd.reconcile_online = lambda s, reason=None: set()
        upd._rebase_backup_chain = lambda s, say, chat=None: None
        upd.update_server(server, rel, say=lambda m: None)
    finally:
        (upd.preflight, upd.ensure_downloaded, upd._install_bedrock) = (
            real_pre, real_dl, real_inst)
        upd.reconcile_online, upd._rebase_backup_chain = real_rec, real_rem

    check("swap" in order and "reattach" in order and "relaunch" in order,
          f"all three steps ran ({order})")
    check(order.index("reattach") < order.index("relaunch"),
          "the watcher is rebound BEFORE relaunch -- bound to the old, "
          "renamed-aside directory it would never see 'Server started.'")
    check(order.index("swap") < order.index("reattach"),
          "and after the swap, so it binds to the new directory")


def test_relaunch_does_not_retry_into_a_live_server():
    print("Bedrock relaunch retry guard:")
    from backends.bedrock import BedrockBackend

    sent, online = [], {"v": False}

    class Waiter:
        def wait(self, timeout=None):
            # Simulate the missed confirmation: the server really did start,
            # but the watcher never saw the line.
            online["v"] = True
            return False

    be = BedrockBackend.__new__(BedrockBackend)
    be.config = types.SimpleNamespace(mux_start_cmd="cd /srv && ./bedrock_server")
    be._watcher = types.SimpleNamespace(expect_line=lambda p: Waiter(),
                                        cancel=lambda w: None)
    be.send_command = lambda c: sent.append(c)
    be.is_online = lambda: online["v"]

    check(be.relaunch(lambda m: None) is True,
          "reports success once it confirms the server is actually up")
    check(len(sent) == 1,
          f"start command sent exactly ONCE ({len(sent)}x) -- a retry would "
          "type a shell line into a live game console ('Unknown command: cd')")


def test_backup_plan_matches_reality():
    print("confirm prompt describes what will actually happen:")
    import core.updates as upd

    def srv(chain, marker, incr=True):
        return types.SimpleNamespace(
            load_manifest=lambda: (chain, "XPS-Java_full.zip", {}),
            read_chain_marker=lambda: marker,
            config=types.SimpleNamespace(incremental_enabled=incr))

    plan = upd.backup_plan(srv("9513aca4", "9513aca4"))
    check("no pre-update backup needed" in plan,
          "a reusable chain is reported as such, not as 'a full backup runs "
          "first' (which stopped being true when it became conditional)")
    check("9513aca4" in plan, "names the chain it will rely on")

    for chain, marker, incr, why in (("", "", True, "no backup chain"),
                                     ("a", "b", True, "invalid"),
                                     ("a", "a", False, "incrementals are disabled")):
        plan = upd.backup_plan(srv(chain, marker, incr))
        check(plan.startswith("A full backup runs first"),
              f"full backup promised when {why}")
        check(why.split()[0] in plan or why in plan,
              "and the reason is given")

    # The pure check and the narrated one must never disagree.
    said = []
    for chain, marker in (("a", "a"), ("a", "b")):
        s = srv(chain, marker)
        pure, _ = upd.rollback_point_status(s)
        check(upd.has_rollback_point(s, said.append) is pure,
              "the narrated check agrees with the pure one")


def test_installed_version_always_names_the_build():
    print("installed-version wording:")
    import types as _t
    import bot as botmod
    import core.updates as upd

    check(mv.describe_version({"mc_version": "26.2", "build": 123})
          == "26.2 build 123", "a Paper record names its build")
    check(mv.describe_version({"mc_version": "1.26.45.1", "build": None})
          == "1.26.45.1", "a build-less record is just the version")
    check(mv.describe_version({}) == "unknown", "an empty record is 'unknown'")

    # The chat message and the log line must agree -- the log used to say
    # "installed: 26.2" for a server updating FROM build 123 TO 124, which
    # made the reason for the update invisible.
    rel = mv.parse_paper_build(PAPER_BUILD, "26.2")          # build 124
    server = _t.SimpleNamespace(
        config=_t.SimpleNamespace(name="world_han"),
        load_installed_version=lambda: {"source": "paper",
                                        "mc_version": "26.2", "build": 123})
    chat = upd.describe_update(server, rel)
    check("installed: 26.2 build 123" in chat,
          f"chat message names the installed build: {chat[:64]}")

    logged = []
    real_log, real_fetch, real_prune = (botmod.logger,
                                        botmod._fetch_shared_artifact,
                                        botmod.updates.mc_versions.prune_cache)
    try:
        botmod.logger = _t.SimpleNamespace(
            info=lambda fmt, *a: logged.append(fmt % a),
            warning=lambda *a: None)
        botmod._fetch_shared_artifact = lambda s, r: True
        botmod.updates.mc_versions.prune_cache = lambda e, keep: []
        botmod._announce_update(server, _t.SimpleNamespace(
            alert_admins=lambda m: None), rel)
    finally:
        (botmod.logger, botmod._fetch_shared_artifact,
         botmod.updates.mc_versions.prune_cache) = (real_log, real_fetch,
                                                    real_prune)
    check(any("installed: 26.2 build 123" in l for l in logged),
          f"and so does the log line: {logged and logged[-1][-52:]}")


def test_notification_goes_to_admin_dm_only():
    print("update notification routing:")
    import bot as botmod
    import types

    calls = {"announce": [], "admin": []}
    fake_bot = types.SimpleNamespace(
        announce=lambda srv, m: calls["announce"].append(m) or 1,
        alert_admins=lambda m: calls["admin"].append(m))
    rel = mv.parse_bedrock_links(BEDROCK_LINKS)
    server = types.SimpleNamespace(
        config=types.SimpleNamespace(name="Square-Friends", edition="bedrock"),
        load_installed_version=lambda: {"source": "bedrock",
                                        "mc_version": "1.26.44.1",
                                        "build": None,
                                        "filename": "old.zip"})

    real_fetch = botmod._fetch_shared_artifact
    real_prune = botmod.updates.mc_versions.prune_cache
    try:
        botmod._fetch_shared_artifact = lambda s, r: True
        botmod.updates.mc_versions.prune_cache = lambda e, keep: []
        botmod._announce_update(server, fake_bot, rel)
    finally:
        botmod._fetch_shared_artifact = real_fetch
        botmod.updates.mc_versions.prune_cache = real_prune

    check(calls["announce"] == [],
          "does NOT post to the server's group chats (/update_server is "
          "admin-and-DM-only, so players could not act on it anyway)")
    check(len(calls["admin"]) == 1, "sends exactly one admin alert")
    check("Square-Friends" in calls["admin"][0],
          "the alert names the server, since an admin DM is not scoped to one")
    check("/update_server" in calls["admin"][0], "tells the admin what to run")


def test_version_command_both_flavours():
    print("`version` command, both Java flavours:")
    from core.logparse import parse_version_command
    import core.updates as upd
    lf = chr(10)

    # Real vanilla output: a structured block, nothing like Paper's banner.
    vanilla = lf.join([
        "[17:28:36] [Server thread/INFO]: Server version info:",
        "[17:28:36] [Server thread/INFO]: id = 26.2",
        "[17:28:36] [Server thread/INFO]: name = 26.2",
        "[17:28:36] [Server thread/INFO]: data = 4903",
        "[17:28:36] [Server thread/INFO]: protocol = 776 (0x308)",
        "[17:28:36] [Server thread/INFO]: pack_resource = 88.0",
        "[17:28:36] [Server thread/INFO]: pack_data = 107.1",
        "[17:28:36] [Server thread/INFO]: stable = yes"])
    got = parse_version_command(vanilla)
    check(got and got["source"] == "vanilla" and got["mc_version"] == "26.2",
          "vanilla's structured block parsed")
    check(got.get("stable") is True, "stable flag read")
    check(got["build"] is None, "vanilla has no build number")

    # Real Paper output, whose trailing line names the PREVIOUS build.
    paper = lf.join([
        "Checking version, please wait...",
        "This server is running Paper version 26.2-121-main@a2a42c5 "
        "(2026-08-29T11:32:25Z) (Implementing API version 26.2.build.121-stable)",
        "You are 2 version(s) behind",
        "Download the new version at: https://papermc.io/downloads/paper",
        "Previous version: 26.2-117-27af5dd (MC: 26.2)"])
    got = parse_version_command(paper)
    check(got and got["source"] == "paper" and got["build"] == 121,
          "Paper's banner parsed")
    check(got["build"] != 117,
          "the 'Previous version:' line is NOT picked up (it would report "
          "the build the server used to run, making it look stale forever)")

    # The two are distinguishable, which is how the flavour is decided.
    check(parse_version_command(vanilla)["source"] !=
          parse_version_command(paper)["source"],
          "the two shapes identify the flavour")

    # Guard rails.
    check(parse_version_command("Unknown command: version.") is None,
          "an unexpected reply yields nothing rather than a bad parse")
    check(parse_version_command("chat: my id = 9.9.9") is None,
          "a stray 'id = ...' without the header is ignored")

    # End to end through the live query.
    for text, want in ((vanilla, "vanilla"), (paper, "paper")):
        srv = types.SimpleNamespace(
            config=types.SimpleNamespace(edition="java", name="t"),
            backend=types.SimpleNamespace(
                is_online=lambda: True,
                capture_command=lambda c, timeout=5, _t=text: _t),
            record_observed_version=lambda p: None)
        res = upd.refresh_installed_version(srv)
        check(res and res["source"] == want,
              f"live query identifies {want}")


def test_live_version_query():
    print("live `version` query (Paper):")
    import core.updates as upd
    lf = chr(10)
    out = lf.join([
        "Checking version, please wait...",
        "This server is running Paper version 26.2-121-main@a2a42c5 "
        "(2026-08-29T11:32:25Z) (Implementing API version 26.2.build.121-stable)",
        "You are 2 version(s) behind",
        "Download the new version at: https://papermc.io/downloads/paper",
        "Previous version: 26.2-117-27af5dd (MC: 26.2)"])
    rec = []
    srv = types.SimpleNamespace(
        config=types.SimpleNamespace(edition="java", name="XPS-Java"),
        backend=types.SimpleNamespace(is_online=lambda: True,
                                      capture_command=lambda c, timeout=5: out),
        record_observed_version=rec.append)
    got = upd.refresh_installed_version(srv)
    check(got and got["mc_version"] == "26.2" and got["build"] == 121,
          "picks the running build out of /version output")
    check(got["build"] != 117,
          "does NOT pick up the 'Previous version: 26.2-117-...' line "
          "printed underneath it")
    check(rec and rec[0] is got, "records what it found")

    # Vanilla has no such command.
    srv.backend.capture_command = lambda c, timeout=5: "Unknown command: version."
    check(upd.refresh_installed_version(srv) is None,
          "vanilla's 'Unknown command' yields nothing rather than a bad parse")

    # A stopped server is never queried.
    srv.backend.is_online = lambda: False
    check(upd.refresh_installed_version(srv) is None,
          "a stopped server is not queried")


def test_per_source_user_agent():
    print("per-source User-Agent:")
    from utils.download import BROWSER_USER_AGENT, USER_AGENT
    paper = mv.parse_paper_build(PAPER_BUILD, "26.2")
    bedrock = mv.parse_bedrock_links(BEDROCK_LINKS)
    vanilla = mv.parse_vanilla_version(MOJANG_VERSION, "26.2")
    check(paper.user_agent == USER_AGENT,
          "Paper gets the descriptive agent (it rejects generic ones)")
    check(vanilla.user_agent == USER_AGENT, "Mojang gets the descriptive agent")
    check(bedrock.user_agent == BROWSER_USER_AGENT,
          "Bedrock gets a browser agent (minecraft.net's CDN accepts the "
          "connection then never sends the body to anything else, so the "
          "download dies on a read timeout)")
    check(BROWSER_USER_AGENT != USER_AGENT, "the two are genuinely different")

    # The UA actually reaches the request.
    seen = {}
    real = urllib.request.urlopen

    def spy(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _FakeResponse(b"data")

    urllib.request.urlopen = spy
    try:
        with tempfile.TemporaryDirectory() as td:
            download_file("https://x/f.zip", Path(td) / "f.zip",
                          user_agent=BROWSER_USER_AGENT, retries=1)
    finally:
        urllib.request.urlopen = real
    check(seen.get("ua") == BROWSER_USER_AGENT,
          "download_file sends the agent it was given")


def test_no_baseline_stays_quiet():
    print("unknown installed version:")
    import core.updates as upd
    rel = mv.parse_bedrock_links(BEDROCK_LINKS)
    server = types.SimpleNamespace(
        config=types.SimpleNamespace(updates_enabled=True, edition="bedrock",
                                     name="Square-Friends"),
        load_installed_version=lambda: {})
    real = mv.latest_bedrock
    try:
        mv.latest_bedrock = lambda: rel
        check(upd.available_update(server) is None,
              "no baseline -> the background poll stays quiet instead of "
              "claiming an update (it cannot know if the server is behind)")
        check(upd.available_update(server, require_baseline=False) is rel,
              "...but /update_server still shows it, since installing it is what "
              "records a baseline in the first place")
        server.load_installed_version = lambda: {
            "source": "bedrock", "mc_version": "1.26.45.1", "build": None}
        check(upd.available_update(server) is None,
              "already on the latest version -> no update")
        server.load_installed_version = lambda: {
            "source": "bedrock", "mc_version": "1.26.44.1", "build": None}
        check(upd.available_update(server) is rel,
              "genuinely behind -> update reported")
    finally:
        mv.latest_bedrock = real


def test_java_flavor_is_not_guessed():
    print("Java flavour:")
    import core.updates as upd
    server = types.SimpleNamespace(
        config=types.SimpleNamespace(updates_enabled=True, edition="java",
                                     java_flavor="", name="XPS-Java",
                                     updates_pin_mc_version=False),
        load_installed_version=lambda: {})
    called = []
    real_v, real_p = mv.latest_vanilla, mv.latest_paper
    try:
        mv.latest_vanilla = lambda: called.append("vanilla")
        mv.latest_paper = lambda pin=None, **kw: called.append("paper")
        check(upd.available_update(server) is None,
              "unknown flavour -> no check at all")
        check(called == [],
              "does NOT default to vanilla (that would poll the wrong "
              "upstream and offer a jar that replaces Paper)")

        server.load_installed_version = lambda: {
            "source": "paper", "mc_version": "26.2", "build": 121}
        upd.available_update(server)
        check(called == ["paper"],
              "a recorded Paper banner routes the check to Paper")
    finally:
        mv.latest_vanilla, mv.latest_paper = real_v, real_p


def test_java_preflight_refuses_missing_jar():
    print("Java preflight:")
    with tempfile.TemporaryDirectory() as td:
        mc = Path(td) / "srv"; mc.mkdir()
        (mc / "paper-26.2-123.jar").write_text("old")
        server = types.SimpleNamespace(config=types.SimpleNamespace(
            edition="java", minecraft_dir=mc, backup_dir=Path(td) / "b",
            server_jar="server.jar"))
        rel = mv.parse_paper_build(PAPER_BUILD, "26.2")
        try:
            up.preflight(server, rel)
            check(False, "missing server.jar should refuse")
        except up.UpdateError as e:
            msg = str(e)
            check("server.jar" in msg and "paper-26.2-123.jar" in msg,
                  "refusal names both the expected jar and the one found")
            check("edition.server_jar" in msg,
                  "refusal tells the operator how to fix it")

        (mc / "server.jar").write_text("ok")
        try:
            up.preflight(server, rel)
            check(True, "passes once the expected jar exists")
        except up.UpdateError as e:
            check(False, f"should have passed: {e}")


def test_update_comparison():
    print("update comparison:")
    rel = mv.parse_paper_build(PAPER_BUILD, "26.2")
    check(rel.same_build_as({"source": "paper", "mc_version": "26.2",
                             "build": 123}),
          "identical build recognised as already installed")
    check(not rel.same_build_as({"source": "paper", "mc_version": "26.2",
                                 "build": 122}),
          "older build is an update")
    check(up.is_major(rel, {"mc_version": "26.1", "build": 99}),
          "different Minecraft version flagged as major")
    check(not up.is_major(rel, {"mc_version": "26.2", "build": 122}),
          "same Minecraft version is not major")
    check(not up.is_major(rel, {}),
          "unknown installed version is not claimed to be a major jump")


def test_cleanup_drops_every_set_aside_jar():
    print("Java rollback jars:")
    with tempfile.TemporaryDirectory() as td:
        mc = Path(td)
        (mc / "server.jar").write_text("build 124")
        olds = []
        for ts in ("20260101_010101", "20260202_020202", "20260303_030303"):
            p = mc / f"server.jar.pre-update-{ts}"
            p.write_text(ts)
            olds.append(p)
        kept = olds[-1]          # this update's outgoing jar

        said = []
        srv = types.SimpleNamespace(config=types.SimpleNamespace(name="world"))
        rel = types.SimpleNamespace(edition="java",
                                    filename="paper-26.2-124.jar")
        real = up.mc_versions.prune_cache
        up.mc_versions.prune_cache = lambda *a, **k: []
        try:
            up._cleanup(srv, rel, kept, said.append)
        finally:
            up.mc_versions.prune_cache = real

        check(not any(p.exists() for p in olds),
              "every set-aside jar is gone once the relaunch is confirmed: "
              "the new server has already migrated the world, so putting the "
              "old jar back could leave a binary that cannot read the data "
              "beside it -- a rollback is a restore")
        check(not list(mc.glob("server.jar.pre-update-*")),
              "including the ones EARLIER updates left: the branch tested "
              "is_dir(), so a file matched nothing and one jar per update "
              "piled up in minecraft_dir -- the directory the full backup "
              "zips, every time")
        check((mc / "server.jar").read_text() == "build 124",
              "the live jar is not touched")
        check(any("/restore" in m for m in said),
              f"and the operator is pointed at the real rollback ({said})")


def test_watch_reason_matches_the_edition():
    print("log-watch reason:")
    java = types.SimpleNamespace(edition="java")
    bedrock = types.SimpleNamespace(edition=up.EDITION_BEDROCK)
    check("jar" in up._watch_reason(java)
          and "dir" not in up._watch_reason(java),
          "a Java update does not claim the server dir was replaced -- only "
          "the jar is")
    check("dir" in up._watch_reason(bedrock),
          "the Bedrock swap genuinely does replace it")


def main():
    for fn in (test_paper, test_paper_falls_back_past_alpha_versions,
               test_vanilla, test_bedrock, test_version_ordering,
               test_download_verification, test_fetch_json, test_prune,
               test_extract_preserves_exec_bit, test_extract_rejects_bad_zips,
               test_custom_pack_diff, test_bedrock_swap_end_to_end,
               test_chain_is_rebased_not_preserved,
               test_paper_startup_does_not_record_vanilla,
               test_missing_banner_message_matches_reality,
               test_version_detection_from_logs,
               test_up_to_date_bedrock_is_quiet,
               test_backup_status_is_not_logged_twice,
               test_rollback_phrasing_reads_as_a_sentence,
               test_first_check_is_prompt_and_staggered,
               test_restore_downgrade_is_noticed,
               test_chain_rebased_after_a_recovered_relaunch,
               test_update_progress_is_logged,
               test_missing_baseline_is_logged,
               test_watcher_rebound_before_relaunch,
               test_relaunch_does_not_retry_into_a_live_server,
               test_backup_plan_matches_reality,
               test_installed_version_always_names_the_build,
               test_notification_goes_to_admin_dm_only,
               test_version_command_both_flavours,
               test_live_version_query,
               test_per_source_user_agent,
               test_no_baseline_stays_quiet,
               test_java_flavor_is_not_guessed,
               test_refuses_while_players_online,
               test_pre_update_backup_is_conditional,
               test_java_preflight_refuses_missing_jar,
               test_update_comparison,
               test_cleanup_drops_every_set_aside_jar,
               test_watch_reason_matches_the_edition):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): " + "; ".join(_failures))
        return 1
    print("ALL UPDATE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
