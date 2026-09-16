"""Fine-tune a TTS model from the recording library's latest kept takes.

@canonical finetune -- the fine-tuning corpus is the latest retained take of every spoken
catalog prompt, read straight from the library: native take heads and legacy (old-app) rows
through the same readers prompt_coverage uses, never through packages or the SQLite index.
A take counts for a prompt under the coverage rule (its recorded prompt, or an earlier
revision a published catalog's keep-recordings alias accepts); the transcript is always the
current prompt text. Every artifact is bound by SHA-256 so a run can be reproduced and
audited.
Related: prompt_coverage.py (readers), recording_index.py (retained predicate),
docs/FINETUNE.md (operator guide), DECISIONS.md D-016.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final, Literal, cast

from pydantic import BaseModel, Field, model_validator

from .audio import CANONICAL_SAMPLE_RATE_HZ
from .canonical import canonical_sha256, sha256_file, write_canonical_json
from .corpus_store import CorpusSnapshotSecurityError, require_referenced_snapshot
from .errors import VoiceLabError
from .models import (
    SILENT_PROMPT_CATEGORIES,
    ContractModel,
    Prompt,
    PromptSuite,
    Sha256,
    StableId,
    utc_now,
)
from .paths import AssetLayout, confined_path
from .prompt_coverage import coverage_catalog, subject_native_takes
from .recording_contracts import (
    FINETUNE_MAX_EPOCHS,
    CatalogReference,
    CatalogSnapshot,
    RecordingFrameRange,
    RecordingHistoryEntry,
    RecordingTake,
    RelativePath,
    UtcTimestamp,
)
from .recording_discovery_contracts import (
    DiscoveryAlias,
    LegacyProjectionRow,
    LegacyProjectionUnknown,
)
from .recording_index import is_retained
from .recording_index_backfill import legacy_projection_records
from .recording_library import RecordingLibrary
from .recording_takes import TAKE_PROVENANCE_ROOT, RecordingTakeStore
from .resources import resource_path

FINETUNE_CORPUS_SCHEMA: Final = "cole-voice-lab.finetune-corpus/v1"
FINETUNE_HYPERPARAMETERS_SCHEMA: Final = "cole-voice-lab.finetune-hyperparameters/v1"
FINETUNE_DATASET_SCHEMA: Final = "cole-voice-lab.finetune-dataset/v1"
FINETUNE_RUN_SCHEMA: Final = "cole-voice-lab.finetune-run/v1"
FINETUNE_SAMPLE_RATE_HZ: Final = 24_000
# F5-TTS inference raises a reference to RMS 0.1 (-20 dBFS); training copies get the same
# level so conditioning at inference matches what the model saw. Raw UMIK-1 takes sit near
# -50 dBFS RMS and would otherwise be trimmed as silence.
FINETUNE_TARGET_RMS = 0.1
FINETUNE_PEAK_CEILING_DBFS = -0.5
FINETUNE_NORMALIZATION_RULE = (
    "linear gain only: RMS raised to 0.1 (-20 dBFS) when below, capped so the peak stays "
    "at or below -0.5 dBFS; no compression, EQ, trimming or noise processing"
)

type FineTuneSourceKind = Literal["native", "legacy"]
type FineTuneDatePrecision = Literal["take", "session", "unknown"]
# Every fine-tune failure code. The recording service sends these under their own code, so
# recording_contracts.ErrorCode lists exactly this set (tests/test_finetune.py checks it).
# CORPUS_INCOMPLETE, JOB_ACTIVE, CANCELLED and INTERRUPTED belong to the service's job path.
type FineTuneErrorCode = Literal[
    "FINETUNE.CANCELLED",
    "FINETUNE.CATALOG_HELD_OUT",
    "FINETUNE.CHECKPOINT_MISSING",
    "FINETUNE.CORPUS_INCOMPLETE",
    "FINETUNE.CORPUS_PROJECTION_FAILED",
    "FINETUNE.DOWNLOADS_NOT_ALLOWED",
    "FINETUNE.DOWNLOAD_FAILED",
    "FINETUNE.EMPTY_CORPUS",
    "FINETUNE.FFMPEG_FAILED",
    "FINETUNE.FFMPEG_UNAVAILABLE",
    "FINETUNE.INTERRUPTED",
    "FINETUNE.JOB_ACTIVE",
    "FINETUNE.PRETRAINED_MISMATCH",
    "FINETUNE.REFERENCE_MISSING",
    "FINETUNE.REQUIREMENTS_MISSING",
    "FINETUNE.RUNTIME_FAILED",
    "FINETUNE.RUNTIME_UNAVAILABLE",
    "FINETUNE.RUN_EXISTS",
    "FINETUNE.SOURCE_CHANGED",
    "FINETUNE.TRAINING_FAILED",
    "FINETUNE.UNSUPPORTED_TEXT",
]


class FineTuneError(VoiceLabError):
    """Named fine-tune failure; ``code`` is one closed machine-readable ``FineTuneErrorCode``."""

    def __init__(self, code: FineTuneErrorCode, message: str) -> None:
        self.code: FineTuneErrorCode = code
        super().__init__(message)


class FineTuneCorpusEntry(ContractModel):
    """One spoken prompt's current recording: the latest retained take and where its audio is.

    ``prompt`` is the current catalog prompt (its text is the training transcript) and
    ``prompt_sha256`` its digest. ``recorded_prompt_sha256`` is the digest the audio was
    recorded against: equal to ``prompt_sha256`` unless the take counts through a
    keep-recordings alias of the current prompt. Writers must always state it; ``None`` only
    appears in a stored entry written before the field existed (omitted again on output, so
    that corpus keeps its exact bytes and digest).
    """

    prompt_id: StableId
    prompt_sha256: Sha256
    if TYPE_CHECKING:
        # No default for type checkers: every constructor call names the recorded digest.
        recorded_prompt_sha256: Sha256 | None
    else:
        # Stored corpora from before the field omit it; they read as None.
        recorded_prompt_sha256: Sha256 | None = Field(
            default=None, exclude_if=lambda value: value is None
        )
    prompt: Prompt
    take_id: StableId
    source_kind: FineTuneSourceKind
    source_path: RelativePath
    source_sha256: Sha256
    source_frames: int = Field(ge=1)
    source_sample_rate_hz: int = Field(ge=8_000, le=384_000)
    frame_range: RecordingFrameRange | None = None
    recorded_at: UtcTimestamp | None
    date_precision: FineTuneDatePrecision

    @model_validator(mode="after")
    def _exact_prompt_and_honest_date(self) -> FineTuneCorpusEntry:
        if self.prompt.prompt_id != self.prompt_id:
            raise ValueError("corpus entry prompt ID must match its embedded prompt")
        if canonical_sha256(self.prompt) != self.prompt_sha256:
            raise ValueError("corpus entry prompt digest must match its embedded prompt")
        if self.prompt.category in SILENT_PROMPT_CATEGORIES or self.prompt.text is None:
            raise ValueError("only spoken prompts enter a fine-tuning corpus")
        if (self.date_precision == "unknown") != (self.recorded_at is None):
            raise ValueError("corpus entry must keep its actual date precision")
        if self.frame_range is not None and self.frame_range.source_frames != self.source_frames:
            raise ValueError("trim recipe must describe the corpus entry's exact source")
        return self

    @property
    def text(self) -> str:
        assert self.prompt.text is not None  # validated above
        return self.prompt.text

    @property
    def retained_frames(self) -> int:
        if self.frame_range is None:
            return self.source_frames
        return self.frame_range.end_frame - self.frame_range.start_frame


class FineTuneCorpus(ContractModel):
    """The live corpus for one subject and catalog: one entry per spoken prompt, catalog order.

    ``catalog_reference`` names the published corpus-store catalog version the corpus was
    selected from; it is absent for a built-in catalog.
    """

    schema_version: Literal["cole-voice-lab.finetune-corpus/v1"] = FINETUNE_CORPUS_SCHEMA
    subject_id: StableId
    catalog_suite_id: StableId
    catalog_sha256: Sha256
    catalog_reference: CatalogReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    entries: tuple[FineTuneCorpusEntry, ...] = Field(max_length=10_000)
    missing_prompt_ids: tuple[StableId, ...] = Field(max_length=10_000)
    unknown_record_ids: tuple[str, ...] = Field(max_length=10_000)
    selected_at: UtcTimestamp

    @model_validator(mode="after")
    def _one_entry_per_prompt(self) -> FineTuneCorpus:
        ids = [entry.prompt_id for entry in self.entries] + list(self.missing_prompt_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("a prompt is either selected once or missing, never both")
        reference = self.catalog_reference
        if reference is not None and (
            reference.catalog_sha256 != self.catalog_sha256
            or reference.suite_id != self.catalog_suite_id
        ):
            raise ValueError("corpus catalog reference must name the exact selected catalog")
        return self

    @property
    def spoken_prompt_count(self) -> int:
        return len(self.entries) + len(self.missing_prompt_ids)

    @property
    def retained_seconds(self) -> float:
        return sum(entry.retained_frames / entry.source_sample_rate_hz for entry in self.entries)

    @property
    def complete(self) -> bool:
        return not self.missing_prompt_ids and not self.unknown_record_ids


class FineTuneHyperparameters(ContractModel):
    """Training settings passed verbatim to the F5-TTS fine-tune launcher."""

    schema_version: Literal["cole-voice-lab.finetune-hyperparameters/v1"] = (
        FINETUNE_HYPERPARAMETERS_SCHEMA
    )
    model_name: Literal["F5TTS_v1_Base"] = "F5TTS_v1_Base"
    epochs: int = Field(default=120, ge=1, le=FINETUNE_MAX_EPOCHS)
    learning_rate: float = Field(default=1e-5, gt=0.0, le=1.0, allow_inf_nan=False)
    batch_frames: int = Field(default=3200, ge=256, le=200_000)
    max_samples_per_batch: int = Field(default=16, ge=1, le=1024)
    warmup_updates: int = Field(default=50, ge=0, le=1_000_000)
    save_per_updates: int = Field(default=540, ge=1, le=10_000_000)
    keep_last_n_checkpoints: int = Field(default=1, ge=0, le=100)
    last_per_updates: int = Field(default=180, ge=1, le=10_000_000)
    max_grad_norm: float = Field(default=1.0, gt=0.0, allow_inf_nan=False)
    dataloader_workers: int = Field(default=2, ge=1, le=64)
    light_checkpoints: bool = True


class FineTuneDatasetClip(ContractModel):
    prompt_id: StableId
    take_id: StableId
    relative_path: RelativePath
    sha256: Sha256
    frames: int = Field(ge=1)
    sample_rate_hz: Literal[24_000] = FINETUNE_SAMPLE_RATE_HZ
    gain_db: float = Field(allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=2_000)


class FineTuneDataset(ContractModel):
    """Materialized training copies (24 kHz mono 16-bit, level-normalized) bound to a corpus."""

    schema_version: Literal["cole-voice-lab.finetune-dataset/v1"] = FINETUNE_DATASET_SCHEMA
    corpus_sha256: Sha256
    dataset_root: RelativePath
    sample_rate_hz: Literal[24_000] = FINETUNE_SAMPLE_RATE_HZ
    normalization_rule: str = Field(min_length=1, max_length=512)
    clips: tuple[FineTuneDatasetClip, ...] = Field(min_length=1, max_length=10_000)
    metadata_csv_sha256: Sha256
    materialized_at: UtcTimestamp

    @model_validator(mode="after")
    def _one_clip_per_prompt(self) -> FineTuneDataset:
        ids = [clip.prompt_id for clip in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("a dataset holds one clip per prompt")
        return self


class FineTuneRuntimeIdentity(ContractModel):
    """The out-of-process trainer: a pinned F5-TTS environment and its pretrained weights."""

    runtime_root: str = Field(min_length=1, max_length=4_096)
    python_sha256: Sha256
    f5_tts_version: str = Field(min_length=1, max_length=64)
    torch_version: str = Field(min_length=1, max_length=64)
    pretrained_path: str = Field(min_length=1, max_length=4_096)
    pretrained_sha256: Sha256


class FineTuneCheckpoint(ContractModel):
    relative_path: RelativePath
    sha256: Sha256
    size_bytes: int = Field(ge=1)
    update: int = Field(ge=0)
    kind: Literal["last", "intermediate"]


class FineTuneRun(ContractModel):
    """One training run: what it read, how it trained, what it produced."""

    schema_version: Literal["cole-voice-lab.finetune-run/v1"] = FINETUNE_RUN_SCHEMA
    run_id: StableId
    subject_id: StableId
    catalog_sha256: Sha256
    corpus_sha256: Sha256
    dataset_sha256: Sha256
    hyperparameters: FineTuneHyperparameters
    runtime: FineTuneRuntimeIdentity
    run_root: RelativePath
    log_path: RelativePath
    checkpoints: tuple[FineTuneCheckpoint, ...] = Field(max_length=200)
    status: Literal["running", "finished", "failed"]
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    error: str | None = Field(default=None, max_length=4_096)

    @model_validator(mode="after")
    def _terminal_state_is_dated(self) -> FineTuneRun:
        if (self.status == "running") == (self.finished_at is not None):
            raise ValueError("a finished or failed run carries its finish time; a running one none")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("failed runs name their error; other runs carry none")
        return self


FINETUNE_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "finetune-corpus": FineTuneCorpus,
    "finetune-hyperparameters": FineTuneHyperparameters,
    "finetune-dataset": FineTuneDataset,
    "finetune-run": FineTuneRun,
}


@dataclass(frozen=True, slots=True)
class _Candidate:
    entry: FineTuneCorpusEntry

    @property
    def order(self) -> tuple[bool, datetime, str]:
        """Newest capture date wins; unknown dates sort oldest; take ID breaks exact ties."""
        recorded = self.entry.recorded_at
        return (
            recorded is not None,
            recorded if recorded is not None else datetime.min.replace(tzinfo=UTC),
            self.entry.take_id,
        )


def _native_candidate(
    take: RecordingTake, prompt: Prompt, prompt_sha256: str
) -> FineTuneCorpusEntry | None:
    """A sealed retained native take as the entry of ``prompt`` (the current prompt)."""
    seal = take.seal
    if seal is None or not is_retained(take.decision, take.analysis):
        return None
    return FineTuneCorpusEntry(
        prompt_id=prompt.prompt_id,
        prompt_sha256=prompt_sha256,
        recorded_prompt_sha256=canonical_sha256(take.reservation.prompt),
        prompt=prompt,
        take_id=take.reservation.take_id,
        source_kind="native",
        source_path=seal.original_path,
        source_sha256=seal.original_sha256,
        source_frames=seal.audio.frames,
        source_sample_rate_hz=seal.audio.sample_rate_hz,
        frame_range=take.frame_range,
        recorded_at=seal.first_sample_at,
        date_precision="take",
    )


def _legacy_candidate(
    row: LegacyProjectionRow, prompt: Prompt, prompt_sha256: str
) -> tuple[FineTuneCorpusEntry | None, str | None]:
    """A legacy row as the entry of ``prompt`` (the current prompt), plus its readable source.

    Trims of legacy takes are not applied here.
    """
    entry: RecordingHistoryEntry = row.entry
    if not is_retained(entry.decision, entry.analysis):
        return None, None
    sources = [alias for alias in row.aliases if alias[2] != "rejected"]
    if not sources or entry.source_sha256 is None or entry.source_frames is None:
        return None, f"{entry.take_id}: retained legacy take has no readable source"
    path, digest, _kind = sources[0]
    if digest != entry.source_sha256:
        return None, f"{entry.take_id}: legacy source digest disagrees with its history row"
    return (
        FineTuneCorpusEntry(
            prompt_id=prompt.prompt_id,
            prompt_sha256=prompt_sha256,
            recorded_prompt_sha256=canonical_sha256(entry.prompt),
            prompt=prompt,
            take_id=entry.take_id,
            source_kind="legacy",
            source_path=path,
            source_sha256=digest,
            source_frames=entry.source_frames,
            source_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
            frame_range=None,
            recorded_at=entry.recorded_at,
            date_precision=entry.date_precision,
        ),
        None,
    )


def select_finetune_corpus(
    layout: AssetLayout,
    catalog: PromptSuite | CatalogSnapshot,
    subject_id: str,
    *,
    reference: CatalogReference | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FineTuneCorpus:
    """Pick the latest retained take of every spoken prompt in ``catalog`` for ``subject_id``.

    Reads native heads and legacy rows read-only, exactly as the coverage report does; never
    opens the SQLite index and never writes under the asset root. Retained means kept and not
    invalid (the index rule). A take counts for a current prompt when it was recorded against
    that prompt or, for a published snapshot, against an earlier revision the prompt's
    keep-recordings aliases accept (same prompt id); the latest retained take across all of
    them wins. Each entry carries the current prompt (its text is the training transcript)
    and the digest the audio was recorded against (``recorded_prompt_sha256``). Held-out
    evaluation material never becomes training audio: a held-out catalog is refused, and a
    take recorded in a held-out group, against a held-out prompt, or against a silent prompt
    is never a candidate (the alias rule already keeps those apart; this is the backstop).
    Params: ``catalog`` is a plain suite (built-in or embedded; only its own prompts count) or
    a published ``CatalogSnapshot``; ``reference`` names the published version ``catalog``
    was materialized from and is recorded as ``FineTuneCorpus.catalog_reference`` (``None``
    for a built-in); ``clock`` stamps ``selected_at``.
    Returns: the corpus, one entry per covered spoken prompt in catalog order.
    Raises: ``FineTuneError`` FINETUNE.CATALOG_HELD_OUT when ``catalog`` is a
    held-out-evaluation catalog, FINETUNE.CORPUS_PROJECTION_FAILED when evidence cannot be
    read; unreadable individual records land in ``unknown_record_ids`` instead of silently
    shrinking the corpus. ``CorpusSnapshotSecurityError`` (a ``SecurityError``), unwrapped,
    with ``CORPUS.SNAPSHOT_MISMATCH`` when ``reference`` does not seal exactly ``catalog``,
    or ``CORPUS.SNAPSHOT_MISSING`` / ``CORPUS.SNAPSHOT_MISMATCH`` when a native take's group
    references a snapshot that is missing or altered.
    """
    layout = AssetLayout.validate(layout.root)
    snapshot = coverage_catalog(catalog)
    if reference is not None:
        require_referenced_snapshot(snapshot, reference)
    suite = snapshot.suite
    if suite.purpose == "held-out-evaluation":
        raise FineTuneError(
            "FINETUNE.CATALOG_HELD_OUT",
            f"catalog {suite.suite_id} is held-out evaluation material and never trains a voice",
        )
    spoken = {
        prompt.prompt_id: (prompt, canonical_sha256(prompt))
        for prompt in suite.prompts
        if prompt.category not in SILENT_PROMPT_CATEGORIES and prompt.text is not None
    }
    candidates: dict[str, dict[str, _Candidate]] = {prompt_id: {} for prompt_id in spoken}
    unknown: list[str] = []

    def counted(recorded: Prompt) -> tuple[Prompt, str] | None:
        """The spoken current prompt (and its digest) a take recorded against ``recorded``
        counts for, or None. Only spoken, non-held-out recordings ever train."""
        if not recorded.spoken or recorded.holdout:
            return None
        current = snapshot.current_prompt_for(recorded)
        return None if current is None else spoken.get(current.prompt_id)

    def consider_native(take: RecordingTake) -> None:
        group = take.reservation.group
        if group.subject_id != subject_id or group.catalog_purpose == "held-out-evaluation":
            return
        target = counted(take.reservation.prompt)
        if target is None:
            return
        entry = _native_candidate(take, *target)
        if entry is not None:
            candidates[entry.prompt_id][entry.take_id] = _Candidate(entry)

    def collect(take: RecordingTake, alias: DiscoveryAlias) -> None:
        # Package aliases resolve to native heads; the store enumeration already saw them.
        consider_native(take)

    try:
        store = (
            RecordingTakeStore(layout, read_only=True)
            if os.path.lexists(layout.root / TAKE_PROVENANCE_ROOT)
            else None
        )
        for take in subject_native_takes(store, subject_id):
            consider_native(take)
        library = RecordingLibrary(layout, snapshot, subject_id)
        for record in legacy_projection_records(library, collect, take_store=store):
            if isinstance(record, LegacyProjectionUnknown):
                unknown.append(record.record_id)
            elif isinstance(record, LegacyProjectionRow):
                row_entry = record.entry
                if row_entry.subject_id != subject_id:
                    continue
                target = counted(row_entry.prompt)
                if target is None:
                    continue
                entry, problem = _legacy_candidate(record, *target)
                if problem is not None:
                    unknown.append(problem)
                elif entry is not None:
                    candidates[entry.prompt_id][entry.take_id] = _Candidate(entry)
    except CorpusSnapshotSecurityError:
        raise  # a sealed reference that no longer resolves is a hard failure, not a gap
    except (OSError, ValueError, VoiceLabError) as error:
        raise FineTuneError(
            "FINETUNE.CORPUS_PROJECTION_FAILED",
            f"fine-tune corpus projection failed for subject {subject_id!r}: {error}",
        ) from error

    entries: list[FineTuneCorpusEntry] = []
    missing: list[str] = []
    for prompt_id, options in candidates.items():  # catalog order
        if not options:
            missing.append(prompt_id)
            continue
        entries.append(max(options.values(), key=lambda candidate: candidate.order).entry)
    return FineTuneCorpus(
        subject_id=subject_id,
        catalog_suite_id=suite.suite_id,
        catalog_sha256=snapshot.catalog_sha256,
        catalog_reference=reference,
        entries=tuple(entries),
        missing_prompt_ids=tuple(missing),
        unknown_record_ids=tuple(sorted(set(unknown))),
        selected_at=clock(),
    )


_VOLUME_PATTERN = re.compile(r"(mean|max)_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


def _run_ffmpeg(ffmpeg: Path, arguments: list[str], *, what: str) -> str:
    """Run ffmpeg without a shell; return its stderr (where filters report)."""
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-y", *arguments]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise FineTuneError(
            "FINETUNE.FFMPEG_UNAVAILABLE", f"ffmpeg could not run for {what}: {error}"
        ) from error
    if completed.returncode != 0:
        raise FineTuneError(
            "FINETUNE.FFMPEG_FAILED",
            f"ffmpeg failed for {what} (exit {completed.returncode}): "
            f"{completed.stderr.strip()[-1024:]}",
        )
    return completed.stderr


def _measure_level(ffmpeg: Path, source: Path, trim: str) -> tuple[float, float]:
    """(RMS dBFS, peak dBFS) of the retained range, via ffmpeg's volumedetect."""
    stderr = _run_ffmpeg(
        ffmpeg,
        ["-i", str(source), "-af", f"{trim}volumedetect", "-f", "null", "-"],
        what=f"level measurement of {source.name}",
    )
    found = {kind: float(value) for kind, value in _VOLUME_PATTERN.findall(stderr)}
    if "mean" not in found or "max" not in found:
        raise FineTuneError("FINETUNE.FFMPEG_FAILED", f"ffmpeg reported no level for {source.name}")
    return found["mean"], found["max"]


