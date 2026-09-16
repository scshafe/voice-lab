"""Legacy (desktop) publication through published corpus-store catalog versions.

A keep-recordings revision keeps every legacy take of its prompt counting, exactly as the
native projection does: the rows keep the prompt the audio was recorded against and are
identical to the pilot-v2 publication (TC2); a default revision leaves the prompt missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from test_pilot_v2_coverage import (
    ROOM_TONES,
    SUBJECT,
    LegacyEvidence,
    build_legacy_root,
    pilot_v1,
    publish,
    released_pilot_v2,
    spoken,
)

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.corpus_store import open_corpus_store
from cole_voice_lab.models import Prompt
from cole_voice_lab.recording_contracts import CatalogSnapshot
from cole_voice_lab.recording_discovery_contracts import LegacyProjectionHeader
from cole_voice_lab.recording_index import RecordingIndex, RecordingIndexError
from cole_voice_lab.recording_index_backfill import (
    backfill_recording_library,
    legacy_projection_records,
)
from cole_voice_lab.recording_library import RecordingHistory, RecordingLibrary
from cole_voice_lab.recording_review import RecordingReviewResolver
from cole_voice_lab.recording_takes import RecordingTakeStore

CATALOG_ID = "cole-corpus"


@dataclass(frozen=True)
class Published:
    evidence: LegacyEvidence
    seeded: CatalogSnapshot  # pilot-v2's prompts, published as-is
    kept: CatalogSnapshot  # spoken 1 and 3 reworded with their recordings kept
    changed: CatalogSnapshot  # then spoken 2 reworded by default


def _text(prompt_id: str) -> str:
    prompt = next(p for p in pilot_v1().prompts if p.prompt_id == prompt_id)
    assert prompt.text is not None
    return prompt.text


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Published]:
    """The TC2 legacy root plus a corpus store publishing three catalog versions."""
    root = tmp_path_factory.mktemp("legacy-aliases").resolve() / "assets"
    evidence = build_legacy_root(root)
    with open_corpus_store(evidence.layout, create=True) as corpus:
        corpus.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
        corpus.create_catalog(CATALOG_ID, purpose="capture-pilot")
        seeded = corpus.materialize(corpus.publish_version(CATALOG_ID))
        for number in (1, 3):
            corpus.edit_prompt(
                spoken(number),
                text=f"{_text(spoken(number))} Again.",
                keep_recordings=True,
                allow_rewording=True,
            )
        kept = corpus.materialize(corpus.publish_version(CATALOG_ID))
        corpus.edit_prompt(spoken(2), text="A different synthetic sentence.")
        changed = corpus.materialize(corpus.publish_version(CATALOG_ID))
    yield Published(evidence, seeded, kept, changed)


def _current(snapshot: CatalogSnapshot, prompt_id: str) -> Prompt:
    prompt = snapshot.prompt(prompt_id)
    assert prompt is not None
    return prompt


def test_keep_recordings_aliases_publish_the_tc2_rows_unchanged(published: Published) -> None:
    layout = published.evidence.layout
    kept = published.kept
    assert {alias.prompt_sha256 for alias in kept.prompt_aliases} == {
        canonical_sha256(_current(kept, spoken(number))) for number in (1, 3)
    }
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        baseline = publish(layout, index, released_pilot_v2())
        assert publish(layout, index, published.seeded) == baseline
        # The same legacy events, byte for byte, now also count for the reworded prompts.
        assert publish(layout, index, kept) == baseline
        page = index.history(kept.catalog_sha256, page_size=200)
        assert page.completeness == "complete" and not page.has_more
        prompts = {row.prompt_id: row for row in page.prompts}
        assert (prompts[spoken(1)].kept_count, prompts[spoken(1)].take_count) == (3, 4)
        assert prompts[spoken(1)].prompt_sha256 == canonical_sha256(_current(kept, spoken(1)))
        assert prompts[spoken(3)].take_count == 2  # the interrupted draft stays listed
        assert page.covered_prompt_count == 34
        for prompt_id in ROOM_TONES:
            assert prompts[prompt_id].take_count == 0
        takes = index.history(kept.catalog_sha256, prompt_id=spoken(1), page_size=200).takes
        recorded = next(p for p in pilot_v1().prompts if p.prompt_id == spoken(1))
        assert len(takes) == 4 and {row.prompt for row in takes} == {recorded}
        assert {row.legacy_session_id for row in takes} == {
            published.evidence.finalized,
            published.evidence.in_progress,
            published.evidence.sidecar_less,
        }
        # Review authorizes the alias-covered legacy source against its recorded prompt.
        target = next(
            row for row in takes if row.legacy_session_id == published.evidence.sidecar_less
        )
        resolver = RecordingReviewResolver(layout, RecordingTakeStore(layout), index)
        source = resolver.resolve(kept.catalog_sha256, target.take_id)
        assert source.prompt == recorded and source.catalog_sha256 == kept.catalog_sha256
        # Save & Next skips every spoken prompt, the alias-covered ones included.
        first = index.next_missing(kept.catalog_sha256).next_prompt
        assert first is not None and first.prompt_id == ROOM_TONES[0]
        after = index.next_missing(kept.catalog_sha256, after_prompt_id=ROOM_TONES[0])
        assert after.next_prompt is not None and after.next_prompt.prompt_id == ROOM_TONES[1]


def test_a_default_revision_leaves_legacy_recordings_of_its_prompt_missing(
    published: Published,
) -> None:
    layout = published.evidence.layout
    changed = published.changed
    assert changed.prompt_aliases == published.kept.prompt_aliases
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        baseline = publish(layout, index, released_pilot_v2())
        assert publish(layout, index, published.kept) == baseline
        assert publish(layout, index, changed) == baseline
        page = index.history(changed.catalog_sha256, page_size=200)
        prompts = {row.prompt_id: row for row in page.prompts}
        assert (prompts[spoken(2)].status, prompts[spoken(2)].take_count) == ("missing", 0)
        assert prompts[spoken(1)].kept_count == 3
        assert page.covered_prompt_count == 33
        after = index.next_missing(changed.catalog_sha256, after_prompt_id=ROOM_TONES[0])
        assert after.next_prompt == _current(changed, spoken(2))
        # The earlier version still counts those rows.
        assert index.history(published.kept.catalog_sha256).covered_prompt_count == 34


def test_library_history_lists_alias_covered_takes_under_their_current_prompt(
    published: Published,
) -> None:
    layout = published.evidence.layout
    library = RecordingLibrary(layout, published.kept, SUBJECT)
    assert library.catalog == published.kept
    assert library.prompt_suite == published.kept.suite
    assert library.catalog_sha256 == published.kept.catalog_sha256
    recorded = next(p for p in pilot_v1().prompts if p.prompt_id == spoken(3))
    assert not RecordingLibrary(layout, published.kept.suite, SUBJECT).counts(recorded)
    assert library.counts(recorded)
    rows = {row.prompt.prompt_id: row for row in library.rows()}
    row = rows[spoken(3)]
    assert row.prompt == _current(published.kept, spoken(3)) != recorded
    assert row.history and {item.recorded_prompt for item in row.history} == {recorded}
    # Only the embedded suite's own prompts count without the snapshot's aliases.
    plain = {
        row.prompt.prompt_id: row
        for row in RecordingLibrary(layout, published.kept.suite, SUBJECT).rows()
    }
    assert plain[spoken(3)].history == ()
    header = next(iter(legacy_projection_records(library, lambda take, alias: None)))
    assert header == LegacyProjectionHeader(catalog_sha256=published.kept.catalog_sha256)


def test_recording_history_names_its_recorded_prompt(published: Published) -> None:
    library = RecordingLibrary(published.evidence.layout, published.seeded, SUBJECT)
    entry = next(item for row in library.rows() for item in row.history)
    with pytest.raises(ValueError, match="recorded prompt's id"):
        RecordingHistory(
            entry.session_id,
            "another-prompt",
            entry.recorded_at,
            entry.date_precision,
            entry.status,
            entry.relative_path,
            entry.sha256,
            entry.session_directory,
            entry.recorded_prompt,
        )


def test_backfill_registers_the_librarys_snapshot_or_refuses_another_context(
    published: Published,
) -> None:
    layout = published.evidence.layout
    with RecordingIndex(layout, subject_id="someone-else") as other:
        with pytest.raises(RecordingIndexError, match="context does not match"):
            backfill_recording_library(RecordingLibrary(layout, published.kept, SUBJECT), other)
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        backfill_recording_library(RecordingLibrary(layout, published.kept, SUBJECT), index)
        stored = index.connection.execute(
            "SELECT recording_prompt_sha256, recording_accepted_prompt_sha256 FROM "
            "recording_prompt_aliases WHERE recording_catalog_sha256=? ORDER BY 1, 2",
            (published.kept.catalog_sha256,),
        ).fetchall()
        assert [tuple(row) for row in stored] == [
            (alias.prompt_sha256, alias.accepted_prompt_sha256)
            for alias in published.kept.prompt_aliases
        ]
