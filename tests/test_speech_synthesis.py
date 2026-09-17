"""Speak: one text spoken by a finished fine-tune run or the pinned base weights.

Covers the engine (finetune.py: run ordering, model and reference resolution, the speech
clip, the digest cache), the supervisor's second job kind (recording_finetune_jobs.py: one
GPU slot, deadline, cancel, digest reuse) and the recording service's dispatch. Runs are
written with real clip and checkpoint files; the F5 runtime is the stub from
finetune_stub_runtime.py, whose few-byte pretrained files are accepted through the
``pretrained_pin`` test seam the shipped service never uses.
"""

from __future__ import annotations

import os
import select
import signal
import stat
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from finetune_stub_runtime import stub_finetune_runtime, write_stub_mode
from native_finetune_service_fixture import format_pins
from test_pilot_v2_coverage import SUBJECT, released_pilot_v2
from test_recording_finetune_jobs import Events, live_group, plenty, wait_until
from test_recording_library import START
from test_recording_packages import analyzer_build as analyzer_build
from test_recording_packages import package_rig as package_rig
from test_recording_service import build_identity, handshake, read_response, request

import cole_voice_lab.finetune as finetune_module
import cole_voice_lab.recording_finetune_jobs as jobs_module
from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.finetune import (
    MAX_TRUSTED_DIGESTS,
    FineTuneCheckpoint,
    FineTuneCorpus,
    FineTuneCorpusEntry,
    FineTuneDataset,
    FineTuneDatasetClip,
    FineTuneDigestCache,
    FineTuneError,
    FineTuneHyperparameters,
    FineTunePretrainedPins,
    FineTuneRun,
    FineTuneRuntimeIdentity,
    FineTuneTrustedDigests,
    SpeechClip,
    describe_finetune_runtime,
    discard_speech_output,
    finished_finetune_run_ids,
    finished_finetune_runs,
    latest_finished_finetune_run,
    load_finetune_dataset,
    load_finetune_run_inputs,
    resolve_speech_model,
    speech_reference_candidates,
    synthesize_speech,
)
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    CancelSynthesisCommand,
    FineTuneCommand,
    RecordingError,
    RecordingResponse,
    SaveEvent,
    ShutdownCommand,
    SynthesisEvent,
    SynthesizeCommand,
    recording_frame_bytes,
)
from cole_voice_lab.recording_finetune_jobs import (
    FINETUNE_WORKER_MODULE,
    FineTuneJobError,
    FineTuneJobQueue,
    FineTuneSupervisor,
    SpeechWorkerReply,
    SpeechWorkerRequest,
    SpeechWorkerResolution,
    decode_finetune_worker_message,
    process_table,
    run_speech_worker_request,
    worker_command_marker,
)
from cole_voice_lab.recording_service import (
    RecordingCancellation,
    RecordingFrameDecoder,
    RecordingServiceError,
    RecordingWorkspaceBackend,
    recording_service_build_identity,
)

REPOSITORY = Path(__file__).resolve().parents[1]
CLIP_FRAMES = 2_400
# Catalog order: a reference candidate without a recording, one with, then a plain prompt.
REFERENCE_ENTRIES = ("reference-neutral-01", "reference-warm-01", "neutral-01")
REFERENCE_CLIPS = ("reference-warm-01", "neutral-01")
PLAIN_ENTRIES = ("neutral-01", "neutral-02")


# -- a library with finished runs ---------------------------------------------------------------