def normalization_gain_db(rms_dbfs: float, peak_dbfs: float) -> float:
    """The FINETUNE_NORMALIZATION_RULE gain: RMS up to the target, never past the peak ceiling."""
    target_rms_dbfs = 20.0 * math.log10(FINETUNE_TARGET_RMS)
    gain = max(0.0, target_rms_dbfs - rms_dbfs)
    return round(min(gain, FINETUNE_PEAK_CEILING_DBFS - peak_dbfs), 2)


def _wav_frames(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path), "rb") as handle:
        return (
            handle.getnframes(),
            handle.getframerate(),
            handle.getnchannels(),
            handle.getsampwidth(),
        )


def materialize_finetune_dataset(
    layout: AssetLayout,
    corpus: FineTuneCorpus,
    destination: str,
    *,
    ffmpeg: Path = Path("/opt/homebrew/bin/ffmpeg"),
    clock: Callable[[], datetime] = utc_now,
) -> FineTuneDataset:
    """Write level-normalized 24 kHz mono 16-bit training copies of ``corpus`` to ``destination``.

    ``destination`` is asset-root-relative (normally ``derived/finetune/<run>/dataset``). Each
    source is re-hashed against the corpus first (FINETUNE.SOURCE_CHANGED on mismatch); a trim
    recipe is applied sample-exactly; the gain follows FINETUNE_NORMALIZATION_RULE and is
    recorded per clip. Writes ``wavs/<prompt_id>.wav``, ``metadata.csv`` (``audio_file|text``
    with absolute paths, the F5-TTS input format), ``corpus.json`` and ``dataset.json``.
    Originals under tmp/, raw/ and provenance/ are only ever read.
    """
    layout = AssetLayout.validate(layout.root)
    if not corpus.entries:
        raise FineTuneError("FINETUNE.EMPTY_CORPUS", "no retained takes to materialize")
    root = confined_path(layout.root, destination)
    wavs = root / "wavs"
    wavs.mkdir(parents=True, exist_ok=True, mode=0o700)
    clips: list[FineTuneDatasetClip] = []
    rows: list[tuple[Path, str]] = []
    for entry in corpus.entries:
        source = confined_path(layout.root, entry.source_path, must_exist=True)
        digest, _ = sha256_file(source)
        if digest != entry.source_sha256:
            raise FineTuneError(
                "FINETUNE.SOURCE_CHANGED",
                f"{entry.prompt_id}: source {entry.source_path} no longer matches the corpus",
            )
        trim = ""
        if entry.frame_range is not None:
            start, end = entry.frame_range.start_frame, entry.frame_range.end_frame
            trim = f"atrim=start_sample={start}:end_sample={end},"
        rms_dbfs, peak_dbfs = _measure_level(ffmpeg, source, trim)
        gain_db = normalization_gain_db(rms_dbfs, peak_dbfs)
        output = wavs / f"{entry.prompt_id}.wav"
        _run_ffmpeg(
            ffmpeg,
            [
                "-i",
                str(source),
                "-af",
                f"{trim}volume={gain_db}dB",
                "-ac",
                "1",
                "-ar",
                str(FINETUNE_SAMPLE_RATE_HZ),
                "-c:a",
                "pcm_s16le",
                str(output),
            ],
            what=f"training copy of {entry.prompt_id}",
        )
        os.chmod(output, 0o600)
        frames, rate, channels, width = _wav_frames(output)
        if (rate, channels, width) != (FINETUNE_SAMPLE_RATE_HZ, 1, 2) or frames < 1:
            raise FineTuneError(
                "FINETUNE.FFMPEG_FAILED", f"{entry.prompt_id}: training copy has the wrong format"
            )
        clip_digest, _ = sha256_file(output)
        clips.append(
            FineTuneDatasetClip(
                prompt_id=entry.prompt_id,
                take_id=entry.take_id,
                relative_path=f"wavs/{entry.prompt_id}.wav",
                sha256=clip_digest,
                frames=frames,
                gain_db=gain_db,
                text=entry.text,
            )
        )
        rows.append((output.resolve(), entry.text))
    metadata = root / "metadata.csv"
    lines = ["audio_file|text"]
    for path, text in rows:
        if "|" in text or "\n" in text:
            raise FineTuneError(
                "FINETUNE.UNSUPPORTED_TEXT", "prompt text cannot contain '|' or a newline"
            )
        lines.append(f"{path}|{text}")
    metadata.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(metadata, 0o600)
    metadata_sha256, _ = sha256_file(metadata)
    write_canonical_json(root / "corpus.json", corpus)
    dataset = FineTuneDataset(
        corpus_sha256=canonical_sha256(corpus),
        dataset_root=root.relative_to(layout.root).as_posix(),
        normalization_rule=FINETUNE_NORMALIZATION_RULE,
        clips=tuple(clips),
        metadata_csv_sha256=metadata_sha256,
        materialized_at=clock(),
    )
    write_canonical_json(root / "dataset.json", dataset)
    return dataset


