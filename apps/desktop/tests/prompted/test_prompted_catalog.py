"""The catalogs Record: prompts records from (D-042), and a session's batch (D-010)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from voice_lab_desktop.prompted import catalog

from cole_voice_lab.models import PROMPT_SUITE_V1, Prompt, PromptSuite
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite


def suite(
    purpose: str = "capture-pilot", *, room_tone: bool = True, **extra: object
) -> PromptSuite:
    prompts = [
        Prompt(
            prompt_id="spoken-01",
            text="A sentence to read.",
            category="neutral",
            style="Neutral",
            expected_seconds=5.0,
            holdout=purpose == "held-out-evaluation",
        )
    ]
    if room_tone:
        prompts.insert(
            0,
            Prompt(
                prompt_id="room-tone-01",
                text="Remain silent.",
                category="room-tone",
                style="Silent",
                expected_seconds=10.0,
                holdout=purpose == "held-out-evaluation",
            ),
        )
    return PromptSuite(suite_id="test-suite", purpose=purpose, prompts=tuple(prompts), **extra)


def test_the_built_in_pilot_catalogs_load_with_their_pins() -> None:
    options = catalog.choices(None)
    assert [o["value"] for o in options] == ["builtin:pilot-v2.json", "builtin:pilot-v1.json"]
    loaded = catalog.load(catalog.DEFAULT_SOURCE, catalog_file=None)
    assert loaded.suite == load_builtin_prompt_suite("pilot-v2.json")
    assert loaded.suite.suite_id == "cole-pilot-v2" and len(loaded.sha256) == 64
    assert (
        catalog.load("builtin:pilot-v1.json", catalog_file=None).suite.suite_id == "cole-pilot-v1"
    )
    with pytest.raises(catalog.CatalogRefused, match="not a built-in catalog offered here"):
        catalog.load("builtin:evaluation-v1.json", catalog_file=None)
    with pytest.raises(catalog.CatalogRefused, match="unknown catalog"):
        catalog.load("server", catalog_file=None)
    with pytest.raises(catalog.CatalogRefused, match="no catalog file is set"):
        catalog.load(catalog.FILE_SOURCE, catalog_file=None)


def test_a_catalog_file_is_read_and_checked(tmp_path: Path) -> None:
    good = tmp_path / "suite.json"
    good.write_text(suite().model_dump_json())
    assert catalog.choices(str(good))[-1] == {"value": "file", "label": "Catalog file: suite.json"}
    loaded = catalog.load(catalog.FILE_SOURCE, catalog_file=str(good))
    assert loaded.suite.suite_id == "test-suite" and loaded.label == "Catalog file: suite.json"

    refusals = {
        "held-out-evaluation": "held-out evaluation catalog",
        "capture-corpus": "corpus catalog: it needs the corpus store",
    }
    for purpose, message in refusals.items():
        path = tmp_path / f"{purpose}.json"
        path.write_text(suite(purpose).model_dump_json())
        with pytest.raises(catalog.CatalogRefused, match=message):
            catalog.load(catalog.FILE_SOURCE, catalog_file=str(path))
    physical = load_builtin_prompt_suite("physical-test-v1.json")
    with pytest.raises(catalog.CatalogRefused, match="physical-test catalog"):
        catalog.check(physical)
    silent = tmp_path / "silent.json"
    silent.write_text(suite(room_tone=False).model_dump_json())
    with pytest.raises(catalog.CatalogRefused, match="has no room tone"):
        catalog.load(catalog.FILE_SOURCE, catalog_file=str(silent))
    # Test steps without spoken text never become capture evidence.
    procedure = Prompt.model_construct(
        prompt_id="step-01", category="procedure", style="Step", expected_seconds=5.0
    )
    stepped = suite().model_copy(update={"prompts": (*suite().prompts, procedure)})
    with pytest.raises(catalog.CatalogRefused, match="test steps without spoken text"):
        catalog.check(stepped)

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"suite_id": "x"}))
    with pytest.raises(catalog.CatalogRefused, match="not a valid prompt suite"):
        catalog.load(catalog.FILE_SOURCE, catalog_file=str(broken))
    with pytest.raises(catalog.CatalogRefused, match="cannot be read"):
        catalog.load(catalog.FILE_SOURCE, catalog_file=str(tmp_path / "missing.json"))
    huge = tmp_path / "huge.json"
    huge.write_bytes(b" " * (catalog.MAX_CATALOG_BYTES + 1))
    with pytest.raises(catalog.CatalogRefused, match="larger than"):
        catalog.load(catalog.FILE_SOURCE, catalog_file=str(huge))


def test_a_batch_is_the_chosen_prompts_and_every_room_tone_in_catalog_order() -> None:
    pilot = load_builtin_prompt_suite("pilot-v2.json")
    ids = catalog.batch_ids(pilot, ["expressive-02", "reference-neutral-01"])
    assert ids == ("room-tone-start", "reference-neutral-01", "expressive-02", "room-tone-end")
    with pytest.raises(catalog.CatalogRefused, match="not in the catalog: nope"):
        catalog.batch_ids(pilot, ["nope"])
    with pytest.raises(catalog.CatalogRefused, match="choose at least one prompt"):
        catalog.batch_ids(pilot, ["room-tone-start"])

    subset = catalog.batch_suite(pilot, ids)
    assert subset.suite_id.startswith("native-subset-") and len(subset.suite_id) == 38
    assert [p.prompt_id for p in subset.prompts] == list(ids)
    assert subset.schema_version == pilot.schema_version and subset.purpose == pilot.purpose
    assert catalog.batch_suite(pilot, ids) == subset  # named by its prompts, so stable
    every = tuple(p.prompt_id for p in pilot.prompts)
    assert catalog.batch_suite(pilot, every) is pilot

    v1 = load_builtin_prompt_suite("pilot-v1.json")
    v1_subset = catalog.batch_suite(v1, catalog.batch_ids(v1, ["expressive-02"]))
    assert v1_subset.schema_version == PROMPT_SUITE_V1
    assert v1_subset.suite_id.startswith("desktop-subset-") and len(v1_subset.suite_id) == 31
