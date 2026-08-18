from __future__ import annotations

from pathlib import Path

import pytest

from cole_voice_lab.resources import load_prompt_suite, resource_path


def test_resource_path_resolves_bundled_file() -> None:
    path = resource_path("pilot-v1.json")
    assert path.is_file()
    assert path.parent.name == "resources"


def test_pilot_suite_is_versioned_and_near_five_minutes() -> None:
    suite = load_prompt_suite("pilot")

    assert suite.purpose == "capture-pilot"
    assert 270 <= sum(prompt.expected_seconds for prompt in suite.prompts) <= 360
    assert sum(prompt.reference_candidate for prompt in suite.prompts) >= 3
    assert suite.prompts[0].category == "room-tone"
    assert suite.prompts[-1].category == "room-tone"


def test_evaluation_suite_is_held_out() -> None:
    suite = load_prompt_suite("evaluation")

    assert suite.purpose == "held-out-evaluation"
    assert len(suite.prompts) >= 10
    assert all(prompt.holdout for prompt in suite.prompts)
    assert not (
        {prompt.text for prompt in suite.prompts}
        & {prompt.text for prompt in load_prompt_suite("pilot").prompts}
    )


def test_prompt_suite_can_load_an_explicit_file(tmp_path: Path) -> None:
    custom = tmp_path / "custom-prompts.json"
    custom.write_bytes(resource_path("pilot-v1.json").read_bytes())

    suite = load_prompt_suite(custom)
    assert suite.purpose == "capture-pilot"


def test_unknown_bundled_prompt_suite_fails_closed() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt_suite("unknown-suite.json")
