"""Independently verify the durable evidence behind the native ``speak`` suite.

The harness report names what to look up (operation ids, the Save As copy) and records what
only the app can observe (its choices, reasons and status lines). Every clip, speech record,
job record and directory is re-read here through the canonical Python modules, so a harness
that reported the wrong result still fails.
"""

from __future__ import annotations

import json
import os
import stat
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

from native_speak_fixture import (
    EARLIER_RUN_ID,
    LATEST_RUN_ID,
    SECOND_REFERENCE_PROMPT_ID,
    SPEECH_TEST_DEADLINE_SECONDS,
)
from verify_native_finetune import _require_no_fixture_processes

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.finetune import (
    FINETUNE_OUTPUT_ROOT,
    SPEECH_CLIP_FILE,
    SPEECH_RECORD_FILE,
    FineTuneDataset,
    FineTuneRun,
    SpeechClip,
    finished_finetune_runs,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import SPEECH_OUTPUT_ROOT, speech_directory
from cole_voice_lab.recording_finetune_jobs import (
    CANCELLED_BY_OPERATOR,
    FINETUNE_JOBS_ROOT,
    SPEECH_ACTIVE_MARKER,
    FineTuneJob,
    FineTuneJobHead,
    FineTuneJobIntent,
    _duration_text,
)
from cole_voice_lab.recording_takes import RecordingTakeStore, read_recording_evidence

if TYPE_CHECKING:
    from native_speak_fixture import NativeSpeakFixture

FIRST_REFERENCE_PROMPT_ID = "finetune-reference"
DEADLINE_MESSAGE = (
    f"speech synthesis took longer than {_duration_text(SPEECH_TEST_DEADLINE_SECONDS)}"
    " and was stopped; try a shorter text"
)
# Which run spoke and whose reference each request used (model, run, reference run, prompt).
EXPECTED_SPEAKERS = {
    "latest": ("latest", LATEST_RUN_ID, LATEST_RUN_ID, FIRST_REFERENCE_PROMPT_ID),
    "base": ("base", None, LATEST_RUN_ID, SECOND_REFERENCE_PROMPT_ID),
    "earlier": (EARLIER_RUN_ID, EARLIER_RUN_ID, EARLIER_RUN_ID, SECOND_REFERENCE_PROMPT_ID),
    # The reference chosen for Base stays chosen: Latest offers it too.
    "after_save": ("latest", LATEST_RUN_ID, LATEST_RUN_ID, SECOND_REFERENCE_PROMPT_ID),
}


def _require_clip(layout: AssetLayout, clip: dict[str, Any], expected: tuple[Any, ...]) -> bytes:
    """Re-read one finished clip and its speech record; return the clip's bytes."""

    operation = clip["operation_id"]
    model, run_id, reference_run_id, reference_prompt_id = expected
    assert (clip["model"], clip["run_id"]) == (model, run_id), clip
    assert (clip["reference_run_id"], clip["reference_prompt_id"]) == (
        reference_run_id,
        reference_prompt_id,
    ), clip
    assert clip["phases"] in (["queued", "running", "finished"], ["queued", "finished"]), clip
    directory = layout.root / speech_directory(operation)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700, f"{directory} is not private"
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        [SPEECH_CLIP_FILE, SPEECH_RECORD_FILE]
    ), f"{directory} holds unexpected files"
    audio = directory / SPEECH_CLIP_FILE
    assert clip["relative_path"] == f"{speech_directory(operation)}/{SPEECH_CLIP_FILE}"
    metadata = audio.lstat()
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1, f"{audio} is not one file"
    assert stat.S_IMODE(metadata.st_mode) == 0o600, f"{audio} is not private"
    digest, _size = sha256_file(audio)
    assert digest == clip["sha256"], f"{audio} does not hash to what the app was told"
    with wave.open(str(audio), "rb") as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (
            24_000,
            1,
            2,
        )
        assert handle.getnframes() == clip["frames"], f"{audio} frame count differs"
    assert clip["sample_rate_hz"] == 24_000
    assert abs(clip["seconds"] - clip["frames"] / 24_000) <= 0.001
    record = SpeechClip.model_validate_json((directory / SPEECH_RECORD_FILE).read_bytes())
    assert record.operation_id == operation and record.text == clip["text"]
    assert record.model == (LATEST_RUN_ID if model == "latest" else model), record
    assert (record.reference_run_id, record.reference_prompt_id) == (
        reference_run_id,
        reference_prompt_id,
    )
    assert (record.relative_path, record.sha256, record.frames) == (
        clip["relative_path"],
        clip["sha256"],
        clip["frames"],
    )
    stat_record = (directory / SPEECH_RECORD_FILE).lstat()
    assert stat.S_IMODE(stat_record.st_mode) == 0o600, "speech.json is not private"
    # The reference the record names is the reference run's own normalized clip.
    dataset = FineTuneDataset.model_validate_json(
        (
            layout.root / FINETUNE_OUTPUT_ROOT / reference_run_id / "dataset" / "dataset.json"
        ).read_bytes()
    )
    reference = next(item for item in dataset.clips if item.prompt_id == reference_prompt_id)
    assert record.reference_audio_sha256 == reference.sha256
    return audio.read_bytes()


