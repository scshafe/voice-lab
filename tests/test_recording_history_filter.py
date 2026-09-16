from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_recording_index import catalog as catalog
from test_recording_index import complete, native_take
from test_recording_index import index as index
from test_recording_index import layout as layout
from test_recording_navigation import NavigationRig
from test_recording_navigation import nav as nav

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.models import PromptSuite
from cole_voice_lab.recording_contracts import RecordingHistoryPage, ServiceRequest
from cole_voice_lab.recording_index import RecordingIndex, RecordingIndexError
from cole_voice_lab.recording_service import RecordingCancellation, RecordingWorkspaceBackend


def populate(index: RecordingIndex, catalog: PromptSuite) -> str:
    for number in range(9):
        index.update_take(
            native_take(
                catalog,
                number,
                prompt_ordinal=0 if number < 5 else 1,
                group_ordinal=number % 3,
            )
        )
    return complete(index, catalog)


def test_selected_prompt_pages_preserve_global_counts_and_never_open_audio(
    index: RecordingIndex, catalog: PromptSuite, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = populate(index, catalog)
    global_page = index.history(digest, page_size=2)
    prompt = catalog.prompts[0].prompt_id
    real_open = os.open

    def no_wave(path: str | Path, flags: int, mode: int = 0o777, **options: object) -> int:
        assert not str(path).endswith(".wav")
        return real_open(path, flags, mode, **options)

    monkeypatch.setattr(os, "open", no_wave)
    cursor = None
    take_ids: list[str] = []
    while True:
        page = index.history(digest, prompt_id=prompt, cursor=cursor, page_size=2)
        assert page.take_prompt_id == prompt
        assert len(page.takes) <= 2
        assert page.total_take_count == global_page.total_take_count == 9
        assert page.total_prompt_count == global_page.total_prompt_count
        assert page.covered_prompt_count == global_page.covered_prompt_count
        assert page.missing_prompt_count == global_page.missing_prompt_count
        assert page.groups == global_page.groups and page.prompts == global_page.prompts
        assert all(entry.prompt.prompt_id == prompt for entry in page.takes)
        take_ids.extend(entry.take_id for entry in page.takes)
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert take_ids == [f"synthetic-take-{number:06d}" for number in range(5)]


@pytest.mark.parametrize("other", [None, "synthetic-0001"])
def test_take_cursor_cannot_cross_filter_scope(
    index: RecordingIndex, catalog: PromptSuite, other: str | None
) -> None:
    digest = populate(index, catalog)
    first = index.history(digest, prompt_id=catalog.prompts[0].prompt_id, page_size=2)
    assert first.next_cursor is not None
    with pytest.raises(RecordingIndexError, match="different query"):
        index.history(digest, prompt_id=other, cursor=first.next_cursor)
    if other is None:
        unfiltered = index.history(digest, page_size=2)
        with pytest.raises(RecordingIndexError, match="different query"):
            index.history(
                digest, prompt_id=catalog.prompts[0].prompt_id, cursor=unfiltered.next_cursor
            )


def test_global_prompt_and_group_cursors_remain_independent_of_take_filter(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = populate(index, catalog)
    first = index.history(digest, page_size=2)
    assert first.next_prompt_cursor is not None and first.next_group_cursor is not None
    whole = index.history(
        digest, prompt_cursor=first.next_prompt_cursor, group_cursor=first.next_group_cursor
    )
    selected = index.history(
        digest,
        prompt_id=catalog.prompts[0].prompt_id,
        prompt_cursor=first.next_prompt_cursor,
        group_cursor=first.next_group_cursor,
    )
    assert selected.prompts == whole.prompts and selected.groups == whole.groups


def test_filter_requires_exact_current_catalog_content(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = populate(index, catalog)
    prompt = catalog.prompts[0]
    changed = catalog.model_copy(
        update={
            "prompts": (
                prompt.model_copy(update={"text": prompt.text + " Changed."}),
                *catalog.prompts[1:],
            )
        }
    )
    index.register_catalog(changed)
    assert index.history(digest, prompt_id=prompt.prompt_id).takes
    assert not index.history(canonical_sha256(changed), prompt_id=prompt.prompt_id).takes
    assert not index.history(digest, prompt_id=catalog.prompts[5].prompt_id).takes
    with pytest.raises(RecordingIndexError, match="exact catalog"):
        index.history(digest, prompt_id="does-not-exist")


def test_library_and_workspace_service_forward_selected_prompt(nav: NavigationRig) -> None:
    first = nav.capture("first")
    nav.save(first, "save-first")
    nav.select(1)
    second = nav.capture("second")
    nav.save(second, "save-second")
    backend = RecordingWorkspaceBackend(
        nav.rig.layout,
        "synthetic-subject",
        nav.context.group.catalog_sha256,
        store=nav.rig.store,
        index=nav.index,
        discover=False,
    )
    prompt = first.reservation.prompt.prompt_id
    request = ServiceRequest.model_validate(
        {
            "data": {
                "command": "history",
                "catalog_sha256": nav.context.group.catalog_sha256,
                "prompt_id": prompt,
            },
            "meta": {
                "request_id": "selected-history",
                "process_generation": "service-generation",
                "state_revision": backend.state_revision,
                "event_sequence": 0,
                "take_id": None,
            },
        }
    )
    result = backend.execute(request, RecordingCancellation())
    assert isinstance(result, RecordingHistoryPage)
    assert result.take_prompt_id == prompt
    assert [take.take_id for take in result.takes] == [first.reservation.take_id]
    assert result.total_take_count == 2
    assert result == nav.library.indexed_history(nav.index, prompt_id=prompt)


def test_response_rejects_takes_from_a_different_filter(
    index: RecordingIndex, catalog: PromptSuite
) -> None:
    digest = populate(index, catalog)
    page = index.history(digest, prompt_id=catalog.prompts[0].prompt_id)
    changed = page.model_dump() | {"take_prompt_id": catalog.prompts[1].prompt_id}
    with pytest.raises(ValidationError, match="selected exact prompt"):
        RecordingHistoryPage.model_validate(changed)