# The pinned pretrained weights. A runtime whose copy hashes differently is refused.
FINETUNE_PRETRAINED_REPOSITORY: Final = "SWivid/F5-TTS"
FINETUNE_PRETRAINED_REVISION: Final = "84e5a410d9cead4de2f847e7c9369a6440bdfaca"
FINETUNE_PRETRAINED_FILE: Final = "F5TTS_v1_Base/model_1250000.safetensors"
FINETUNE_PRETRAINED_VOCAB_FILE: Final = "F5TTS_v1_Base/vocab.txt"
FINETUNE_PRETRAINED_SHA256: Final = (
    "670900fd14e6c458b95da6e9ed317cdb20dbaf7a1c02ac06a05475a9d32b6a38"
)
FINETUNE_PRETRAINED_SIZE_BYTES: Final = 1_348_435_761
# finetune_cli copies the pretrained vocabulary from this dataset name inside the runtime.
_F5_PRETRAINED_VOCAB_DATASET: Final = "Emilia_ZH_EN_pinyin"
# importlib.resources.files() can hand back a MultiplexedPath whose str() is a repr, so the
# package directory comes from the module's own __path__.
_RUNTIME_PROBE: Final = (
    "import json, os, importlib.metadata as metadata, torch, f5_tts\n"
    "print(json.dumps({'f5_tts_version': metadata.version('f5-tts'), "
    "'torch_version': torch.__version__, "
    "'package_path': os.path.abspath(list(f5_tts.__path__)[0])}))\n"
)


