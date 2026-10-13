"""``scripts/seed-pc-catalog``: the owner's one command that seeds the PC's catalog.

docs/design/RECORDING-V1.md §7 step 1. The script runs ``voice-lab catalog …`` as the
``voice-lab`` user; here ``sudo``, ``curl`` and the deployed CLI are stand-ins on ``PATH``, so
every branch of the script's own logic runs without the network, a lab or root. The CLI
stand-in answers from a small state directory and logs each call, so a test can say exactly
which steps ran. The import and digest themselves are the CLI's, tested in
test_corpus_store_cli.py; only the pins the script carries are compared with them here.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cole_voice_lab.standard_prompt_sets import STANDARD_SET_SHA256

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "seed-pc-catalog"
VERSION_2 = "1ebd48cbc6f6a2b2ea0fe70d0cff8f1df1e91b3bbf2a5be52959ffba141ff90f"
#: ``catalog activate``'s output from the build with ``GET /api/v1/catalog`` on, and before it.
ACTIVATED = (
    '{\n  "read_model": "/data/voice-lab/lab/derived/corpus/active-catalog-cole-self.json"\n}\n'
)
ACTIVATED_BEFORE_THE_API = '{\n  "subject_id": "cole-self"\n}\n'

#: ``sudo``: run the command as ourselves; ``install -o`` drops the owner.
SUDO = """#!/bin/sh
if [ "$1" = "-u" ]; then shift 2; exec "$@"; fi
if [ "$1" = install ]; then
  shift; set -- $(printf '%s\\n' "$@" | sed '/^-o$/{N;d;}'); exec install "$@"
fi
exec "$@"
"""

#: ``curl -fsSLo <file> <url>``: writes $SEED_DOWNLOAD's bytes, logs the URL.
CURL = """#!/bin/sh
printf 'curl %s\\n' "$3" >>"$SEED_LOG"
cp "$SEED_DOWNLOAD" "$2"
"""

#: The deployed CLI: answers ``catalog list|show|activate`` from $SEED_STATE, logs the rest.
CLI = """#!/bin/sh
printf '%s\\n' "$*" >>"$SEED_LOG"
case "$1 $2" in
  "catalog list") cat "$SEED_STATE/list"; exit "$(cat "$SEED_STATE/list-status")" ;;
  "catalog show") [ -f "$SEED_STATE/show" ] || exit 1; cat "$SEED_STATE/show" ;;
  "catalog publish") printf 'cole-corpus version 2 · 0 kept-recording aliases · catalog %s\\n' \
      "$(cat "$SEED_STATE/publishes")" >"$SEED_STATE/show" ;;
  "catalog activate") cat "$SEED_STATE/activate" ;;
esac
"""


@dataclass
class Bench:
    root: Path
    state: Path
    log: Path
    environment: dict[str, str]

    def store(self, listing: str, *, status: int = 0) -> None:
        (self.state / "list").write_text(listing)
        (self.state / "list-status").write_text(str(status))

    def version_2(self, digest: str) -> None:
        (self.state / "show").write_text(
            f"cole-corpus version 2 · 0 kept-recording aliases · catalog {digest}\nwarm-01 …\n"
        )

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), *argv],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def calls(self) -> list[str]:
        """The CLI's calls without ``--asset-root``, and every download."""
        if not self.log.exists():
            return []
        suffix = f" --asset-root {self.root / 'lab'}"
        return [line.removesuffix(suffix) for line in self.log.read_text().splitlines()]


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in (("sudo", SUDO), ("curl", CURL), ("voice-lab", CLI)):
        (binaries / name).write_text(body)
        (binaries / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "activate").write_text(ACTIVATED)
    (state / "publishes").write_text(VERSION_2)
    download = tmp_path / "download"
    download.write_bytes(b"<html>not the published list</html>\n")
    log = tmp_path / "calls.log"
    environment = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "VOICE_LAB_SEED_LAB": str(tmp_path / "lab"),
        "VOICE_LAB_SEED_CLI": str(binaries / "voice-lab"),
        "SEED_STATE": str(state),
        "SEED_LOG": str(log),
        "SEED_DOWNLOAD": str(download),
    }
    return Bench(tmp_path, state, log, environment)


LIST_MISSING = "voice-lab: CORPUS.STORE_MISSING: no corpus store at …; run voice-lab catalog init\n"
LIST_SEEDED = (
    f"cole-corpus · suite cole-corpus · capture-pilot · latest version 2 · {VERSION_2}\n"
    "prompts: 1886 live · 0 retired\ncole-self records against cole-corpus version 2\n"
)
LIST_IMPORTED = (
    "cole-corpus · suite cole-corpus · capture-pilot · latest version 1 · e234…\n"
    "prompts: 1886 live · 0 retired\ncole-self records against cole-corpus version 1\n"
)
LIST = "catalog list --subject-id cole-self"
SHOW = "catalog show --version 2"
ACTIVATE = "catalog activate --subject-id cole-self --version 2"


