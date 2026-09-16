"""Synthetic-only desktop history, durability, and fail-closed capture tests."""

from __future__ import annotations

import os
import shutil
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import cole_voice_lab.recording_library as library_module
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, write_canonical_json
from cole_voice_lab.capture import (
    CaptureInterrupted,
    CaptureProgressError,
    TakeEngine,
    expected_recording_paths,
)
from cole_voice_lab.errors import ContractError, ExternalToolError, SecurityError
from cole_voice_lab.intake import accept_capture_session
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_library import (
    DesktopRecordingSession,
    DesktopSessionEvidence,
    DesktopTakeEvidence,
    RecordingLibrary,
)
from cole_voice_lab.references import load_accepted_utterance
from cole_voice_lab.resources import load_prompt_suite

START = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
DEVICE = CaptureDevice(device_id="2", display_name="miniDSP UMIK-1")


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


class Timer:
    def __init__(self, seconds: float, callback: Callable[[], None]) -> None:
        self.callback = callback

    def start(self) -> None:
        return None

    def cancel(self) -> None:
        return None


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sample = 100 if "room-tone" in path.name else 2_000_000
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(48_000)
        writer.writeframes(
            b"".join(n.to_bytes(3, "little", signed=True) for n in [sample, -sample] * 480)
        )
    path.chmod(0o600)


class Process:
    def __init__(self, command: Sequence[str]) -> None:
        self.path = Path(command[-1])
        self.return_code: int | None = None
        self.media_frames: int | None = None
        self.path.touch(mode=0o600)

    @property
    def captured_frames(self) -> int | None:
        return self.media_frames

    def signal_quit(self) -> None:
        write_wav(self.path)
        self.media_frames = 960

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        self.return_code = 0
        return 0

    def send_signal(self, signal_number: int) -> None:
        self.return_code = 0

    def terminate(self) -> None:
        self.return_code = 0

    def kill(self) -> None:
        self.return_code = 0


@pytest.fixture
def catalog() -> PromptSuite:
    return PromptSuite(
        suite_id="desktop-test-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone",
                text="Remain silent.",
                category="room-tone",
                style="Silent",
                expected_seconds=10.0,
            ),
            Prompt(
                prompt_id="neutral-one",
                text="One exact test sentence.",
                category="neutral",
                style="Natural",
                expected_seconds=10.0,
            ),
            Prompt(
                prompt_id="numbers-two",
                text="Twenty two apples.",
                category="numbers",
                style="Clear",
                expected_seconds=10.0,
            ),
        ),
    )


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "private-assets")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def library(layout: AssetLayout, catalog: PromptSuite, clock: Clock) -> RecordingLibrary:
    return RecordingLibrary(
        layout,
        catalog,
        process_factory=Process,
        timer_factory=Timer,
        clock=clock,
        device_resolver=lambda device: device,
    )


def take(session: DesktopRecordingSession) -> None:
    session.start_take()
    session.stop_take()
    session.accept_take()


def complete(session: DesktopRecordingSession) -> None:
    while session.current().status == "ready":
        take(session)


def sidecar(session: DesktopRecordingSession) -> Path:
    return (
        session.library.layout.root
        / "provenance/capture-desktop"
        / (f"{session.evidence.session_id}.json")
    )


def test_empty_library_lists_full_pilot_catalog(layout: AssetLayout) -> None:
    rows = RecordingLibrary(layout, load_prompt_suite("pilot")).rows()
    assert len(rows) == 36
    assert all(row.status == "Missing" and row.take_count == 0 for row in rows)


def test_library_forwards_confirmed_media_duration_without_changing_take_timestamp(
    library: RecordingLibrary, clock: Clock
) -> None:
    processes: list[Process] = []

    def process_factory(command: Sequence[str]) -> Process:
        process = Process(command)
        processes.append(process)
        return process

    library.process_factory = process_factory
    session = library.begin_session(["neutral-one"], DEVICE)
    try:
        state = session.start_take()
        timestamp = session.evidence.takes[0].take_started_at
        assert state.status == "recording" and state.elapsed_seconds is None
        clock.value += timedelta(seconds=2)
        assert session.current().elapsed_seconds is None
        processes[0].media_frames = 480
        assert session.current().elapsed_seconds == pytest.approx(0.01)
        clock.value += timedelta(seconds=20)
        assert session.current().elapsed_seconds == pytest.approx(0.01)
        stopped = session.stop_take()
        assert stopped.status == "review" and stopped.metrics is not None
        assert stopped.metrics.duration_seconds == pytest.approx(0.02)
        assert session.evidence.takes[0].take_started_at == timestamp
    finally:
        session.abort()


