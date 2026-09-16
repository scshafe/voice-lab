"""Synthetic PCM edit evidence, non-destructive publication, and boundary failures."""

from __future__ import annotations

import fcntl
import os
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_edits import (
    MAX_EDIT_BYTES,
    RecordingEdit,
    assert_recording_not_removed,
    effective_recording_path,
    read_recording_edit,
    recording_edit_key,
    source_edit_state,
    update_recording_edit,
)

EDIT_TIME = datetime(2026, 9, 8, tzinfo=UTC)
EDIT_SAMPLES = (1_000_000, -1_000_000, 2_000_000, -2_000_000, 3_000_000, -3_000_000)
EDIT_PCM = b"".join(sample.to_bytes(3, "little", signed=True) for sample in EDIT_SAMPLES)


@pytest.fixture
def edit_source(tmp_path: Path) -> tuple[AssetLayout, Path, RecordingEdit]:
    layout = AssetLayout.initialize(tmp_path / "synthetic-edit-assets")
    source = layout.directory("tmp") / "source.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(48_000)
        writer.writeframes(EDIT_PCM)
    source.chmod(0o600)
    state = source_edit_state(
        layout,
        source,
        subject_id="cole",
        session_id="session-one",
        prompt_id="prompt-one",
        source_sha256=inspect_canonical_wav(source).sha256,
        updated_at=EDIT_TIME,
    )
    return layout, source, state


def edit_path(layout: AssetLayout, state: RecordingEdit) -> Path:
    key = recording_edit_key(
        subject_id=state.subject_id,
        session_id=state.session_id,
        prompt_id=state.prompt_id,
        source_sha256=state.source_sha256,
    )
    return layout.directory("provenance") / "recording-edits" / f"{key}.json"


def read_state(layout: AssetLayout, state: RecordingEdit) -> RecordingEdit | None:
    return read_recording_edit(
        layout,
        subject_id=state.subject_id,
        session_id=state.session_id,
        prompt_id=state.prompt_id,
        source_sha256=state.source_sha256,
    )


def test_unedited_read_does_not_create_provenance(
    edit_source: tuple[AssetLayout, Path, RecordingEdit],
) -> None:
    layout, source, state = edit_source
    assert not state.trimmed and effective_recording_path(layout, source, state) == source
    assert read_state(layout, state) is None
    assert not edit_path(layout, state).parent.exists()


def test_trim_is_frame_exact_and_preserves_all_original_bytes(
    edit_source: tuple[AssetLayout, Path, RecordingEdit],
) -> None:
    layout, source, state = edit_source
    original = source.read_bytes()
    edit = update_recording_edit(
        layout, source, state, start_frame=1, end_frame=5, updated_at=EDIT_TIME
    )
    output = effective_recording_path(layout, source, edit)
    with wave.open(str(output), "rb") as reader:
        assert reader.getnframes() == 4
        assert reader.readframes(4) == EDIT_PCM[3:15]
    assert source.read_bytes() == original
    assert read_state(layout, state) == edit
    unchanged = update_recording_edit(
        layout, source, edit, start_frame=1, end_frame=5, updated_at=EDIT_TIME
    )
    assert unchanged == edit
    reset = update_recording_edit(
        layout, source, edit, start_frame=0, end_frame=6, updated_at=EDIT_TIME
    )
    assert not reset.trimmed and effective_recording_path(layout, source, reset) == source
    assert output.is_file()


@pytest.mark.parametrize("bounds", [(-1, 5), (0, 7), (5, 5), (5, 3), (False, 5), (0, None)])
def test_invalid_trim_bounds_never_change_original(
    edit_source: tuple[AssetLayout, Path, RecordingEdit], bounds: tuple[int, int | None]
) -> None:
    layout, source, state = edit_source
    original = source.read_bytes()
    with pytest.raises(ContractError, match="frame range"):
        update_recording_edit(
            layout, source, state, start_frame=bounds[0], end_frame=bounds[1], updated_at=EDIT_TIME
        )
    assert source.read_bytes() == original
    assert read_state(layout, state) is None


def test_removed_state_can_be_restored_without_losing_trim(
    edit_source: tuple[AssetLayout, Path, RecordingEdit],
) -> None:
    layout, source, state = edit_source
    trimmed = update_recording_edit(
        layout, source, state, start_frame=1, end_frame=5, updated_at=EDIT_TIME
    )
    removed = update_recording_edit(
        layout, source, trimmed, removed=True, reason="Wrong prompt", updated_at=EDIT_TIME
    )
    with pytest.raises(ContractError, match="removed"):
        assert_recording_not_removed(
            layout,
            subject_id=state.subject_id,
            session_id=state.session_id,
            prompt_id=state.prompt_id,
            source_sha256=state.source_sha256,
        )
    restored = update_recording_edit(layout, source, removed, removed=False, updated_at=EDIT_TIME)
    assert restored.derived_sha256 == trimmed.derived_sha256 and not restored.removed
    assert_recording_not_removed(
        layout,
        subject_id=state.subject_id,
        session_id=state.session_id,
        prompt_id=state.prompt_id,
        source_sha256=state.source_sha256,
    )


