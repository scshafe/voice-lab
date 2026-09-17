"""Catalog port conformance: what every CatalogReader and CatalogEditor must do.

Written only against ``catalog_ports``: stores are obtained through
``storage_binding.open_catalog_editor`` / ``open_catalog_reader`` and never by importing an
implementation. Timestamps are checked only for being timezone-aware and in order. A
published snapshot is looked up where the contract puts it
(``recording_contracts.catalog_snapshot_path``). Collected by pytest through
``python_files`` (pyproject); implementation-specific checks live in
``test_corpus_store_sqlite.py`` and shared catalog logic in ``test_corpus_store.py``, which
both reuse this module's fixtures and helpers.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import pickle
import re
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest

import cole_voice_lab.catalog_ports as catalog_ports
import cole_voice_lab.corpus_store as corpus_store
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.catalog_ports import (
    CORPUS_ERROR_CLASSES,
    UNCHANGED,
    CatalogEditor,
    CorpusSnapshotSecurityError,
    CorpusStoreCode,
    CorpusStoreError,
    CorpusStoreSecurityError,
    PromptRecord,
)
from cole_voice_lab.corpus_store import PromptSourceReference, read_catalog_snapshot
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import PROMPT_SUITE_V2, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import BUILTIN_PROMPT_SUITE_SHA256, load_builtin_prompt_suite
from cole_voice_lab.recording_contracts import (
    CATALOG_SNAPSHOT_ROOT,
    CatalogReference,
    CatalogSnapshot,
    ErrorCode,
    PromptAlias,
    catalog_snapshot_path,
)
from cole_voice_lab.storage_binding import open_catalog_editor, open_catalog_reader

SUBJECT = "cole-self"
CATALOG = "cole-corpus"
EPOCH = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
SOURCE_REFERENCE = PromptSourceReference(
    url="https://example.invalid/harvard.html",
    fetched_at=EPOCH,
    sha256="a" * 64,
    license="IEEE Recommended Practice for Speech Quality Measurements",
)
SOURCE = Path(corpus_store.__file__).parent


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private-assets")


@pytest.fixture
def store(layout: AssetLayout) -> Iterator[CatalogEditor]:
    with open_catalog_editor(layout, create=True) as opened:
        yield opened


def add(store: CatalogEditor, prompt_id: str, text: str, **overrides: Any) -> PromptRecord:
    fields: dict[str, Any] = {
        "text": text,
        "category": "neutral",
        "style": "Natural neutral speech.",
        "expected_seconds": 6.0,
        "source": "manual",
    }
    fields.update(overrides)
    return store.add_prompt(prompt_id, **fields)


def seeded(store: CatalogEditor, count: int = 3) -> tuple[PromptRecord, ...]:
    return tuple(
        add(store, f"prompt-{n:02d}", f"Sentence number {n}.") for n in range(1, count + 1)
    )


def published(store: CatalogEditor, catalog_id: str = CATALOG) -> CatalogReference:
    store.create_catalog(catalog_id, purpose="capture-corpus", description="the living corpus")
    return store.publish_version(catalog_id)


def snapshot_directory(layout: AssetLayout) -> Path:
    """Where every published snapshot of ``layout``'s catalog lives, by contract."""
    return layout.root / CATALOG_SNAPSHOT_ROOT


def snapshot_file(layout: AssetLayout, reference: CatalogReference) -> Path:
    return layout.root / reference.snapshot_path


def file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def aware(*values: datetime | None) -> None:
    """Every value is a timezone-aware instant."""
    for value in values:
        assert value is not None and value.utcoffset() is not None, value


# -- errors ------------------------------------------------------------------------------


def test_error_carries_its_code_and_survives_pickling() -> None:
    error = CorpusStoreError("CORPUS.PROMPT_UNKNOWN", "probe")
    assert isinstance(error, ContractError)
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is CorpusStoreError
    assert (restored.code, str(restored)) == ("CORPUS.PROMPT_UNKNOWN", "probe")
    security = pickle.loads(
        pickle.dumps(CorpusSnapshotSecurityError("CORPUS.SNAPSHOT_MISSING", "gone"))
    )
    assert isinstance(security, SecurityError)
    assert isinstance(security, CorpusStoreError)
    assert security.code == "CORPUS.SNAPSHOT_MISSING"
    assert isinstance(CorpusStoreSecurityError("CORPUS.STORE_UNSAFE", "x"), SecurityError)


def test_every_corpus_code_has_one_error_class_and_every_raise_uses_it() -> None:
    codes = set(get_args(CorpusStoreCode.__value__))
    assert set(CORPUS_ERROR_CLASSES) == codes
    for code, error_class in CORPUS_ERROR_CLASSES.items():
        assert issubclass(error_class, CorpusStoreError), code
        assert issubclass(error_class, SecurityError) is (error_class is not CorpusStoreError)
    security = {
        code: cls for code, cls in CORPUS_ERROR_CLASSES.items() if cls is not CorpusStoreError
    }
    assert security == {
        "CORPUS.STORE_UNSAFE": CorpusStoreSecurityError,
        "CORPUS.SNAPSHOT_MISSING": CorpusSnapshotSecurityError,
        "CORPUS.SNAPSHOT_MISMATCH": CorpusSnapshotSecurityError,
    }
    # The wire sends a subset of the store's codes.
    wire = {code for code in get_args(ErrorCode.__value__) if code.startswith("CORPUS.")}
    assert wire and wire <= codes
    # Every CORPUS code a module names is a store code, and every error built from a code
    # literal is built as the class the map names (the recording service and the fine-tune
    # worker test for the security classes before the code); every code is raised somewhere.
    classes = {cls.__name__: cls for cls in CORPUS_ERROR_CLASSES.values()}
    raised: set[str] = set()
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert set(re.findall(r'"(CORPUS\.[A-Z_]+)"', text)) <= codes, path.name
        for name, code in re.findall(r'\b(Corpus\w*Error)\(\s*"(CORPUS\.[A-Z_]+)"', text):
            assert CORPUS_ERROR_CLASSES[code] is classes[name], (path.name, name, code)
            raised.add(code)
        # These helpers build a plain CorpusStoreError from their code argument.
        for code in re.findall(
            r'\b(?:_validated|_unknown_catalog)\([^)]*?"(CORPUS\.[A-Z_]+)"', text
        ):
            assert CORPUS_ERROR_CLASSES[code] is CorpusStoreError, (path.name, code)
            raised.add(code)
    assert raised == codes


# -- opening and closing -----------------------------------------------------------------


def test_closed_store_refuses_queries_and_close_is_idempotent(layout: AssetLayout) -> None:
    store = open_catalog_editor(layout, create=True)
    store.close()
    store.close()
    with pytest.raises(CorpusStoreError, match="closed") as caught:
        store.list_prompts()
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"