@dataclass(frozen=True, slots=True)
class FineTuneRuntime:
    """A provisioned F5-TTS environment: where its python, package, data and checkpoints live."""

    root: Path
    identity: FineTuneRuntimeIdentity
    package_path: Path

    @property
    def python(self) -> Path:
        return self.root / "bin" / "python"

    @property
    def data_directory(self) -> Path:
        # finetune_cli resolves datasets relative to the installed package: <pkg>/../../data
        return (self.package_path / ".." / ".." / "data").resolve()

    @property
    def checkpoints_directory(self) -> Path:
        return (self.package_path / ".." / ".." / "ckpts").resolve()

    @property
    def pretrained_path(self) -> Path:
        return Path(self.identity.pretrained_path)

    @property
    def pretrained_vocab_path(self) -> Path:
        return self.root / "pretrained" / FINETUNE_PRETRAINED_VOCAB_FILE


def _run_runtime(
    runtime_python: Path, arguments: list[str], *, cwd: Path | None, log: Path | None, what: str
) -> str:
    """Run the runtime's python out of process; stdout is returned or, with ``log``, appended."""
    command = [str(runtime_python), "-B", *arguments]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        if log is None:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False, cwd=cwd, env=environment
            )
            returncode, output, errors = completed.returncode, completed.stdout, completed.stderr
        else:
            with log.open("ab") as stream:
                returncode = subprocess.run(
                    command,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    check=False,
                    cwd=cwd,
                    env=environment,
                ).returncode
            output, errors = "", ""
    except OSError as error:
        raise FineTuneError(
            "FINETUNE.RUNTIME_UNAVAILABLE", f"the F5 runtime could not run {what}: {error}"
        ) from error
    if returncode != 0:
        detail = errors.strip()[-1024:] if log is None else f"see {log}"
        raise FineTuneError(
            "FINETUNE.RUNTIME_FAILED", f"{what} failed (exit {returncode}): {detail}"
        )
    return output


