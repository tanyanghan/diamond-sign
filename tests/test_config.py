"""Self-contained tests for config validation.

Run with plain ``python tests/test_config.py`` -- no pytest, no dependencies
beyond the stdlib. Exits non-zero if anything fails.

Covers the problems that are silent at runtime rather than loud: a config that
loads fine and then makes the bot answer for the wrong server.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config import (AppConfig, BotConfig, ServerConfig, validate_config,
                          EDITION_BEDROCK, EDITION_JAVA)

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


def _server(name, mc_dir, *, edition=EDITION_JAVA, port=25575,
            host="localhost"):
    return ServerConfig(name=name, edition=edition, minecraft_dir=mc_dir,
                        backup_dir=mc_dir / "backups", rcon_host=host,
                        rcon_port=port, rcon_password="secret")


def _problems(servers):
    """Validate one bot holding these servers; return only RCON complaints."""
    app = AppConfig(bots=[BotConfig(name="b", platforms=("telegram",),
                                    bot_token="123:abc", servers=servers)])
    return [p for p in validate_config(app) if "RCON endpoint" in p]


def test_shared_rcon_endpoint_is_refused():
    print("two Java servers on one RCON endpoint:")
    with tempfile.TemporaryDirectory() as td:
        mc = Path(td)

        # Neither sets edition.rcon.port, so both take the 25575 default --
        # exactly what the example config used to encourage.
        bad = _problems([_server("alpha", mc), _server("bravo", mc)])
        check(len(bad) == 1,
              f"the collision is reported exactly once ({len(bad)})")
        check(bad and "bravo" in bad[0] and "alpha" in bad[0],
              "and names BOTH servers, so it is obvious which pair collides")
        check(bad and "25575" in bad[0],
              "and the endpoint, which is the part nobody set deliberately")
        check(bad and "server.properties" in bad[0],
              "and says where the real port comes from")

        ok = _problems([_server("alpha", mc, port=25575),
                        _server("bravo", mc, port=25576)])
        check(not ok, "distinct ports are fine")

        # A remote server can reuse the port number on its own host.
        ok = _problems([_server("alpha", mc),
                        _server("bravo", mc, host="10.0.0.9")])
        check(not ok, "same port on a DIFFERENT host is fine")

        # Bedrock has no RCON at all, so it must not be dragged in.
        ok = _problems([_server("alpha", mc, edition=EDITION_BEDROCK),
                        _server("bravo", mc, edition=EDITION_BEDROCK)])
        check(not ok, "two Bedrock servers are not an RCON collision")

        ok = _problems([_server("alpha", mc, edition=EDITION_BEDROCK),
                        _server("bravo", mc)])
        check(not ok, "nor is one of each")


def test_collision_is_fatal_not_advisory():
    print("severity:")
    with tempfile.TemporaryDirectory() as td:
        mc = Path(td)
        app = AppConfig(bots=[BotConfig(name="b", platforms=("telegram",),
                                        bot_token="123:abc",
                                        servers=[_server("alpha", mc),
                                                 _server("bravo", mc)])])
        check(any("RCON endpoint" in p for p in validate_config(app)),
              "it lands in validate_config's problem list, which load_config "
              "turns into a refusal to start -- a warning would be read once "
              "and scrolled past, and the symptom (a stopped server reporting "
              "itself online) looks nothing like its cause")


def test_shipped_example_does_not_demonstrate_the_collision():
    print("diamondsign.example.json:")
    import json
    from utils.config import _app_from_dict

    root = Path(__file__).resolve().parent.parent
    doc = json.loads((root / "diamondsign.example.json").read_text())
    app = _app_from_dict(doc)
    java = [s for s in app.all_servers() if s.edition == EDITION_JAVA]
    check(len(java) > 1,
          f"the example still shows more than one Java server ({len(java)}) -- "
          "the shape this check exists for")
    bad = [p for p in validate_config(app) if "RCON endpoint" in p]
    check(not bad,
          f"and it no longer teaches the collision: its second Java server "
          f"omitted edition.rcon.port, so it inherited 25575 from the first "
          f"and the shipped example was itself an example of the bug ({bad})")


def main():
    for fn in (test_shared_rcon_endpoint_is_refused,
               test_collision_is_fatal_not_advisory,
               test_shipped_example_does_not_demonstrate_the_collision):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): " + "; ".join(_failures))
        return 1
    print("ALL CONFIG TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