def test_a_reader_serves_what_an_editor_published_and_never_creates_a_store(
    layout: AssetLayout, store: CatalogEditor
) -> None:
    records = seeded(store, 2)
    reference = published(store)
    store.activate(SUBJECT, reference)
    snapshot = store.materialize(reference)
    with open_catalog_reader(layout) as reader:
        assert reader.list_prompts() == records
        assert reader.list_prompts(include_retired=True) == records
        assert reader.prompt_history("prompt-01") == (records[0],)
        assert reader.list_catalogs() == store.list_catalogs()
        assert reader.resolve_reference(CATALOG) == reader.resolve_reference(CATALOG, 1)
        assert reader.resolve_reference(CATALOG) == reference
        assert reader.materialize(reference) == snapshot
        assert reader.active_catalog(SUBJECT) == (snapshot.suite, reference)
    reader.close()
    with pytest.raises(CorpusStoreError, match="closed") as caught:
        reader.list_catalogs()
    assert caught.value.code == "CORPUS.STORE_UNAVAILABLE"
    empty = AssetLayout.initialize(layout.root.parent / "without-a-store")
    with pytest.raises(CorpusStoreError) as missing:
        open_catalog_reader(empty)
    assert missing.value.code == "CORPUS.STORE_MISSING"
    assert not (empty.root / "provenance" / "corpus").exists()


# -- prompts -------------------------------------------------------------------------


def test_added_prompt_is_revision_one_with_the_contract_digest(store: CatalogEditor) -> None:
    record = add(
        store,
        "warm-01",
        "We can take this one step at a time.",
        category="warm-narration",
        labels=("gentle",),
        reference_candidate=True,
    )
    assert record.revision == 1
    assert record.keeps_recordings is False
    assert record.accepted_prompt_sha256s == ()
    assert record.prompt_sha256 == canonical_sha256(record.prompt)
    assert record.prompt.labels == ("gentle",)
    assert record.source == "manual"
    assert record.source_reference is None
    assert record.retired_at is None
    aware(record.created_at)
    assert record.created_at == record.revision_created_at
    assert store.list_prompts() == (record,)
    assert store.prompt_history("warm-01") == (record,)


def test_silent_prompt_needs_no_text_and_source_reference_is_canonicalized(
    store: CatalogEditor,
) -> None:
    record = store.add_prompt(
        "room-tone-start",
        text=None,
        category="room-tone",
        style="Silence; keep the recording position unchanged.",
        expected_seconds=15.0,
        instructions="Remain still and silent while the room tone is recorded.",
        source="harvard",
        source_reference=SOURCE_REFERENCE.model_dump_json(indent=2),
    )
    assert record.prompt.text is None
    assert record.source == "harvard"
    assert record.source_reference == canonical_json_bytes(SOURCE_REFERENCE).decode()
    add(store, "spoken", "A repeated sentence.")
    store.add_prompt(
        "room-tone-end",
        text=None,
        category="room-tone",
        style="Silence.",
        expected_seconds=15.0,
        instructions="Remain still.",
        source="manual",
    )
    assert [record.prompt.prompt_id for record in store.list_prompts()] == [
        "room-tone-start",
        "spoken",
        "room-tone-end",
    ]


def test_a_source_reference_records_an_accepted_unpinned_source_only_when_there_is_one(
    store: CatalogEditor,
) -> None:
    pinned = canonical_json_bytes(SOURCE_REFERENCE).decode()
    # References written before the field existed are the canonical form of a pinned source.
    assert "unpinned_source_accepted" not in json.loads(pinned)
    assert PromptSourceReference.model_validate_json(pinned).unpinned_source_accepted is False
    unpinned = SOURCE_REFERENCE.model_copy(update={"unpinned_source_accepted": True})
    unpinned_json = unpinned.model_dump_json()
    assert json.loads(canonical_json_bytes(unpinned))["unpinned_source_accepted"] is True
    explicit_false = SOURCE_REFERENCE.model_dump(mode="json") | {"unpinned_source_accepted": False}
    for index, (reference, stored) in enumerate(
        (
            (json.dumps(explicit_false), pinned),
            (unpinned_json, canonical_json_bytes(unpinned).decode()),
        )
    ):
        record = add(
            store,
            f"harvard-01-0{index + 1}",
            f"Sentence number {index + 1}.",
            source="harvard",
            source_reference=reference,
        )
        assert record.source_reference == stored
    with pytest.raises(CorpusStoreError, match="unpinned_source_accepted") as caught:
        add(
            store,
            "harvard-01-03",
            "Sentence number 3.",
            source="harvard",
            source_reference=json.dumps(explicit_false | {"unpinned_source_accepted": "yes"}),
        )
    assert caught.value.code == "CORPUS.SOURCE_REFERENCE_INVALID"


@pytest.mark.parametrize(
    "fields, code, message",
    [
        ({"category": "sonnet"}, "CORPUS.PROMPT_INVALID", "category"),
        ({"text": None}, "CORPUS.PROMPT_INVALID", "only room tone and procedure prompts"),
        ({"source": "wikipedia"}, "CORPUS.SOURCE_INVALID", "prompt source"),
        ({"source": "arctic"}, "CORPUS.SOURCE_REFERENCE_INVALID", "requires a source reference"),
        (
            {"source": "import", "source_reference": '{"url": "x"}'},
            "CORPUS.SOURCE_REFERENCE_INVALID",
            "fetched_at",
        ),
    ],
    ids=["bad-category", "spoken-without-text", "bad-source", "no-reference", "bad-reference"],
)
def test_invalid_prompt_fields_are_refused_before_any_write(
    store: CatalogEditor, fields: dict[str, Any], code: str, message: str
) -> None:
    request: dict[str, Any] = {
        "text": "Sentence.",
        "category": "neutral",
        "style": "Natural neutral speech.",
        "expected_seconds": 6.0,
        "source": "manual",
        **fields,
    }
    with pytest.raises(CorpusStoreError, match=message) as caught:
        store.add_prompt("prompt-01", **request)
    assert caught.value.code == code
    assert store.list_prompts() == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"text": "Hello\x07world."},
        {"text": "Hello\u200dworld."},
        {"style": "Neutral\x1b[31m"},
        {"text": "Hello\x00world."},
        {"text": None, "category": "room-tone", "instructions": "Stay\x07still."},
    ],
    ids=["bell", "zero-width-joiner", "escape-in-style", "nul", "bell-in-instructions"],
)
def test_format_and_control_characters_are_refused_when_a_prompt_is_added(
    store: CatalogEditor, fields: dict[str, Any]
) -> None:
    with pytest.raises(CorpusStoreError, match="format or control characters") as caught:
        add(store, "prompt-01", **{"text": "Sentence.", **fields})
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert store.list_prompts() == ()