def describe_finetune_runtime(
    runtime_root: Path,
    *,
    pretrained_pin: tuple[str, int] | None = None,
) -> FineTuneRuntime:
    """Bind a runtime directory: its python, installed F5-TTS, and the pinned pretrained weights.

    Raises FINETUNE.RUNTIME_UNAVAILABLE when ``<root>/bin/python`` or the package is missing
    and FINETUNE.PRETRAINED_MISMATCH when ``<root>/pretrained/<pinned file>`` is absent or
    hashes differently from ``pretrained_pin`` (the F5-TTS v1 Base pin; tests substitute).
    """
    root = Path(runtime_root).expanduser().resolve()
    python = root / "bin" / "python"
    if not python.is_file():
        raise FineTuneError(
            "FINETUNE.RUNTIME_UNAVAILABLE", f"no runtime python at {python}; provision it first"
        )
    probe = _run_runtime(python, ["-c", _RUNTIME_PROBE], cwd=None, log=None, what="runtime probe")
    try:
        described = json.loads(probe.strip().splitlines()[-1])
        package_path = Path(str(described["package_path"]))
        f5_version, torch_version = (
            str(described["f5_tts_version"]),
            str(described["torch_version"]),
        )
    except (ValueError, KeyError, IndexError) as error:
        raise FineTuneError(
            "FINETUNE.RUNTIME_UNAVAILABLE", f"runtime probe returned no usable description: {error}"
        ) from error
    # A relative or repr-like package path would make data/ and ckpts/ resolve against the
    # caller's working directory (observed once as a stray "MultiplexedPath(…" tree).
    if not package_path.is_absolute() or not package_path.is_dir():
        raise FineTuneError(
            "FINETUNE.RUNTIME_UNAVAILABLE",
            f"runtime probe named a package path that is not an absolute directory: {package_path}",
        )
    pretrained = root / "pretrained" / FINETUNE_PRETRAINED_FILE
    if not pretrained.is_file():
        raise FineTuneError(
            "FINETUNE.PRETRAINED_MISMATCH", f"pretrained weights missing at {pretrained}"
        )
    digest, size = sha256_file(pretrained)
    expected = pretrained_pin or (FINETUNE_PRETRAINED_SHA256, FINETUNE_PRETRAINED_SIZE_BYTES)
    if (digest, size) != expected:
        raise FineTuneError(
            "FINETUNE.PRETRAINED_MISMATCH",
            f"pretrained weights at {pretrained} do not match the pinned F5-TTS v1 Base",
        )
    return FineTuneRuntime(
        root=root,
        identity=FineTuneRuntimeIdentity(
            runtime_root=str(root),
            python_sha256=sha256_file(python.resolve())[0],
            f5_tts_version=f5_version,
            torch_version=torch_version,
            pretrained_path=str(pretrained),
            pretrained_sha256=digest,
        ),
        package_path=package_path,
    )