def verify(root: Path, prepared: NativeSpeakFixture) -> dict[str, object]:
    """Check the ``speak`` fixture root; write ``root/python-speak-evidence.json``.

    Returns: the evidence written.
    Raises: ``AssertionError`` naming the first disagreement.
    """

    fixture = prepared.fixture
    report = json.loads((root / "native" / "speak-report.json").read_text())
    layout = AssetLayout.validate(fixture.layout.root)

    # The service listed the two written runs, newest first, with their reference prompts.
    runs = finished_finetune_runs(layout)
    assert [run.run_id for run in runs] == [LATEST_RUN_ID, EARLIER_RUN_ID], runs
    assert list(runs[0].reference_prompt_ids) == [
        FIRST_REFERENCE_PROMPT_ID,
        SECOND_REFERENCE_PROMPT_ID,
    ]
    assert list(runs[1].reference_prompt_ids) == [SECOND_REFERENCE_PROMPT_ID]
    assert report["model_choices"] == [
        {"id": "latest", "title": f"Latest · {LATEST_RUN_ID}"},
        {"id": EARLIER_RUN_ID, "title": EARLIER_RUN_ID},
        {"id": "base", "title": "Base"},
    ]
    assert report["reference_choices"] == [FIRST_REFERENCE_PROMPT_ID, SECOND_REFERENCE_PROMPT_ID]
    assert report["empty_text_reason"] == "Type the words to speak"
    for run_id in (LATEST_RUN_ID, EARLIER_RUN_ID):
        run = FineTuneRun.model_validate_json(
            (layout.root / FINETUNE_OUTPUT_ROOT / run_id / "run.json").read_bytes()
        )
        assert run.status == "finished", "Speak changed a finished run"

    # Every finished request: a private, header-checked clip bound by its speech record.
    clips = {clip["operation_id"]: clip for clip in report["clips"]}
    requests = report["requests"]
    assert set(requests) == set(EXPECTED_SPEAKERS)
    assert sorted(clips) == sorted(requests.values()), (
        "the app kept a clip the suite did not ask for"
    )
    clip_bytes = {
        name: _require_clip(layout, clips[operation], EXPECTED_SPEAKERS[name])
        for name, operation in requests.items()
    }
    # The clip that was played is the latest request's, loaded as a speech source.
    played = report["played"]
    assert played["operation_id"] == requests["latest"] and played["format"] == "speech"
    assert played["source_frames"] == clips[requests["latest"]]["frames"]
    assert played["rendered_frames"] == played["source_frames"] // 2 > 0
    assert played["phase_after_pause"] == "paused"
    # The engine rendered the clip's own samples: their peak over the rendered half.
    with wave.open(str(layout.root / clips[requests["latest"]]["relative_path"]), "rb") as handle:
        pcm = handle.readframes(played["rendered_frames"])
    peak = max(
        abs(int.from_bytes(pcm[index : index + 2], "little", signed=True)) / 32_768
        for index in range(0, len(pcm), 2)
    )
    assert peak > 0 and abs(played["rendered_peak"] - peak) < 1e-6, (played, peak)

    # Cancelled and past-deadline requests leave no output behind.
    cancelled = report["cancelled"]
    deadline = report["deadline"]
    speaking = report["gpu_exclusion"]["speaking_operation_id"]
    for operation in (cancelled["operation_id"], deadline["operation_id"], speaking):
        assert not os.path.lexists(layout.root / speech_directory(operation)), operation
    assert cancelled["phases"] == ["queued", "running", "cancelled"], cancelled
    assert cancelled["cancelling_line"] == "Cancelling Speak"
    assert deadline["phases"] == ["queued", "running", "failed"], deadline
    assert deadline["error_code"] == "FINETUNE.SYNTHESIS_DEADLINE"
    assert deadline["status_line"] == DEADLINE_MESSAGE, deadline["status_line"]
    speech_root = layout.root / SPEECH_OUTPUT_ROOT
    assert sorted(path.name for path in speech_root.iterdir()) == sorted(requests.values()), (
        "derived/speech holds output the suite did not keep"
    )

    # One GPU slot: Fine-tune model waited for Speak (nothing was submitted), and Speak waited
    # for the one fine-tune job, which was cancelled without touching the finished runs.
    exclusion = report["gpu_exclusion"]
    assert exclusion["fine_tune_enabled_while_speaking"] is False
    assert exclusion["fine_tune_help_while_speaking"].startswith("Wait for Speak to finish")
    assert exclusion["speak_reason_while_training"] == "Speak is unavailable while a fine-tune runs"
    jobs = layout.root / FINETUNE_JOBS_ROOT
    job = exclusion["fine_tune_operation_id"]
    assert [path.stem for path in (jobs / "requests").iterdir()] == [job], (
        "the app submitted a fine-tune the suite did not start"
    )
    intent = read_recording_evidence(jobs / "requests" / f"{job}.json", FineTuneJobIntent)
    assert intent.command.allow_missing is True and intent.catalog_reference == fixture.reference
    ended = read_recording_evidence(jobs / "terminal" / f"{job}.json", FineTuneJob)
    assert ended.state == "cancelled" and ended.run_id is not None and ended.run_root is not None
    assert read_recording_evidence(jobs / "head.json", FineTuneJobHead).job is None
    # Every Speak request ended, so none is marked running for command-line training.
    assert not os.path.lexists(layout.root / SPEECH_ACTIVE_MARKER), "a Speak marker remains"
    stopped = FineTuneRun.model_validate_json(
        (layout.root / ended.run_root / "run.json").read_bytes()
    )
    assert stopped.status == "failed" and stopped.error == CANCELLED_BY_OPERATOR

    # The capture interlock, and the take saved under it.
    interlock = report["interlock"]
    during = interlock["during_capture"]
    assert during["phase"] == "recording" and during["speak_capture_owned"] is True
    assert during["speak_playback_phase"] in {"stopped", "paused"}
    assert during["can_play"] is False
    assert during["play_reason"] == "Playback is unavailable while recording"
    assert during["can_speak"] is False
    assert during["speak_reason"] == "Speak is unavailable while recording"
    assert during["can_switch_page"] is False
    assert during["page_switch_reason"] == "Finish the current take before switching pages"
    assert interlock["after_stop"] == {"speak_capture_owned": False, "can_speak": True}
    assert interlock["while_saving"] == {
        "phase": "saving",
        "speak_reason": "Wait for the take to finish saving before using Speak",
        "page_switch_reason": "Wait for the take to finish saving before switching pages",
    }
    assert interlock["after_save_operation_id"] == requests["after_save"]
    store = RecordingTakeStore(layout, read_only=True)
    take = store.get_take(interlock["take_id"])
    assert take is not None and take.seal is not None and take.decision == "kept"
    assert sha256_file(layout.root / take.seal.original_path)[0] == take.seal.original_sha256

    # Save As: a byte-identical copy outside the library, never a link to the clip.
    save_as = report["save_as"]
    assert save_as["operation_id"] == requests["latest"] and save_as["notice"] == "Saved speech.wav"
    copy = Path(save_as["path"])
    assert copy == root / "native" / "save-as" / "speech.wav"
    assert not copy.resolve().is_relative_to(layout.root.resolve())
    assert copy.read_bytes() == clip_bytes["latest"]
    assert all(copy.read_bytes() != clip_bytes[name] for name in ("base", "earlier")), (
        "the clips are not distinguishable, so the copy proves nothing"
    )
    clip_file = layout.root / clips[requests["latest"]]["relative_path"]
    assert copy.lstat().st_ino != clip_file.lstat().st_ino and clip_file.lstat().st_nlink == 1
    assert sorted(path.name for path in copy.parent.iterdir()) == ["speech.wav"]

    # What VoiceOver would have been told, and the fixed Record geometry.
    announcements = report["announcements"]
    # Seven requests were sent; the presses refused during the take and the fine-tune were not.
    assert announcements.count("Generating speech") == 7, announcements
    assert "Speech ready, under 0.1 seconds" in announcements
    assert DEADLINE_MESSAGE in announcements
    assert report["fixed_geometry"] is True and report["speak_page_region_probes"] == 0
    assert report["visible_windows"] == 0, "the harness showed a window"

    _require_no_fixture_processes(layout.root, fixture.runtime_root)
    evidence: dict[str, object] = {
        "finished_runs": [run.run_id for run in runs],
        "clips": {
            name: {"operation_id": operation, "sha256": clips[operation]["sha256"]}
            for name, operation in requests.items()
        },
        "clip_digest": canonical_sha256(sorted(clip["sha256"] for clip in report["clips"])),
        "cancelled_operation_id": cancelled["operation_id"],
        "deadline_message": DEADLINE_MESSAGE,
        "fine_tune_cancelled_run_id": ended.run_id,
        "saved_take_id": interlock["take_id"],
        "save_as_bytes": len(clip_bytes["latest"]),
        "fixture_processes_remaining": 0,
    }
    write_canonical_json(root / "python-speak-evidence.json", evidence)
    return evidence