def test_edit_and_import_apply_the_same_text_hygiene(store: CatalogEditor) -> None:
    add(store, "prompt-01", "Sentence one.")
    with pytest.raises(CorpusStoreError, match="format or control characters") as caught:
        store.edit_prompt("prompt-01", text="Sentence\u200bone.")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert [record.revision for record in store.prompt_history("prompt-01")] == [1]
    # prompt-suite/v1 exactness has no hygiene rule, so an operator-supplied v1 file can
    # carry a control character; the store refuses it before anything is written.
    legacy = PromptSuite(
        suite_id="legacy",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="clean-01",
                text="A clean sentence.",
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
            ),
            Prompt(
                prompt_id="bell-01",
                text="Hello\x07world.",
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
            ),
        ),
    )
    with pytest.raises(CorpusStoreError, match="prompt bell-01 text cannot") as caught:
        store.import_prompt_suite(legacy, source="import")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["prompt-01"]


def test_duplicate_prompt_id_or_live_text_is_refused(store: CatalogEditor) -> None:
    add(store, "prompt-01", "Sentence one.")
    with pytest.raises(CorpusStoreError, match="prompt id already exists") as caught:
        add(store, "prompt-01", "Different sentence.")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    with pytest.raises(
        CorpusStoreError, match="repeats the text of live prompt prompt-01"
    ) as caught:
        add(store, "prompt-02", "Sentence one.")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    store.retire_prompt("prompt-01")
    add(store, "prompt-02", "Sentence one.")
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["prompt-02"]


def test_a_prompt_that_reads_like_another_live_prompt_is_refused_everywhere(
    store: CatalogEditor,
) -> None:
    """Two live prompts that read the same would count one recording twice."""
    add(store, "prompt-01", "The birch canoe slid on the smooth planks.")
    add(store, "prompt-02", "Sentence two.")
    add(store, "dots-01", "...")
    same = "prompt echo-01 reads the same as live prompt prompt-01"
    with pytest.raises(CorpusStoreError, match=same) as caught:
        add(store, "echo-01", "the Birch canoe slid on the smooth  planks!")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    assert (
        '(ignoring case, punctuation and spacing): "the Birch canoe slid on the smooth  planks!" '
        'and "The birch canoe slid on the smooth planks."'
    ) in str(caught.value)
    # Text without words reads the same only when it is the same text.
    add(store, "dots-02", "!!!")
    with pytest.raises(CorpusStoreError, match="repeats the text of live prompt dots-01"):
        add(store, "dots-03", "...")
    with pytest.raises(CorpusStoreError, match="prompt-02 reads the same as live prompt prompt-01"):
        store.edit_prompt("prompt-02", text="The birch canoe slid on the smooth planks?")
    # A prompt may keep its own reading, e.g. in a keep-recordings punctuation fix.
    kept = store.edit_prompt(
        "prompt-01", text="The birch canoe slid on the smooth planks!", keep_recordings=True
    )
    assert kept.revision == 2
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="operator-suite",
        purpose="capture-corpus",
        prompts=tuple(
            Prompt(
                prompt_id=prompt_id,
                text=text,
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
            )
            for prompt_id, text in (
                ("fresh-01", "A fresh sentence."),
                ("fresh-02", "a FRESH sentence"),
            )
        ),
    )
    with pytest.raises(CorpusStoreError, match="fresh-02 reads the same as live prompt fresh-01"):
        store.import_prompt_suite(suite, source="import")
    assert [r.prompt.prompt_id for r in store.list_prompts()] == [
        "prompt-01",
        "prompt-02",
        "dots-01",
        "dots-02",
    ]
    # Retiring a prompt frees its reading.
    store.retire_prompt("prompt-01")
    assert add(store, "echo-01", "the Birch canoe slid on the smooth planks").revision == 1


def test_unknown_prompt_is_named_everywhere(store: CatalogEditor) -> None:
    for call in (
        lambda: store.edit_prompt("absent", text="x"),
        lambda: store.retire_prompt("absent"),
        lambda: store.prompt_history("absent"),
    ):
        with pytest.raises(CorpusStoreError, match="unknown prompt: absent") as caught:
            call()
        assert caught.value.code == "CORPUS.PROMPT_UNKNOWN"


def test_default_edit_creates_a_revision_that_accepts_nothing(store: CatalogEditor) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    second = store.edit_prompt("prompt-01", text="Sentence one, revised.")
    assert second.revision == 2
    assert second.prompt.text == "Sentence one, revised."
    assert second.prompt_sha256 != first.prompt_sha256
    assert second.keeps_recordings is False
    assert second.accepted_prompt_sha256s == ()
    aware(second.created_at, second.revision_created_at)
    assert second.created_at == first.created_at
    # The revision is stamped when the edit runs, not when the prompt was added.
    assert first.revision_created_at < second.revision_created_at
    assert store.list_prompts() == (second,)
    assert store.prompt_history("prompt-01") == (first, second)


def test_every_field_can_change_and_a_no_op_edit_is_refused(store: CatalogEditor) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    edited = store.edit_prompt(
        "prompt-01",
        text=None,
        category="room-tone",
        style="Silence.",
        instructions="Stay silent.",
        labels=["quiet"],
        expected_seconds=15.0,
        reference_candidate=True,
        holdout=True,
    )
    assert edited.prompt.model_dump() == {
        "prompt_id": "prompt-01",
        "category": "room-tone",
        "style": "Silence.",
        "instructions": "Stay silent.",
        "labels": ("quiet",),
        "expected_seconds": 15.0,
        "reference_candidate": True,
        "holdout": True,
    }
    with pytest.raises(CorpusStoreError, match="leaves prompt prompt-01 unchanged") as caught:
        store.edit_prompt("prompt-01", style="Silence.")
    assert caught.value.code == "CORPUS.PROMPT_UNCHANGED"
    with pytest.raises(CorpusStoreError, match="prompt is invalid") as caught:
        store.edit_prompt("prompt-01", category="neutral")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert store.prompt_history("prompt-01") == (first, edited)
    assert UNCHANGED is catalog_ports.UNCHANGED


def test_edit_cannot_take_another_live_prompts_text(store: CatalogEditor) -> None:
    add(store, "prompt-01", "Sentence one.")
    add(store, "prompt-02", "Sentence two.")
    with pytest.raises(
        CorpusStoreError, match="repeats the text of live prompt prompt-01"
    ) as caught:
        store.edit_prompt("prompt-02", text="Sentence one.")
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    assert store.list_prompts()[1].revision == 1


