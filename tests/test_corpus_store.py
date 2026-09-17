"""Catalog logic every store shares: spoken words, fit, typo class, and the snapshot readers.

The file-only readers (``read_catalog_snapshot``, ``resolve_group_catalog``,
``GroupCatalogCache``) are checked against snapshots a store published; the store is opened
through ``storage_binding`` (``catalog_conformance``'s fixtures).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from catalog_conformance import (
    CATALOG,
    EPOCH,
    SUBJECT,
    published,
    seeded,
    snapshot_directory,
    snapshot_file,
)
from catalog_conformance import layout as layout
from catalog_conformance import store as store

import cole_voice_lab.catalog_ports as catalog_ports
import cole_voice_lab.corpus_store as corpus_store
from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.catalog_ports import (
    CatalogEditor,
    CorpusSnapshotSecurityError,
    CorpusStoreError,
)
from cole_voice_lab.corpus_store import (
    GroupCatalogCache,
    read_catalog_snapshot,
    require_referenced_snapshot,
    resolve_group_catalog,
)
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import BUILTIN_PROMPT_SUITE_SHA256, load_builtin_prompt_suite
from cole_voice_lab.recording_contracts import (
    RECORDING_GROUP_V1,
    RECORDING_GROUP_V2,
    CatalogReference,
    CatalogSnapshot,
    PreparedCaptureGate,
    RecordingGroup,
)

# -- spoken words, fit, catalog definitions and the typo class --------------------------


def test_spoken_words_ignore_case_punctuation_and_spacing() -> None:
    assert corpus_store.spoken_words("  It's   the BIRCH canoe!  ") == (
        "its",
        "the",
        "birch",
        "canoe",
    )
    assert corpus_store.spoken_words("Café, NAÏVE déjà vu.") == ("café", "naïve", "déjà", "vu")
    assert corpus_store.spoken_words("snake_case & 42") == ("snakecase", "42")
    assert corpus_store.spoken_words("...") == ()


def test_prompt_catalog_fit_names_each_purpose_that_refuses_a_prompt() -> None:
    spoken = Prompt(
        prompt_id="held-01",
        text="A held out sentence.",
        category="neutral",
        style="Plain.",
        expected_seconds=2.0,
        holdout=True,
    )
    fit = corpus_store.prompt_catalog_fit(spoken)
    assert list(fit.suites) == ["held-out-evaluation"] and fit.holds("held-out-evaluation")
    assert not fit.holds("capture-pilot") and not fit.holds("karaoke")
    assert fit.suites["held-out-evaluation"].prompts == (spoken,)
    assert fit.explanation() == (
        "capture-pilot, capture-corpus, physical-test: prompt holdout flags must agree with "
        "suite purpose"
    )
    tested = spoken.model_copy(update={"holdout": False, "labels": ("test",)})
    only = corpus_store.prompt_catalog_fit(tested, ("capture-pilot", "physical-test", "karaoke"))
    assert list(only.suites) == ["physical-test"]
    assert only.suites["physical-test"].suite_id.startswith("cole-physical-test-")
    assert only.problems == {
        "capture-pilot": "only physical-test catalogs may use the test label or procedures"
    }


def test_catalog_definitions_refuse_a_mismatched_prefix_and_a_terminal_unsafe_description() -> None:
    definition = corpus_store.catalog_definition(
        "cole-corpus", purpose="capture-pilot", description="Living corpus · v2"
    )
    assert definition == catalog_ports.CatalogDefinition(
        "cole-corpus", "cole-corpus", "capture-pilot", "Living corpus · v2"
    )
    physical = corpus_store.catalog_definition(
        "bench", suite_id="cole-physical-test-bench", purpose="physical-test"
    )
    assert physical.suite_id == "cole-physical-test-bench"
    for fields, message in (
        ({"catalog_id": "bench", "purpose": "physical-test"}, "cannot name a physical-test"),
        (
            {"catalog_id": "cole-physical-test-x", "purpose": "capture-pilot"},
            "cannot name a capture-pilot",
        ),
        ({"catalog_id": "c", "purpose": "capture-pilot", "description": "a\nb"}, "control"),
        (
            {"catalog_id": "c", "purpose": "capture-pilot", "description": "safe\x1b]0;x\x07"},
            "control",
        ),
        ({"catalog_id": "c", "purpose": "capture-pilot", "description": "a\u2028b"}, "separator"),
        ({"catalog_id": "c", "purpose": "capture-pilot", "description": "a\u200eb"}, "format"),
    ):
        with pytest.raises(CorpusStoreError, match=message) as caught:
            corpus_store.catalog_definition(**fields)
        assert caught.value.code == "CORPUS.CATALOG_INVALID"
    with pytest.raises(CorpusStoreError, match="subject id") as subject:
        corpus_store.validated_subject_id("Cole Self")
    assert subject.value.code == "CORPUS.SUBJECT_INVALID"
    assert corpus_store.validated_subject_id(SUBJECT) == SUBJECT


@pytest.mark.parametrize(
    ("before", "after", "words", "characters"),
    [
        ("The birch canoe.", "the Birch canoe!", 0, 0),
        ("It's easy.", "Its  easy", 0, 0),
        ("Four words right here.", "Four words right there.", 1, 1),
        ("The smoth planks.", "The smooth planks.", 1, 1),
        ("Send it to me.", "Send it me.", 1, 3),
        ("Colour me red.", "Color me red.", 1, 1),
        ("Synthetic sentence number 0.", "Synthetic sentence zero.", 2, None),
        ("A cat sat.", "A dog sat.", 1, 3),
        ("A cat sat.", "A horse sat.", 1, None),
        ("One two three.", "Three two one.", 2, None),
        ("One.", "One two three four.", None, None),
        ("A long sentence about birds.", "Bananas are yellow.", None, None),
    ],
)
def test_text_changes_are_measured_on_the_words_read_aloud(
    before: str, after: str, words: int | None, characters: int | None
) -> None:
    change = corpus_store.text_change(before, after)
    assert (change.before, change.after) == (before, after)
    assert (change.word_edits, change.character_edits) == (words, characters)
    assert change.typo_class is (words is not None and characters is not None)
    assert change.spoken_words_unchanged is (words == 0)


def _levenshtein(first: str, second: str) -> int:
    previous = list(range(len(second) + 1))
    for row, left in enumerate(first, 1):
        current = [row]
        for column, right in enumerate(second, 1):
            current.append(
                min(previous[column] + 1, current[-1] + 1, previous[column - 1] + (left != right))
            )
        previous = current
    return previous[-1]


def test_the_banded_edit_distance_agrees_with_the_full_one_within_its_band() -> None:
    import random

    generator = random.Random(20260916)
    for _ in range(3000):
        first = "".join(generator.choice("ab ") for _ in range(generator.randint(0, 9)))
        second = "".join(generator.choice("ab ") for _ in range(generator.randint(0, 9)))
        limit = generator.randint(0, 4)
        exact = _levenshtein(first, second)
        expected = exact if exact <= limit else None
        assert corpus_store._edits_within(first, second, limit) == expected, (first, second)


# -- the file-only snapshot reader ------------------------------------------------------


def test_file_reader_refuses_symlinks_links_open_modes_and_oversized_files(
    layout: AssetLayout, store: CatalogEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(store, 1)
    reference = published(store)
    path = snapshot_file(layout, reference)
    linked = tmp_path / "hard-link.json"
    os.link(path, linked)
    with pytest.raises(CorpusSnapshotSecurityError, match="owned private regular file") as caught:
        read_catalog_snapshot(layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    linked.unlink()
    path.chmod(0o644)
    with pytest.raises(CorpusSnapshotSecurityError, match="owned private regular file"):
        read_catalog_snapshot(layout, reference)
    path.chmod(0o600)
    monkeypatch.setattr(corpus_store, "MAX_CATALOG_SNAPSHOT_BYTES", 16)
    with pytest.raises(CorpusSnapshotSecurityError, match="exceeds its byte limit"):
        read_catalog_snapshot(layout, reference)
    monkeypatch.undo()
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(CorpusSnapshotSecurityError, match="symlink component") as caught:
        read_catalog_snapshot(layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(CorpusSnapshotSecurityError, match="not a regular file") as caught:
        read_catalog_snapshot(layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    path.rmdir()
    path.write_bytes(outside.read_bytes())
    path.chmod(0o000)
    with pytest.raises(CorpusSnapshotSecurityError, match="cannot be opened") as caught:
        read_catalog_snapshot(layout, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    path.chmod(0o600)
    assert read_catalog_snapshot(layout, reference).catalog_sha256 == reference.catalog_sha256
    other_root = AssetLayout.initialize(tmp_path / "other-assets")
    (other_root.root / "provenance" / "corpus").mkdir(mode=0o700)
    (other_root.root / "provenance" / "corpus" / "snapshots").symlink_to(snapshot_directory(layout))
    with pytest.raises(CorpusSnapshotSecurityError, match="symlink component") as caught:
        read_catalog_snapshot(other_root, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    (other_root.root / "provenance" / "corpus" / "snapshots").unlink()
    (other_root.root / "provenance" / "corpus" / "snapshots").write_bytes(b"")
    with pytest.raises(CorpusSnapshotSecurityError, match="cannot be examined") as caught:
        read_catalog_snapshot(other_root, reference)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


def _swap_after_lstat(
    monkeypatch: pytest.MonkeyPatch, target: Path, swap: Callable[[Path], None]
) -> None:
    """Replace ``target`` right after the reader's own lstat of it, before its open."""
    real_lstat = os.lstat

    def lstat_then_swap(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        metadata = real_lstat(path, *args, **kwargs)
        # Path.lstat in confined_path's walk also lands here; only the reader's own call
        # (made from the corpus_store module) is the check whose window is under test.
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller == corpus_store.__name__ and Path(path) == target:
            monkeypatch.setattr(corpus_store.os, "lstat", real_lstat)
            swap(target)
        return metadata

    monkeypatch.setattr(corpus_store.os, "lstat", lstat_then_swap)


def _swap_to_fifo(path: Path) -> None:
    path.unlink()
    os.mkfifo(path, 0o600)


def _swap_to_symlink(path: Path) -> None:
    outside = path.parent.parent / "swapped-outside.json"
    outside.write_bytes(path.read_bytes())
    outside.chmod(0o600)
    path.unlink()
    path.symlink_to(outside)


@pytest.mark.parametrize(
    ("swap", "code", "message"),
    [
        (_swap_to_fifo, "CORPUS.SNAPSHOT_MISMATCH", "owned private regular file"),
        (Path.unlink, "CORPUS.SNAPSHOT_MISSING", "is missing"),
        (_swap_to_symlink, "CORPUS.SNAPSHOT_MISMATCH", "cannot be opened"),
    ],
    ids=["fifo", "deleted", "symlink"],
)
def test_snapshot_swapped_between_the_file_readers_check_and_open_fails_closed(
    layout: AssetLayout,
    store: CatalogEditor,
    monkeypatch: pytest.MonkeyPatch,
    swap: Callable[[Path], None],
    code: str,
    message: str,
) -> None:
    seeded(store, 1)
    reference = published(store)
    _swap_after_lstat(monkeypatch, snapshot_file(layout, reference), swap)
    with pytest.raises(CorpusSnapshotSecurityError, match=message) as caught:
        read_catalog_snapshot(layout, reference)
    assert caught.value.code == code


@pytest.mark.parametrize("snapshot_path", ["/etc/hosts", "../../../../../etc/hosts"])
def test_unvalidated_reference_with_an_escaping_path_is_a_named_mismatch(
    layout: AssetLayout, store: CatalogEditor, snapshot_path: str
) -> None:
    seeded(store, 1)
    forged = published(store).model_copy(update={"snapshot_path": snapshot_path})
    with pytest.raises(CorpusSnapshotSecurityError, match="cannot be examined") as caught:
        read_catalog_snapshot(layout, forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


# -- the group catalog reader --------------------------------------------------------------


def group_for(catalog: PromptSuite | CatalogReference) -> RecordingGroup:
    """A capture group that embeds ``catalog`` (v1) or references it (v2)."""
    embedded = isinstance(catalog, PromptSuite)
    purpose = catalog.purpose
    return RecordingGroup(
        schema_version=RECORDING_GROUP_V1 if embedded else RECORDING_GROUP_V2,
        group_id="group-synthetic",
        context_revision=0,
        subject_id=SUBJECT,
        catalog=catalog if embedded else None,
        catalog_reference=None if embedded else catalog,
        catalog_sha256=canonical_sha256(catalog) if embedded else catalog.catalog_sha256,
        capture_device=CaptureDevice(
            backend="coreaudio", device_id="synthetic", display_name="Synthetic UMIK-1"
        ),
        capture_host="synthetic-host",
        opened_at=EPOCH,
        reason="app-start",
        gate=PreparedCaptureGate(
            state="unprepared" if purpose == "capture-corpus" else "not-required"
        ),
    )


def test_group_catalog_is_the_embedded_suite_or_the_referenced_snapshot_without_sqlite(
    layout: AssetLayout, store: CatalogEditor, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = seeded(store, 2)
    store.edit_prompt("prompt-01", text="Sentence number 1!", keep_recordings=True)
    reference = published(store)
    published_snapshot = store.materialize(reference)
    builtin = load_builtin_prompt_suite("pilot-v2.json")

    def no_database(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the group catalog reader opened a database")

    monkeypatch.setattr(sqlite3, "connect", no_database)
    embedded = resolve_group_catalog(layout, group_for(builtin))
    assert embedded == CatalogSnapshot.embedded(builtin)
    assert embedded.prompt_aliases == () and embedded.suite == builtin
    assert embedded.catalog_sha256 == BUILTIN_PROMPT_SUITE_SHA256["pilot-v2.json"]
    referenced = resolve_group_catalog(layout, group_for(reference))
    assert referenced == published_snapshot and referenced.prompt_aliases
    current = referenced.prompt("prompt-01")
    assert current is not None and current.text == "Sentence number 1!"
    # The recorded first revision counts for the current prompt under the same id only.
    assert referenced.current_prompt_for(first.prompt) == current
    assert referenced.current_prompt_for(current) == current
    assert referenced.current_prompt_for(first.prompt.model_copy(update={"prompt_id": "x"})) is None
    assert referenced.prompt("prompt-99") is None
    assert embedded.current_prompt_for(first.prompt) is None


def test_group_catalog_reference_fails_closed_when_its_snapshot_is_missing_or_altered(
    layout: AssetLayout,
    store: CatalogEditor,
) -> None:
    seeded(store, 2)
    reference = published(store)
    group = group_for(reference)
    path = snapshot_file(layout, reference)
    original = path.read_bytes()
    path.write_bytes(original.replace(b"Sentence number 1.", b"Sentence number 7."))
    with pytest.raises(CorpusSnapshotSecurityError, match="published bytes") as caught:
        resolve_group_catalog(layout, group)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert isinstance(caught.value, SecurityError)
    path.unlink()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing") as caught:
        resolve_group_catalog(layout, group)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"
    assert isinstance(caught.value, SecurityError)
    path.write_bytes(original)
    path.chmod(0o600)
    assert resolve_group_catalog(layout, group) == store.materialize(reference)


def test_an_in_memory_snapshot_must_be_exactly_the_file_its_reference_seals(
    store: CatalogEditor,
) -> None:
    seeded(store, 2)
    first = published(store)
    store.edit_prompt("prompt-01", text="Sentence number 1!", keep_recordings=True)
    second = store.publish_version(CATALOG)
    one, two = store.materialize(first), store.materialize(second)
    assert one.prompt_aliases == () and two.prompt_aliases
    require_referenced_snapshot(one, first)
    require_referenced_snapshot(two, second)
    # A plain suite stands for a published version only when that version accepts no alias.
    require_referenced_snapshot(CatalogSnapshot.embedded(one.suite), first)
    renamed = first.model_copy(update={"suite_id": "another-suite"})
    repurposed = first.model_copy(update={"purpose": "capture-pilot"})
    for snapshot, reference in (
        (CatalogSnapshot.embedded(two.suite), second),  # its aliases would be dropped
        (two, first),
        (one, second),
        (one, renamed),
        (one, repurposed),
    ):
        with pytest.raises(CorpusSnapshotSecurityError) as caught:
            require_referenced_snapshot(snapshot, reference)
        assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
        assert isinstance(caught.value, SecurityError)


def test_group_catalog_cache_verifies_each_catalog_once_and_evicts_the_oldest(
    layout: AssetLayout, store: CatalogEditor, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="at least one"):
        GroupCatalogCache(layout, capacity=0)
    seeded(store, 2)
    reference = published(store)
    referenced, embedded = (
        group_for(reference),
        group_for(load_builtin_prompt_suite("pilot-v2.json")),
    )
    # An embedded suite and a snapshot of it never share an entry; the sealed reference counts.
    assert GroupCatalogCache.key(embedded) == (embedded.catalog_sha256, None)
    assert GroupCatalogCache.key(referenced) == (reference.catalog_sha256, reference)
    reads: list[str] = []
    real = corpus_store.resolve_group_catalog

    def counted(layout: Any, group: RecordingGroup) -> CatalogSnapshot:
        reads.append(group.catalog_sha256)
        return real(layout, group)

    monkeypatch.setattr(corpus_store, "resolve_group_catalog", counted)
    cache = GroupCatalogCache(layout, capacity=1)
    assert cache.resolve(referenced) == store.materialize(reference)
    snapshot_file(layout, reference).unlink()
    # Verified bytes are served from the cache; a new owner detects the missing file.
    assert cache.resolve(referenced).catalog_sha256 == reference.catalog_sha256
    assert reads == [reference.catalog_sha256]
    with pytest.raises(CorpusSnapshotSecurityError, match="missing"):
        GroupCatalogCache(layout).resolve(referenced)
    assert cache.resolve(embedded).prompt_aliases == ()
    with pytest.raises(CorpusSnapshotSecurityError, match="missing"):
        cache.resolve(referenced)  # evicted by the embedded catalog, so read again
    assert cache.resolve(embedded).catalog_sha256 == embedded.catalog_sha256
    assert reads.count(embedded.catalog_sha256) == 1


def test_a_reference_resolved_before_its_group_exists_shares_the_group_entry(
    layout: AssetLayout, store: CatalogEditor, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(store, 2)
    reference = published(store)
    reads: list[CatalogReference] = []
    real = corpus_store.read_catalog_snapshot

    def counted(layout: Any, named: CatalogReference) -> CatalogSnapshot:
        reads.append(named)
        return real(layout, named)

    monkeypatch.setattr(corpus_store, "read_catalog_snapshot", counted)
    cache = GroupCatalogCache(layout)
    missing = reference.model_copy(update={"catalog_version": 2})
    snapshot = cache.resolve_reference(reference)
    assert snapshot == store.materialize(reference)
    # The group minted on that reference is served from the same entry.
    assert cache.resolve(group_for(reference)) is snapshot
    assert cache.resolve_reference(reference) is snapshot
    assert reads == [reference]
    forged = reference.model_copy(update={"suite_id": "another-suite"})
    with pytest.raises(CorpusSnapshotSecurityError, match="declares suite"):
        cache.resolve_reference(forged)
    snapshot_file(layout, reference).unlink()
    # A failed read caches nothing; a verified entry keeps its checked bytes.
    for _ in range(2):
        with pytest.raises(CorpusSnapshotSecurityError, match="missing"):
            cache.resolve_reference(missing)
    assert reads == [reference, forged, missing, missing]
    assert cache.resolve_reference(reference) is snapshot


@pytest.mark.parametrize(
    "changes",
    [{"suite_id": "another-suite"}, {"purpose": "capture-pilot"}],
    ids=["suite-id", "purpose"],
)
def test_warm_cache_still_refuses_a_reference_that_disagrees_with_a_verified_snapshot(
    layout: AssetLayout, store: CatalogEditor, changes: dict[str, str]
) -> None:
    seeded(store, 2)
    reference = published(store)
    cache = GroupCatalogCache(layout)
    assert cache.resolve(group_for(reference)) == store.materialize(reference)
    # Same suite digest and snapshot bytes, but a reference the file does not repeat.
    forged = group_for(reference.model_copy(update=changes))
    assert GroupCatalogCache.key(forged) != GroupCatalogCache.key(group_for(reference))
    with pytest.raises(CorpusSnapshotSecurityError, match="declares suite") as caught:
        cache.resolve(forged)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    assert cache.resolve(group_for(reference)) == store.materialize(reference)
