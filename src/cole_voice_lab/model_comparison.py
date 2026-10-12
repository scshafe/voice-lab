"""Comparing model families: what a run cost to train, how it renders, and how it sounds.

@canonical model_comparison -- one write-once record per measurement of a fine-tuned run,
``provenance/model-comparisons/<run id>/<comparison id>.json`` (0400; D-049):

- **training**: the run's family and trainer model, its corpus and dataset (digests, clips,
  seconds, rate), epochs and the last update its log reports, the wall time from its run
  record, the device (from the runtime identity when the family records it, else as the
  operator names it), the training host's fingerprint when the run recorded one
  (``host_fingerprint``), and the runtime's identity;
- **inference** (optional): a benchmark on a fixed held-out text set (``BENCHMARK_TEXTS``,
  none of which may be a text the run trained on), rendered through the same
  ``SpeechBackend`` port the speech API uses, sentence by sentence with seeds derived from
  stable inputs only (the run, its files' digests, the text set and the render settings), so
  a rerun renders the same: load time, time to the first segment, real-time factor, peak
  accelerator memory with how the backend measured it, whether a rerun of a segment is
  byte-identical, the host's fingerprint, and an advisory snapshot of the GPU's other users
  before the load and after the unload. The run is rendered as an unwritten release preview
  (``speech.release.designate(write=False)``) under an approved consent record for
  ``private-home-host-synthesis`` on ``home-host``; the audio is hashed in the benchmark's
  scratch under the speech state (never the lab) and deleted, never kept or served. On the PC
  it runs in the benchmark unit (``cli_models``);
- **quality** (optional): a link to the blind review that judged it, made only from the score
  sheet itself (its digest must match, it must verify against the sealed review package, and
  it must have scored the run), with the run's scores; blind listening stays the decision
  signal (AGENTS.md).

``comparison_rows`` composes, per run, the newest training summary, the newest benchmark and
the newest review, each with when it was recorded, and flags runs trained on another corpus
(``voice-lab families compare``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import Field, ValidationError, model_validator

from .blind import ScoreSheet, aggregate_scores
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
from .host_fingerprint import (
    GpuContention,
    HostFingerprint,
    gpu_contention,
    host_fingerprint,
    training_host,
)
from .models import ContractModel, Sha256, StableId
from .paths import AssetLayout, confined_path
from .recording_contracts import UtcTimestamp
from .speech.backend import SpeechBackend
from .speech.contracts import API_SAMPLE_RATE_HZ, RenderFingerprint
from .speech.release import (
    SPEECH_SUBJECT,
    VoiceRelease,
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
MAX_SCORE_SHEET_BYTES: Final = 4 * 1024 * 1024
SCRATCH_PREFIX: Final = ".model-benchmark-"
LOG: Final = logging.getLogger(__name__)


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
    host: HostFingerprint | None = None
    runtime: dict[str, str] = Field(max_length=24)


class SegmentTiming(ContractModel):
    """One rendered segment of the benchmark (its digest; the audio itself is deleted)."""

    text_index: int = Field(ge=1)
    segment_index: int = Field(ge=1)
    characters: int = Field(ge=1)
    render_ms: int = Field(ge=0)
    audio_ms: int = Field(ge=0)
    sha256: Sha256


class MeasuredPeak(ContractModel):
    """Peak accelerator memory and how the backend measured it (a process's own allocations
    and a whole card's use are different numbers; compare like with like)."""

    bytes: int = Field(ge=0)
    method: str = Field(min_length=1, max_length=512)


class InferenceBenchmark(ContractModel):
    """How a run renders the fixed text set through the speech backend port.

    ``seed_basis_sha256`` is what the seeds derive from (the run, its files' digests), with
    the text set's digest and the render settings' digest: none of it changes between reruns,
    so a rerun renders the same segments. ``release_sha256`` is the preview's digest, which
    does (it is dated), and is kept only as evidence of what was checked.
    """

    text_set: Literal["benchmark-v1"] = BENCHMARK_TEXT_SET
    text_set_sha256: Sha256
    seed_basis_sha256: Sha256
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
    peak_memory: MeasuredPeak | None
    deterministic: bool | None
    host: HostFingerprint
    # Advisory: the GPU's other users (llama-swap shares the PC's card) before the model
    # loaded and after the last segment, as far as an unprivileged process can see.
    contention_before: GpuContention
    contention_after: GpuContention
    # Only loopback was visible and the benchmark unit's marker was set (cli_models): a claim
    # about the process's network and how it was started, not proof of the unit's fences.
    sandboxed: bool
    segments: tuple[SegmentTiming, ...] = Field(min_length=1, max_length=256)


class QualityLink(ContractModel):
    """The blind review that judged a run: its id and the score sheet's digest, verified
    against the sealed review package, and the run's scores in it (a review that did not
    score the run is never linked to it)."""

    review_id: StableId
    score_sheet_sha256: Sha256
    candidate_id: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(ge=1)
    overall_mean: float = Field(ge=1.0, le=5.0, allow_inf_nan=False)
    means: dict[StableId, float] = Field(max_length=16)


class ModelComparison(ContractModel):
    """One measurement of one run (docs/MODEL_FAMILIES.md, D-049)."""

    schema_version: Literal["cole-voice-lab.model-comparison/v1"] = MODEL_COMPARISON_SCHEMA
    comparison_id: StableId
    model_family: StableId
    run_id: StableId
    created_at: UtcTimestamp
    training: TrainingSummary
    inference: InferenceBenchmark | None = None
    quality: QualityLink | None = None

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
    ``UnknownTrainingFamily`` for a family this build cannot read; ``VoiceLabError`` for a
    training-host record that is not one.
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
        host=training_host(layout, run_root),
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


def seed_basis(release: VoiceRelease) -> str:
    """What a benchmark's seeds derive from: the run and the digests of the files it renders
    with (never the preview's date or sequence, so a rerun gets the same seeds)."""
    return canonical_sha256(
        {
            "basis": "cole-voice-lab.benchmark-seed/v1",
            "run_id": release.run_id,
            "model_family": release.model_family,
            "files": sorted([item.role, item.sha256] for item in release.files),
        }
    )


def _log_removal(function: Callable[..., object], path: str, error: BaseException) -> None:
    LOG.warning("could not remove benchmark scratch %s: %s", path, error)


def remove_scratch(directory: Path) -> None:
    """Remove a benchmark's scratch directory; whatever cannot be removed is logged."""
    if os.path.lexists(directory):
        shutil.rmtree(directory, onexc=_log_removal)


def sweep_benchmark_scratch(scratch: Path, *, keep: Path | None = None) -> tuple[str, ...]:
    """Remove the scratch a killed benchmark left in ``scratch`` (the benchmark's own
    directory under the speech state, never the lab; call it holding the GPU slot, so no
    other benchmark is running). Returns the names removed."""
    try:
        names = sorted(name for name in os.listdir(scratch) if name.startswith(SCRATCH_PREFIX))
    except FileNotFoundError:
        return ()
    removed: list[str] = []
    for name in names:
        path = scratch / name
        if keep is not None and path == keep:
            continue
        LOG.warning("removing the scratch a stopped benchmark left: %s", name)
        remove_scratch(path)
        removed.append(name)
    return tuple(removed)


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
    sandboxed: bool = False,
    host: Callable[[], HostFingerprint] | None = None,
    contention: Callable[[], GpuContention] | None = None,
) -> InferenceBenchmark:
    """Render ``texts`` with run ``run_id`` through ``backend`` and measure it.

    The caller holds the GPU slot. The backend is unloaded here once the last segment is
    measured, before the GPU's other users are looked at again (its resident worker holds the
    device while loaded). ``work_directory`` is private scratch for the rendered segments,
    outside the lab (the CLI's is under the speech state; created here, removed before
    returning; a file that cannot be removed is logged, never ignored). ``sandboxed`` records
    whether it ran in the benchmark unit's sandbox (``cli_models.in_speech_sandbox``);
    ``host`` and ``contention`` are test seams.
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
    machine = (host or host_fingerprint)()
    snapshot = contention or gpu_contention
    before = snapshot()
    basis = seed_basis(release)
    try:
        started = monotonic()
        fingerprint = backend.prepare(verified)
        load_seconds = monotonic() - started
        seed = job_seed(text_set_sha256(texts), basis, canonical_sha256(fingerprint.settings))
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
        peak = backend.peak_memory()
        backend.unload()
        after = snapshot()
    finally:
        remove_scratch(work_directory)
    audio_ms = sum(item.audio_ms for item in timings)
    return InferenceBenchmark(
        text_set_sha256=text_set_sha256(texts),
        seed_basis_sha256=basis,
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
        peak_memory=None if peak is None else MeasuredPeak(bytes=peak.bytes, method=peak.method),
        deterministic=deterministic,
        host=machine,
        contention_before=before,
        contention_after=after,
        sandboxed=sandboxed,
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


def _when(record: ModelComparison | None) -> str | None:
    return None if record is None else record.created_at.isoformat().replace("+00:00", "Z")


def comparison_rows(records: list[ModelComparison]) -> list[dict[str, object]]:
    """One row per run, composed from its records: the newest training summary, the newest
    benchmark and the newest blind-review link, each with the record it came from and when
    that was recorded (a later record without a benchmark never hides an earlier one's).
    ``corpus_differs`` marks a run trained on another corpus than the most common one of
    the runs shown: its numbers do not compare with theirs.
    """
    ordered = sorted(records, key=lambda item: (item.created_at, item.comparison_id))
    training: dict[str, ModelComparison] = {}
    benchmark: dict[str, ModelComparison] = {}
    review: dict[str, ModelComparison] = {}
    for record in ordered:
        training[record.run_id] = record
        if record.inference is not None:
            benchmark[record.run_id] = record
        if record.quality is not None:
            review[record.run_id] = record
    corpora = [record.training.corpus_sha256 for record in training.values()]
    common = max(sorted(set(corpora)), key=corpora.count) if corpora else None
    rows: list[dict[str, object]] = []
    for run_id, record in sorted(
        training.items(), key=lambda item: (item[1].model_family, item[0])
    ):
        summary = record.training
        measured, judged = benchmark.get(run_id), review.get(run_id)
        inference = None if measured is None else measured.inference
        quality = None if judged is None else judged.quality
        host = summary.host
        rows.append(
            {
                "run_id": run_id,
                "model_family": record.model_family,
                "training_comparison_id": record.comparison_id,
                "training_recorded_at": _when(record),
                "corpus_sha256": summary.corpus_sha256,
                "corpus_differs": summary.corpus_sha256 != common,
                "dataset_sha256": summary.dataset_sha256,
                "dataset_seconds": summary.dataset_seconds,
                "epochs": summary.epochs,
                "updates": summary.updates,
                "training_wall_seconds": summary.wall_seconds,
                "training_device": summary.device,
                "training_host": None if host is None else _host_label(host),
                "benchmark_comparison_id": None if measured is None else measured.comparison_id,
                "benchmark_recorded_at": _when(measured),
                "real_time_factor": None if inference is None else inference.real_time_factor,
                "time_to_first_audio_ms": (
                    None if inference is None else inference.time_to_first_audio_ms
                ),
                "load_ms": None if inference is None else inference.load_ms,
                "peak_memory_bytes": (
                    None
                    if inference is None or inference.peak_memory is None
                    else inference.peak_memory.bytes
                ),
                "peak_memory_method": (
                    None
                    if inference is None or inference.peak_memory is None
                    else inference.peak_memory.method
                ),
                "deterministic": None if inference is None else inference.deterministic,
                "device": None if inference is None else inference.fingerprint.device,
                "benchmark_host": None if inference is None else _host_label(inference.host),
                "gpu_shared_with": (None if inference is None else _shared_with(inference)),
                "sandboxed": None if inference is None else inference.sandboxed,
                "review_comparison_id": None if judged is None else judged.comparison_id,
                "review_recorded_at": _when(judged),
                "blind_review": None if quality is None else quality.review_id,
                "review_overall_mean": None if quality is None else quality.overall_mean,
            }
        )
    return rows


def _host_label(host: HostFingerprint) -> str:
    gpus = ", ".join(
        " ".join(
            part
            for part in (
                item.name or f"{item.vendor_id or '?'}:{item.device_id or '?'}",
                item.driver,
                item.driver_version,
            )
            if part
        )
        for item in host.accelerators
    )
    parts = [host.cpu_model or host.machine, gpus or "no GPU listed"]
    if host.rocm_version:
        parts.append(f"ROCm {host.rocm_version}")
    if host.memory_bytes:
        parts.append(f"{host.memory_bytes / 2**30:.0f} GiB")
    return "; ".join(parts)


def _shared_with(inference: InferenceBenchmark) -> list[str] | None:
    seen = [
        name
        for snapshot in (inference.contention_before, inference.contention_after)
        for name in (snapshot.gpu_processes or ())
    ]
    if inference.contention_before.gpu_processes is None and (
        inference.contention_after.gpu_processes is None
    ):
        return None
    return sorted(set(seen))


def comparison_warnings(rows: list[dict[str, object]]) -> list[str]:
    """What makes the rows not compare like with like: corpora, hosts and the GPU's sharing."""
    warnings = [
        f"{row['run_id']} trained on another corpus ({str(row['corpus_sha256'])[:12]}…)"
        for row in rows
        if row["corpus_differs"]
    ]
    hosts = {row["benchmark_host"] for row in rows if row["benchmark_host"] is not None}
    if len(hosts) > 1:
        warnings.append("the benchmarks ran on different hosts")
    trained = {row["training_host"] for row in rows if row["training_host"] is not None}
    if len(trained) > 1:
        warnings.append("the runs trained on different hosts")
    for row in rows:
        shared = row["gpu_shared_with"]
        if shared:
            names = ", ".join(cast(list[str], shared))
            warnings.append(f"{row['run_id']}'s benchmark shared the GPU with {names}")
        if row["sandboxed"] is False:
            warnings.append(f"{row['run_id']}'s benchmark ran outside the speech sandbox")
    return warnings


def blind_review_link(
    layout: AssetLayout,
    review_id: str,
    score_sheet: bytes,
    *,
    score_sheet_sha256: str,
    candidate_id: str | None,
    run_id: str,
) -> QualityLink:
    """The link to blind review ``review_id``, from its score sheet itself.

    The sheet's digest must be ``score_sheet_sha256``, it must be review ``review_id``'s, and
    it must verify against the sealed review package (``blind.aggregate_scores``), and it must
    have scored the run as candidate ``candidate_id`` (default: the run's id), whose scores
    the link carries.
    Raises: ``ComparisonError``; ``ContractError``/``SecurityError`` from the package check.
    """
    if hashlib.sha256(score_sheet).hexdigest() != score_sheet_sha256:
        raise ComparisonError("the score sheet's digest is not the one given")
    try:
        sheet = ScoreSheet.model_validate_json(score_sheet)
    except ValidationError as error:
        raise ComparisonError(f"that is not a score sheet: {error}") from error
    if sheet.review_id != review_id:
        raise ComparisonError(f"that score sheet is review {sheet.review_id}'s, not {review_id}'s")
    summary = aggregate_scores(layout, sheet)
    wanted = candidate_id or run_id
    tracks = cast(dict[str, dict[str, list[dict[str, object]]]], summary["tracks"])
    found = next(
        (
            candidate
            for track in tracks.values()
            for candidate in track["candidates"]
            if candidate["candidate_id"] == wanted
        ),
        None,
    )
    if found is None:
        hint = "" if candidate_id is not None else " (name its candidate with --review-candidate)"
        raise ComparisonError(
            f"review {review_id} scored no candidate {wanted}: it did not judge run {run_id}{hint}"
        )
    means = cast(dict[str, float], found["means"])
    return QualityLink(
        review_id=review_id,
        score_sheet_sha256=score_sheet_sha256,
        candidate_id=wanted,
        sample_count=cast(int, found["sample_count"]),
        overall_mean=round(cast(float, found["overall_mean"]), 4),
        means={name: round(value, 4) for name, value in means.items()},
    )