def test_a_dry_run_edit_runs_every_check_and_stores_nothing(store: CatalogEditor) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    add(store, "prompt-02", "Sentence two.")
    preview = store.edit_prompt(
        "prompt-01",
        text="Sentence one, revised.",
        keep_recordings=True,
        allow_rewording=True,
        dry_run=True,
    )
    assert (preview.revision, preview.prompt.text) == (2, "Sentence one, revised.")
    assert preview.keeps_recordings is True
    assert preview.accepted_prompt_sha256s == (first.prompt_sha256,)
    assert store.prompt_history("prompt-01") == (first,)
    assert store.list_prompts()[0] == first
    for changes, code in (
        ({"text": "Sentence two."}, "CORPUS.PROMPT_DUPLICATE"),
        ({"text": "Sentence one."}, "CORPUS.PROMPT_UNCHANGED"),
        ({"text": None}, "CORPUS.PROMPT_INVALID"),
    ):
        with pytest.raises(CorpusStoreError) as caught:
            store.edit_prompt("prompt-01", dry_run=True, **changes)
        assert caught.value.code == code
    store.retire_prompt("prompt-02")
    with pytest.raises(CorpusStoreError) as retired:
        store.edit_prompt("prompt-02", style="Brisk.", dry_run=True)
    assert retired.value.code == "CORPUS.PROMPT_RETIRED"
    written = store.edit_prompt(
        "prompt-01", text="Sentence one, revised.", keep_recordings=True, allow_rewording=True
    )
    assert written.prompt == preview.prompt and written.revision == preview.revision
    assert written.accepted_prompt_sha256s == preview.accepted_prompt_sha256s
    assert store.prompt_history("prompt-01") == (first, written)


def test_an_edit_is_refused_once_the_prompt_moved_past_the_revision_the_caller_saw(
    store: CatalogEditor,
) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    # Another editor lands a revision between the caller's look and its write.
    other = store.edit_prompt("prompt-01", style="Brisk.", expected_revision=1)
    assert other.revision == 2
    for dry_run in (True, False):
        with pytest.raises(CorpusStoreError, match="at revision 2, not the expected") as caught:
            store.edit_prompt(
                "prompt-01",
                text="Sentence one!",
                keep_recordings=True,
                expected_revision=1,
                dry_run=dry_run,
            )
        assert caught.value.code == "CORPUS.PROMPT_CHANGED"
    assert store.prompt_history("prompt-01") == (first, other)
    kept = store.edit_prompt(
        "prompt-01", text="Sentence one!", keep_recordings=True, expected_revision=2
    )
    assert kept.accepted_prompt_sha256s == (other.prompt_sha256,)


def test_a_prompt_no_prompt_suite_v2_catalog_can_hold_is_never_stored(store: CatalogEditor) -> None:
    """Every stored prompt can publish somewhere: the store publishes prompt-suite/v2 only."""
    with pytest.raises(CorpusStoreError, match="fits no prompt-suite/v2 catalog") as caught:
        add(store, "tone-01", "Silence please.", category="room-tone")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert "room tone requires instructions and no text" in str(caught.value)
    assert "Value error" not in str(caught.value)
    add(store, "step-01", "Press Record.", category="neutral")
    with pytest.raises(CorpusStoreError, match="fits no prompt-suite/v2 catalog") as caught:
        store.edit_prompt("step-01", text=None, category="procedure", instructions="Press it.")
    assert "physical-test: physical-test prompts require the test label" in str(caught.value)
    assert len(store.prompt_history("step-01")) == 1
    procedure = store.edit_prompt(
        "step-01", text=None, category="procedure", instructions="Press it.", labels=("test",)
    )
    assert procedure.prompt.category == "procedure"
    # A prompt-suite/v1 room tone carries its text; the store takes all of a suite or nothing.
    pilot = load_builtin_prompt_suite("pilot-v1.json")
    with pytest.raises(CorpusStoreError, match="prompt room-tone-start fits no") as caught:
        store.import_prompt_suite(pilot, source="pilot-v1")
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["step-01"]


def test_creating_a_catalog_applies_its_definition(store: CatalogEditor) -> None:
    with pytest.raises(CorpusStoreError, match="control") as caught:
        store.create_catalog("second", purpose="capture-pilot", description="safe\x1b[2J")
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    with pytest.raises(CorpusStoreError, match="cannot name a physical-test"):
        store.create_catalog("second", purpose="physical-test")
    assert store.list_catalogs() == ()


def test_initializing_a_catalog_stores_seed_version_and_activation_or_nothing(
    store: CatalogEditor,
) -> None:
    seed = load_builtin_prompt_suite("pilot-v2.json")
    pin = BUILTIN_PROMPT_SUITE_SHA256["pilot-v2.json"]
    definition = corpus_store.catalog_definition(
        "pilot-mirror", suite_id=seed.suite_id, purpose=seed.purpose
    )
    for fields in ({"seed": seed}, {"subject_id": SUBJECT}):
        with pytest.raises(ValueError, match="exactly one subject"):
            store.initialize_catalog(definition, **fields)
    with pytest.raises(CorpusStoreError, match="subject id") as caught:
        store.initialize_catalog(definition, seed=seed, subject_id="Not A Subject")
    assert caught.value.code == "CORPUS.SUBJECT_INVALID"
    # A refusal inside the transaction (the seed's prompts are stored before the catalog
    # that cannot hold them is refused) keeps none of it.
    refusing = corpus_store.catalog_definition(
        "pilot-mirror", suite_id=seed.suite_id, purpose="held-out-evaluation"
    )
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts") as caught:
        store.initialize_catalog(refusing, seed=seed, subject_id=SUBJECT)
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    assert store.list_prompts(include_retired=True) == () and store.list_catalogs() == ()
    with pytest.raises(CorpusStoreError, match="no active catalog"):
        store.active_catalog(SUBJECT)

    record, reference = store.initialize_catalog(definition, seed=seed, subject_id=SUBJECT)
    assert reference is not None
    assert reference.catalog_sha256 == pin
    assert record.versions == (reference,) and record.catalog_id == "pilot-mirror"
    assert store.active_catalog(SUBJECT) == (seed, reference)
    assert {r.source for r in store.list_prompts()} == {"import"}
    with pytest.raises(CorpusStoreError, match="holds 36 prompt") as caught:
        store.initialize_catalog(corpus_store.catalog_definition("other", purpose="capture-corpus"))
    assert caught.value.code == "CORPUS.CATALOG_DUPLICATE"


def test_initializing_without_a_seed_creates_only_the_empty_catalog(store: CatalogEditor) -> None:
    definition = corpus_store.catalog_definition("cole-corpus", purpose="capture-corpus")
    record, reference = store.initialize_catalog(definition)
    assert reference is None and record.versions == () and record.purpose == "capture-corpus"
    assert store.list_prompts() == ()


def test_a_version_number_outside_the_sqlite_range_is_simply_unknown(store: CatalogEditor) -> None:
    seeded(store, 1)
    store.create_catalog(CATALOG, purpose="capture-corpus")
    store.publish_version(CATALOG)
    for version in (0, -1, 2**63, 2**70):
        with pytest.raises(CorpusStoreError, match=f"has no version {version}$") as caught:
            store.resolve_reference(CATALOG, version)
        assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    assert store.resolve_reference(CATALOG, 1).catalog_version == 1


