"""Closed, pinned built-in catalogs and content-defined pilot-v1 lineage (design §2.5, §3.3).

TR1 (Step 5) extends these checks to the released pilot-v2 and physical-test-v1 resources.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from recording_fixtures import (
    synthetic_catalog,
    synthetic_physical_test_catalog,
    synthetic_pilot_v2_catalog,
)

import cole_voice_lab.prompt_catalogs as catalogs
from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import Prompt, PromptSuite
from cole_voice_lab.prompt_catalogs import (
    BUILTIN_PROMPT_SUITE_SHA256,
    PILOT_V1_SHA256,
    legacy_pilot_v1,
    legacy_pilot_v1_suite,
    load_builtin_prompt_suite,
)
from cole_voice_lab.resources import load_prompt_suite, resource_path

# Release order. pilot-v2 is the design §2.5 golden; physical-test-v1 has no design golden and
# is pinned from its authored resource in the commit that ships it.
GOLDENS = {
    "pilot-v1.json": "eac118a995a1e184d5ed20b9ee23c3e0924c272b28d87308df2abeee1f8772d0",
    "evaluation-v1.json": "2854decad263e9e82a131f130782ff7c846d4ffd14c21eab87cab8cca2e6caf2",
    "pilot-v2.json": "0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028",
    "physical-test-v1.json": "b17cc176ee36ac5aa053c949e7ff44ce6e1b0c13b95b4cf7184693c09e48750b",
}


def test_pins_equal_design_goldens_in_release_order() -> None:
    assert list(BUILTIN_PROMPT_SUITE_SHA256.items()) == list(GOLDENS.items())
    assert next(iter(BUILTIN_PROMPT_SUITE_SHA256)) == "pilot-v1.json"
    for resource, pin in BUILTIN_PROMPT_SUITE_SHA256.items():
        suite = load_builtin_prompt_suite(resource)
        assert canonical_sha256(suite) == pin
        assert suite == PromptSuite.model_validate_json(resource_path(resource).read_bytes())
    with pytest.raises(TypeError):
        BUILTIN_PROMPT_SUITE_SHA256["pilot-v3.json"] = "0" * 64  # type: ignore[index]


@pytest.mark.parametrize(
    "name",
    [
        "pilot",
        "pilot-v1",
        "pilot-v2",
        "physical-test",
        "physical-test-v1",
        "../x",
        "../resources/pilot-v1.json",
        "pilot-v9.json",
        "consent.pending.json",
        "",
    ],
)
def test_loader_accepts_table_names_only(name: str) -> None:
    with pytest.raises(ContractError, match="built-in"):
        load_builtin_prompt_suite(name)


def test_loader_refuses_absolute_resource_paths() -> None:
    with pytest.raises(ContractError, match="built-in"):
        load_builtin_prompt_suite(str(resource_path("pilot-v1.json")))


def test_working_directory_file_never_shadows_a_built_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    impostor = synthetic_catalog(2)
    for name in ("pilot-v1.json", "pilot-v1", "pilot", "pilot-v2.json", "pilot-v2"):
        (tmp_path / name).write_text(impostor.model_dump_json(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_prompt_suite("pilot-v1.json") == impostor  # path-first loader, F8
    assert canonical_sha256(load_builtin_prompt_suite("pilot-v1.json")) == PILOT_V1_SHA256
    assert canonical_sha256(load_builtin_prompt_suite("pilot-v2.json")) == GOLDENS["pilot-v2.json"]


def test_edited_resource_fails_its_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    edited = json.loads(resource_path("pilot-v1.json").read_bytes())
    edited["prompts"][1]["style"] += "!"
    copy = tmp_path / "pilot-v1.json"
    copy.write_text(json.dumps(edited), encoding="utf-8")
    monkeypatch.setattr(catalogs, "resource_path", lambda name: tmp_path / name)
    with pytest.raises(SecurityError, match="release pin"):
        load_builtin_prompt_suite("pilot-v1.json")


def test_legacy_pilot_v1_is_decided_by_content_not_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pilot = load_builtin_prompt_suite("pilot-v1.json")
    evaluation = load_builtin_prompt_suite("evaluation-v1.json")
    pilot_v2 = synthetic_pilot_v2_catalog()
    # TR1: released values, independent of which built-ins this build ships.
    assert legacy_pilot_v1(load_builtin_prompt_suite("pilot-v2.json"))
    assert not legacy_pilot_v1(load_builtin_prompt_suite("physical-test-v1.json"))
    shared = PromptSuite(
        suite_id="cole-private-shared-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="private-room-tone",
                text="Stay silent.",
                category="room-tone",
                style="Silence",
                expected_seconds=10.0,
            ),
            pilot.prompts[5],
        ),
    )
    restyled = PromptSuite.model_validate(
        shared.model_dump()
        | {"prompts": (shared.prompts[0], pilot.prompts[5].model_copy(update={"style": "Slower"}))}
    )
    assert legacy_pilot_v1(pilot) and legacy_pilot_v1(pilot_v2) and legacy_pilot_v1(shared)
    assert not legacy_pilot_v1(evaluation)
    assert not legacy_pilot_v1(restyled)
    assert not legacy_pilot_v1(synthetic_physical_test_catalog())
    assert legacy_pilot_v1_suite(pilot) is pilot
    assert legacy_pilot_v1_suite(shared) == pilot

    catalogs._pilot_v1.cache_clear()
    opened: list[str] = []
    real = catalogs.resource_path

    def counted(name: str) -> Path:
        opened.append(name)
        return real(name)

    monkeypatch.setattr(catalogs, "resource_path", counted)
    try:
        # pilot-v1 is recognized by its pin without opening any resource.
        assert legacy_pilot_v1(pilot) and legacy_pilot_v1_suite(pilot) is pilot
        assert opened == []
        for _ in range(3):
            assert legacy_pilot_v1(shared) and legacy_pilot_v1_suite(pilot_v2) == pilot
        assert opened == ["pilot-v1.json"]
    finally:
        catalogs._pilot_v1.cache_clear()


def test_importing_prompt_catalogs_opens_no_resource(tmp_path: Path) -> None:
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import json, sys\n"
        "OPENS = []\n"
        "def hook(event, arguments):\n"
        "    # Resource JSON only; resources.py sits beside the directory with the same prefix.\n"
        "    if event == 'open' and isinstance(arguments[0], str) and "
        "arguments[0].rpartition('/')[0] == sys.argv[1]:\n"
        "        OPENS.append(arguments[0])\n"
        "sys.addaudithook(hook)\n"
        "import cole_voice_lab.cli, cole_voice_lab.prompt_catalogs\n"
        "import cole_voice_lab.recording_library, cole_voice_lab.prompt_coverage\n"
        "print(json.dumps(OPENS))\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        (sys.executable, "-I", "-B", str(driver), str(resource_path("pilot-v1.json").parent)),
        cwd="/",
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == []
