"""Comparing model families: what a run cost to train, how it renders, and how it sounds.

@canonical model_comparison -- one write-once record per measurement of a fine-tuned run,
``provenance/model-comparisons/<run id>/<comparison id>.json`` (0400; D-049):

- **training**: the run's family and trainer model, its dataset (digest, clips, seconds, rate),
  epochs and the last update its log reports, the wall time from its run record, the device
  (from the runtime identity when the family records it, else as the operator names it), and
  the runtime's identity;
- **inference** (optional): a benchmark on a fixed held-out text set (``BENCHMARK_TEXTS``,
  none of which may be a text the run trained on), rendered through the same
  ``SpeechBackend`` port the speech API uses, sentence by sentence with derived seeds: load
  time, time to the first segment, real-time factor, peak accelerator memory when the
  backend reports it, and whether a rerun of a segment is byte-identical. The run is
  rendered as an unwritten release preview (``speech.release.designate(write=False)``) under
  an approved consent record for ``private-home-host-synthesis`` on ``home-host``; the audio
  is hashed and deleted, never kept or served;
- **quality** (optional): a link to the blind review that judged it (its id and score-sheet
  digest), which stays the decision signal (AGENTS.md: blind listening first).

``comparison_rows`` flattens the newest record of each run for the command line
(``voice-lab families compare``).
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError, model_validator

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    write_new_private_file,
)
from .errors import ContractError, SecurityError, VoiceLabError
from .finetune import (
    FINETUNE_OUTPUT_ROOT,
    FamilyRuntimeIdentity,
    FineTuneDigestCache,
    FineTuneRuntimeIdentity,
    ModelName,
    load_finetune_dataset,
    require_finished_run,
)
from .models import ContractModel, Sha256, StableId
from .paths import AssetLayout, confined_path
from .recording_contracts import UtcTimestamp
from .speech.backend import SpeechBackend
from .speech.contracts import API_SAMPLE_RATE_HZ, RenderFingerprint
from .speech.release import (
    SPEECH_SUBJECT,
    BlindReviewRef,
    VoiceReleaseError,
    current_release,
    designate,
    verify_release,
)
from .speech.runner import check_wav
from .speech.segments import job_seed, segment_seed, split_sentences
from .training import run_family, training_adapter

MODEL_COMPARISON_SCHEMA: Final = "cole-voice-lab.model-comparison/v1"
COMPARISON_DIRECTORY: Final = "model-comparisons"
BENCHMARK_PURPOSE: Final = "private-home-host-synthesis"
BENCHMARK_TEXT_SET: Final = "benchmark-v1"
# Fixed, public, held out of every corpus catalog: short and long, a question, numbers, a
# list, a name. Changing them is a new text set, never an edit of this one.
BENCHMARK_TEXTS: Final = (
    "The kettle clicked off just as the rain started again.",
    "Could you move the meeting to Thursday afternoon, after three?",
    "Bring two oranges, a loaf of rye bread, and the blue umbrella from the hall.",
    "Marguerite read the letter twice, folded it along its old creases, and put it back "
    "in the drawer without a word.",
    "Version 4.2 shipped on the 9th; the next one, if all goes well, lands in about six weeks.",
    "Honestly? I think the second plan is better, but only if we start before the river "
    "floods the lower road, which could happen any day now that the snow is melting in the "
    "hills.",
)
MAX_TRAINING_LOG_BYTES: Final = 64 * 1024
MAX_RECORD_BYTES: Final = 256 * 1024


class ComparisonError(VoiceLabError):
    """A comparison cannot be measured or recorded (the message says why)."""


def text_set_sha256(texts: tuple[str, ...] = BENCHMARK_TEXTS) -> str:
    return canonical_sha256({"text_set": BENCHMARK_TEXT_SET, "texts": list(texts)})


class TrainingSummary(ContractModel):
    """What one run cost to train."""

    run_id: StableId
    model_family: StableId
    model_name: ModelName
    dataset_sha256: Sha256
    corpus_sha256: Sha256
    dataset_clips: int = Field(ge=1)
    dataset_seconds: float = Field(gt=0.0, allow_inf_nan=False)
    dataset_sample_rate_hz: int = Field(ge=8_000, le=192_000)
    epochs: int = Field(ge=1)
    updates: int | None = Field(ge=0)
    wall_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    device: str | None = Field(min_length=1, max_length=128)
    runtime: dict[str, str] = Field(max_length=24)


class SegmentTiming(ContractModel):
    """One rendered segment of the benchmark (its digest; the audio itself is deleted)."""

    text_index: int = Field(ge=1)
    segment_index: int = Field(ge=1)
    characters: int = Field(ge=1)
    render_ms: int = Field(ge=0)
    audio_ms: int = Field(ge=0)
    sha256: Sha256


class InferenceBenchmark(ContractModel):
    """How a run renders the fixed text set through the speech backend port."""

    text_set: Literal["benchmark-v1"] = BENCHMARK_TEXT_SET
    text_set_sha256: Sha256
    release_sha256: Sha256
    designated: bool
    consent_id: StableId
    consent_sha256: Sha256
    fingerprint: RenderFingerprint
    load_ms: int = Field(ge=0)
    first_segment_ms: int = Field(ge=0)
    time_to_first_audio_ms: int = Field(ge=0)
    render_ms: int = Field(ge=0)
    audio_ms: int = Field(ge=1)
    real_time_factor: float = Field(ge=0.0, allow_inf_nan=False)
    peak_memory_bytes: int | None = Field(ge=0)
    deterministic: bool | None
    segments: tuple[SegmentTiming, ...] = Field(min_length=1, max_length=256)


class ModelComparison(ContractModel):
    """One measurement of one run (docs/MODEL_FAMILIES.md, D-049)."""

    schema_version: Literal["cole-voice-lab.model-comparison/v1"] = MODEL_COMPARISON_SCHEMA
    comparison_id: StableId
    model_family: StableId
    run_id: StableId
    created_at: UtcTimestamp
    training: TrainingSummary
    inference: InferenceBenchmark | None = None
    quality: BlindReviewRef | None = None

    @model_validator(mode="after")
    def _one_run(self) -> ModelComparison:
        if (self.training.run_id, self.training.model_family) != (self.run_id, self.model_family):
            raise ValueError("a comparison's training summary is its run's")
        if self.inference is not None and self.inference.fingerprint.model_family != (
            self.model_family
        ):
            raise ValueError("a comparison's benchmark rendered its run's family")
        return self


def _last_log_text(path: Path) -> str:
    try:
        with open_regular_no_follow(path, nonblocking=True) as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(0, size - MAX_TRAINING_LOG_BYTES))
            return stream.read(MAX_TRAINING_LOG_BYTES).decode("utf-8", errors="replace")
    except (OSError, VoiceLabError):
        return ""


def training_summary(
    layout: AssetLayout, run_id: str, *, training_device: str | None = None
) -> TrainingSummary:
    """What finished run ``run_id`` cost to train, from its records and its log.

    Raises: ``FineTuneError`` for a missing, invalid or unfinished run;
    ``UnknownTrainingFamily`` for a family this build cannot read.
    """
    run_root = f"{FINETUNE_OUTPUT_ROOT}/{run_id}"
    run = require_finished_run(layout, run_root, run_id)
    adapter = training_adapter(run_family(run))
    dataset, _ = load_finetune_dataset(layout, run_root)
    progress = adapter.parse_progress(_last_log_text(confined_path(layout.root, run.log_path)))
    identity = run.runtime
    if isinstance(identity, FineTuneRuntimeIdentity):
        runtime = {
            "f5_tts_version": identity.f5_tts_version,
            "torch_version": identity.torch_version,
            "python_sha256": identity.python_sha256,
            "pretrained_sha256": identity.pretrained_sha256,
        }
        device = training_device
    else:
        assert isinstance(identity, FamilyRuntimeIdentity)
        runtime = {
            **identity.packages,
            "python_sha256": identity.python_sha256,
            "pretrained_sha256": identity.pretrained_sha256,
        }
        device = identity.device or training_device
    assert run.finished_at is not None
    return TrainingSummary(
        run_id=run.run_id,
        model_family=adapter.family_id,
        model_name=run.hyperparameters.model_name,
        dataset_sha256=run.dataset_sha256,
        corpus_sha256=run.corpus_sha256,
        dataset_clips=len(dataset.clips),
        dataset_seconds=round(sum(clip.frames / clip.sample_rate_hz for clip in dataset.clips), 3),
        dataset_sample_rate_hz=dataset.sample_rate_hz,
        epochs=run.hyperparameters.epochs,
        updates=None if progress is None else progress.update,
        wall_seconds=round((run.finished_at - run.started_at).total_seconds(), 3),
        device=device,
        runtime=runtime,
    )


def _held_out(layout: AssetLayout, run_id: str, texts: tuple[str, ...]) -> None:
    dataset, _ = load_finetune_dataset(layout, f"{FINETUNE_OUTPUT_ROOT}/{run_id}")
    trained = {" ".join(clip.text.casefold().split()) for clip in dataset.clips}
    for text in texts:
        if " ".join(text.casefold().split()) in trained:
            raise ComparisonError("a benchmark text is one the run trained on; it is not held out")


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def benchmark_run(
    layout: AssetLayout,
    run_id: str,
    backend: SpeechBackend,
    *,
    consent_id: str,
    now: datetime,
    work_directory: Path,
    subject_id: str = SPEECH_SUBJECT,
    texts: tuple[str, ...] = BENCHMARK_TEXTS,
    segment_deadline_seconds: float = 120.0,
    monotonic: Callable[[], float] = time.monotonic,
    digests: FineTuneDigestCache | None = None,
) -> InferenceBenchmark:
    """Render ``texts`` with run ``run_id`` through ``backend`` and measure it.

    The caller holds the GPU slot and unloads the backend afterwards. ``work_directory`` is
    private scratch for the rendered segments (created here, removed before returning).
    Raises: ``VoiceReleaseError`` ``not_designatable`` when the run or the consent record does
    not allow it; ``ComparisonError`` when a text is not held out; ``SpeechBackendError``.
    """
    cache = FineTuneDigestCache() if digests is None else digests
    _held_out(layout, run_id, texts)
    release, digest = designate(
        layout,
        run_id=run_id,
        designated_by="model comparison",
        now=now,
        consent_id=consent_id,
        subject_id=subject_id,
        digests=cache,
        write=False,
        purpose=BENCHMARK_PURPOSE,
    )
    verified = verify_release(layout, release, digest, digests=cache)
    try:
        current, _ = current_release(layout)
        designated = current.run_id == run_id
    except (VoiceReleaseError, VoiceLabError, OSError, ValueError):
        designated = False
    if os.path.lexists(work_directory):
        raise ComparisonError(f"{work_directory} already exists")
    work_directory.mkdir(mode=0o700, parents=True)
    try:
        started = monotonic()
        fingerprint = backend.prepare(verified)
        load_seconds = monotonic() - started
        seed = job_seed(text_set_sha256(texts), digest, canonical_sha256(fingerprint.settings))
        timings: list[SegmentTiming] = []
        render_seconds = 0.0
        first: tuple[str, int, Path] | None = None
        index = 0
        for text_index, text in enumerate(texts, start=1):
            pieces = split_sentences(text, max_bytes=max(verified.max_segment_bytes, 16))
            for segment_index, piece in enumerate(pieces, start=1):
                index += 1
                output = work_directory / f"{index:04d}.wav"
                began = monotonic()
                backend.render_segment(
                    piece,
                    seed=segment_seed(seed, index),
                    output=output,
                    deadline_seconds=segment_deadline_seconds,
                )
                spent = monotonic() - began
                render_seconds += spent
                audio = check_wav(output)
                if first is None:
                    first = (piece, segment_seed(seed, index), output)
                timings.append(
                    SegmentTiming(
                        text_index=text_index,
                        segment_index=segment_index,
                        characters=len(piece),
                        render_ms=_milliseconds(spent),
                        audio_ms=round(audio.frames * 1000 / API_SAMPLE_RATE_HZ),
                        sha256=audio.sha256,
                    )
                )
        assert first is not None
        deterministic: bool | None = None
        if fingerprint.seeded:
            again = work_directory / "rerun.wav"
            backend.render_segment(
                first[0], seed=first[1], output=again, deadline_seconds=segment_deadline_seconds
            )
            deterministic = check_wav(again).sha256 == timings[0].sha256
        peak = backend.peak_memory_bytes()
    finally:
        shutil.rmtree(work_directory, ignore_errors=True)
    audio_ms = sum(item.audio_ms for item in timings)
    return InferenceBenchmark(
        text_set_sha256=text_set_sha256(texts),
        release_sha256=digest,
        designated=designated,
        consent_id=release.consent_id,
        consent_sha256=release.consent_sha256,
        fingerprint=fingerprint,
        load_ms=_milliseconds(load_seconds),
        first_segment_ms=timings[0].render_ms,
        time_to_first_audio_ms=_milliseconds(load_seconds) + timings[0].render_ms,
        render_ms=_milliseconds(render_seconds),
        audio_ms=max(audio_ms, 1),
        real_time_factor=round(render_seconds / max(audio_ms / 1000, 0.001), 4),
        peak_memory_bytes=peak,
        deterministic=deterministic,
        segments=tuple(timings),
    )


def comparison_id(now: datetime) -> str:
    return f"mc-{now.strftime('%Y%m%dt%H%M%Sz')}-{secrets.token_hex(3)}"


def comparison_directory(layout: AssetLayout, run_id: str) -> Path:
    return layout.directory("provenance") / COMPARISON_DIRECTORY / run_id


def record_comparison(layout: AssetLayout, comparison: ModelComparison) -> Path:
    """Write ``comparison`` once (0400). Raises: ``FileExistsError`` for a used id."""
    directory = comparison_directory(layout, comparison.run_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{comparison.comparison_id}.json"
    write_new_private_file(path, canonical_json_bytes(comparison) + b"\n", mode=0o400)
    return path


def read_comparisons(layout: AssetLayout, run_ids: tuple[str, ...] = ()) -> list[ModelComparison]:
    """Every recorded comparison (of ``run_ids`` when given), oldest first.

    Raises: ``ContractError`` for a record that is not a comparison; ``SecurityError`` for a
    link.
    """
    root = layout.directory("provenance") / COMPARISON_DIRECTORY
    if not root.is_dir():
        return []
    found: list[ModelComparison] = []
    for run_directory in sorted(root.iterdir()):
        if run_ids and run_directory.name not in run_ids:
            continue
        if run_directory.is_symlink() or not run_directory.is_dir():
            raise SecurityError(f"{run_directory} is not a directory")
        for path in sorted(run_directory.glob("*.json")):
            with open_regular_no_follow(path) as stream:
                data = stream.read(MAX_RECORD_BYTES + 1)
            try:
                found.append(ModelComparison.model_validate_json(data))
            except ValidationError as error:
                raise ContractError(f"{path.name} is not a model comparison: {error}") from error
    return sorted(found, key=lambda item: (item.created_at, item.comparison_id))


def comparison_rows(records: list[ModelComparison]) -> list[dict[str, object]]:
    """The newest record of each run, flattened for printing side by side."""
    newest: dict[str, ModelComparison] = {}
    for record in records:
        newest[record.run_id] = record
    rows: list[dict[str, object]] = []
    for record in sorted(newest.values(), key=lambda item: (item.model_family, item.run_id)):
        training, inference = record.training, record.inference
        rows.append(
            {
                "run_id": record.run_id,
                "model_family": record.model_family,
                "comparison_id": record.comparison_id,
                "dataset_sha256": training.dataset_sha256,
                "dataset_seconds": training.dataset_seconds,
                "epochs": training.epochs,
                "updates": training.updates,
                "training_wall_seconds": training.wall_seconds,
                "training_device": training.device,
                "real_time_factor": None if inference is None else inference.real_time_factor,
                "time_to_first_audio_ms": (
                    None if inference is None else inference.time_to_first_audio_ms
                ),
                "load_ms": None if inference is None else inference.load_ms,
                "peak_memory_bytes": None if inference is None else inference.peak_memory_bytes,
                "deterministic": None if inference is None else inference.deterministic,
                "device": None if inference is None else inference.fingerprint.device,
                "blind_review": None if record.quality is None else record.quality.review_id,
            }
        )
    return rows


def require_blind_review(layout: AssetLayout, review: BlindReviewRef) -> None:
    """Refuse a link to a blind review the lab does not hold.

    Raises: ``ComparisonError``.
    """
    reviews = layout.directory("evaluations") / "blind-reviews"
    if not (reviews / review.review_id).is_dir():
        raise ComparisonError(f"there is no blind review {review.review_id}")