def write_wav(path: Path, frames: int = CLIP_FRAMES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(bytes(index % 251 for index in range(frames * 2)))
    path.chmod(0o600)


def corpus_entry(prompt_id: str) -> FineTuneCorpusEntry:
    prompt = next(item for item in released_pilot_v2().prompts if item.prompt_id == prompt_id)
    return FineTuneCorpusEntry(
        prompt_id=prompt_id,
        prompt_sha256=canonical_sha256(prompt),
        recorded_prompt_sha256=canonical_sha256(prompt),
        prompt=prompt,
        take_id=f"take-{prompt_id}",
        source_kind="native",
        source_path=f"tmp/recording-takes/take-{prompt_id}/original.wav",
        source_sha256="0" * 64,
        source_frames=48_000,
        source_sample_rate_hz=48_000,
        recorded_at=None,
        date_precision="unknown",
    )


def write_speech_run(
    layout: AssetLayout,
    run_id: str,
    *,
    status: str = "finished",
    finished_minute: int = 10,
    entries: tuple[str, ...] = REFERENCE_ENTRIES,
    clips: tuple[str, ...] = REFERENCE_CLIPS,
    checkpoint: bool = True,
) -> FineTuneRun:
    """A run directory bound like a trained one: clips, corpus, dataset, checkpoint, run.json."""
    catalog = released_pilot_v2()
    run_root = f"derived/finetune/{run_id}"
    directory = layout.root / run_root
    corpus = FineTuneCorpus(
        subject_id=SUBJECT,
        catalog_suite_id=catalog.suite_id,
        catalog_sha256=canonical_sha256(catalog),
        entries=tuple(corpus_entry(prompt_id) for prompt_id in entries),
        missing_prompt_ids=(),
        unknown_record_ids=(),
        selected_at=START,
    )
    texts = {entry.prompt_id: entry.text for entry in corpus.entries}
    dataset_clips = []
    for prompt_id in clips:
        path = directory / "dataset" / "wavs" / f"{prompt_id}.wav"
        write_wav(path)
        dataset_clips.append(
            FineTuneDatasetClip(
                prompt_id=prompt_id,
                take_id=f"take-{prompt_id}",
                relative_path=f"wavs/{prompt_id}.wav",
                sha256=sha256_file(path)[0],
                frames=CLIP_FRAMES,
                gain_db=0.0,
                text=texts[prompt_id],
            )
        )
    dataset = FineTuneDataset(
        corpus_sha256=canonical_sha256(corpus),
        dataset_root=f"{run_root}/dataset",
        normalization_rule="synthetic",
        clips=tuple(dataset_clips),
        metadata_csv_sha256="4" * 64,
        materialized_at=START,
    )
    write_canonical_json(directory / "dataset" / "corpus.json", corpus)
    write_canonical_json(directory / "dataset" / "dataset.json", dataset)
    checkpoints: tuple[FineTuneCheckpoint, ...] = ()
    if checkpoint:
        weights = directory / "checkpoints" / "model_last.pt"
        weights.parent.mkdir(mode=0o700)
        weights.write_bytes(f"fine-tuned weights of {run_id}".encode())
        digest, size = sha256_file(weights)
        checkpoints = (
            FineTuneCheckpoint(
                relative_path=f"{run_root}/checkpoints/model_last.pt",
                sha256=digest,
                size_bytes=size,
                update=0,
                kind="last",
            ),
        )
    run = FineTuneRun(
        run_id=run_id,
        subject_id=SUBJECT,
        catalog_sha256=corpus.catalog_sha256,
        corpus_sha256=canonical_sha256(corpus),
        dataset_sha256=canonical_sha256(dataset),
        hyperparameters=FineTuneHyperparameters(epochs=3),
        runtime=FineTuneRuntimeIdentity(
            runtime_root="/synthetic/runtime",
            python_sha256="5" * 64,
            f5_tts_version="stub",
            torch_version="stub",
            pretrained_path="/synthetic/runtime/pretrained/model.safetensors",
            pretrained_sha256="6" * 64,
        ),
        run_root=run_root,
        log_path=f"{run_root}/train.log",
        checkpoints=checkpoints,
        status=status,
        started_at=START,
        finished_at=None if status == "running" else START + timedelta(minutes=finished_minute),
        error="stopped" if status == "failed" else None,
    )
    write_canonical_json(directory / "run.json", run)
    return run


@dataclass(frozen=True)
class Speech:
    """An empty library and a stub F5 runtime with its pins."""

    layout: AssetLayout
    runtime: Path
    pin: FineTunePretrainedPins

    def mode(self, **mode: Any) -> None:
        write_stub_mode(self.runtime, mode)

    def path(self, relative: str) -> Path:
        return self.layout.root / relative

    def checkpoint(self, run_id: str) -> Path:
        return self.path(f"derived/finetune/{run_id}/checkpoints/model_last.pt")

    def speak(
        self, run_id: str | None = None, operation_id: str = "speak-1", **options: Any
    ) -> Any:
        """Resolve ``run_id`` (``latest`` when absent) and speak one short text."""
        resolution = resolve_speech_model(self.layout, run_id or "latest")
        options.setdefault("text", "Hello from the stub.")
        options.setdefault("pretrained_pin", self.pin)
        return synthesize_speech(
            self.layout, resolution, self.runtime, operation_id=operation_id, **options
        )


@pytest.fixture
def speech(tmp_path: Path) -> Speech:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    runtime, pin = stub_finetune_runtime(tmp_path.resolve() / "runtime")
    return Speech(layout, runtime, pin)


def refused(code: str, call: Callable[[], Any]) -> FineTuneError:
    with pytest.raises(FineTuneError) as failure:
        call()
    assert failure.value.code == code, failure.value
    return failure.value


# -- the finished runs and the model choice -------------------------------------------------------


def test_finished_runs_are_ordered_newest_first_and_limited(speech: Speech) -> None:
    layout = speech.layout
    assert finished_finetune_run_ids(layout) == () and finished_finetune_runs(layout) == ()
    write_speech_run(layout, "finetune-a", finished_minute=10)
    write_speech_run(layout, "finetune-b", finished_minute=30)
    write_speech_run(layout, "finetune-c", finished_minute=30)  # a tie: the greater id first
    write_speech_run(layout, "finetune-d", status="running")
    write_speech_run(layout, "finetune-e", status="failed", finished_minute=50)
    (layout.root / "derived/finetune/dataset-only/dataset").mkdir(parents=True)
    assert finished_finetune_run_ids(layout) == ("finetune-c", "finetune-b", "finetune-a")
    runs = finished_finetune_runs(layout)
    assert [run.run_id for run in runs] == ["finetune-c", "finetune-b", "finetune-a"]
    assert [run.run_id for run in finished_finetune_runs(layout, limit=2)] == [
        "finetune-c",
        "finetune-b",
    ]
    latest = latest_finished_finetune_run(layout)
    assert latest == runs[0]
    # Speak's reference choices: candidates with a recording, in catalog order.
    assert latest is not None and latest.reference_prompt_ids == ("reference-warm-01",)
    with pytest.raises(ValueError, match="at least one"):
        finished_finetune_runs(layout, limit=0)


def test_run_inputs_load_bound_together_or_not_at_all(speech: Speech) -> None:
    layout = speech.layout
    run = write_speech_run(layout, "finetune-a")
    loaded, dataset, corpus = load_finetune_run_inputs(layout, run.run_root)
    assert loaded == run and canonical_sha256(dataset) == run.dataset_sha256
    assert speech_reference_candidates(dataset, corpus) == ("reference-warm-01",)
    assert load_finetune_dataset(layout, run.run_root) == (dataset, corpus)
    write_canonical_json(
        layout.root / run.run_root / "run.json", run.model_copy(update={"run_root": "derived/x"})
    )
    assert "does not match" in str(
        refused("FINETUNE.SOURCE_CHANGED", lambda: load_finetune_run_inputs(layout, run.run_root))
    )
    corpus_path = layout.root / run.run_root / "dataset" / "corpus.json"
    write_canonical_json(corpus_path, corpus.model_copy(update={"subject_id": "someone-else"}))
    assert "does not match its corpus" in str(
        refused("FINETUNE.SOURCE_CHANGED", lambda: load_finetune_dataset(layout, run.run_root))
    )


def test_latest_speaks_with_the_newest_run_and_its_first_recorded_reference(
    speech: Speech,
) -> None:
    layout = speech.layout
    write_speech_run(layout, "finetune-a", finished_minute=10)
    newest = write_speech_run(layout, "finetune-b", finished_minute=30)
    write_speech_run(layout, "finetune-c", status="running")
    resolution = resolve_speech_model(layout, "latest")
    assert resolution.run == newest and resolution.reference_run == newest
    assert (resolution.model, resolution.run_id) == ("finetune-b", "finetune-b")
    # reference-neutral-01 is the first candidate but has no recording in this run.
    assert resolution.reference.prompt_id == "reference-warm-01"
    named = resolve_speech_model(layout, "latest", reference_prompt_id="neutral-01")
    assert named.reference.prompt_id == "neutral-01"  # any recorded prompt may be named


def test_base_speaks_with_the_pinned_weights_and_the_latest_runs_reference(
    speech: Speech,
) -> None:
    layout = speech.layout
    write_speech_run(layout, "finetune-a", finished_minute=10)
    newest = write_speech_run(
        layout, "finetune-b", finished_minute=30, entries=PLAIN_ENTRIES, clips=PLAIN_ENTRIES
    )
    resolution = resolve_speech_model(layout, "base")
    assert resolution.run is None and (resolution.model, resolution.run_id) == ("base", None)
    assert resolution.reference_run == newest
    # No reference candidate in this run: its first clip.
    assert resolution.reference.prompt_id == "neutral-01"


def test_a_chosen_run_speaks_only_once_it_has_finished(speech: Speech) -> None:
    layout = speech.layout
    earlier = write_speech_run(layout, "finetune-a", finished_minute=10)
    write_speech_run(layout, "finetune-b", finished_minute=30)
    write_speech_run(layout, "finetune-c", status="running")
    write_speech_run(layout, "finetune-d", status="failed")
    chosen = resolve_speech_model(layout, "finetune-a")
    assert chosen.run == earlier and chosen.reference_run == earlier
    assert "finetune-c is running" in str(
        refused("FINETUNE.NO_FINISHED_RUN", lambda: resolve_speech_model(layout, "finetune-c"))
    )
    assert "finetune-d is failed" in str(
        refused("FINETUNE.NO_FINISHED_RUN", lambda: resolve_speech_model(layout, "finetune-d"))
    )
    assert "there is no fine-tune run finetune-z" in str(
        refused("FINETUNE.NO_FINISHED_RUN", lambda: resolve_speech_model(layout, "finetune-z"))
    )
    with pytest.raises(ValueError):
        resolve_speech_model(layout, "Not A Run")
    # A record filed under another directory name is refused, whoever asks.
    stray = write_speech_run(layout, "finetune-x")
    write_canonical_json(
        layout.root / stray.run_root / "run.json", stray.model_copy(update={"run_id": "finetune-y"})
    )
    assert "names finetune-y" in str(
        refused("FINETUNE.SOURCE_CHANGED", lambda: resolve_speech_model(layout, "finetune-x"))
    )
    refused("FINETUNE.SOURCE_CHANGED", lambda: resolve_speech_model(layout, "latest"))


def test_nothing_speaks_before_a_run_has_finished(speech: Speech) -> None:
    layout = speech.layout
    for model in ("latest", "base", "finetune-a"):
        message = str(
            refused(
                "FINETUNE.NO_FINISHED_RUN",
                lambda model=model: resolve_speech_model(layout, model),
            )
        )
        assert "fine-tune once before using Speak" in message
    write_speech_run(layout, "finetune-a", status="running")
    for model in ("latest", "base"):
        refused("FINETUNE.NO_FINISHED_RUN", lambda model=model: resolve_speech_model(layout, model))
    assert "is running" in str(
        refused("FINETUNE.NO_FINISHED_RUN", lambda: resolve_speech_model(layout, "finetune-a"))
    )


def test_a_named_reference_needs_a_recording_in_the_reference_run(speech: Speech) -> None:
    layout = speech.layout
    write_speech_run(layout, "finetune-a")
    for model in ("latest", "base", "finetune-a"):
        message = str(
            refused(
                "FINETUNE.REFERENCE_MISSING",
                lambda model=model: resolve_speech_model(
                    layout, model, reference_prompt_id="reference-neutral-01"
                ),
            )
        )
        assert "reference-neutral-01 has no recording in fine-tune run finetune-a" in message


# -- the clip -------------------------------------------------------------------------------------


def test_speech_is_private_header_checked_and_bound_to_what_spoke(speech: Speech) -> None:
    run = write_speech_run(speech.layout, "finetune-a")
    clip = speech.speak(text="Line one.\nLine two.", nfe_steps=8)
    directory = speech.path("derived/speech/speak-1")
    assert sorted(path.name for path in directory.iterdir()) == ["speech-01.wav", "speech.json"]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    audio = directory / "speech-01.wav"
    assert stat.S_IMODE(audio.stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / "speech.json").stat().st_mode) == 0o600
    with wave.open(str(audio), "rb") as handle:
        header = (handle.getframerate(), handle.getnchannels(), handle.getsampwidth())
        frames = handle.getnframes()
    assert header == (24_000, 1, 2) and clip.frames == frames == 240
    assert clip.seconds == 0.01 and clip.sample_rate_hz == 24_000
    assert (clip.relative_path, clip.sha256) == (
        "derived/speech/speak-1/speech-01.wav",
        sha256_file(audio)[0],
    )
    assert (clip.model, clip.reference_run_id, clip.reference_prompt_id) == (
        "finetune-a",
        "finetune-a",
        "reference-warm-01",
    )
    assert clip.checkpoint_sha256 == run.checkpoints[0].sha256
    dataset = load_finetune_dataset(speech.layout, run.run_root)[0]
    reference = next(item for item in dataset.clips if item.prompt_id == "reference-warm-01")
    assert clip.reference_audio_sha256 == reference.sha256
    assert (clip.text, clip.nfe_steps, clip.use_ema) == ("Line one.\nLine two.", 8, True)
    record = (directory / "speech.json").read_text()
    assert SpeechClip.model_validate_json(record) == clip
    assert str(speech.layout.root) not in record and str(speech.runtime) not in record
    base = synthesize_speech(
        speech.layout,
        resolve_speech_model(speech.layout, "base"),
        speech.runtime,
        operation_id="speak-2",
        text="From the base weights.",
        pretrained_pin=speech.pin,
    )
    assert (base.model, base.reference_run_id) == ("base", "finetune-a")
    assert base.checkpoint_sha256 == speech.pin.weights.sha256


def test_the_speech_record_binds_its_own_clip() -> None:
    clip = {
        "operation_id": "speak-1",
        "model": "finetune-a",
        "reference_run_id": "finetune-a",
        "reference_prompt_id": "reference-warm-01",
        "reference_audio_sha256": "1" * 64,
        "checkpoint_sha256": "2" * 64,
        "use_ema": True,
        "nfe_steps": 32,
        "text": "Hello.",
        "relative_path": "derived/speech/speak-1/speech-01.wav",
        "sha256": "3" * 64,
        "frames": 240,
        "seconds": 0.01,
        "created_at": START,
    }
    assert SpeechClip.model_validate(clip).sample_rate_hz == 24_000
    for changes, message in (
        ({"relative_path": "derived/speech/speak-2/speech-01.wav"}, "speech-01.wav"),
        ({"relative_path": "derived/speech/speak-1/speech-02.wav"}, "speech-01.wav"),
        ({"model": "finetune-b"}, "its own reference"),
        ({"seconds": 1.0}, "frames at its sample rate"),
        ({"nfe_steps": 3}, "greater than or equal to 4"),
        ({"text": "​"}, "control characters"),
    ):
        with pytest.raises(ValueError, match=message):
            SpeechClip.model_validate(clip | changes)
    assert SpeechClip.model_validate(clip | {"model": "base"}).model == "base"


def test_speech_refuses_what_it_cannot_say_before_writing(speech: Speech) -> None:
    write_speech_run(speech.layout, "finetune-a")
    for text in ("", "   \n ", "Tab\there", "x" * 2_001):
        assert "Speak cannot say this text" in str(
            refused("FINETUNE.UNSUPPORTED_TEXT", lambda text=text: speech.speak(text=text))
        )
    for steps in (3, 65):
        with pytest.raises(ValueError, match="nfe_steps must be 4 to 64"):
            speech.speak(nfe_steps=steps)
    with pytest.raises(ValueError):
        speech.speak(operation_id="Speak One")
    assert not speech.path("derived/speech").exists()


def test_a_reused_operation_is_refused_and_keeps_its_clip(speech: Speech) -> None:
    write_speech_run(speech.layout, "finetune-a")
    first = speech.speak()
    message = str(refused("FINETUNE.RUN_EXISTS", lambda: speech.speak(text="Again.")))
    assert "derived/speech/speak-1 already exists" in message
    assert sha256_file(speech.path(first.relative_path))[0] == first.sha256


def test_a_changed_or_missing_checkpoint_or_reference_is_refused(speech: Speech) -> None:
    layout = speech.layout
    write_speech_run(layout, "finetune-a")
    checkpoint = speech.checkpoint("finetune-a")
    original = checkpoint.read_bytes()
    checkpoint.write_bytes(original.replace(b"weights", b"weightz"))
    assert "changed since fine-tune run finetune-a finished" in str(
        refused("FINETUNE.SOURCE_CHANGED", lambda: speech.speak(operation_id="speak-1"))
    )
    checkpoint.unlink()
    refused("FINETUNE.CHECKPOINT_MISSING", lambda: speech.speak(operation_id="speak-2"))
    write_speech_run(layout, "finetune-b", finished_minute=20, checkpoint=False)
    assert "records no derived/finetune/finetune-b/checkpoints/model_last.pt" in str(
        refused("FINETUNE.CHECKPOINT_MISSING", lambda: speech.speak(operation_id="speak-3"))
    )
    write_speech_run(layout, "finetune-c", finished_minute=30)
    clip = layout.root / "derived/finetune/finetune-c/dataset/wavs/reference-warm-01.wav"
    clip.write_bytes(clip.read_bytes()[:-2] + b"\x01\x01")
    assert "reference-warm-01 of fine-tune run finetune-c changed since packing" in str(
        refused("FINETUNE.SOURCE_CHANGED", lambda: speech.speak(operation_id="speak-4"))
    )
    clip.unlink()
    assert "is missing or not a regular file" in str(
        refused("FINETUNE.SOURCE_CHANGED", lambda: speech.speak(operation_id="speak-5"))
    )