def test_progress_error_before_desktop_start_acknowledgment_stops_capture_and_retains_partial(
    library: RecordingLibrary,
) -> None:
    processes: list[Process] = []

    class LateProgressFailure(Process):
        progress_reads = 0

        @property
        def captured_frames(self) -> int | None:
            self.progress_reads += 1
            if self.progress_reads > 1:
                raise CaptureProgressError("synthetic late startup progress failure")
            return None

    def process_factory(command: Sequence[str]) -> Process:
        process = LateProgressFailure(command)
        processes.append(process)
        return process

    library.process_factory = process_factory
    session = library.begin_session(["neutral-one"], DEVICE)
    try:
        with pytest.raises(CaptureProgressError, match="late startup"):
            session.start_take()
        assert processes[0].return_code == 0
        assert session.engine._live_process is None
        assert processes[0].path.is_file()
        assert processes[0].path.stat().st_mode & 0o077 == 0
        canonical = expected_recording_paths(session.evidence.selected_suite)[0]
        assert not (session.engine.session_directory / canonical).exists()
        assert session.evidence.takes[0].rejected
        assert not (session.engine.session_directory / "READY").exists()
    finally:
        session.abort()


@pytest.mark.parametrize("cleanup_failure", ["abort", "reject"])
def test_desktop_start_cleanup_failures_are_visible_and_preserve_original_cause(
    library: RecordingLibrary, monkeypatch: pytest.MonkeyPatch, cleanup_failure: str
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    original = CaptureProgressError("synthetic initial startup failure")

    def fail_start() -> None:
        raise original

    def fail_cleanup(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic cleanup error")

    monkeypatch.setattr(session.engine, "start_take", fail_start)
    monkeypatch.setattr(
        session.engine if cleanup_failure == "abort" else session,
        "abort" if cleanup_failure == "abort" else "_update_take",
        fail_cleanup,
    )
    try:
        with pytest.raises(ExternalToolError, match="cleanup also failed") as caught:
            session.start_take()
        assert caught.value.__cause__ is original
        assert "synthetic cleanup error" in str(caught.value)
    finally:
        monkeypatch.undo()
        session.abort()


def test_selected_prompt_records_out_of_order_and_resumes_v1(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one", "numbers-two"], DEVICE)
    session.select_prompt("numbers-two")
    take(session)
    stored = sidecar(session).read_bytes()
    assert DesktopSessionEvidence.model_validate_json(stored).schema_version.endswith("/v1")
    session.abort()
    resumed = library.resume_session(session.evidence.session_id)
    assert resumed.current().prompt == library.prompt_suite.prompts[0]
    assert resumed.current().takes[2].status == "accepted"
    resumed.select_prompt("neutral-one")
    take(resumed)
    take(resumed)
    result = resumed.finalize()
    assert tuple(file.path for file in result.manifest.files) == expected_recording_paths(
        library.prompt_suite
    )


def test_switching_preserves_multiple_unreviewed_drafts(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one", "numbers-two"], DEVICE)
    for prompt_id in ("numbers-two", "neutral-one", "room-tone"):
        session.select_prompt(prompt_id)
        session.start_take()
        session.stop_take()
    assert all(view.status == "review" for view in session.current().takes)
    session.abort()
    resumed = library.resume_session(session.evidence.session_id)
    for prompt_id in ("neutral-one", "numbers-two", "room-tone"):
        state = resumed.select_prompt(prompt_id)
        assert state.prompt is not None and state.prompt.prompt_id == prompt_id
        resumed.accept_take()
    assert resumed.current().status == "complete"
    resumed.finalize()


def test_selecting_while_recording_or_after_close_is_rejected(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    with pytest.raises(ContractError, match="belong"):
        session.select_prompt("numbers-two")
    with pytest.raises(ContractError, match="out of range"):
        session.engine.select_take(-1)
    session.select_prompt("neutral-one")
    session.start_take()
    with pytest.raises(ContractError, match="recording"):
        session.select_prompt("room-tone")
    assert session.current().prompt == library.prompt_suite.prompts[1]
    session.stop_take()
    session.abort()
    with pytest.raises(ContractError, match="closed"):
        session.select_prompt("room-tone")


def test_revisit_and_retry_earlier_kept_take_preserves_original(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one", "numbers-two"], DEVICE)
    complete(session)
    before = library.rows()[1].history[0]
    original = library.original_preview_path(before).read_bytes()
    assert session.select_prompt("neutral-one").status == "review"
    session.reject_take("wrong words")
    assert session.current().prompt == library.prompt_suite.prompts[1]
    take(session)
    session.finalize()
    entries = library.rows()[1].history
    rejected = next(h for h in entries if h.status == "Rejected")
    assert library.original_preview_path(rejected).read_bytes() == original
    assert len(entries) == 2


def test_trim_retains_finalized_and_accepted_raw_package_bytes(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    complete(session)
    finalized = session.finalize()
    incoming = library.layout.directory("incoming") / session.evidence.session_id
    shutil.copytree(finalized.session_directory, incoming)
    receipt = accept_capture_session(library.layout, incoming)
    entry = library.rows()[1].history[0]
    source = library.original_preview_path(entry)
    original = source.read_bytes()
    edit = library.save_trim(entry, 120, 720)
    preview = library.preview_path(entry)
    assert preview != source and edit.trimmed
    with wave.open(str(preview), "rb") as reader:
        assert reader.getnframes() == 600
    assert source.read_bytes() == original
    assert (
        load_accepted_utterance(
            library.layout, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="neutral-one"
        ).path
        == source
    )
    library.clear_trim(entry)
    assert library.preview_path(entry) == source
    assert preview.is_file()


def test_remove_restore_updates_coverage_and_raw_eligibility(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    complete(session)
    finalized = session.finalize()
    incoming = library.layout.directory("incoming") / session.evidence.session_id
    shutil.copytree(finalized.session_directory, incoming)
    receipt = accept_capture_session(library.layout, incoming)
    original = library.rows()[1].history[0]
    source_bytes = library.original_preview_path(original).read_bytes()
    library.remove_take(original, "recorded another prompt")
    row = library.rows()[1]
    assert row.status == "Missing" and row.latest_recorded_at is None and row.take_count == 1
    assert row.history[0].status == "Removed"
    with pytest.raises(ContractError, match="removed"):
        load_accepted_utterance(
            library.layout, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="neutral-one"
        )
    other = RecordingLibrary(library.layout, library.prompt_suite)
    assert other.rows()[1].history[0].status == "Removed"
    other.restore_take(other.rows()[1].history[0])
    restored = other.rows()[1].history[0]
    assert restored.status == "Accepted raw" and restored.recorded_at == original.recorded_at
    assert (
        load_accepted_utterance(
            library.layout, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="neutral-one"
        ).path.read_bytes()
        == source_bytes
    )


def test_removing_latest_keeps_earlier_usable_take(library: RecordingLibrary, clock: Clock) -> None:
    for _ in range(2):
        session = library.begin_session(["neutral-one"], DEVICE)
        complete(session)
        session.finalize()
        clock.value += timedelta(days=1)
    latest = library.rows()[1].history[0]
    library.remove_take(latest, "wrong script")
    row = library.rows()[1]
    assert row.status == "Finalized" and row.latest_recorded_at == START
    assert row.take_count == 2


def test_active_take_preview_uses_trim_but_retains_original_engine_bytes(
    library: RecordingLibrary,
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.select_prompt("neutral-one")
    session.start_take()
    session.stop_take()
    history = library.rows()[1].history[0]
    source = session.engine.preview_path()
    original = source.read_bytes()
    library.save_trim(history, 100, 800)
    assert session.preview_path() == library.preview_path(history)
    assert session.preview_path() != source and source.read_bytes() == original
    session.abort()


def test_selected_batch_includes_room_tone_without_reordering(library: RecordingLibrary) -> None:
    session = library.begin_session(["numbers-two"], DEVICE)
    assert tuple(p.prompt_id for p in session.engine.prompt_suite.prompts) == (
        "room-tone",
        "numbers-two",
    )
    assert session.engine.prompt_suite.suite_id.startswith("desktop-subset-")
    session.abort()


def test_full_batch_retains_exact_suite(library: RecordingLibrary, catalog: PromptSuite) -> None:
    session = library.begin_session([p.prompt_id for p in catalog.prompts], DEVICE)
    assert session.engine.prompt_suite == catalog
    session.abort()


def test_timestamp_written_before_ffmpeg_starts_and_survives_reopen(
    library: RecordingLibrary, clock: Clock
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    clock.value += timedelta(days=3)
    observed: list[datetime] = []

    def process(command: Sequence[str]) -> Process:
        evidence = DesktopSessionEvidence.model_validate_json(sidecar(session).read_bytes())
        observed.append(evidence.takes[-1].take_started_at)
        assert evidence.takes[-1].audio_sha256 is None
        return Process(command)

    session.engine.process_factory = process
    take(session)
    session.abort()
    recorded = library.rows()[0].history[0]
    os.utime(recorded.session_directory / recorded.relative_path, (0, 0))
    resumed = library.resume_session(session.evidence.session_id)
    assert observed == [START + timedelta(days=3)]
    assert resumed.engine.captured_at == START
    assert library.rows()[0].latest_recorded_at == START + timedelta(days=3)
    resumed.abort()


def test_reopen_restores_unaccepted_recording_for_review(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.start_take()
    session.stop_take()
    session.abort()
    resumed = library.resume_session(session.evidence.session_id)
    assert resumed.current().status == "review"
    assert library.rows()[0].status == "Needs review"
    resumed.accept_take()
    assert resumed.current().prompt.prompt_id == "neutral-one"  # type: ignore[union-attr]
    resumed.abort()


def test_auto_stop_recovers_original_start_timestamp(
    library: RecordingLibrary, clock: Clock
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    timers: list[Timer] = []

    def timer(seconds: float, callback: Callable[[], None]) -> Timer:
        result = Timer(seconds, callback)
        timers.append(result)
        return result

    session.engine.timer_factory = timer
    session.start_take()
    clock.value += timedelta(hours=1)
    timers[-1].callback()
    assert session.evidence.takes[0].audio_sha256 is None
    assert session.current().status == "review"
    assert session.evidence.takes[0].take_started_at == START
    assert session.evidence.takes[0].audio_sha256 is not None
    session.abort()


def test_completed_audio_survives_crash_before_digest_binding(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.start_take()
    session.engine.stop_take()
    session.abort()
    resumed = library.resume_session(session.evidence.session_id)
    assert resumed.current().status == "review"
    assert resumed.evidence.takes[0].audio_sha256 is not None
    resumed.abort()


def test_interrupted_partial_is_retained_and_can_be_retried(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.start_take()
    with pytest.raises(CaptureInterrupted):
        session.abort()
    resumed = library.resume_session(session.evidence.session_id)
    assert resumed.current().status == "ready"
    assert resumed.evidence.takes[0].rejected
    take(resumed)
    resumed.abort()


def test_new_session_retakes_keep_old_history_and_dates(
    library: RecordingLibrary, clock: Clock
) -> None:
    first = library.begin_session(["neutral-one"], DEVICE)
    complete(first)
    old = first.finalize()
    clock.value += timedelta(days=90)
    second = library.begin_session(["neutral-one"], DEVICE)
    complete(second)
    second.finalize()
    row = library.rows()[1]
    assert row.take_count == 2
    assert [h.recorded_at for h in row.history] == [clock.value, START]
    assert old.session_directory.exists()
    assert library.preview_path(row.history[-1]).is_file()


def test_reject_take_preserves_evidence_and_gets_a_new_timestamp(
    library: RecordingLibrary, clock: Clock
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.start_take()
    session.stop_take()
    session.reject_take("cough")
    clock.value += timedelta(minutes=1)
    take(session)
    assert session.evidence.takes[0].rejected
    assert session.evidence.takes[0].audio_sha256 is not None
    assert session.evidence.takes[1].take_started_at == clock.value
    session.abort()


def test_finalization_requires_all_selected_prompts_and_acceptance(
    library: RecordingLibrary,
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    with pytest.raises(ContractError, match="every selected"):
        session.finalize()
    take(session)
    session.start_take()
    session.stop_take()
    with pytest.raises(ContractError, match="every selected"):
        session.finalize()
    session.accept_take()
    result = session.finalize()
    assert result.manifest.captured_at == START
    assert not (result.session_directory / "desktop-session.json").exists()
    assert library.sessions()[0].status == "Finalized"


def test_two_libraries_cannot_record_concurrently(library: RecordingLibrary) -> None:
    first = library.begin_session(["neutral-one"], DEVICE)
    other = RecordingLibrary(library.layout, library.prompt_suite)
    with pytest.raises(ContractError, match="already open"):
        other.begin_session(["neutral-one"], DEVICE)
    first.abort()
    second = other.begin_session(["neutral-one"], DEVICE)
    second.abort()
    with pytest.raises(ContractError, match="closed"):
        first.start_take()


@pytest.mark.parametrize("field,value", [("text", "Changed sentence."), ("style", "Whisper")])
def test_history_requires_exact_prompt_version(
    library: RecordingLibrary, field: str, value: str
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    complete(session)
    session.finalize()
    prompts = list(library.prompt_suite.prompts)
    prompts[1] = prompts[1].model_copy(update={field: value})
    changed = library.prompt_suite.model_copy(update={"prompts": tuple(prompts)})
    other = RecordingLibrary(library.layout, changed)
    assert other.rows()[1].status == "Missing"
    assert other.rows()[0].take_count == 1


def test_subjects_never_share_recording_history(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    complete(session)
    session.finalize()
    other = RecordingLibrary(library.layout, library.prompt_suite, "someone-else")
    assert all(row.take_count == 0 for row in other.rows())


def test_raw_and_tmp_copies_deduplicate_by_manifest(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    complete(session)
    result = session.finalize()
    incoming = library.layout.directory("incoming") / result.manifest.session_id
    shutil.copytree(result.session_directory, incoming)
    accept_capture_session(library.layout, incoming, min_free_bytes=0, clock=lambda: START)
    row = library.rows()[1]
    assert row.take_count == 1
    assert row.history[0].status == "Accepted raw"
    assert row.history[0].session_directory.parent.name == "raw"


def test_audio_tampering_is_not_displayed_as_missing(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    take(session)
    session.abort()
    path = (
        session.engine.session_directory / expected_recording_paths(session.engine.prompt_suite)[0]
    )
    with path.open("r+b") as stream:
        stream.seek(-3, os.SEEK_END)
        stream.write((101).to_bytes(3, "little", signed=True))
    with pytest.raises(SecurityError, match="hash changed"):
        library.rows()


def test_sidecar_changes_during_open_session_fail_visibly(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    changed = session.evidence.model_copy(update={"capture_host": "unexpected-host"})
    write_canonical_json(sidecar(session), changed)
    with pytest.raises(SecurityError, match="provenance changed"):
        session.current()
    session.abort()


def test_noncanonical_sidecar_fails_visibly(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.abort()
    sidecar(session).write_bytes(canonical_json_bytes(session.evidence))
    with pytest.raises(SecurityError, match="not canonical"):
        library.rows()


def test_missing_session_directory_fails_visibly(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.abort()
    session.engine.session_directory.rename(session.engine.session_directory.with_name("archived"))
    with pytest.raises(SecurityError, match="disappeared"):
        library.rows()


def test_symlinked_sidecar_fails_closed(library: RecordingLibrary, tmp_path: Path) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.abort()
    path = sidecar(session)
    target = tmp_path / "outside.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(SecurityError, match="symlink"):
        library.rows()


def test_stale_history_preview_fails_after_recording_changes(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    take(session)
    history = library.rows()[0].history[0]
    session.abort()
    changed = history.__class__(
        session_id=history.session_id,
        prompt_id=history.prompt_id,
        recorded_at=history.recorded_at,
        date_precision="session",
        status=history.status,
        relative_path=history.relative_path,
        sha256=history.sha256,
        session_directory=history.session_directory,
        recorded_prompt=history.recorded_prompt,
    )
    with pytest.raises(SecurityError, match="history changed"):
        library.preview_path(changed)


def test_corpus_view_is_available_but_capture_requires_bound_pilot_gate(
    layout: AssetLayout, catalog: PromptSuite
) -> None:
    corpus = catalog.model_copy(update={"purpose": "capture-corpus"})
    library = RecordingLibrary(layout, corpus)
    assert len(library.rows()) == 3
    with pytest.raises(ContractError, match="pilot gate"):
        library.begin_session(["neutral-one"], DEVICE)


def test_legacy_unfinished_session_has_no_guessed_date(
    library: RecordingLibrary,
) -> None:
    engine = TakeEngine(
        layout=library.layout,
        prompt_suite=library.prompt_suite,
        subject_id="cole-self",
        device=DEVICE,
        session_id="old-console",
    )
    assert library.sessions()[0].session_started_at is None
    assert not library.sessions()[0].resumable
    engine.abort()


def test_rejection_write_failure_recovers_from_hash_bound_forensic_log(
    library: RecordingLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.start_take()
    session.stop_take()
    original_writer = library_module.write_canonical_json

    def interrupted_write(*args: object, **kwargs: object) -> str:
        raise OSError("simulated interrupted sidecar write")

    monkeypatch.setattr(library_module, "write_canonical_json", interrupted_write)
    with pytest.raises(OSError, match="interrupted"):
        session.reject_take("cough")
    session.abort()
    monkeypatch.setattr(library_module, "write_canonical_json", original_writer)
    assert library.rows()[0].history[0].status == "Rejected"
    resumed = library.resume_session(session.evidence.session_id)
    assert resumed.evidence.takes[0].rejected
    take(resumed)
    assert library.rows()[0].take_count == 2
    resumed.abort()


def test_interrupted_atomic_writer_file_is_preserved_without_hiding_history(
    library: RecordingLibrary,
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    take(session)
    session.abort()
    temporary = sidecar(session).with_name(f".{sidecar(session).name}.tmp-0123456789abcdef")
    temporary.write_bytes(b"partial atomic write")
    temporary.chmod(0o600)
    assert library.rows()[0].take_count == 1
    assert temporary.exists()


def test_device_index_is_resolved_before_each_take_and_persisted(
    library: RecordingLibrary,
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    library.device_resolver = lambda device: device.model_copy(update={"device_id": "7"})
    take(session)
    assert session.engine.device.device_id == "7"
    assert session.evidence.takes[0].capture_device.device_id == "7"
    library.device_resolver = lambda device: device.model_copy(update={"device_id": "9"})
    take(session)
    result = session.finalize()
    assert result.manifest.capture_device.device_id == "9"
    assert session.evidence.takes[0].capture_device.device_id == "7"
    assert session.evidence.takes[1].capture_device.device_id == "9"


def test_device_resolver_cannot_substitute_different_microphone(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    library.device_resolver = lambda device: device.model_copy(update={"display_name": "Other"})
    with pytest.raises(ContractError, match="physical device"):
        session.start_take()
    assert not session.evidence.takes
    session.abort()


def test_deleted_desktop_sidecar_fails_instead_of_showing_missing(
    library: RecordingLibrary,
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.abort()
    sidecar(session).unlink()
    with pytest.raises(SecurityError, match="provenance disappeared"):
        library.rows()


def test_legacy_finalized_pilot_uses_explicit_session_date(layout: AssetLayout) -> None:
    suite = load_prompt_suite("pilot")
    engine = TakeEngine(
        layout=layout,
        prompt_suite=suite,
        subject_id="cole-self",
        device=DEVICE,
        session_id="legacy-pilot",
        process_factory=Process,
        timer_factory=Timer,
        clock=lambda: START,
    )
    for _ in suite.prompts:
        engine.start_take()
        engine.stop_take()
        engine.accept_take()
    engine.finalize()
    rows = RecordingLibrary(layout, suite).rows()
    assert all(row.take_count == 1 for row in rows)
    assert all(row.history[0].date_precision == "session" for row in rows)
    assert all(row.latest_recorded_at == START for row in rows)


@pytest.mark.parametrize(
    "mutation",
    [
        "catalog-hash",
        "prompt-text",
        "purpose",
        "full-suite-id",
        "subset-id",
        "room-tone",
        "duplicate-takes",
        "unknown-take",
        "old-take",
        "naive-session",
        "naive-take",
        "accepted-empty",
    ],
)
def test_desktop_provenance_contract_rejects_invalid_bindings(
    library: RecordingLibrary,
    mutation: str,
) -> None:
    session = library.begin_session([p.prompt_id for p in library.prompt_suite.prompts], DEVICE)
    data = session.evidence.model_dump()
    session.abort()
    take_data = DesktopTakeEvidence(
        prompt_id="neutral-one", take_started_at=START, capture_device=DEVICE
    ).model_dump()
    selected = library.prompt_suite.model_dump()
    if mutation == "catalog-hash":
        data["catalog_sha256"] = "0" * 64
    elif mutation == "prompt-text":
        selected["prompts"][1]["text"] = "Unexpected revision."
    elif mutation == "purpose":
        selected["purpose"] = "capture-corpus"
    elif mutation == "full-suite-id":
        selected["suite_id"] = "wrong-full-id"
    elif mutation == "subset-id":
        selected["prompts"] = selected["prompts"][:2]
    elif mutation == "room-tone":
        selected["prompts"] = selected["prompts"][1:]
        digest = canonical_sha256(
            [p.model_dump(mode="json") for p in library.prompt_suite.prompts[1:]]
        )[:16]
        selected["suite_id"] = f"desktop-subset-{digest}"
    elif mutation == "duplicate-takes":
        data["takes"] = (take_data, take_data)
    elif mutation == "unknown-take":
        take_data["prompt_id"] = "unknown"
        data["takes"] = (take_data,)
    elif mutation == "old-take":
        take_data["take_started_at"] = START - timedelta(days=1)
        data["takes"] = (take_data,)
    elif mutation == "naive-session":
        data["session_started_at"] = START.replace(tzinfo=None)
    elif mutation == "naive-take":
        take_data["take_started_at"] = START.replace(tzinfo=None)
        data["takes"] = (take_data,)
    elif mutation == "accepted-empty":
        take_data["accepted"] = True
        data["takes"] = (take_data,)
    data["selected_suite"] = selected
    with pytest.raises(ValidationError):
        DesktopSessionEvidence.model_validate(data)


@pytest.mark.parametrize("mutation", ["undeclared", "nested", "world-audio", "hardlink", "both"])
def test_unfinished_audio_file_boundary_fails_closed(
    library: RecordingLibrary,
    mutation: str,
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    take(session)
    session.abort()
    directory = session.engine.session_directory
    path = directory / expected_recording_paths(session.engine.prompt_suite)[0]
    if mutation == "undeclared":
        (directory / "unexpected.txt").touch(mode=0o600)
    elif mutation == "nested":
        (directory / "audio/nested").mkdir(mode=0o700)
    elif mutation == "world-audio":
        path.chmod(0o644)
    elif mutation == "hardlink":
        os.link(path, directory / "extra-hardlink.wav")
    elif mutation == "both":
        path.with_suffix(".partial.wav").touch(mode=0o600)
    with pytest.raises(SecurityError):
        library.rows()


@pytest.mark.parametrize("mutation", ["invalid-json", "public", "wrong-name", "unknown-file"])
def test_invalid_sidecar_evidence_fails_closed(library: RecordingLibrary, mutation: str) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.abort()
    path = sidecar(session)
    if mutation == "invalid-json":
        path.write_bytes(b"{}\n")
    elif mutation == "public":
        path.chmod(0o644)
    elif mutation == "wrong-name":
        path.rename(path.with_name("wrong.json"))
    elif mutation == "unknown-file":
        path.with_name("unexpected.txt").touch(mode=0o600)
    with pytest.raises((SecurityError, ContractError)):
        library.rows()


def test_wrong_subject_resume_is_rejected(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.abort()
    other = RecordingLibrary(library.layout, library.prompt_suite, "someone-else")
    with pytest.raises(ContractError, match="different subject"):
        other.resume_session(session.evidence.session_id)


def test_failed_microphone_start_keeps_a_retryable_event(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)

    def failed_process(command: Sequence[str]) -> Process:
        raise OSError("microphone disconnected")

    session.engine.process_factory = failed_process
    with pytest.raises(OSError, match="disconnected"):
        session.start_take()
    assert session.evidence.takes[0].rejected
    session.engine.process_factory = Process
    take(session)
    session.abort()


@pytest.mark.parametrize("device_count", [0, 1, 2])
def test_default_resolver_requires_one_exact_native_uid_and_name(
    library: RecordingLibrary,
    monkeypatch: pytest.MonkeyPatch,
    device_count: int,
) -> None:
    native_device = CaptureDevice(
        backend="coreaudio", device_id="synthetic-umik-uid", display_name=DEVICE.display_name
    )
    inventory = (native_device,) * device_count
    monkeypatch.setattr(library_module, "list_coreaudio_input_devices", lambda: inventory)
    if device_count == 1:
        assert library._resolve_device(native_device) == native_device
    else:
        with pytest.raises(ContractError, match="absent or ambiguous"):
            library._resolve_device(native_device)


def test_default_resolver_requires_new_session_for_legacy_capture(
    library: RecordingLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_discovery() -> None:
        raise AssertionError("Legacy session must fail before new device discovery")

    monkeypatch.setattr(library_module, "list_coreaudio_input_devices", unexpected_discovery)
    with pytest.raises(ContractError, match="older microphone recorder"):
        library._resolve_device(DEVICE)


def test_default_native_resolver_preserves_serial_and_calibration_evidence(
    library: RecordingLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = CaptureDevice(
        backend="coreaudio",
        device_id="synthetic-umik-uid",
        display_name=DEVICE.display_name,
        microphone_serial="synthetic-serial",
        calibration_sha256="a" * 64,
    )
    discovered = CaptureDevice(
        backend="coreaudio", device_id=selected.device_id, display_name=selected.display_name
    )
    monkeypatch.setattr(library_module, "list_coreaudio_input_devices", lambda: (discovered,))
    assert library._resolve_device(selected) == selected


@pytest.mark.parametrize("changed_field", ["device_id", "display_name"])
def test_default_resolver_does_not_rebind_a_native_uid(
    library: RecordingLibrary, monkeypatch: pytest.MonkeyPatch, changed_field: str
) -> None:
    native_device = CaptureDevice(
        backend="coreaudio", device_id="synthetic-umik-uid", display_name=DEVICE.display_name
    )
    wrong_device = native_device.model_copy(update={changed_field: "other-device"})
    monkeypatch.setattr(library_module, "list_coreaudio_input_devices", lambda: (wrong_device,))
    with pytest.raises(ContractError, match="absent or ambiguous"):
        library._resolve_device(native_device)


def test_native_desktop_take_preserves_backend_uid_and_existing_legacy_audio(
    library: RecordingLibrary,
) -> None:
    legacy = library.begin_session(["neutral-one"], DEVICE)
    take(legacy)
    original = legacy.preview_path(0).read_bytes()
    original_path = legacy.preview_path(0)
    legacy.abort()
    native_device = CaptureDevice(
        backend="coreaudio", device_id="synthetic-umik-uid", display_name=DEVICE.display_name
    )
    native = library.begin_session(["neutral-one"], native_device)
    try:
        take(native)
        assert native.evidence.capture_device == native_device
        assert native.evidence.takes[0].capture_device == native_device
        assert original_path.read_bytes() == original
        assert native.evidence.session_id != legacy.evidence.session_id
    finally:
        native.abort()


def test_invalid_capture_transitions_fail_without_overwrite(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    with pytest.raises(ContractError, match="available to accept"):
        session.accept_take()
    with pytest.raises(ContractError, match="available to reject"):
        session.reject_take("retake")
    session.start_take()
    with pytest.raises(ContractError, match="while capture is recording"):
        session.start_take()
    session.stop_take()
    assert session.preview_path().is_file()
    session.abort()


def test_invalid_catalog_selection_never_creates_a_session(library: RecordingLibrary) -> None:
    for selected in ([], ["unknown"]):
        with pytest.raises(ContractError, match="known catalog"):
            library.begin_session(selected, DEVICE)
    no_room = library.prompt_suite.model_copy(update={"prompts": library.prompt_suite.prompts[1:]})
    with pytest.raises(ContractError, match="room tone"):
        RecordingLibrary(library.layout, no_room).begin_session(["neutral-one"], DEVICE)
    evaluation = load_prompt_suite("evaluation")
    with pytest.raises(ContractError, match="held-out"):
        RecordingLibrary(library.layout, evaluation).begin_session(
            [evaluation.prompts[0].prompt_id],
            DEVICE,
        )


def test_rejected_only_row_is_missing_but_keeps_history(library: RecordingLibrary) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.start_take()
    session.stop_take()
    session.reject_take("cough")
    row = library.rows()[0]
    assert row.status == "Missing" and row.latest_recorded_at is None
    assert row.take_count == 1 and row.history[0].status == "Rejected"
    session.abort()


def test_failed_keep_write_cannot_advance_and_resume_restores_review(
    library: RecordingLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    session.start_take()
    session.stop_take()
    original_writer = library_module.write_canonical_json

    def interrupted_write(*args: object, **kwargs: object) -> str:
        raise OSError("simulated interrupted Keep write")

    monkeypatch.setattr(library_module, "write_canonical_json", interrupted_write)
    with pytest.raises(OSError, match="Keep write"):
        session.accept_take()
    assert session.current().status == "review"
    assert library.rows()[0].status == "Needs review"
    with pytest.raises(ContractError, match="while capture is review"):
        session.start_take()
    session.abort()
    monkeypatch.setattr(library_module, "write_canonical_json", original_writer)
    resumed = library.resume_session(session.evidence.session_id)
    assert resumed.current().status == "review"
    resumed.accept_take()
    assert resumed.current().status == "ready"
    complete(resumed)
    resumed.finalize()


def test_finalized_package_survives_interrupted_desktop_digest_write(
    library: RecordingLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = library.begin_session(["neutral-one"], DEVICE)
    complete(session)

    def interrupted_write(*args: object, **kwargs: object) -> str:
        raise OSError("simulated interrupted final digest write")

    monkeypatch.setattr(library_module, "write_canonical_json", interrupted_write)
    with pytest.raises(OSError, match="final digest write"):
        session.finalize()
    assert session.current().status == "finalized"
    assert library.rows()[0].status == "Finalized"
    assert session.evidence.manifest_sha256 is None
    session.abort()
    reopened = RecordingLibrary(library.layout, library.prompt_suite)
    assert reopened.sessions()[0].status == "Finalized"
    assert not reopened.sessions()[0].resumable
    assert reopened.rows()[1].take_count == 1