def finetune_cli_arguments(
    hyperparameters: FineTuneHyperparameters, *, dataset_name: str, pretrained_path: Path
) -> list[str]:
    """The exact argv the launcher hands to ``f5_tts.train.finetune_cli`` (pure, testable)."""
    lab = ["--lab-dataloader-workers", str(hyperparameters.dataloader_workers)]
    if hyperparameters.light_checkpoints:
        lab.append("--lab-light-checkpoints")
    return [
        *lab,
        "--exp_name",
        hyperparameters.model_name,
        "--dataset_name",
        dataset_name,
        "--finetune",
        "--pretrain",
        str(pretrained_path),
        "--tokenizer",
        "pinyin",
        "--learning_rate",
        repr(hyperparameters.learning_rate),
        "--batch_size_type",
        "frame",
        "--batch_size_per_gpu",
        str(hyperparameters.batch_frames),
        "--max_samples",
        str(hyperparameters.max_samples_per_batch),
        "--grad_accumulation_steps",
        "1",
        "--max_grad_norm",
        repr(hyperparameters.max_grad_norm),
        "--epochs",
        str(hyperparameters.epochs),
        "--num_warmup_updates",
        str(hyperparameters.warmup_updates),
        "--save_per_updates",
        str(hyperparameters.save_per_updates),
        "--keep_last_n_checkpoints",
        str(hyperparameters.keep_last_n_checkpoints),
        "--last_per_updates",
        str(hyperparameters.last_per_updates),
    ]


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _checkpoints_in(directory: Path, relative_root: Path) -> tuple[FineTuneCheckpoint, ...]:
    found: list[FineTuneCheckpoint] = []
    for path in sorted(directory.glob("model_*.pt")):
        if path.is_symlink() or not path.is_file():
            continue
        digest, size = sha256_file(path)
        stem = path.stem.removeprefix("model_")
        kind: Literal["last", "intermediate"] = "last" if stem == "last" else "intermediate"
        update = 0 if stem == "last" else int(stem)
        found.append(
            FineTuneCheckpoint(
                relative_path=path.relative_to(relative_root).as_posix(),
                sha256=digest,
                size_bytes=size,
                update=update,
                kind=kind,
            )
        )
    return tuple(found)


def run_f5_finetune(
    layout: AssetLayout,
    dataset: FineTuneDataset,
    runtime: FineTuneRuntime,
    hyperparameters: FineTuneHyperparameters,
    run_root: str,
    *,
    run_id: str,
    subject_id: str,
    catalog_sha256: str,
    launcher: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FineTuneRun:
    """Train F5-TTS on ``dataset`` in the runtime; blocks until done and returns the run record.

    Packs the dataset with the runtime's own ``prepare_csv_wavs.py`` under the runtime's
    ``data/<run_id>_pinyin``, points the runtime's ``ckpts/<run_id>`` at ``<run_root>/checkpoints``
    (a symlink inside the runtime tree; nothing is written under the asset root by the runtime
    except through it), hard-links the pinned pretrained weights there so finetune_cli copies
    nothing, and streams the trainer's output to ``<run_root>/train.log``. ``run.json`` is
    written with status ``running`` before launch and rewritten ``finished`` or ``failed``.
    Raises FINETUNE.TRAINING_FAILED after writing the failed record.
    """
    layout = AssetLayout.validate(layout.root)
    run_directory = confined_path(layout.root, run_root)
    run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoints = run_directory / "checkpoints"
    checkpoints.mkdir(exist_ok=True, mode=0o700)
    log = run_directory / "train.log"
    dataset_root = confined_path(layout.root, dataset.dataset_root, must_exist=True)
    metadata = dataset_root / "metadata.csv"
    if sha256_file(metadata)[0] != dataset.metadata_csv_sha256:
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED", f"{metadata} changed since the dataset was materialized"
        )
    started = clock()
    run = FineTuneRun(
        run_id=run_id,
        subject_id=subject_id,
        catalog_sha256=catalog_sha256,
        corpus_sha256=dataset.corpus_sha256,
        dataset_sha256=canonical_sha256(dataset),
        hyperparameters=hyperparameters,
        runtime=runtime.identity,
        run_root=run_directory.relative_to(layout.root).as_posix(),
        log_path=log.relative_to(layout.root).as_posix(),
        checkpoints=(),
        status="running",
        started_at=started,
    )
    write_canonical_json(run_directory / "run.json", run)

    def fail(code: FineTuneErrorCode, message: str) -> FineTuneError:
        failed = run.model_copy(
            update={
                "status": "failed",
                "finished_at": clock(),
                "error": message[:4_000],
                "checkpoints": _checkpoints_in(checkpoints, layout.root),
            }
        )
        write_canonical_json(run_directory / "run.json", failed)
        return FineTuneError(code, message)

    try:
        vocab_target = runtime.data_directory / _F5_PRETRAINED_VOCAB_DATASET / "vocab.txt"
        if not vocab_target.is_file():
            if not runtime.pretrained_vocab_path.is_file():
                raise FineTuneError(
                    "FINETUNE.PRETRAINED_MISMATCH",
                    f"pretrained vocabulary missing at {runtime.pretrained_vocab_path}",
                )
            vocab_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(runtime.pretrained_vocab_path, vocab_target)
        packed = runtime.data_directory / f"{run_id}_pinyin"
        _run_runtime(
            runtime.python,
            [
                str(runtime.package_path / "train" / "datasets" / "prepare_csv_wavs.py"),
                str(metadata),
                str(packed),
                "--workers",
                "2",
            ],
            cwd=run_directory,
            log=log,
            what="dataset packing",
        )
        runtime.checkpoints_directory.mkdir(parents=True, exist_ok=True)
        link = runtime.checkpoints_directory / run_id
        if link.is_symlink() or link.exists():
            if not (link.is_symlink() and link.resolve() == checkpoints.resolve()):
                raise FineTuneError("FINETUNE.RUN_EXISTS", f"{link} already belongs to another run")
        else:
            os.symlink(checkpoints.resolve(), link)
        _link_or_copy(
            runtime.pretrained_path, checkpoints / f"pretrained_{runtime.pretrained_path.name}"
        )
        launcher_path = launcher or resource_path("f5_finetune_launcher.py")
        _run_runtime(
            runtime.python,
            [
                str(launcher_path),
                *finetune_cli_arguments(
                    hyperparameters, dataset_name=run_id, pretrained_path=runtime.pretrained_path
                ),
            ],
            cwd=run_directory,
            log=log,
            what="training",
        )
    except FineTuneError as error:
        raise fail(
            "FINETUNE.TRAINING_FAILED" if error.code == "FINETUNE.RUNTIME_FAILED" else error.code,
            str(error),
        ) from error
    produced = _checkpoints_in(checkpoints, layout.root)
    if not any(checkpoint.kind == "last" for checkpoint in produced):
        raise fail("FINETUNE.TRAINING_FAILED", f"training ended without model_last.pt; see {log}")
    finished = run.model_copy(
        update={"status": "finished", "finished_at": clock(), "checkpoints": produced}
    )
    write_canonical_json(run_directory / "run.json", finished)
    return finished