def test_the_script_is_executable_parses_and_explains_itself(bench: Bench) -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=False).returncode == 0
    helped = bench.run("--help")
    assert helped.returncode == 0 and helped.stdout.startswith("Seed the PC's corpus catalog")
    assert "Exit status:" in helped.stdout
    refused = bench.run("--force")
    assert refused.returncode == 64 and "usage:" in refused.stderr
    assert bench.calls() == []


def test_its_pins_and_paths_are_the_repositorys() -> None:
    text = SCRIPT.read_text()

    def pinned(name: str) -> str:
        found = re.search(rf"^{name}=(\S+)$", text, re.MULTILINE)
        assert found is not None, name
        return found.group(1)

    assert pinned("HARVARD_SHA256") == STANDARD_SET_SHA256["harvard"]
    assert pinned("ARCTIC_SHA256") == STANDARD_SET_SHA256["arctic"]
    assert pinned("VERSION_2_SHA256") == VERSION_2
    corpus_store = (REPOSITORY / "docs" / "CORPUS_STORE.md").read_text()
    assert f"| 1,886 | `{VERSION_2}` |" in corpus_store
    assert pinned("FETCHED_AT") == "2026-09-16T04:17:51Z"
    # The lab and the CLI the intake publisher runs on the PC.
    unit = (REPOSITORY / "deploy" / "host" / "voice-lab-intake.service").read_text()
    assert f"ExecStart={pinned('CLI').split(':-')[1].rstrip('}')} intake publish" in unit
    assert f"--asset-root {pinned('LAB').split(':-')[1].rstrip('}')}" in unit


def test_a_list_that_is_not_the_pinned_one_stops_before_anything_is_written(bench: Bench) -> None:
    bench.store(LIST_MISSING, status=1)
    result = bench.run()
    assert result.returncode == 1
    assert "harvard.html has sha256" in result.stderr and "nothing was written" in result.stderr
    assert [call for call in bench.calls() if not call.startswith("curl ")] == [LIST]
    assert not any(path.name.startswith("tmp.") for path in bench.root.iterdir())


def test_an_unreadable_store_is_reported_and_left_alone(bench: Bench) -> None:
    bench.store("voice-lab: CORPUS.STORE_UNSAFE: the store directory is not private\n", status=1)
    result = bench.run()
    assert result.returncode == 1 and "CORPUS.STORE_UNSAFE" in result.stderr
    assert bench.calls() == [LIST]


def test_a_store_with_every_prompt_publishes_checks_and_activates_version_2(bench: Bench) -> None:
    bench.store(LIST_IMPORTED)
    result = bench.run()
    assert result.returncode == 0, result.stderr
    assert bench.calls() == [LIST, SHOW, "catalog publish", SHOW, ACTIVATE]
    assert "the API serves it: GET /api/v1/catalog" in result.stdout
    assert result.stdout.rstrip().endswith("(1,886 prompts)")


def test_a_version_2_that_is_not_the_macs_is_never_activated(bench: Bench) -> None:
    bench.store(LIST_IMPORTED)
    (bench.state / "publishes").write_text("0" * 64)
    result = bench.run()
    assert result.returncode == 1 and "not activated" in result.stderr
    assert ACTIVATE not in bench.calls()
    bench.store(LIST_SEEDED)  # activated by hand: the script re-checks, and still refuses
    bench.version_2("1" * 64)
    assert bench.run().returncode == 1 and ACTIVATE not in bench.calls()


@pytest.mark.parametrize(
    ("activated", "said"),
    ((ACTIVATED, "the API serves it"), (ACTIVATED_BEFORE_THE_API, "run this again after it has")),
)
def test_once_seeded_a_run_rechecks_version_2_and_activates_it_again(
    bench: Bench, activated: str, said: str
) -> None:
    bench.store(LIST_SEEDED)
    bench.version_2(VERSION_2)
    (bench.state / "activate").write_text(activated)
    result = bench.run()
    assert result.returncode == 0, result.stderr
    assert bench.calls() == [LIST, SHOW, ACTIVATE]  # nothing downloaded, imported or published
    assert "already seeded" in result.stdout and said in result.stdout


def test_a_missing_cli_is_a_missing_tool(bench: Bench) -> None:
    bench.environment["VOICE_LAB_SEED_CLI"] = str(bench.root / "absent" / "voice-lab")
    result = bench.run()
    assert result.returncode == 69 and "is voice-lab deployed" in result.stderr
    assert bench.calls() == []