def test_keep_recordings_chain_accumulates_until_a_default_edit_breaks_it(
    store: CatalogEditor,
) -> None:
    first = add(store, "prompt-01", "the quick brown fox")
    second = store.edit_prompt("prompt-01", text="The quick brown fox", keep_recordings=True)
    assert second.keeps_recordings is True
    assert second.accepted_prompt_sha256s == (first.prompt_sha256,)
    third = store.edit_prompt("prompt-01", text="The quick brown fox.", keep_recordings=True)
    assert third.accepted_prompt_sha256s == tuple(
        sorted((first.prompt_sha256, second.prompt_sha256))
    )
    reverted = store.edit_prompt("prompt-01", text="The quick brown fox", keep_recordings=True)
    assert reverted.prompt_sha256 == second.prompt_sha256
    assert reverted.accepted_prompt_sha256s == tuple(
        sorted((first.prompt_sha256, third.prompt_sha256))
    )
    fifth = store.edit_prompt("prompt-01", text="A completely different sentence.")
    assert fifth.keeps_recordings is False
    assert fifth.accepted_prompt_sha256s == ()
    sixth = store.edit_prompt(
        "prompt-01", text="A completely different sentence!", keep_recordings=True
    )
    assert sixth.accepted_prompt_sha256s == (fifth.prompt_sha256,)
    assert [record.revision for record in store.prompt_history("prompt-01")] == [1, 2, 3, 4, 5, 6]


def test_keep_recordings_edit_cannot_change_held_out_or_spoken_behaviour(
    store: CatalogEditor,
) -> None:
    first = add(store, "prompt-01", "Sentence one.")
    tone = add(
        store,
        "tone-01",
        None,
        category="room-tone",
        style="Silence.",
        instructions="Stay silent.",
    )
    for prompt_id, changes in (
        ("prompt-01", {"holdout": True}),
        ("prompt-01", {"text": None, "category": "room-tone", "instructions": "Stay silent."}),
        ("tone-01", {"text": "Now spoken.", "category": "neutral", "instructions": None}),
    ):
        with pytest.raises(CorpusStoreError, match="cannot change its held-out flag") as caught:
            store.edit_prompt(prompt_id, keep_recordings=True, **changes)
        assert caught.value.code == "CORPUS.PROMPT_INVALID"
    assert store.list_prompts() == (first, tone)
    # Another spoken category or style keeps the identity; a default edit may change anything.
    kept = store.edit_prompt(
        "prompt-01", keep_recordings=True, category="technical", style="Careful."
    )
    assert kept.accepted_prompt_sha256s == (first.prompt_sha256,)
    moved = store.edit_prompt("prompt-01", holdout=True)
    assert moved.prompt.holdout and moved.accepted_prompt_sha256s == ()
    back = store.edit_prompt("prompt-01", keep_recordings=True, text="Sentence one!")
    assert back.accepted_prompt_sha256s == (moved.prompt_sha256,)


def test_a_keep_recordings_edit_rewords_only_when_the_caller_vouches_for_the_recordings(
    store: CatalogEditor,
) -> None:
    """The typo-class bound is the store's rule, so every editor applies it."""
    first = add(store, "prompt-01", "Synthetic sentence number 0.")
    tone = add(
        store, "tone-01", None, category="room-tone", style="Silence.", instructions="Be still."
    )
    for dry_run in (True, False):
        with pytest.raises(CorpusStoreError, match="is a rewording") as caught:
            store.edit_prompt(
                "prompt-01",
                text="Synthetic sentence zero.",
                keep_recordings=True,
                dry_run=dry_run,
            )
        assert caught.value.code == "CORPUS.REWORDING_REFUSED"
        message = str(caught.value)
        assert '"Synthetic sentence number 0." -> "Synthetic sentence zero."' in message
        assert "add --allow-rewording if the recordings already say the new text" in message
    assert store.prompt_history("prompt-01") == (first,)
    # The identity rule is checked first: a spoken prompt never becomes silent coverage.
    with pytest.raises(CorpusStoreError, match="cannot change its held-out flag") as caught:
        store.edit_prompt(
            "prompt-01",
            text=None,
            category="room-tone",
            instructions="Be still.",
            keep_recordings=True,
            allow_rewording=True,
        )
    assert caught.value.code == "CORPUS.PROMPT_INVALID"
    with pytest.raises(ValueError, match="only qualifies a keep_recordings edit"):
        store.edit_prompt("prompt-01", text="Synthetic sentence zero.", allow_rewording=True)
    # A typo-class fix, a silent prompt's instructions and a default rewording need no word.
    typo = store.edit_prompt("prompt-01", text="Synthetic sentense number 0!", keep_recordings=True)
    assert typo.accepted_prompt_sha256s == (first.prompt_sha256,)
    silent = store.edit_prompt("tone-01", instructions="Stay silent now.", keep_recordings=True)
    assert silent.accepted_prompt_sha256s == (tone.prompt_sha256,)
    reworded = store.edit_prompt(
        "prompt-01", text="Synthetic sentence zero.", keep_recordings=True, allow_rewording=True
    )
    assert reworded.accepted_prompt_sha256s == tuple(
        sorted((first.prompt_sha256, typo.prompt_sha256))
    )
    assert store.edit_prompt("prompt-01", text="A new sentence entirely.").revision == 4


def test_every_live_prompt_must_fit_every_catalog_of_the_store(store: CatalogEditor) -> None:
    """``publish`` takes every live prompt, so the store refuses what a catalog cannot hold."""
    held = {"holdout": True}
    # Without catalogs any storable prompt is taken.
    add(store, "held-00", "Held sentence zero.", **held)
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts held-00;") as caught:
        store.create_catalog(CATALOG, purpose="capture-pilot")
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    assert str(caught.value).endswith(
        "every version publishes every live prompt (`voice-lab catalog publish`), so nothing "
        "was written"
    )
    # A retired prompt is never published again, so it constrains no new catalog.
    store.retire_prompt("held-00")
    store.create_catalog(CATALOG, purpose="capture-pilot")
    seeded(store, 1)
    unpublishable = "CORPUS.PROMPT_UNPUBLISHABLE"
    with pytest.raises(CorpusStoreError) as caught:
        add(store, "held-01", "Held sentence one.", **held)
    assert caught.value.code == unpublishable
    assert str(caught.value) == (
        "`voice-lab catalog publish` publishes every live prompt, and a catalog of this store "
        "could never publish held-01 (catalog cole-corpus is capture-pilot: prompt holdout "
        "flags must agree with suite purpose); nothing was written"
    )
    with pytest.raises(CorpusStoreError, match="only physical-test catalogs") as caught:
        add(store, "label-01", "A labelled sentence.", labels=("test",))
    assert caught.value.code == unpublishable
    for dry_run in (True, False):
        with pytest.raises(CorpusStoreError, match="could never publish prompt-01") as caught:
            store.edit_prompt("prompt-01", holdout=True, dry_run=dry_run)
        assert caught.value.code == unpublishable
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="evaluation",
        purpose="held-out-evaluation",
        prompts=tuple(
            Prompt(
                prompt_id=f"eval-{number:02d}",
                text=f"Evaluation sentence {number}.",
                category="neutral",
                style="Plain.",
                expected_seconds=3.0,
                holdout=True,
            )
            for number in range(12)
        ),
    )
    with pytest.raises(CorpusStoreError, match="eval-09 .* and 2 more; nothing") as caught:
        store.import_prompt_suite(suite, source="import")
    assert caught.value.code == unpublishable and "eval-10" not in str(caught.value)
    assert [r.prompt.prompt_id for r in store.list_prompts(include_retired=True)] == [
        "held-00",
        "prompt-01",
    ]
    assert len(store.prompt_history("prompt-01")) == 1
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts prompt-01") as caught:
        store.create_catalog("evaluation", purpose="held-out-evaluation")
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    # A duplicate id is named before the fit, as before.
    with pytest.raises(CorpusStoreError, match="already exists") as caught:
        store.create_catalog(CATALOG, purpose="held-out-evaluation")
    assert caught.value.code == "CORPUS.CATALOG_DUPLICATE"
    assert [record.catalog_id for record in store.list_catalogs()] == [CATALOG]


def test_republishing_reverted_content_names_the_recordings_that_version_does_not_accept(
    store: CatalogEditor,
) -> None:
    first, _ = seeded(store, 2)
    original = published(store)
    reworded = {"keep_recordings": True, "allow_rewording": True}
    store.edit_prompt("prompt-01", text="Sentence number one.", **reworded)
    store.edit_prompt("prompt-01", text=first.prompt.text, **reworded)
    # The reverted suite is version 1's content, which accepts no earlier recording.
    with pytest.raises(CorpusStoreError, match=f"catalog {CATALOG} version 1, which") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.VERSION_UNCHANGED"
    assert "other earlier recordings of prompt-01 than the current revisions" in str(caught.value)
    assert "prompt-02" not in str(caught.value)
    assert store.materialize(original).prompt_aliases == ()
    # Changing another prompt publishes the revert with its accepted recordings.
    store.edit_prompt("prompt-02", text="Sentence number two!")
    second = store.materialize(store.publish_version(CATALOG))
    assert second.current_prompt_for(first.prompt) == first.prompt
    assert len(second.prompt_aliases) == 1


def test_republishing_names_at_most_ten_prompts_whose_accepted_recordings_differ(
    store: CatalogEditor,
) -> None:
    records = seeded(store, 12)
    published(store)
    for record in records:
        store.edit_prompt(record.prompt.prompt_id, text="Edited " + str(record.prompt.text))
        store.edit_prompt(
            record.prompt.prompt_id,
            text=str(record.prompt.text),
            keep_recordings=True,
            allow_rewording=True,
        )
    with pytest.raises(CorpusStoreError, match="prompt-10 and 2 more than") as caught:
        store.publish_version(CATALOG)
    assert "prompt-01, prompt-02" in str(caught.value) and "prompt-11" not in str(caught.value)


def test_retired_prompt_leaves_new_versions_and_refuses_edits(store: CatalogEditor) -> None:
    seeded(store, 2)
    before = published(store)
    retired = store.retire_prompt("prompt-01")
    assert retired.retired_at is not None
    aware(retired.created_at, retired.retired_at)
    assert retired.created_at < retired.retired_at
    assert [record.prompt.prompt_id for record in store.list_prompts()] == ["prompt-02"]
    assert [record.prompt.prompt_id for record in store.list_prompts(include_retired=True)] == [
        "prompt-01",
        "prompt-02",
    ]
    for call in (
        lambda: store.retire_prompt("prompt-01"),
        lambda: store.edit_prompt("prompt-01", text="Changed."),
        lambda: store.publish_version(CATALOG, prompt_ids=("prompt-01",)),
    ):
        with pytest.raises(CorpusStoreError, match="retired") as caught:
            call()
        assert caught.value.code == "CORPUS.PROMPT_RETIRED"
    after = store.publish_version(CATALOG)
    assert after.catalog_version == 2
    assert [p.prompt_id for p in store.materialize(after).suite.prompts] == ["prompt-02"]
    assert [p.prompt_id for p in store.materialize(before).suite.prompts] == [
        "prompt-01",
        "prompt-02",
    ]


def test_import_seeds_a_suite_in_order_or_nothing_at_all(store: CatalogEditor) -> None:
    builtin = load_builtin_prompt_suite("evaluation-v1.json")
    assert store.import_prompt_suite(builtin, source="import") == tuple(
        prompt.prompt_id for prompt in builtin.prompts
    )
    records = store.list_prompts()
    assert [record.prompt for record in records] == list(builtin.prompts)
    assert {record.source for record in records} == {"import"}
    fresh = builtin.prompts[0].model_copy(
        update={"prompt_id": "fresh-01", "text": "A sentence nobody has yet.", "holdout": False}
    )
    for prompts, message in (
        ((fresh, builtin.prompts[1].model_copy(update={"holdout": False})), "already exists"),
        ((fresh, fresh.model_copy(update={"prompt_id": "fresh-02"})), "repeats the text"),
    ):
        second = PromptSuite(suite_id="second", purpose="capture-pilot", prompts=prompts)
        with pytest.raises(CorpusStoreError, match=message) as caught:
            store.import_prompt_suite(second, source="import")
        assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
        assert store.list_prompts() == records


# -- catalogs and versions -------------------------------------------------------------


def test_create_publish_activate_materialize_round_trip_equals_the_contract(
    layout: AssetLayout,
    store: CatalogEditor,
) -> None:
    records = seeded(store, 3)
    catalog = store.create_catalog(CATALOG, purpose="capture-corpus", description="the corpus")
    assert (catalog.catalog_id, catalog.suite_id, catalog.purpose) == (
        CATALOG,
        CATALOG,
        "capture-corpus",
    )
    assert catalog.description == "the corpus"
    assert catalog.retired_at is None
    assert catalog.versions == ()
    reference = store.publish_version(CATALOG)
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id=CATALOG,
        purpose="capture-corpus",
        prompts=tuple(record.prompt for record in records),
    )
    digest = canonical_sha256(suite)
    snapshot = CatalogSnapshot(suite=suite, catalog_sha256=digest, prompt_aliases=())
    payload = canonical_json_bytes(snapshot) + b"\n"
    assert reference == CatalogReference(
        catalog_id=CATALOG,
        catalog_version=1,
        catalog_sha256=digest,
        snapshot_sha256=file_sha256(payload),
        suite_id=CATALOG,
        purpose="capture-corpus",
        snapshot_path=catalog_snapshot_path(digest),
    )
    store.activate(SUBJECT, reference)
    assert store.active_catalog(SUBJECT) == (suite, reference)
    assert store.materialize(reference) == snapshot
    assert read_catalog_snapshot(layout, reference) == snapshot
    path = snapshot_file(layout, reference)
    assert path == snapshot_directory(layout) / f"{digest}.json"
    assert mode(path) == 0o600
    assert path.read_bytes() == payload
    assert sorted(snapshot_directory(layout).iterdir()) == [path]
    assert store.resolve_reference(CATALOG) == reference
    assert store.resolve_reference(CATALOG, 1) == reference
    assert store.list_catalogs()[0].versions == (reference,)