def test_stale_edit_and_changed_source_fail_closed(
    edit_source: tuple[AssetLayout, Path, RecordingEdit],
) -> None:
    layout, source, state = edit_source
    edit = update_recording_edit(
        layout,
        source,
        state,
        removed=True,
        reason="wrong",
        updated_at=EDIT_TIME + timedelta(seconds=1),
    )
    with pytest.raises(SecurityError, match="edit changed"):
        update_recording_edit(layout, source, state, removed=False, updated_at=EDIT_TIME)
    source.write_bytes(source.read_bytes()[:-3])
    with pytest.raises((SecurityError, ContractError)):
        update_recording_edit(layout, source, edit, removed=False, updated_at=EDIT_TIME)


@pytest.mark.parametrize(
    "mutation",
    ["noncanonical", "invalid", "wrong-identity", "public", "hardlink", "oversized", "symlink"],
)
def test_damaged_edit_evidence_never_looks_unedited(
    edit_source: tuple[AssetLayout, Path, RecordingEdit], mutation: str
) -> None:
    layout, source, state = edit_source
    update_recording_edit(layout, source, state, removed=True, reason="wrong", updated_at=EDIT_TIME)
    path = edit_path(layout, state)
    if mutation == "noncanonical":
        path.write_bytes(b" " + path.read_bytes())
    elif mutation == "invalid":
        path.write_bytes(b"{}\n")
    elif mutation == "wrong-identity":
        edit = read_state(layout, state)
        assert edit is not None
        write_canonical_json(path, edit.model_copy(update={"subject_id": "someone-else"}))
    elif mutation == "public":
        path.chmod(0o644)
    elif mutation == "hardlink":
        os.link(path, path.with_suffix(".linked"))
    elif mutation == "oversized":
        path.write_bytes(b"x" * (MAX_EDIT_BYTES + 1))
    else:
        backup = path.with_suffix(".backup")
        path.rename(backup)
        path.symlink_to(backup)
    with pytest.raises((SecurityError, ContractError)):
        read_state(layout, state)


@pytest.mark.parametrize("mutation", ["bytes", "hardlink", "public", "symlink", "directory-public"])
def test_invalid_derivative_is_not_played(
    edit_source: tuple[AssetLayout, Path, RecordingEdit], mutation: str
) -> None:
    layout, source, state = edit_source
    edit = update_recording_edit(
        layout, source, state, start_frame=1, end_frame=5, updated_at=EDIT_TIME
    )
    output = effective_recording_path(layout, source, edit)
    if mutation == "bytes":
        payload = bytearray(output.read_bytes())
        payload[-1] ^= 1
        output.write_bytes(payload)
    elif mutation == "hardlink":
        os.link(output, output.with_suffix(".linked"))
    elif mutation == "public":
        output.chmod(0o644)
    elif mutation == "directory-public":
        output.parent.chmod(0o755)
    else:
        backup = output.with_suffix(".backup")
        output.rename(backup)
        output.symlink_to(backup)
    with pytest.raises(SecurityError):
        effective_recording_path(layout, source, edit)


@pytest.mark.parametrize(
    "updates",
    [
        {"updated_at": datetime(2026, 9, 8)},
        {"start_frame": 6},
        {"removed": True},
        {"removal_reason": "reason"},
        {"removal_reason": "\n"},
        {"derived_path": "raw/source.wav"},
        {"derived_path": "derived/recording-edits/a.wav"},
        {"start_frame": 1},
        {"sample_rate": 44_100},
    ],
)
def test_edit_contract_rejects_invalid_bindings(
    edit_source: tuple[AssetLayout, Path, RecordingEdit], updates: dict[str, object]
) -> None:
    _, _, state = edit_source
    payload = state.model_dump()
    payload.update(updates)
    with pytest.raises(ValidationError):
        RecordingEdit.model_validate(payload)


def test_bad_removal_reason_is_explicit(
    edit_source: tuple[AssetLayout, Path, RecordingEdit],
) -> None:
    layout, source, state = edit_source
    with pytest.raises(ContractError, match="invalid recording edit"):
        update_recording_edit(layout, source, state, removed=True, reason=" ", updated_at=EDIT_TIME)
    assert read_state(layout, state) is None


def test_concurrent_edit_and_unsafe_lock_fail_without_replacing_evidence(
    edit_source: tuple[AssetLayout, Path, RecordingEdit],
) -> None:
    layout, source, state = edit_source
    current = update_recording_edit(
        layout, source, state, removed=True, reason="wrong", updated_at=EDIT_TIME
    )
    lock = edit_path(layout, state).with_name(f".{edit_path(layout, state).stem}.lock")
    descriptor = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ContractError, match="another recording editor"):
            update_recording_edit(layout, source, current, removed=False, updated_at=EDIT_TIME)
    finally:
        os.close(descriptor)
    lock.chmod(0o644)
    with pytest.raises(SecurityError, match="edit lock"):
        update_recording_edit(layout, source, current, removed=False, updated_at=EDIT_TIME)
    assert read_state(layout, state) == current