@pytest.mark.parametrize(
    ("mode", "text"),
    [
        ({"synthesis_format": [48_000, 1, 2]}, "as 48000 Hz, 1 channel(s), 16-bit"),
        ({"synthesis_format": [24_000, 2, 2]}, "as 24000 Hz, 2 channel(s), 16-bit"),
        ({"synthesis_format": [24_000, 1, 3]}, "as 24000 Hz, 1 channel(s), 24-bit"),
        ({"fail_synthesis": True}, "speech synthesis with finetune-a failed (exit 1)"),
        ({"synthesis_report": "{not json"}, "printed an unreadable report"),
        ({"synthesis_report": '{"index": 1}'}, "printed an unreadable report"),
        ({"synthesis_report": '{"index": 2, "path": "CLIP"}'}, "which it was not asked for"),
        ({"synthesis_report": '{"index": true, "path": "CLIP"}'}, "which it was not asked for"),
        ({"synthesis_report": '{"index": 1, "path": "/tmp/speech-01.wav"}'}, "not asked for"),
        (
            {"synthesis_report": '{"index": 1, "path": "CLIP"}\n{"index": 1, "path": "CLIP"}'},
            "which it was not asked for",
        ),
        ({"synthesis_report": "done"}, "synthesizer reported 0 of 1 samples"),
    ],
)
def test_a_synthesizer_that_writes_or_reports_the_wrong_audio_fails_by_name(
    speech: Speech, mode: dict[str, Any], text: str
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    clip = str(speech.path("derived/speech/speak-1/speech-01.wav"))
    if "synthesis_report" in mode:
        mode = {"synthesis_report": mode["synthesis_report"].replace("CLIP", clip)}
    speech.mode(**mode)
    failure = refused("FINETUNE.RUNTIME_FAILED", speech.speak)
    assert text in str(failure)
    # The request's partial output is the service's to discard.
    assert discard_speech_output(speech.layout, "speak-1") is True
    assert not speech.path("derived/speech/speak-1").exists()


@pytest.mark.parametrize("damage", ["empty", "garbage", "link", "truncated", "silent"])
def test_a_clip_that_is_not_complete_private_pcm_is_refused(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "speech-01.wav"
    if damage == "empty":
        path.write_bytes(b"")
    elif damage == "garbage":
        path.write_bytes(b"RIFF-but-not-a-wave-file")
    elif damage == "link":
        write_wav(tmp_path / "elsewhere.wav")
        path.symlink_to(tmp_path / "elsewhere.wav")
    elif damage == "truncated":
        write_wav(path, frames=240)
        path.write_bytes(path.read_bytes()[:-100])
    else:
        write_wav(path, frames=0)
    with pytest.raises(FineTuneError) as failure:
        finetune_module._checked_clip(path)
    assert failure.value.code == "FINETUNE.RUNTIME_FAILED"
    expected = "empty or truncated" if damage in {"truncated", "silent"} else "not a PCM WAV file"
    assert expected in str(failure.value)


def test_discarding_speech_output_removes_only_a_real_directory(speech: Speech) -> None:
    layout = speech.layout
    assert discard_speech_output(layout, "speak-1") is False
    directory = speech.path("derived/speech/speak-1")
    directory.mkdir(parents=True)
    (directory / "speech-01.wav").write_bytes(b"partial")
    assert discard_speech_output(layout, "speak-1") is True and not directory.exists()
    directory.write_bytes(b"not a directory")
    with pytest.raises(SecurityError, match="not a directory"):
        discard_speech_output(layout, "speak-1")
    directory.unlink()
    elsewhere = layout.root / "derived" / "elsewhere"
    elsewhere.mkdir()
    directory.symlink_to(elsewhere)
    with pytest.raises(SecurityError):
        discard_speech_output(layout, "speak-1")
    assert elsewhere.is_dir()


# -- digests verified once --------------------------------------------------------------------


def hashing_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hashed: list[str] = []
    original = finetune_module.sha256_file

    def record(path: Path) -> tuple[str, int]:
        hashed.append(path.name)
        return original(path)

    monkeypatch.setattr(finetune_module, "sha256_file", record)
    return hashed


def probe_counter(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    probes: list[Path] = []
    original = finetune_module._probe_runtime

    def count(python: Path) -> Any:
        probes.append(python)
        return original(python)

    monkeypatch.setattr(finetune_module, "_probe_runtime", count)
    return probes


LARGE_FILES = {"model_1250000.safetensors", "pytorch_model.bin", "config.yaml", "model_last.pt"}


def test_repeated_speech_reuses_verified_digests_until_a_file_changes(
    speech: Speech, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    hashed = hashing_recorder(monkeypatch)
    probes = probe_counter(monkeypatch)
    first = FineTuneDigestCache()
    speech.speak(operation_id="speak-1", digests=first)
    assert LARGE_FILES <= set(hashed) and len(probes) == 1
    trusted = first.trusted()
    assert trusted.probe is not None
    assert {Path(entry.identity.path).name for entry in trusted.files} >= LARGE_FILES
    # The next request hashes only what it must: the reference clip.
    hashed.clear()
    second = FineTuneDigestCache(trusted)
    speech.speak(operation_id="speak-2", digests=second)
    assert not LARGE_FILES & set(hashed) and hashed == ["reference-warm-01.wav"]
    assert len(probes) == 1
    # A touched checkpoint is hashed again (and still matches); a changed one is refused.
    checkpoint = speech.checkpoint("finetune-a")
    os.utime(checkpoint, ns=(time.time_ns(), time.time_ns() + 1_000_000_000))
    hashed.clear()
    third = FineTuneDigestCache(second.trusted())
    speech.speak(operation_id="speak-3", digests=third)
    assert "model_last.pt" in hashed and "model_1250000.safetensors" not in hashed
    checkpoint.write_bytes(checkpoint.read_bytes().replace(b"weights", b"weightz"))
    refused(
        "FINETUNE.SOURCE_CHANGED",
        lambda: speech.speak(operation_id="speak-4", digests=FineTuneDigestCache(third.trusted())),
    )
    # A pinned file changed in place is refused although its old digest was trusted.
    weights = speech.runtime / "pretrained/F5TTS_v1_Base/model_1250000.safetensors"
    weights.write_bytes(weights.read_bytes().replace(b"weights", b"weightz"))
    refused(
        "FINETUNE.PRETRAINED_MISMATCH",
        lambda: describe_finetune_runtime(
            speech.runtime, pretrained_pin=speech.pin, digests=FineTuneDigestCache(trusted)
        ),
    )


def test_the_runtime_probe_is_reused_until_its_packages_change(
    speech: Speech, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = probe_counter(monkeypatch)
    cache = FineTuneDigestCache()
    described = describe_finetune_runtime(speech.runtime, pretrained_pin=speech.pin, digests=cache)
    again = describe_finetune_runtime(speech.runtime, pretrained_pin=speech.pin, digests=cache)
    assert again == described and len(probes) == 1
    trusted = cache.trusted()
    reused = describe_finetune_runtime(
        speech.runtime, pretrained_pin=speech.pin, digests=FineTuneDigestCache(trusted)
    )
    assert reused.identity == described.identity and len(probes) == 1
    # Installing a package changes site-packages: the runtime is probed again.
    site_packages = described.package_path.parent
    (site_packages / "another_package").mkdir()
    describe_finetune_runtime(
        speech.runtime, pretrained_pin=speech.pin, digests=FineTuneDigestCache(trusted)
    )
    assert len(probes) == 2
    # Another runtime never reuses this one's answer.
    other, other_pin = stub_finetune_runtime(speech.runtime.parent / "other-runtime")
    describe_finetune_runtime(other, pretrained_pin=other_pin, digests=FineTuneDigestCache(trusted))
    assert len(probes) == 3


def test_a_file_changing_while_it_is_hashed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "weights.bin"
    path.write_bytes(b"weights")
    original = finetune_module.sha256_file

    def racing(target: Path) -> tuple[str, int]:
        result = original(target)
        with target.open("ab") as stream:
            stream.write(b"!")
        return result

    monkeypatch.setattr(finetune_module, "sha256_file", racing)
    with pytest.raises(FineTuneError, match="changed while it was being hashed"):
        FineTuneDigestCache().sha256(path)


def test_trusted_digests_are_bounded_and_name_each_file_once(tmp_path: Path) -> None:
    cache = FineTuneDigestCache()
    for index in range(MAX_TRUSTED_DIGESTS + 1):
        path = tmp_path / f"file-{index:02d}"
        path.write_bytes(str(index).encode())
        cache.sha256(path)
    files = cache.trusted().files
    assert len(files) == MAX_TRUSTED_DIGESTS
    assert Path(files[0].identity.path).name == "file-01"  # the oldest was dropped
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "file-00")
    with pytest.raises(SecurityError):
        cache.sha256(link)
    with pytest.raises(ValueError, match="each file once"):
        FineTuneTrustedDigests(files=(files[0], files[0]))


# -- the supervisor's second job kind -------------------------------------------------------------


def said(state: str, **fields: Any) -> Callable[[Any], bool]:
    def matches(event: Any) -> bool:
        return (
            isinstance(event, SynthesisEvent)
            and event.state == state
            and all(getattr(event, name) == value for name, value in fields.items())
        )

    return matches


def say(operation_id: str = "speak-1", **changes: Any) -> SynthesizeCommand:
    return SynthesizeCommand.model_validate(
        {"operation_id": operation_id, "text": "Hello from the stub.", "model": "latest"} | changes
    )


def hush(operation_id: str = "speak-1") -> CancelSynthesisCommand:
    return CancelSynthesisCommand(operation_id="hush-1", cancelled_operation_id=operation_id)


@pytest.fixture
def speaking(speech: Speech) -> Iterator[tuple[FineTuneJobQueue, Events, list[FineTuneSupervisor]]]:
    queue = FineTuneJobQueue(
        speech.layout, subject_id=SUBJECT, runtime_root=speech.runtime, free_bytes=plenty
    )
    supervisors: list[FineTuneSupervisor] = []
    try:
        yield queue, Events(), supervisors
    finally:
        for supervisor in supervisors:
            supervisor.close()
        queue.close()


def supervisor_for(speech: Speech, jobs: Any, **options: Any) -> FineTuneSupervisor:
    queue, events, supervisors = jobs
    options.setdefault("pretrained_pin", speech.pin)
    options.setdefault("cancel_grace", 1.0)
    supervisor = FineTuneSupervisor(queue, on_event=events, **options)
    supervisors.append(supervisor)
    return supervisor


def speech_group(speech: Speech, operation_id: str) -> int:
    """The worker group of ``operation_id``'s Speak request, once its synthesizer runs."""
    marker = worker_command_marker(speech.layout, operation_id)
    found: list[int] = []

    def synthesizing() -> bool:
        rows = process_table()
        leaders = [row.pid for row in rows if row.command.endswith(marker) and not row.zombie]
        found[:] = leaders
        return bool(leaders) and any(
            "f5_synthesize.py" in row.command for row in rows if row.pgid == leaders[0]
        )

    wait_until(synthesizing)
    return found[0]


def test_a_speak_request_runs_from_queued_to_finished(speech: Speech, speaking: Any) -> None:
    write_speech_run(speech.layout, "finetune-a")
    _queue, events, _ = speaking
    supervisor = supervisor_for(speech, speaking)
    queued = supervisor.synthesize(say())
    assert (queued.state, queued.model, queued.run_id, queued.relative_path) == (
        "queued",
        "latest",
        None,
        None,
    )
    assert supervisor.synthesize(say()) == queued  # an identical resend replays
    supervisor.start()
    finished = events.wait_for(said("finished"))
    assert [event.state for event in events.events] == ["queued", "running", "finished"]
    assert {request_id for _, request_id, _ in events.items} == {"speak-1"}
    assert (finished.run_id, finished.reference_run_id, finished.reference_prompt_id) == (
        "finetune-a",
        "finetune-a",
        "reference-warm-01",
    )
    audio = speech.path(finished.relative_path)
    assert sha256_file(audio)[0] == finished.sha256 and finished.frames == 240
    assert stat.S_IMODE(audio.stat().st_mode) == 0o600
    assert supervisor.synthesize(say()) == finished
    assert supervisor.cancel_synthesis(hush()) == finished
    with pytest.raises(FineTuneJobError) as reused:
        supervisor.synthesize(say(text="Something else."))
    assert reused.value.recording_error.code == "RECORDING.INVALID_MESSAGE"
    names = {Path(entry.identity.path).name for entry in supervisor.trusted_digests.files}
    assert LARGE_FILES <= names and supervisor.trusted_digests.probe is not None


def test_one_gpu_slot_refuses_speech_and_fine_tuning_in_both_directions(
    speech: Speech, speaking: Any
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    queue, events, _ = speaking
    supervisor = supervisor_for(speech, speaking)

    def busy(call: Callable[[], Any]) -> RecordingError:
        with pytest.raises(FineTuneJobError) as refusal:
            call()
        error = refusal.value.recording_error
        assert error.code == "FINETUNE.JOB_ACTIVE" and error.retryable
        return error

    supervisor.synthesize(say("speak-1"))
    assert (
        "Speak request speak-1 is queued"
        in busy(lambda: supervisor.synthesize(say("speak-2"))).message
    )
    tune = FineTuneCommand(operation_id="tune-1", allow_missing=True)
    assert "fine-tune once it has finished" in busy(lambda: supervisor.submit(tune)).message
    assert not (queue.directory / "requests" / "tune-1.json").exists()
    cancelled = supervisor.cancel_synthesis(hush("speak-1"))
    assert cancelled.state == "cancelled" and events.events[-1] == cancelled
    assert supervisor.submit(tune).state == "queued"
    assert (
        "fine-tune tune-1 is queued" in busy(lambda: supervisor.synthesize(say("speak-3"))).message
    )
    queue.request_cancel("tune-1")
    assert supervisor.synthesize(say("speak-3")).state == "queued"
    # A resent fine-tune that already ended is replayed, not refused.
    assert supervisor.submit(tune).state == "cancelled"
    # While a request runs, the fine-tune is still refused.
    speech.mode(synthesis_seconds=60)
    supervisor.start()
    events.wait_for(said("running", operation_id="speak-3"))
    busy(lambda: supervisor.submit(FineTuneCommand(operation_id="tune-2", allow_missing=True)))
    running = supervisor.cancel_synthesis(hush("speak-3"))
    assert running.state == "running"
    events.wait_for(said("cancelled", operation_id="speak-3"), timeout=30)


def test_speak_submit_is_metadata_only_and_names_its_refusals(
    speech: Speech, speaking: Any, tmp_path: Path
) -> None:
    queue, _events, _ = speaking
    supervisor = supervisor_for(speech, speaking)

    def refusal(call: Callable[[], Any], code: str) -> RecordingError:
        with pytest.raises(FineTuneJobError) as refused_request:
            call()
        assert refused_request.value.recording_error.code == code
        return refused_request.value.recording_error

    assert (
        "fine-tune once before using Speak"
        in refusal(lambda: supervisor.synthesize(say()), "FINETUNE.NO_FINISHED_RUN").message
    )
    write_speech_run(speech.layout, "finetune-a")
    speech.path("derived/speech/speak-9").mkdir(parents=True)
    assert (
        "needs a new operation id"
        in refusal(lambda: supervisor.synthesize(say("speak-9")), "FINETUNE.RUN_EXISTS").message
    )
    refusal(lambda: supervisor.cancel_synthesis(hush("speak-0")), "RECORDING.INVALID_TRANSITION")
    # Nothing is probed or hashed while a request is accepted.
    speech.mode(probe_seconds=30)
    started = time.monotonic()
    assert supervisor.synthesize(say()).state == "queued"
    assert time.monotonic() - started < 1.0
    supervisor.fatal_error = RuntimeError("synthetic")
    refusal(lambda: supervisor.synthesize(say("speak-3")), "RECORDING.GATE_NOT_READY")
    refusal(lambda: supervisor.cancel_synthesis(hush()), "RECORDING.GATE_NOT_READY")
    assert queue.active_job() is None
    # A service without a usable runtime refuses by name.
    other = AssetLayout.initialize(tmp_path.resolve() / "other")
    write_speech_run(other, "finetune-a")
    for runtime_root in (None, tmp_path / "no-runtime"):
        bare = FineTuneJobQueue(other, subject_id=SUBJECT, runtime_root=runtime_root)
        try:
            refusal(
                lambda bare=bare: FineTuneSupervisor(bare).synthesize(say()),
                "FINETUNE.RUNTIME_UNAVAILABLE",
            )
        finally:
            bare.close()


def test_unreadable_run_records_are_left_to_the_worker(speech: Speech, speaking: Any) -> None:
    run = write_speech_run(speech.layout, "finetune-a")
    (speech.path(run.run_root) / "run.json").write_text("{damaged")
    _queue, events, _ = speaking
    supervisor = supervisor_for(speech, speaking)
    assert supervisor.synthesize(say()).state == "queued"
    supervisor.start()
    failed = events.wait_for(said("failed"))
    assert failed.error is not None and failed.error.code == "FINETUNE.SOURCE_CHANGED"
    assert failed.run_id is None and failed.reference_run_id is None


def test_a_speak_request_past_its_deadline_is_killed_with_its_output(
    speech: Speech, speaking: Any
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    speech.mode(synthesis_seconds=60)
    _queue, events, _ = speaking
    supervisor = supervisor_for(speech, speaking, synthesis_deadline=1.5)
    supervisor.synthesize(say())
    supervisor.start()
    group = speech_group(speech, "speak-1")
    failed = events.wait_for(said("failed"), timeout=30)
    assert failed.error is not None and failed.error.code == "FINETUNE.SYNTHESIS_DEADLINE"
    assert failed.error.message.startswith("speech synthesis took longer than 1.5 seconds")
    assert failed.error.retryable
    wait_until(lambda: live_group(group) == [], timeout=10)
    assert not speech.path("derived/speech/speak-1").exists()
    assert supervisor.fatal_error is None


def test_cancelling_a_running_speak_request_kills_its_whole_group(
    speech: Speech, speaking: Any
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    speech.mode(synthesis_seconds=60)
    _queue, events, _ = speaking
    supervisor = supervisor_for(speech, speaking)
    supervisor.synthesize(say())
    supervisor.start()
    group = speech_group(speech, "speak-1")
    reply = supervisor.cancel_synthesis(hush())
    assert reply.state == "running" and reply.error is None
    started = time.monotonic()
    cancelled = events.wait_for(said("cancelled"), timeout=30)
    assert time.monotonic() - started < 5 and cancelled.error is None
    assert live_group(group) == []
    assert not speech.path("derived/speech/speak-1").exists()
    assert supervisor.cancel_synthesis(hush()) == cancelled
    assert [event.state for event in events.events] == ["queued", "running", "cancelled"]


def test_a_failed_speak_request_names_its_failure_and_what_it_resolved(
    speech: Speech, speaking: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    _queue, events, _ = speaking
    supervisor = supervisor_for(speech, speaking)
    supervisor.start()
    supervisor.synthesize(say("speak-1", model="finetune-z"))
    missing = events.wait_for(said("failed", operation_id="speak-1"))
    assert missing.error.code == "FINETUNE.NO_FINISHED_RUN" and missing.run_id is None
    supervisor.synthesize(say("speak-2", reference_prompt_id="reference-neutral-01"))
    reference = events.wait_for(said("failed", operation_id="speak-2"))
    assert reference.error.code == "FINETUNE.REFERENCE_MISSING"
    speech.mode(fail_synthesis=True)
    supervisor.synthesize(say("speak-3", model="base"))
    broken = events.wait_for(said("failed", operation_id="speak-3"))
    assert broken.error.code == "FINETUNE.RUNTIME_FAILED"
    assert (broken.run_id, broken.reference_run_id, broken.reference_prompt_id) == (
        None,
        "finetune-a",
        "reference-warm-01",
    )
    assert not speech.path("derived/speech/speak-3").exists()

    # Output that cannot be removed is named; a named failure keeps its own error.
    def stuck(*args: Any, **kwargs: Any) -> bool:
        raise OSError("synthetic busy directory")

    monkeypatch.setattr(jobs_module, "discard_speech_output", stuck)
    supervisor.synthesize(say("speak-4", model="base"))
    kept = events.wait_for(said("failed", operation_id="speak-4"))
    assert kept.error.code == "FINETUNE.RUNTIME_FAILED"
    speech.mode(synthesis_seconds=60)
    supervisor.synthesize(say("speak-5"))
    speech_group(speech, "speak-5")
    supervisor.cancel_synthesis(hush("speak-5"))
    unremoved = events.wait_for(said("failed", operation_id="speak-5"), timeout=30)
    assert unremoved.error.code == "RECORDING.WRITE_FAILED"
    assert "partial output was not removed: synthetic busy directory" in unremoved.error.message
    assert supervisor.fatal_error is None


def test_speak_workers_reuse_verified_digests_and_notice_changes(
    speech: Speech, speaking: Any, tmp_path: Path
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    probe_seconds = 3.0
    speech.mode(probe_seconds=probe_seconds)
    _queue, events, _ = speaking
    supervisor = supervisor_for(speech, speaking)
    supervisor.start()

    def timed(operation_id: str, state: str) -> tuple[SynthesisEvent, float]:
        started = time.monotonic()
        supervisor.synthesize(say(operation_id))
        event = events.wait_for(said(state, operation_id=operation_id), timeout=60)
        return event, time.monotonic() - started

    _, first = timed("speak-1", "finished")
    _, second = timed("speak-2", "finished")
    # The first request probed the runtime; the second reused that answer.
    assert first >= probe_seconds and first - second > probe_seconds / 2
    checkpoint = str(speech.checkpoint("finetune-a").resolve())
    digests = supervisor._digests
    assert checkpoint in digests
    # A trusted digest is used as is while the file is unchanged: a wrong one proves it.
    digests[checkpoint] = digests[checkpoint].model_copy(update={"sha256": "0" * 64})
    trusted_wrong, _ = timed("speak-3", "failed")
    assert trusted_wrong.error is not None and trusted_wrong.error.code == "FINETUNE.SOURCE_CHANGED"
    # The worker reported the digest it trusted, so the cache still holds the wrong one; a
    # touched file is hashed again and speaks.
    os.utime(checkpoint, ns=(time.time_ns(), time.time_ns() + 1_000_000_000))
    _, _ = timed("speak-4", "finished")
    weights = speech.runtime / "pretrained/F5TTS_v1_Base/model_1250000.safetensors"
    weights.write_bytes(weights.read_bytes().replace(b"weights", b"weightz"))
    mismatch, _ = timed("speak-5", "failed")
    assert mismatch.error is not None and mismatch.error.code == "FINETUNE.PRETRAINED_MISMATCH"
    write_canonical_json(
        tmp_path / "speech-digest-reuse-evidence.json",
        {
            "synthetic_only": True,
            "stub_probe_seconds": probe_seconds,
            "first_request_seconds": round(first, 3),
            "second_request_seconds": round(second, 3),
        },
    )


def test_the_digest_cache_keeps_the_newest_entries_and_probe(
    speech: Speech, speaking: Any, tmp_path: Path
) -> None:
    supervisor = supervisor_for(speech, speaking)
    cache = FineTuneDigestCache()
    for index in range(MAX_TRUSTED_DIGESTS):
        path = tmp_path / f"file-{index:02d}"
        path.write_bytes(str(index).encode())
        cache.sha256(path)
    supervisor._remember_digests(cache.trusted())
    later = FineTuneDigestCache()
    (tmp_path / "file-99").write_bytes(b"99")
    later.sha256(tmp_path / "file-99")
    later.sha256(tmp_path / "file-03")  # seen again: it becomes one of the newest
    describe_finetune_runtime(speech.runtime, pretrained_pin=speech.pin, digests=later)
    supervisor._remember_digests(later.trusted())
    trusted = supervisor.trusted_digests
    names = [Path(entry.identity.path).name for entry in trusted.files]
    # 21 distinct files: the five oldest (file-00, 01, 02, 04, 05) are dropped.
    assert names == [f"file-{index:02d}" for index in range(6, 16)] + [
        "file-99",
        "file-03",
        "config.yaml",
        "pytorch_model.bin",
        "model_1250000.safetensors",
        "python",
    ]
    assert trusted.probe is not None
    supervisor._remember_digests(FineTuneTrustedDigests())
    assert supervisor.trusted_digests.probe == trusted.probe


SPEECH_FAKE_WORKER = r"""
import json, os, sys, time
mode = sys.argv[1]
def read_exact(count):
    data = b""
    while len(data) < count:
        chunk = os.read(0, count - len(data))
        if not chunk:
            sys.exit(5)
        data += chunk
    return data
request = json.loads(read_exact(int.from_bytes(read_exact(4), "big")))
def frame(value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    return len(payload).to_bytes(4, "big") + payload
base = {"schema_version": "cole-voice-lab.finetune-worker/v1",
        "generation": request["generation"], "operation_id": request["operation_id"]}
op = request["operation_id"]
clip = {"operation_id": op, "model": "base", "reference_run_id": "finetune-a",
        "reference_prompt_id": "reference-warm-01", "reference_audio_sha256": "1" * 64,
        "checkpoint_sha256": "2" * 64, "use_ema": True, "nfe_steps": 32,
        "text": request["command"]["text"], "relative_path": f"derived/speech/{op}/speech-01.wav",
        "sha256": "3" * 64, "frames": 240, "seconds": 0.01, "created_at": "2026-09-16T00:00:00Z"}
resolution = {"run_id": None, "reference_run_id": "finetune-a",
              "reference_prompt_id": "reference-warm-01"}
result = dict(base, kind="synthesis-result", request_sha256=request["request_sha256"],
              resolution=resolution, clip=clip)
if mode == "split":
    whole = frame(result)
    os.write(1, whole[:10])
    time.sleep(0.5)
    os.write(1, whole[10:])
    sys.exit(0)
frames = {
    "exit": b"",
    "stage": frame(dict(base, kind="stage", stage="dataset")),
    "foreign-result": frame(dict(result, request_sha256="0" * 64)),
    "other-text": frame(dict(result, clip=dict(clip, text="Something else."))),
    "other-model": frame(dict(result, resolution=dict(resolution, run_id="finetune-a"),
                              clip=dict(clip, model="finetune-a"))),
}[mode]
if mode == "exit":
    sys.exit(3)
os.write(1, frames)
time.sleep(60)
"""


@pytest.mark.parametrize(
    ("mode", "code", "text"),
    [
        ("exit", "FINETUNE.INTERRUPTED", "speech worker stopped without a result (exit status 3)"),
        ("stage", "RECORDING.INVALID_MESSAGE", "names another request"),
        ("foreign-result", "RECORDING.INVALID_MESSAGE", "names another request"),
        ("other-text", "RECORDING.INVALID_MESSAGE", "spoken for another request"),
        ("other-model", "RECORDING.INVALID_MESSAGE", "names its run unless the base model"),
        ("silent", "FINETUNE.INTERRUPTED", "the speech worker did not accept its request"),
    ],
)
def test_a_speak_worker_that_breaks_the_protocol_fails_its_request(
    speech: Speech, speaking: Any, monkeypatch: pytest.MonkeyPatch, mode: str, code: str, text: str
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    _queue, events, _ = speaking
    if mode == "silent":
        monkeypatch.setattr(jobs_module, "_SEND_DEADLINE_SECONDS", -1.0)
        command = [sys.executable, "-c", "import time; time.sleep(60)"]
    else:
        command = [sys.executable, "-c", SPEECH_FAKE_WORKER, mode]
    monkeypatch.setattr(FineTuneSupervisor, "_worker_command", lambda self, job: command)
    supervisor = supervisor_for(speech, speaking)
    supervisor.synthesize(say(model="base"))
    supervisor.start()
    failed = events.wait_for(said("failed"), timeout=20)
    assert failed.error.code == code and text in failed.error.message
    assert supervisor.fatal_error is None and supervisor.worker_pid is None


def test_a_result_that_arrives_in_pieces_is_assembled(
    speech: Speech, speaking: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    _queue, events, _ = speaking
    command = [sys.executable, "-c", SPEECH_FAKE_WORKER, "split"]
    monkeypatch.setattr(FineTuneSupervisor, "_worker_command", lambda self, job: command)
    supervisor = supervisor_for(speech, speaking)
    supervisor.synthesize(say(model="base"))
    supervisor.start()
    finished = events.wait_for(said("finished"), timeout=20)
    assert (finished.run_id, finished.reference_run_id, finished.sha256) == (
        None,
        "finetune-a",
        "3" * 64,
    )


def test_a_stopping_service_ends_no_speak_request_and_leaves_no_worker(
    speech: Speech, speaking: Any
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    speech.mode(synthesis_seconds=60)
    _queue, events, supervisors = speaking
    supervisor = supervisor_for(speech, speaking)
    supervisor.synthesize(say())
    supervisor.start()
    group = speech_group(speech, "speak-1")
    supervisors.remove(supervisor)
    supervisor.close()
    assert live_group(group) == []
    assert [event.state for event in events.events] == ["queued", "running"]


def test_only_the_newest_ended_requests_are_remembered(speech: Speech, speaking: Any) -> None:
    supervisor = supervisor_for(speech, speaking)
    for index in range(jobs_module.MAX_ENDED_SPEECH + 1):
        command = say(f"speak-{index:03d}")
        job = jobs_module._SpeechJob(
            command, canonical_sha256(command), jobs_module._speech_event(command, "queued")
        )
        with supervisor._speech_lock:
            supervisor._speech = job
            supervisor._end_speech_locked(job, jobs_module._speech_event(command, "cancelled"))
    with pytest.raises(FineTuneJobError) as forgotten:
        supervisor.cancel_synthesis(hush("speak-000"))
    assert forgotten.value.recording_error.code == "RECORDING.INVALID_TRANSITION"
    assert supervisor.cancel_synthesis(hush("speak-001")).state == "cancelled"


def test_a_speak_worker_that_cannot_start_fails_its_request(
    speech: Speech, speaking: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    _queue, events, _ = speaking
    missing = str(speech.layout.root / "no-such-python")
    monkeypatch.setattr(FineTuneSupervisor, "_worker_command", lambda self, job: [missing])
    supervisor = supervisor_for(speech, speaking)
    supervisor.synthesize(say())
    supervisor.start()
    failed = events.wait_for(said("failed"))
    assert failed.error.code == "FINETUNE.INTERRUPTED"
    assert "the speech worker could not start" in failed.error.message


# -- the worker ---------------------------------------------------------------------------------


def speech_request(speech: Speech, command: SynthesizeCommand | None = None, **changes: Any) -> Any:
    spoken = command or say()
    return SpeechWorkerRequest(
        generation="speech-generation",
        operation_id=spoken.operation_id,
        request_sha256=canonical_sha256(spoken),
        command=spoken,
        runtime_root=str(speech.runtime),
        pretrained_pin=speech.pin,
        **changes,
    )


def test_the_speech_worker_speaks_and_names_every_failure(
    speech: Speech, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = speech.layout
    nothing = run_speech_worker_request(layout, speech_request(speech))
    assert nothing.error is not None and nothing.error.code == "FINETUNE.NO_FINISHED_RUN"
    assert nothing.resolution is None and nothing.clip is None
    write_speech_run(layout, "finetune-a")
    reply = run_speech_worker_request(layout, speech_request(speech))
    assert reply.error is None and reply.clip is not None
    assert reply.resolution == SpeechWorkerResolution(
        run_id="finetune-a", reference_run_id="finetune-a", reference_prompt_id="reference-warm-01"
    )
    assert {Path(entry.identity.path).name for entry in reply.trusted.files} >= LARGE_FILES
    again = run_speech_worker_request(
        layout, speech_request(speech, say("speak-2"), trusted=reply.trusted)
    )
    assert again.error is None and again.clip is not None

    def failing(error: BaseException) -> None:
        def raise_error(*args: Any, **kwargs: Any) -> None:
            raise error

        monkeypatch.setattr(jobs_module, "synthesize_speech", raise_error)

    for error, code, retryable in (
        (SecurityError("synthetic unsafe path"), "RECORDING.SOURCE_CHANGED", False),
        (OSError("synthetic disk failure"), "RECORDING.WRITE_FAILED", True),
        (RuntimeError("synthetic bug"), "RECORDING.WRITE_FAILED", False),
        (
            FineTuneJobError("RECORDING.GATE_NOT_READY", "synthetic"),
            "RECORDING.GATE_NOT_READY",
            False,
        ),
    ):
        failing(error)
        named = run_speech_worker_request(layout, speech_request(speech, say("speak-3")))
        assert named.error is not None and (named.error.code, named.error.retryable) == (
            code,
            retryable,
        )
        assert named.resolution is not None and named.resolution.run_id == "finetune-a"
    failing(RuntimeError("synthetic bug"))
    unexpected = run_speech_worker_request(layout, speech_request(speech, say("speak-3")))
    assert unexpected.error is not None
    assert "speech failed unexpectedly: RuntimeError: synthetic bug" in unexpected.error.message


def test_speech_worker_messages_bind_their_request_and_result() -> None:
    command = say()
    with pytest.raises(ValueError, match="binds its exact command"):
        SpeechWorkerRequest(
            generation="g",
            operation_id="speak-other",
            request_sha256=canonical_sha256(command),
            command=command,
            runtime_root="/synthetic/runtime",
        )
    with pytest.raises(ValueError, match="binds its exact command"):
        SpeechWorkerRequest(
            generation="g",
            operation_id="speak-1",
            request_sha256="0" * 64,
            command=command,
            runtime_root="/synthetic/runtime",
        )
    base = {"generation": "g", "operation_id": "speak-1", "request_sha256": "0" * 64}
    failure = RecordingError(code="FINETUNE.RUNTIME_FAILED", message="Synthetic", retryable=False)
    with pytest.raises(ValueError, match="its clip or its error"):
        SpeechWorkerReply(**base)
    clip = SpeechClip(
        operation_id="speak-1",
        model="base",
        reference_run_id="finetune-a",
        reference_prompt_id="reference-warm-01",
        reference_audio_sha256="1" * 64,
        checkpoint_sha256="2" * 64,
        use_ema=True,
        nfe_steps=32,
        text="Hello.",
        relative_path="derived/speech/speak-1/speech-01.wav",
        sha256="3" * 64,
        frames=240,
        seconds=0.01,
        created_at=START,
    )
    with pytest.raises(ValueError, match="its clip or its error"):
        SpeechWorkerReply(**base, clip=clip, error=failure)
    with pytest.raises(ValueError, match="belongs to its request"):
        SpeechWorkerReply(**base, clip=clip)  # no resolution
    resolution = SpeechWorkerResolution(
        run_id=None, reference_run_id="finetune-a", reference_prompt_id="reference-warm-01"
    )
    assert SpeechWorkerReply(**base, clip=clip, resolution=resolution).clip == clip
    with pytest.raises(ValueError, match="belongs to its request"):
        SpeechWorkerReply(
            **base, clip=clip, resolution=resolution.model_copy(update={"run_id": "finetune-a"})
        )
    with pytest.raises(ValueError, match="belongs to its request"):
        SpeechWorkerReply(**(base | {"operation_id": "speak-2"}), clip=clip, resolution=resolution)


def test_worker_main_serves_a_speak_request(
    speech: Speech, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    write_speech_run(speech.layout, "finetune-a")
    parent_read, worker_write = os.pipe()
    worker_read, parent_write = os.pipe()
    worker_input = os.fdopen(worker_read, "rb", buffering=0)
    worker_output = os.fdopen(worker_write, "wb", buffering=0)
    monkeypatch.setattr(
        jobs_module, "sys", SimpleNamespace(stdin=worker_input, stdout=worker_output)
    )
    results: list[Any] = []
    thread = threading.Thread(
        target=lambda: results.append(
            jobs_module._worker_main(speech.layout, "speak-1", kill_group=lambda: None)
        ),
        daemon=True,
    )
    thread.start()
    try:
        os.write(parent_write, recording_frame_bytes(speech_request(speech)))
        decoder = RecordingFrameDecoder()
        reply = None
        deadline = time.monotonic() + 30
        while reply is None and time.monotonic() < deadline:
            if select.select([parent_read], [], [], 0.05)[0]:
                for payload in decoder.feed(os.read(parent_read, 65_536)):
                    reply = decode_finetune_worker_message(payload)
        assert isinstance(reply, SpeechWorkerReply) and reply.error is None
        thread.join(timeout=10)
        assert results == [0]
    finally:
        os.close(parent_write)
        thread.join(timeout=2)
        worker_input.close()
        worker_output.close()
        os.close(parent_read)


def test_the_worker_process_restores_the_umask_it_works_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def observe(*args: Any, **kwargs: Any) -> int:
        current = os.umask(0)
        os.umask(current)
        seen.append(current)
        return 0

    monkeypatch.setattr(jobs_module, "_worker_main", observe)
    monkeypatch.setattr(jobs_module.os, "getpgrp", lambda: os.getpid())
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    arguments = ["--worker", "--asset-root", str(layout.root), "--operation-id", "speak-1"]
    previous = os.umask(0o022)
    try:
        assert jobs_module.main(arguments, kill_group=lambda: None) == 0
        assert seen == [0o077]  # the worker and everything it starts create private files
        assert os.umask(0o022) == 0o022  # the caller's umask is back
    finally:
        os.umask(previous)


# -- the recording service ----------------------------------------------------------------------


def test_the_workspace_backend_answers_speak_commands_without_an_open_context(
    speech: Speech, speaking: Any
) -> None:
    write_speech_run(speech.layout, "finetune-a")
    supervisor = supervisor_for(speech, speaking)
    workspace = RecordingWorkspaceBackend(speech.layout, SUBJECT, discover=False)

    def execute(backend: Any, data: Any) -> Any:
        return backend.execute(request(data), RecordingCancellation())

    try:
        with pytest.raises(RecordingServiceError) as unavailable:
            execute(workspace, say())
        assert unavailable.value.recording_error.code == "RECORDING.GATE_NOT_READY"
        workspace.finetune_jobs = supervisor
        queued = execute(workspace, say())
        assert isinstance(queued, SynthesisEvent) and queued.state == "queued"
        with pytest.raises(RecordingServiceError) as busy:
            execute(workspace, say("speak-2"))
        assert busy.value.recording_error.code == "FINETUNE.JOB_ACTIVE"
        assert busy.value.recording_error.retryable
        assert execute(workspace, hush()).state == "cancelled"
        cancelled = RecordingCancellation()
        cancelled.cancel()
        with pytest.raises(RecordingServiceError) as stopped:
            workspace.execute(request(say("speak-3")), cancelled)
        assert stopped.value.recording_error.code == "RECORDING.CANCELLED"
    finally:
        workspace.index.close()


def test_a_real_service_speaks_while_a_take_is_saved(package_rig: Any, tmp_path: Path) -> None:
    rig, analysis, _packages = package_rig
    layout = rig.layout
    write_speech_run(layout, "finetune-a")
    runtime, pin = stub_finetune_runtime(tmp_path.resolve() / "runtime", {"synthesis_seconds": 3})
    # A separate unsaved draft makes the concurrent Save a real durable mutation.
    rig.now = utc_now() - timedelta(seconds=5)
    rig.store.clock = lambda: rig.now
    draft = rig.seal(rig.reserve("service-draft", "synthetic-0001"), "service-draft-seal")
    rig.store.clock = utc_now
    analysis.close()
    build = recording_service_build_identity()
    own, client = tmp_path / "service-build.json", tmp_path / "client-build.json"
    write_canonical_json(own, build)
    write_canonical_json(client, build_identity())
    group = rig.store.get_group(rig.group_id)
    # The shipped service never overrides the pins; the test-only wrapper hands the stub's
    # pins to its supervisor and runs the unchanged service.
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPOSITORY / "tests" / "native_finetune_service_fixture.py"),
            "--test-pretrained-pins",
            format_pins(pin),
            "--asset-root",
            str(layout.root),
            "--catalog-sha256",
            group.catalog_sha256,
            "--recording-group-id",
            group.group_id,
            "--process-generation",
            "generation-1",
            "--build-identity",
            str(own),
            "--expected-client-build",
            str(client),
            "--finetune-runtime-root",
            str(runtime),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    responses: list[RecordingResponse] = []

    def read() -> RecordingResponse:
        response = read_response(process.stdout.fileno(), timeout=30)
        responses.append(response)
        return response

    def send(message: Any) -> Any:
        process.stdin.write(recording_frame_bytes(message))
        process.stdin.flush()
        while True:
            response = read()
            assert response.error is None, response.error
            if response.meta.request_id == message.meta.request_id:
                return response.data

    group_id: int | None = None
    try:
        send(handshake(service=build))
        queued = send(request(say(), request_id="speak-request"))
        assert isinstance(queued, SynthesisEvent) and queued.state == "queued"
        marker = worker_command_marker(layout, "speak-1")

        def synthesizing() -> bool:
            nonlocal group_id
            rows = process_table()
            leaders = [row.pid for row in rows if row.command.endswith(marker)]
            group_id = leaders[0] if leaders else None
            return group_id is not None and any(
                "f5_synthesize.py" in row.command for row in rows if row.pgid == group_id
            )

        wait_until(synthesizing)
        assert group_id is not None
        started = time.monotonic()
        saved = send(
            request(
                rig.command(draft, "service-fast-save"),
                request_id="save",
                take_id=draft.reservation.take_id,
            )
        )
        elapsed = time.monotonic() - started
        assert isinstance(saved, SaveEvent) and saved.take.decision == "kept" and elapsed < 2
        workers = [row for row in process_table() if FINETUNE_WORKER_MODULE in row.command]
        assert [row.pid for row in workers if row.pgid == group_id] == [group_id]
        while not any(
            isinstance(response.data, SynthesisEvent) and response.data.state == "finished"
            for response in responses
        ):
            assert read().error is None
        lane = [
            response
            for response in responses
            if response.meta.request_id == "speak-1" and isinstance(response.data, SynthesisEvent)
        ]
        assert [response.data.state for response in lane] == ["queued", "running", "finished"]
        sequences = [response.meta.event_sequence for response in lane]
        assert sequences == sorted(sequences)
        assert all(response.meta.take_id is None for response in lane)
        finished = lane[-1].data
        assert (
            finished.run_id == "finetune-a" and finished.reference_prompt_id == "reference-warm-01"
        )
        audio = layout.root / finished.relative_path
        assert sha256_file(audio)[0] == finished.sha256  # the test re-hashes the clip
        with wave.open(str(audio), "rb") as handle:
            assert handle.getnframes() == finished.frames and handle.getframerate() == 24_000
        assert stat.S_IMODE(audio.stat().st_mode) == 0o600
        record = SpeechClip.model_validate_json((audio.parent / "speech.json").read_bytes())
        assert (record.sha256, record.frames) == (finished.sha256, finished.frames)
        wait_until(lambda: live_group(group_id) == [], timeout=10)
        unknown = request(hush("speak-unknown"), request_id="hush-unknown")
        process.stdin.write(recording_frame_bytes(unknown))
        process.stdin.flush()
        refusal = read()
        while refusal.meta.request_id != "hush-unknown":
            refusal = read()
        assert refusal.error is not None and refusal.error.code == "RECORDING.INVALID_TRANSITION"
        write_canonical_json(
            tmp_path / "actual-service-speech-evidence.json",
            {
                "synthetic_only": True,
                "service_pid": process.pid,
                "speech_worker_pid": group_id,
                "save_seconds_while_speech_worker_ran": elapsed,
            },
        )
        send(request(ShutdownCommand(), request_id="shutdown"))
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        assert process.stderr is not None
        process.stderr.close()
        if group_id is not None:
            for row in live_group(group_id):
                os.kill(row.pid, signal.SIGKILL)