class FineTuneSample(ContractModel):
    index: int = Field(ge=1)
    relative_path: RelativePath
    sha256: Sha256
    seconds: float = Field(gt=0.0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=2_000)


class FineTuneSampleSet(ContractModel):
    """Listening samples generated from one checkpoint with one reference clip; not accepted raw."""

    schema_version: Literal["cole-voice-lab.finetune-samples/v1"] = (
        "cole-voice-lab.finetune-samples/v1"
    )
    label: StableId
    checkpoint_path: str = Field(min_length=1, max_length=4_096)
    checkpoint_sha256: Sha256
    use_ema: bool
    nfe_steps: int = Field(ge=1, le=256)
    device: Literal["mps", "cpu"]
    reference_prompt_id: StableId
    reference_audio_sha256: Sha256
    samples: tuple[FineTuneSample, ...] = Field(min_length=1, max_length=1_000)
    created_at: UtcTimestamp


FINETUNE_SCHEMA_MODELS["finetune-samples"] = FineTuneSampleSet

FINETUNE_DEFAULT_SAMPLE_TEXTS: Final = (
    "This is a quick check of the fine-tuned voice, reading a sentence it has never seen before.",
    "The meeting moved to Thursday at four thirty, "
    "so please update the shared calendar before noon.",
    "For a few quiet minutes, the whole house seemed to breathe with the wind in the trees.",
)