def test_catalog_creation_validates_ids_purpose_description_and_uniqueness(
    store: CatalogEditor,
) -> None:
    for fields, message in (
        ({"catalog_id": "Bad Id", "purpose": "capture-corpus"}, "catalog id"),
        ({"catalog_id": "ok", "suite_id": "@", "purpose": "capture-corpus"}, "suite id"),
        ({"catalog_id": "ok", "purpose": "karaoke"}, "purpose"),
        (
            {"catalog_id": "ok", "purpose": "capture-corpus", "description": "x" * 1025},
            "description",
        ),
    ):
        with pytest.raises(CorpusStoreError, match=message) as caught:
            store.create_catalog(**fields)
        assert caught.value.code == "CORPUS.CATALOG_INVALID"
    explicit = store.create_catalog("mirror", suite_id="cole-pilot-v2", purpose="capture-pilot")
    assert explicit.suite_id == "cole-pilot-v2"
    with pytest.raises(CorpusStoreError, match="already exists") as caught:
        store.create_catalog("mirror", purpose="capture-corpus")
    assert caught.value.code == "CORPUS.CATALOG_DUPLICATE"
    assert [catalog.catalog_id for catalog in store.list_catalogs()] == ["mirror"]


def test_unknown_catalog_and_version_are_named(store: CatalogEditor) -> None:
    for call in (
        lambda: store.publish_version("absent"),
        lambda: store.resolve_reference("absent"),
    ):
        with pytest.raises(CorpusStoreError, match="unknown catalog: absent") as caught:
            call()
        assert caught.value.code == "CORPUS.CATALOG_UNKNOWN"
    store.create_catalog(CATALOG, purpose="capture-corpus")
    with pytest.raises(CorpusStoreError, match="no published version") as caught:
        store.resolve_reference(CATALOG)
    assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    seeded(store, 1)
    reference = store.publish_version(CATALOG)
    with pytest.raises(CorpusStoreError, match="has no version 2") as caught:
        store.resolve_reference(CATALOG, 2)
    assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    later = reference.model_copy(update={"catalog_version": 2})
    with pytest.raises(CorpusStoreError, match="has no version 2") as caught:
        store.materialize(later)
    assert caught.value.code == "CORPUS.VERSION_UNKNOWN"
    elsewhere = reference.model_copy(update={"catalog_id": "absent"})
    with pytest.raises(CorpusStoreError, match="unknown catalog: absent"):
        store.activate(SUBJECT, elsewhere)


def test_publishing_validates_selection_and_the_materialized_suite(
    layout: AssetLayout, store: CatalogEditor
) -> None:
    store.create_catalog(CATALOG, purpose="capture-corpus")
    with pytest.raises(CorpusStoreError, match="no prompts to publish") as caught:
        store.publish_version(CATALOG)
    assert caught.value.code == "CORPUS.EMPTY_CATALOG"
    seeded(store, 2)
    with pytest.raises(CorpusStoreError, match="listed more than once") as caught:
        store.publish_version(CATALOG, prompt_ids=("prompt-01", "prompt-01"))
    assert caught.value.code == "CORPUS.PROMPT_DUPLICATE"
    with pytest.raises(CorpusStoreError, match="unknown prompt: absent") as caught:
        store.publish_version(CATALOG, prompt_ids=("prompt-01", "absent"))
    assert caught.value.code == "CORPUS.PROMPT_UNKNOWN"
    with pytest.raises(CorpusStoreError, match="cannot hold the live prompts") as caught:
        store.create_catalog("held-out", purpose="held-out-evaluation")
    assert caught.value.code == "CORPUS.CATALOG_UNPUBLISHABLE"
    assert store.list_catalogs()[0].versions == ()
    assert list(snapshot_directory(layout).iterdir()) == []


def test_publishing_refuses_a_suite_one_byte_over_what_the_app_receives_in_one_message(
    layout: AssetLayout,
    store: CatalogEditor,
) -> None:
    from test_recording_contracts import catalog_of_exactly

    from cole_voice_lab.recording_contracts import MAX_CATALOG_SUITE_BYTES

    exact = catalog_of_exactly(MAX_CATALOG_SUITE_BYTES)
    pad = exact.prompts[-1]
    store.import_prompt_suite(exact, source="manual")
    # The same pad one character longer, under an id of the same length.
    assert pad.text is not None
    add(store, "shape-pae", pad.text + "p", style=pad.style, expected_seconds=6.3)
    store.create_catalog(exact.suite_id, purpose=exact.purpose)
    shaped = tuple(prompt.prompt_id for prompt in exact.prompts[:-1])
    with pytest.raises(CorpusStoreError, match="app receives in one message") as caught:
        store.publish_version(exact.suite_id, prompt_ids=(*shaped, "shape-pae"))
    assert caught.value.code == "CORPUS.CATALOG_INVALID"
    assert f"would be {MAX_CATALOG_SUITE_BYTES + 1} bytes with {len(exact.prompts)}" in str(
        caught.value
    )
    assert store.list_catalogs()[0].versions == ()
    assert list(snapshot_directory(layout).iterdir()) == []
    reference = store.publish_version(exact.suite_id, prompt_ids=(*shaped, pad.prompt_id))
    assert store.materialize(reference).suite == exact
    assert reference.catalog_sha256 == canonical_sha256(exact)


def test_explicit_prompt_ids_set_the_order_and_identical_content_is_not_republished(
    store: CatalogEditor,
) -> None:
    seeded(store, 3)
    store.create_catalog(CATALOG, purpose="capture-corpus")
    first = store.publish_version(CATALOG, prompt_ids=("prompt-03", "prompt-01"))
    assert [p.prompt_id for p in store.materialize(first).suite.prompts] == [
        "prompt-03",
        "prompt-01",
    ]
    with pytest.raises(CorpusStoreError, match=f"catalog {CATALOG} version 1") as caught:
        store.publish_version(CATALOG, prompt_ids=("prompt-03", "prompt-01"))
    assert caught.value.code == "CORPUS.VERSION_UNCHANGED"
    second = store.publish_version(CATALOG, prompt_ids=("prompt-01", "prompt-03"))
    assert second.catalog_version == 2
    assert second.catalog_sha256 != first.catalog_sha256
    store.create_catalog("twin", suite_id=CATALOG, purpose="capture-corpus")
    with pytest.raises(CorpusStoreError, match=f"catalog {CATALOG} version 2") as caught:
        store.publish_version("twin", prompt_ids=("prompt-01", "prompt-03"))
    assert caught.value.code == "CORPUS.VERSION_UNCHANGED"
    assert store.resolve_reference(CATALOG) == second
    assert store.list_catalogs()[0].versions == (first, second)