def synthesize_finetune_samples(
    layout: AssetLayout,
    dataset: FineTuneDataset,
    runtime: FineTuneRuntime,
    checkpoint: Path,
    output_root: str,
    *,
    label: str,
    reference_prompt_id: str,
    texts: tuple[str, ...] = FINETUNE_DEFAULT_SAMPLE_TEXTS,
    use_ema: bool = True,
    nfe_steps: int = 32,
    device: Literal["mps", "cpu"] = "mps",
    synthesizer: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FineTuneSampleSet:
    """Generate ``texts`` with ``checkpoint`` (fine-tuned or the pinned base) into ``output_root``.

    The reference is the dataset's normalized copy of ``reference_prompt_id`` with its exact
    prompt text. Writes ``sample-NN.wav`` and ``samples.json``. Raises FINETUNE.RUNTIME_FAILED
    with the synthesizer's last output when generation fails.
    """
    layout = AssetLayout.validate(layout.root)
    clip = next((item for item in dataset.clips if item.prompt_id == reference_prompt_id), None)
    if clip is None:
        raise FineTuneError(
            "FINETUNE.REFERENCE_MISSING", f"{reference_prompt_id} is not in the dataset"
        )
    dataset_root = confined_path(layout.root, dataset.dataset_root, must_exist=True)
    reference_audio = dataset_root / clip.relative_path
    if sha256_file(reference_audio)[0] != clip.sha256:
        raise FineTuneError("FINETUNE.SOURCE_CHANGED", f"{reference_audio} changed since packing")
    checkpoint = checkpoint.expanduser().resolve()
    if not checkpoint.is_file():
        raise FineTuneError("FINETUNE.CHECKPOINT_MISSING", f"no checkpoint at {checkpoint}")
    vocab = runtime.data_directory / _F5_PRETRAINED_VOCAB_DATASET / "vocab.txt"
    if not vocab.is_file():
        vocab = runtime.pretrained_vocab_path
    output_directory = confined_path(layout.root, output_root)
    output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    job = {
        "checkpoint_path": str(checkpoint),
        "vocab_path": str(vocab),
        "use_ema": use_ema,
        "device": device,
        "nfe_steps": nfe_steps,
        "reference_audio_path": str(reference_audio),
        "reference_text": clip.text,
        "output_directory": str(output_directory),
        "texts": list(texts),
    }
    job_path = output_directory / "job.json"
    write_canonical_json(job_path, job)
    output = _run_runtime(
        runtime.python,
        [str(synthesizer or resource_path("f5_synthesize.py")), str(job_path)],
        cwd=output_directory,
        log=None,
        what=f"sample synthesis for {label}",
    )
    samples: list[FineTuneSample] = []
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        reported = json.loads(line)
        path = Path(str(reported["path"]))
        samples.append(
            FineTuneSample(
                index=int(reported["index"]),
                relative_path=path.relative_to(layout.root).as_posix(),
                sha256=sha256_file(path)[0],
                seconds=float(reported["seconds"]),
                text=texts[int(reported["index"]) - 1],
            )
        )
    if len(samples) != len(texts):
        raise FineTuneError(
            "FINETUNE.RUNTIME_FAILED",
            f"synthesizer reported {len(samples)} of {len(texts)} samples",
        )
    sample_set = FineTuneSampleSet(
        label=label,
        checkpoint_path=str(checkpoint),
        checkpoint_sha256=sha256_file(checkpoint)[0],
        use_ema=use_ema,
        nfe_steps=nfe_steps,
        device=device,
        reference_prompt_id=reference_prompt_id,
        reference_audio_sha256=clip.sha256,
        samples=tuple(samples),
        created_at=clock(),
    )
    write_canonical_json(output_directory / "samples.json", sample_set)
    return sample_set


# Provisioning: one venv from the pinned requirements plus the two pinned Hugging Face files.
FINETUNE_RUNTIME_REQUIREMENTS: Final = "requirements/runtimes/f5-tts-finetune-1.1.22-mps.txt"
FINETUNE_RUNTIME_PYTHON: Final = "/opt/homebrew/opt/python@3.12/bin/python3.12"
FINETUNE_PRETRAINED_VOCAB_SHA256: Final = (
    "2a05f992e00af9b0bd3800a8d23e78d520dbd705284ed2eedb5f4bd29398fa3c"
)
FINETUNE_PRETRAINED_VOCAB_SIZE_BYTES: Final = 13_800
FINETUNE_HUGGINGFACE_BASE_URL: Final = "https://huggingface.co"
_DOWNLOAD_CHUNK_BYTES: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class FineTuneDownload:
    url: str
    destination: Path
    sha256: str
    size_bytes: int

    @property
    def present(self) -> bool:
        if not self.destination.is_file():
            return False
        return sha256_file(self.destination) == (self.sha256, self.size_bytes)


@dataclass(frozen=True, slots=True)
class FineTuneProvisionPlan:
    """What provisioning would run; printed without ``--execute``, executed with it."""

    runtime_root: Path
    python: Path
    requirements: Path
    create_venv: bool
    install_command: tuple[str, ...]
    downloads: tuple[FineTuneDownload, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "runtime_root": str(self.runtime_root),
            "python": str(self.python),
            "requirements": str(self.requirements),
            "create_venv": self.create_venv,
            "install_command": list(self.install_command),
            "downloads": [
                {
                    "url": item.url,
                    "destination": str(item.destination),
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "present": item.present,
                }
                for item in self.downloads
            ],
        }


def default_finetune_requirements() -> Path:
    """The pinned requirements file in this checkout; wheel installs must pass one explicitly."""
    candidate = Path(__file__).resolve().parents[2] / FINETUNE_RUNTIME_REQUIREMENTS
    if not candidate.is_file():
        raise FineTuneError(
            "FINETUNE.REQUIREMENTS_MISSING",
            f"no pinned requirements at {candidate}; pass --requirements",
        )
    return candidate


def pretrained_downloads(runtime_root: Path) -> tuple[FineTuneDownload, ...]:
    base = f"{FINETUNE_HUGGINGFACE_BASE_URL}/{FINETUNE_PRETRAINED_REPOSITORY}/resolve"
    pretrained = runtime_root / "pretrained"
    return (
        FineTuneDownload(
            url=f"{base}/{FINETUNE_PRETRAINED_REVISION}/{FINETUNE_PRETRAINED_FILE}",
            destination=pretrained / FINETUNE_PRETRAINED_FILE,
            sha256=FINETUNE_PRETRAINED_SHA256,
            size_bytes=FINETUNE_PRETRAINED_SIZE_BYTES,
        ),
        FineTuneDownload(
            url=f"{base}/{FINETUNE_PRETRAINED_REVISION}/{FINETUNE_PRETRAINED_VOCAB_FILE}",
            destination=pretrained / FINETUNE_PRETRAINED_VOCAB_FILE,
            sha256=FINETUNE_PRETRAINED_VOCAB_SHA256,
            size_bytes=FINETUNE_PRETRAINED_VOCAB_SIZE_BYTES,
        ),
    )


def plan_finetune_runtime_provisioning(
    runtime_root: Path, *, python: Path | None = None, requirements: Path | None = None
) -> FineTuneProvisionPlan:
    root = Path(runtime_root).expanduser().resolve(strict=False)
    interpreter = (python or Path(FINETUNE_RUNTIME_PYTHON)).expanduser()
    pins = (requirements or default_finetune_requirements()).expanduser().resolve()
    if not pins.is_file():
        raise FineTuneError("FINETUNE.REQUIREMENTS_MISSING", f"no requirements file at {pins}")
    return FineTuneProvisionPlan(
        runtime_root=root,
        python=interpreter,
        requirements=pins,
        create_venv=not (root / "bin" / "python").is_file(),
        install_command=(
            str(root / "bin" / "python"),
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-virtualenv",
            "-r",
            str(pins),
        ),
        downloads=pretrained_downloads(root),
    )


type Opener = Callable[[str], IO[bytes]]


def _default_opener(url: str) -> IO[bytes]:
    from urllib.request import Request, urlopen

    return cast(IO[bytes], urlopen(Request(url, headers={"User-Agent": "cole-voice-lab"})))


def download_verified(item: FineTuneDownload, *, opener: Opener = _default_opener) -> Path:
    """Stream ``item.url`` to a temporary file, verify size and SHA-256, then publish it."""
    item.destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = item.destination.with_name(f".{item.destination.name}.partial")
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(item.url) as source, partial.open("wb") as sink:
            while chunk := source.read(_DOWNLOAD_CHUNK_BYTES):
                digest.update(chunk)
                received += len(chunk)
                if received > item.size_bytes:
                    raise FineTuneError(
                        "FINETUNE.PRETRAINED_MISMATCH", f"{item.url} is larger than its pin"
                    )
                sink.write(chunk)
        if received != item.size_bytes or digest.hexdigest() != item.sha256:
            raise FineTuneError(
                "FINETUNE.PRETRAINED_MISMATCH",
                f"{item.url} does not match its pin (got {received} bytes, {digest.hexdigest()})",
            )
        os.chmod(partial, 0o600)
        os.replace(partial, item.destination)
    except OSError as error:
        raise FineTuneError(
            "FINETUNE.DOWNLOAD_FAILED", f"could not download {item.url}: {error}"
        ) from error
    finally:
        partial.unlink(missing_ok=True)
    return item.destination


def fetch_finetune_pretrained(
    runtime_root: Path, *, allow_downloads: bool, opener: Opener = _default_opener
) -> tuple[Path, ...]:
    """Ensure the pinned weights and vocabulary exist under ``<root>/pretrained``.

    Files already matching their pin are kept. Anything else needs ``allow_downloads``;
    without it FINETUNE.DOWNLOADS_NOT_ALLOWED names what is missing.
    """
    root = Path(runtime_root).expanduser().resolve(strict=False)
    items = pretrained_downloads(root)
    missing = [item for item in items if not item.present]
    if missing and not allow_downloads:
        raise FineTuneError(
            "FINETUNE.DOWNLOADS_NOT_ALLOWED",
            "pretrained files are missing or differ from their pin: "
            + ", ".join(str(item.destination) for item in missing)
            + "; rerun with --allow-downloads",
        )
    for item in missing:
        download_verified(item, opener=opener)
    return tuple(item.destination for item in items)


def execute_finetune_runtime_provisioning(
    plan: FineTuneProvisionPlan,
    *,
    allow_downloads: bool,
    opener: Opener = _default_opener,
    runner: Callable[[tuple[str, ...]], int] | None = None,
) -> FineTuneRuntime:
    """Create the venv, install the pins, fetch the pinned files, then describe the runtime.

    pip needs the network, so installing requires ``allow_downloads``. ``runner`` executes one
    command and returns its exit status (tests substitute one).
    """
    if not allow_downloads:
        raise FineTuneError(
            "FINETUNE.DOWNLOADS_NOT_ALLOWED",
            "installing the runtime downloads packages and weights; rerun with --allow-downloads",
        )

    def run(command: tuple[str, ...]) -> int:
        try:
            return subprocess.run(list(command), check=False).returncode
        except OSError as error:
            raise FineTuneError(
                "FINETUNE.RUNTIME_UNAVAILABLE", f"could not run {command[0]}: {error}"
            ) from error

    execute = runner or run
    if plan.create_venv:
        plan.runtime_root.parent.mkdir(parents=True, exist_ok=True)
        status = execute((str(plan.python), "-B", "-m", "venv", str(plan.runtime_root)))
        if status != 0:
            raise FineTuneError("FINETUNE.RUNTIME_UNAVAILABLE", f"venv creation failed ({status})")
        os.chmod(plan.runtime_root, 0o700)
    status = execute(plan.install_command)
    if status != 0:
        raise FineTuneError("FINETUNE.RUNTIME_UNAVAILABLE", f"pip install failed ({status})")
    fetch_finetune_pretrained(plan.runtime_root, allow_downloads=True, opener=opener)
    return describe_finetune_runtime(plan.runtime_root)