def test_default_edit_changes_the_catalog_digest_and_keep_edits_publish_aliases(
    store: CatalogEditor,
) -> None:
    first, second, _ = seeded(store, 3)
    before = published(store)
    edited = store.edit_prompt("prompt-01", text="Sentence number 1!")
    after = store.publish_version(CATALOG)
    assert after.catalog_sha256 != before.catalog_sha256
    assert store.materialize(after).prompt_aliases == ()
    kept = store.edit_prompt("prompt-02", text="Sentence number 2!", keep_recordings=True)
    kept_again = store.edit_prompt("prompt-02", text="Sentence number 2?", keep_recordings=True)
    third = store.publish_version(CATALOG)
    snapshot = store.materialize(third)
    assert snapshot.prompt_aliases == tuple(
        PromptAlias(prompt_sha256=kept_again.prompt_sha256, accepted_prompt_sha256=accepted)
        for accepted in sorted((second.prompt_sha256, kept.prompt_sha256))
    )
    assert snapshot.accepted_hashes(kept_again.prompt_sha256) == {
        kept_again.prompt_sha256,
        kept.prompt_sha256,
        second.prompt_sha256,
    }
    assert snapshot.current_for(second.prompt_sha256) == kept_again.prompt_sha256
    assert snapshot.current_for(first.prompt_sha256) is None
    assert snapshot.current_for(edited.prompt_sha256) == edited.prompt_sha256
    assert store.materialize(before).prompt_aliases == ()


def test_activation_needs_a_published_readable_version_and_a_valid_subject(
    store: CatalogEditor,
) -> None:
    with pytest.raises(CorpusStoreError, match="no active catalog") as caught:
        store.active_catalog(SUBJECT)
    assert caught.value.code == "CORPUS.NO_ACTIVE_CATALOG"
    assert "voice-lab catalog init" in str(caught.value)
    seeded(store, 2)
    first = published(store)
    with pytest.raises(CorpusStoreError, match="subject id") as caught:
        store.activate("Not Valid", first)
    assert caught.value.code == "CORPUS.SUBJECT_INVALID"
    store.activate(SUBJECT, first)
    store.retire_prompt("prompt-02")
    second = store.publish_version(CATALOG)
    assert store.active_catalog(SUBJECT)[1] == first
    store.activate(SUBJECT, second)
    assert store.active_catalog(SUBJECT)[1] == second
    store.activate("other-subject", first)
    assert store.active_catalog("other-subject")[1] == first


# -- snapshots -----------------------------------------------------------------------


def test_reference_that_disagrees_with_the_published_version_fails_closed(
    layout: AssetLayout,
    store: CatalogEditor,
) -> None:
    seeded(store, 1)
    reference = published(store)
    forged = reference.model_copy(update={"suite_id": "other"})
    with pytest.raises(CorpusSnapshotSecurityError, match="disagrees with the published") as caught:
        store.materialize(forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert isinstance(caught.value, SecurityError)
    with pytest.raises(CorpusSnapshotSecurityError, match="not other/capture-corpus") as caught:
        read_catalog_snapshot(layout, forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


# -- hash stability against the released built-in -------------------------------------


def test_seeded_pilot_v2_spoken_prompts_hash_exactly_like_the_built_in(
    store: CatalogEditor,
) -> None:
    builtin = load_builtin_prompt_suite("pilot-v2.json")
    spoken = [prompt for prompt in builtin.prompts if prompt.spoken]
    assert len(spoken) == 34
    for prompt in spoken:
        record = store.add_prompt(
            prompt.prompt_id,
            text=prompt.text,
            category=prompt.category,
            style=prompt.style,
            expected_seconds=prompt.expected_seconds,
            reference_candidate=prompt.reference_candidate,
            holdout=prompt.holdout,
            source="pilot-v2",
        )
        assert record.prompt == prompt
        assert record.prompt_sha256 == canonical_sha256(prompt)
    assert len(store.list_prompts()) == 34


def test_publishing_every_pilot_v2_prompt_reproduces_the_released_catalog_digest(
    store: CatalogEditor,
) -> None:
    builtin = load_builtin_prompt_suite("pilot-v2.json")
    assert len(builtin.prompts) == 36
    assert store.import_prompt_suite(builtin, source="pilot-v2") == tuple(
        prompt.prompt_id for prompt in builtin.prompts
    )
    store.create_catalog("pilot-v2-mirror", suite_id=builtin.suite_id, purpose=builtin.purpose)
    reference = store.publish_version("pilot-v2-mirror")
    assert reference.catalog_sha256 == BUILTIN_PROMPT_SUITE_SHA256["pilot-v2.json"]
    assert reference.suite_id == "cole-pilot-v2"
    snapshot = store.materialize(reference)
    assert snapshot.suite == builtin
    assert canonical_json_bytes(snapshot.suite) == canonical_json_bytes(builtin)


# -- concurrency ------------------------------------------------------------------------


def _publish_from_child(root: str, prompt_id: str, barrier: Any, results: Any) -> None:
    layout = AssetLayout.validate(Path(root))
    try:
        with open_catalog_editor(layout) as store:
            barrier.wait(timeout=60)
            reference = store.publish_version(CATALOG, prompt_ids=("shared-01", prompt_id))
            results.put(("published", reference.catalog_version, reference.catalog_sha256))
    except Exception as error:  # The parent asserts on whatever happened.
        results.put(("failed", repr(error)))


def race_publishers(layout: AssetLayout) -> list[Any]:
    """Publish version 1 of ``CATALOG`` into a new store, then race two publishing processes.

    Each spawned child opens the store before a shared barrier and publishes ``shared-01``
    plus its own prompt right after it.
    Returns: each child's outcome, ``("published", version, catalog_sha256)`` or
    ``("failed", repr)``.
    """
    with open_catalog_editor(layout, create=True) as store:
        add(store, "shared-01", "A sentence both children publish.")
        add(store, "left-01", "The left child's sentence.")
        add(store, "right-01", "The right child's sentence.")
        store.create_catalog(CATALOG, purpose="capture-corpus")
        store.publish_version(CATALOG, prompt_ids=("shared-01",))
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    children = [
        context.Process(
            target=_publish_from_child, args=(str(layout.root), prompt_id, barrier, results)
        )
        for prompt_id in ("left-01", "right-01")
    ]
    for child in children:
        child.start()
    outcomes = [results.get(timeout=120) for _ in children]
    for child in children:
        child.join(timeout=120)
        assert child.exitcode == 0
    return outcomes


def test_two_processes_publishing_at_once_queue_on_the_lock_and_get_consecutive_versions(
    layout: AssetLayout,
) -> None:
    outcomes = race_publishers(layout)
    assert sorted(outcome[0] for outcome in outcomes) == ["published", "published"]
    assert sorted(outcome[1] for outcome in outcomes) == [2, 3]
    assert len({outcome[2] for outcome in outcomes}) == 2
    with open_catalog_editor(layout) as store:
        versions = store.list_catalogs()[0].versions
        assert [reference.catalog_version for reference in versions] == [1, 2, 3]
        assert len({reference.catalog_sha256 for reference in versions}) == 3
        for reference in versions:
            store.materialize(reference)
