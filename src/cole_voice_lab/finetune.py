"""Fine-tune a TTS model from the recording library's latest kept takes.

@canonical finetune -- the fine-tuning corpus is the latest retained take of every spoken
catalog prompt, read straight from the library: native take heads and legacy (old-app) rows
through the same readers prompt_coverage uses, never through packages or the SQLite index.
A take counts for a prompt under the coverage rule (its recorded prompt, or an earlier
revision a published catalog's keep-recordings alias accepts); the transcript is always the
current prompt text. Every artifact is bound by SHA-256 so a run can be reproduced and
audited. ``run_finetune_pipeline`` is the one select → dataset → train → sample chain; the
CLI and the recording service's job worker both call it, and ``finished_finetune_run_ids``
is the one ordering of finished runs ("the latest model" is its first). Speak requests
(``resolve_speech_model`` → ``synthesize_speech``) speak one text with a finished run or the
pinned base weights into ``derived/speech/<operation id>/``, through the same synthesizer
core as the listening samples; ``FineTuneDigestCache`` lets repeated requests reuse the
digests of unchanged runtime files and checkpoints instead of hashing gigabytes again, and
``FineTuneSummaryCache`` lets repeated status replies reuse the summaries of unchanged runs.
``discard_unfinished_speech`` removes what an interrupted Speak request left. The synthesizer
keeps F5-TTS's temporary files in a private work directory inside its output directory. A
finished run can also leave the Mac: once the library vault holds a verified copy, ``archived.json``
(``FineTuneRunArchive``) records that, ``local-removal.json`` (``FineTuneRunRemoval``) records the
checkpoints removed from this Mac, and ``finetune_run_location`` is what the app's status, Speak
and the command line read to tell a local run from an archived one (an archived run is refused
until ``voice-lab finetune restore-run`` brings its checkpoints back).
Related: prompt_coverage.py (readers), recording_index.py (retained predicate),
recording_finetune_jobs.py (the service's job queue and worker), docs/FINETUNE.md
(operator guide), DECISIONS.md D-016.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import re
import shutil
import stat
import subprocess
import threading
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Annotated, Final, Literal, NamedTuple, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .audio import CANONICAL_SAMPLE_RATE_HZ
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    fsync_directory,
    open_regular_no_follow,
    rename_no_replace,
    sha256_file,
    sha256_stream,
    write_canonical_json,
    write_new_private_file,
)
from .catalog_ports import CorpusSnapshotSecurityError
from .corpus_store import require_referenced_snapshot
from .errors import SecurityError, VoiceLabError
from .models import (
    SILENT_PROMPT_CATEGORIES,
    ContractModel,
    Prompt,
    PromptSuite,
    Sha256,
    StableId,
    utc_now,
)
from .paths import AssetLayout, confined_path, validate_absolute_path
from .prompt_coverage import coverage_catalog, subject_native_takes
from .recording_contracts import (
    FINETUNE_MAX_EPOCHS,
    MAX_FINISHED_RUNS,
    MAX_RECORDING_FRAMES,
    SPEECH_DEFAULT_NFE_STEPS,
    SPEECH_MAX_NFE_STEPS,
    SPEECH_MIN_NFE_STEPS,
    SPEECH_OUTPUT_ROOT,
    SPEECH_SECONDS_TOLERANCE,
    CatalogReference,
    CatalogSnapshot,
    FineTuneRunSummary,
    FineTuneStage,
    RecordingFrameRange,
    RecordingHistoryEntry,
    RecordingTake,
    RelativePath,
    SpeechText,
    UtcTimestamp,
    speech_directory,
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
from .storage_contracts import LibraryId

FINETUNE_CORPUS_SCHEMA: Final = "cole-voice-lab.finetune-corpus/v1"
FINETUNE_HYPERPARAMETERS_SCHEMA: Final = "cole-voice-lab.finetune-hyperparameters/v1"
FINETUNE_DATASET_SCHEMA: Final = "cole-voice-lab.finetune-dataset/v1"
FINETUNE_RUN_SCHEMA: Final = "cole-voice-lab.finetune-run/v1"
FINETUNE_ARCHIVE_SCHEMA: Final = "cole-voice-lab.finetune-run-archive/v1"
FINETUNE_REMOVAL_SCHEMA: Final = "cole-voice-lab.finetune-run-removal/v1"
# Every run lives in <asset root>/derived/finetune/<run id>/ (the CLI's --output-root default).
FINETUNE_OUTPUT_ROOT: Final = "derived/finetune"
# A run's archive records, beside its run.json. archived.json says the vault holds a verified
# copy; local-removal.json says the large files were removed from this Mac and names the
# generation they can be read back from. A restore renames the removal record to
# local-removal-restored-<n>.json, so the history of removals and restores is kept.
FINETUNE_ARCHIVE_NAME: Final = "archived.json"
FINETUNE_REMOVAL_NAME: Final = "local-removal.json"
FINETUNE_REMOVAL_RESTORED_PREFIX: Final = "local-removal-restored-"
# Only the run's checkpoints are removed: every record a run summary, the run ordering, Speak
# resolution and the dataset need stays on the Mac.
FINETUNE_REMOVABLE_DIRECTORY: Final = "checkpoints"
FINETUNE_FFMPEG: Final = Path("/opt/homebrew/bin/ffmpeg")
# run.json, dataset.json, corpus.json and samples.json are read through this bound.
MAX_FINETUNE_RECORD_BYTES: Final = 64 * 1024 * 1024
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
# JOB_ACTIVE, INTERRUPTED and SYNTHESIS_DEADLINE belong to the service's job path (the CLI
# also refuses JOB_ACTIVE); NO_FINISHED_RUN refuses Speak before any fine-tune finished;
# RUN_ARCHIVED refuses a run whose checkpoints were removed after the vault copy was verified
# (``voice-lab finetune restore-run`` brings them back); a cancelled job carries no error at
# all.
type FineTuneErrorCode = Literal[
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
    "FINETUNE.NO_FINISHED_RUN",
    "FINETUNE.PRETRAINED_MISMATCH",
    "FINETUNE.REFERENCE_MISSING",
    "FINETUNE.REQUIREMENTS_MISSING",
    "FINETUNE.RUNTIME_FAILED",
    "FINETUNE.RUNTIME_UNAVAILABLE",
    "FINETUNE.RUN_ARCHIVED",
    "FINETUNE.RUN_EXISTS",
    "FINETUNE.SOURCE_CHANGED",
    "FINETUNE.SYNTHESIS_DEADLINE",
    "FINETUNE.TRAINING_FAILED",
    "FINETUNE.UNSUPPORTED_TEXT",
]
type AbsolutePath = Annotated[
    str, StringConstraints(min_length=1, max_length=4_096), AfterValidator(validate_absolute_path)
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


class FineTuneArchivedFile(ContractModel):
    """One file of an archived run, as the vault generation that holds it names it.

    ``path`` is relative to the run directory (``checkpoints/model_last.pt``), so an archive
    record survives a restore into another asset root. ``kind`` is ``pin`` for the hard link
    to the runtime's pretrained weights, which the vault records by its pin and never holds.
    """

    path: RelativePath
    sha256: Sha256
    size: int = Field(ge=0)
    kind: Literal["file", "pin"]


class FineTuneRunArchive(ContractModel):
    """``archived.json``: this run has a verified encrypted copy in the library vault.

    Written once, after one push put the run in the vault and a verify downloaded and
    decrypted every chunk of every entry the run owns. ``generation`` and ``manifest_sha256``
    name the generation that was verified and ``files`` every file of the run it holds.
    The record says nothing about what is still on the Mac: ``local-removal.json``
    (``FineTuneRunRemoval``) does.
    """

    schema_version: Literal["cole-voice-lab.finetune-run-archive/v1"] = FINETUNE_ARCHIVE_SCHEMA
    run_id: StableId
    library_id: LibraryId
    destination_sha256: Sha256
    generation: int = Field(ge=1)
    manifest_sha256: Sha256
    archived_at: UtcTimestamp
    files: tuple[FineTuneArchivedFile, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _one_entry_per_file(self) -> FineTuneRunArchive:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("an archive record names each file once")
        return self


class FineTuneRunRemoval(ContractModel):
    """``local-removal.json``: the run's large files were removed from this Mac.

    Written once, before the first unlink, so a crash in the middle leaves the record and
    ``voice-lab finetune restore-run`` can finish the job. ``generation`` and
    ``manifest_sha256`` name the generation whose manifest copy holds the removed files'
    chunks; every later generation carries those entries forward, so the vault keeps naming
    them. ``files`` are the removed files, relative to the run directory, and every one of
    them is under ``checkpoints/``: the record is what the removal unlinks by, so the rule
    that a removal never takes a record the app's status or Speak needs is enforced here,
    where the record is read, and not only where it is written. The record is renamed to
    ``local-removal-restored-<n>.json`` once the files are back.
    """

    schema_version: Literal["cole-voice-lab.finetune-run-removal/v1"] = FINETUNE_REMOVAL_SCHEMA
    run_id: StableId
    generation: int = Field(ge=1)
    manifest_sha256: Sha256
    removed_at: UtcTimestamp
    files: tuple[FineTuneArchivedFile, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _one_entry_per_file(self) -> FineTuneRunRemoval:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("a removal record names each file once")
        prefix = f"{FINETUNE_REMOVABLE_DIRECTORY}/"
        outside = sorted(path for path in paths if not path.startswith(prefix))
        if outside:
            raise ValueError(
                f"a removal record names only {prefix} files, never {', '.join(outside)}"
            )
        return self


FINETUNE_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "finetune-corpus": FineTuneCorpus,
    "finetune-hyperparameters": FineTuneHyperparameters,
    "finetune-dataset": FineTuneDataset,
    "finetune-run": FineTuneRun,
    "finetune-run-archive": FineTuneRunArchive,
    "finetune-run-removal": FineTuneRunRemoval,
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


def require_complete_corpus(corpus: FineTuneCorpus, *, allow_missing: bool) -> None:
    """Refuse an incomplete corpus unless the caller allows training on what is recorded.

    Incomplete means a spoken prompt has no kept take or a record could not be read. The
    message leads with the counts, so a bounded copy of it (a wire error keeps 1,024
    characters) still says what is missing; the full lists follow.
    Params: ``allow_missing`` true accepts any corpus (the app always sends it, because the
    active catalog lists unrecorded standard sentence sets).
    Raises: ``FineTuneError`` FINETUNE.CORPUS_INCOMPLETE.
    """
    if corpus.complete or allow_missing:
        return
    raise FineTuneError(
        "FINETUNE.CORPUS_INCOMPLETE",
        f"the corpus is incomplete: {len(corpus.missing_prompt_ids)} of "
        f"{corpus.spoken_prompt_count} spoken prompts have no kept take and "
        f"{len(corpus.unknown_record_ids)} records could not be read; record the missing "
        "prompts or allow missing prompts (--allow-missing) (missing: "
        + ", ".join(corpus.missing_prompt_ids)
        + "; unknown: "
        + ", ".join(corpus.unknown_record_ids)
        + ")",
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
    ffmpeg: Path = FINETUNE_FFMPEG,
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
# The pinned Vocos vocoder synthesis loads from <runtime>/pretrained/vocos-mel-24khz/ (never
# from the Hugging Face cache). Training loads no vocoder: finetune_cli only does so with
# --log_samples, which finetune_cli_arguments never passes and the launcher refuses.
FINETUNE_VOCODER_REPOSITORY: Final = "charactr/vocos-mel-24khz"
FINETUNE_VOCODER_REVISION: Final = "0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21"
FINETUNE_VOCODER_DIRECTORY: Final = "vocos-mel-24khz"
FINETUNE_VOCODER_CONFIG_FILE: Final = "config.yaml"
FINETUNE_VOCODER_CONFIG_SHA256: Final = (
    "da9033922f969a47f0c160010226919e59f27761fd5066f3828d46de6650b0fc"
)
FINETUNE_VOCODER_CONFIG_SIZE_BYTES: Final = 461
FINETUNE_VOCODER_MODEL_FILE: Final = "pytorch_model.bin"
FINETUNE_VOCODER_MODEL_SHA256: Final = (
    "97ec976ad1fd67a33ab2682d29c0ac7df85234fae875aefcc5fb215681a91b2a"
)
FINETUNE_VOCODER_MODEL_SIZE_BYTES: Final = 54_365_991
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


class FineTunePinnedFile(ContractModel):
    """One pinned runtime file: its exact size and SHA-256."""

    sha256: Sha256
    size_bytes: int = Field(ge=1)


class FineTunePretrainedPins(ContractModel):
    """The pretrained files a runtime must hold, each pinned by size and SHA-256.

    ``weights`` is ``pretrained/F5TTS_v1_Base/model_1250000.safetensors``;
    ``vocoder_config`` and ``vocoder_model`` are ``pretrained/vocos-mel-24khz/config.yaml``
    and ``pytorch_model.bin``. Only tests substitute them (a stub runtime's few-byte files).
    """

    weights: FineTunePinnedFile
    vocoder_config: FineTunePinnedFile
    vocoder_model: FineTunePinnedFile


def finetune_pretrained_pins() -> FineTunePretrainedPins:
    """The shipped pins: F5-TTS v1 Base and the Vocos vocoder at their pinned revisions.

    Read from the module's constants at call time.
    """
    return FineTunePretrainedPins(
        weights=FineTunePinnedFile(
            sha256=FINETUNE_PRETRAINED_SHA256, size_bytes=FINETUNE_PRETRAINED_SIZE_BYTES
        ),
        vocoder_config=FineTunePinnedFile(
            sha256=FINETUNE_VOCODER_CONFIG_SHA256, size_bytes=FINETUNE_VOCODER_CONFIG_SIZE_BYTES
        ),
        vocoder_model=FineTunePinnedFile(
            sha256=FINETUNE_VOCODER_MODEL_SHA256, size_bytes=FINETUNE_VOCODER_MODEL_SIZE_BYTES
        ),
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

    @property
    def vocoder_path(self) -> Path:
        """The pinned Vocos vocoder directory (``config.yaml`` and ``pytorch_model.bin``)."""
        return self.root / "pretrained" / FINETUNE_VOCODER_DIRECTORY


def _runtime_environment() -> dict[str, str]:
    """The F5 runtime's environment: this process's, plus the settings it always needs.

    ``HOME`` comes from the password database when absent (the app's frozen service
    environment has none, and the runtime's libraries cache under it), and
    ``HF_HUB_OFFLINE=1`` keeps Hugging Face from downloading anything (downloads need an
    explicit flag, AGENTS.md).
    """
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1"}
    if "HOME" not in environment:
        environment["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    return environment


def _run_runtime(
    runtime_python: Path, arguments: list[str], *, cwd: Path | None, log: Path | None, what: str
) -> str:
    """Run the runtime's python out of process; stdout is returned or, with ``log``, appended.

    The child stays in this process's process group, so whoever stops this process's group
    (the fine-tune job supervisor) stops the runtime with it.
    """
    command = [str(runtime_python), "-B", *arguments]
    environment = _runtime_environment()
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


class FineTuneFileIdentity(ContractModel):
    """The stat identity a verified digest is bound to; any change means hash again."""

    path: AbsolutePath
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    mtime_ns: int
    ctime_ns: int


class FineTuneVerifiedDigest(ContractModel):
    identity: FineTuneFileIdentity
    sha256: Sha256


class FineTuneRuntimeProbe(ContractModel):
    """The runtime probe's answer, bound to the interpreter and its site-packages directory.

    Installing or removing a package changes the directory, so a changed identity probes
    the runtime again.
    """

    runtime_root: AbsolutePath
    python: FineTuneFileIdentity
    site_packages: FineTuneFileIdentity
    f5_tts_version: str = Field(min_length=1, max_length=64)
    torch_version: str = Field(min_length=1, max_length=64)
    package_path: AbsolutePath


MAX_TRUSTED_DIGESTS: Final = 16


class FineTuneTrustedDigests(ContractModel):
    """File digests and a runtime probe verified by an earlier call, keyed by file identity."""

    files: tuple[FineTuneVerifiedDigest, ...] = Field(default=(), max_length=MAX_TRUSTED_DIGESTS)
    probe: FineTuneRuntimeProbe | None = None

    @model_validator(mode="after")
    def _one_digest_per_file(self) -> FineTuneTrustedDigests:
        paths = [entry.identity.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("trusted digests name each file once")
        return self


def _file_identity(path: Path) -> FineTuneFileIdentity:
    """``path``'s own identity (a link's, not its target's).

    Raises: ``OSError`` (``FileNotFoundError``) when it does not exist.
    """
    metadata = os.stat(path, follow_symlinks=False)
    return FineTuneFileIdentity(
        path=str(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class _ProbeAnswer:
    f5_tts_version: str
    torch_version: str
    package_path: Path


class FineTuneDigestCache:
    """Digests (and the runtime probe) reused while the files they describe are unchanged.

    Built from what an earlier call verified; ``trusted()`` is what this one verified, for
    the next call. A digest is reused only while the file's identity (device, inode, size,
    mtime, ctime) equals the recorded one. Anything else is hashed again, so a changed file
    is always noticed: a pinned file must still match its pin and a checkpoint its run
    record. The recording service keeps one per process so Speak requests do not re-hash
    the 1.35 GB base weights or a 2.7 GB checkpoint.
    """

    def __init__(self, trusted: FineTuneTrustedDigests | None = None) -> None:
        known = FineTuneTrustedDigests() if trusted is None else trusted
        self._trusted = {entry.identity.path: entry for entry in known.files}
        self._trusted_probe = known.probe
        self._verified: dict[str, FineTuneVerifiedDigest] = {}
        self._probe: FineTuneRuntimeProbe | None = None

    def sha256(self, path: Path) -> str:
        """``path``'s SHA-256: the trusted digest while its identity is unchanged, else hashed.

        ``path``'s directories are resolved (the key); its last component is not, so a link
        in its place is refused.
        Raises: ``SecurityError`` when the file is a link or not a regular file; ``OSError``
        when it is missing or unreadable; ``FineTuneError`` FINETUNE.SOURCE_CHANGED when it
        changed while it was being hashed.
        """
        target = path.parent.resolve() / path.name
        key = str(target)
        before = _file_identity(target)
        known = self._verified.get(key) or self._trusted.get(key)
        if known is not None and known.identity == before:
            self._verified[key] = known
            return known.sha256
        digest, _ = sha256_file(target)
        after = _file_identity(target)
        if after != before:
            raise FineTuneError(
                "FINETUNE.SOURCE_CHANGED", f"{target} changed while it was being hashed"
            )
        self._verified[key] = FineTuneVerifiedDigest(identity=after, sha256=digest)
        return digest

    def runtime_probe(
        self, root: Path, python: Path, probe: Callable[[], _ProbeAnswer]
    ) -> _ProbeAnswer:
        """The runtime probe's answer: the trusted one while the interpreter and its
        site-packages directory are unchanged, else ``probe()``'s (then recorded)."""
        for known in (self._probe, self._trusted_probe):
            if known is not None and self._probe_current(known, root, python):
                self._probe = known
                return _ProbeAnswer(
                    known.f5_tts_version, known.torch_version, Path(known.package_path)
                )
        answer = probe()
        self._probe = FineTuneRuntimeProbe(
            runtime_root=str(root),
            python=_file_identity(python.resolve()),
            site_packages=_file_identity(answer.package_path.parent),
            f5_tts_version=answer.f5_tts_version,
            torch_version=answer.torch_version,
            package_path=str(answer.package_path),
        )
        return answer

    @staticmethod
    def _probe_current(known: FineTuneRuntimeProbe, root: Path, python: Path) -> bool:
        """Whether ``known`` still describes ``root``'s python (which exists)."""
        package = Path(known.package_path)
        return (
            known.runtime_root == str(root)
            and known.python == _file_identity(python.resolve())
            and package.is_dir()
            and known.site_packages == _file_identity(package.parent)
        )

    def trusted(self) -> FineTuneTrustedDigests:
        """What this cache verified (the newest ``MAX_TRUSTED_DIGESTS`` files) and its probe."""
        files = tuple(self._verified.values())[-MAX_TRUSTED_DIGESTS:]
        return FineTuneTrustedDigests(files=files, probe=self._probe)


def _require_pinned_file(
    path: Path, pin: FineTunePinnedFile, what: str, digests: FineTuneDigestCache
) -> str:
    """``path``'s digest once its size and SHA-256 equal ``pin``.

    Raises: ``FineTuneError`` FINETUNE.PRETRAINED_MISMATCH naming ``what`` and ``path``,
    also when ``path`` is a link (the file must be the file itself).
    """
    if path.stat().st_size != pin.size_bytes:
        raise FineTuneError(
            "FINETUNE.PRETRAINED_MISMATCH",
            f"{what} at {path} is {path.stat().st_size} bytes, not the pinned {pin.size_bytes}",
        )
    try:
        digest = digests.sha256(path)
    except SecurityError as error:
        raise FineTuneError(
            "FINETUNE.PRETRAINED_MISMATCH",
            f"{what} at {path} must be a regular file, not a link: {error}",
        ) from error
    if digest != pin.sha256:
        raise FineTuneError(
            "FINETUNE.PRETRAINED_MISMATCH", f"{what} at {path} does not match its pinned SHA-256"
        )
    return digest


def _probe_runtime(python: Path) -> _ProbeAnswer:
    """Ask the runtime's python for its F5-TTS and torch versions and the package directory.

    Raises: ``FineTuneError`` FINETUNE.RUNTIME_UNAVAILABLE when the probe cannot run or its
    answer is unusable; FINETUNE.RUNTIME_FAILED when it exits non-zero.
    """
    probe = _run_runtime(python, ["-c", _RUNTIME_PROBE], cwd=None, log=None, what="runtime probe")
    try:
        described = json.loads(probe.strip().splitlines()[-1])
        answer = _ProbeAnswer(
            f5_tts_version=str(described["f5_tts_version"]),
            torch_version=str(described["torch_version"]),
            package_path=Path(str(described["package_path"])),
        )
    except (ValueError, KeyError, IndexError) as error:
        raise FineTuneError(
            "FINETUNE.RUNTIME_UNAVAILABLE", f"runtime probe returned no usable description: {error}"
        ) from error
    # A relative or repr-like package path would make data/ and ckpts/ resolve against the
    # caller's working directory (observed once as a stray "MultiplexedPath(…" tree).
    package_path = answer.package_path
    if not package_path.is_absolute() or not package_path.is_dir():
        raise FineTuneError(
            "FINETUNE.RUNTIME_UNAVAILABLE",
            f"runtime probe named a package path that is not an absolute directory: {package_path}",
        )
    return answer


def describe_finetune_runtime(
    runtime_root: Path,
    *,
    pretrained_pin: FineTunePretrainedPins | None = None,
    digests: FineTuneDigestCache | None = None,
) -> FineTuneRuntime:
    """Bind a runtime directory: its python, installed F5-TTS, and the pinned pretrained files.

    Every pinned file is checked here, before any dataset is written: missing files first,
    then each file's size and digest (the 1.35 GB weights last), so a runtime without the
    vocoder fails in seconds instead of after training.
    Params: ``pretrained_pin`` substitutes the pins (tests only; ``None`` is
    ``finetune_pretrained_pins()``); ``digests`` supplies digests and a probe answer an
    earlier call verified (reused only for unchanged files; ``None`` probes and hashes
    everything) and records what this call verified.
    Raises: ``FineTuneError`` FINETUNE.RUNTIME_UNAVAILABLE when ``<root>/bin/python`` or the
    package is missing, or a Vocos vocoder file is missing (the message names it and the
    fetch command); FINETUNE.PRETRAINED_MISMATCH when the F5-TTS v1 Base weights are missing,
    or any pinned file's size or SHA-256 differs from its pin; FINETUNE.SOURCE_CHANGED when
    a file changed while it was hashed.
    """
    root = Path(runtime_root).expanduser().resolve()
    python = root / "bin" / "python"
    if not python.is_file():
        raise FineTuneError(
            "FINETUNE.RUNTIME_UNAVAILABLE", f"no runtime python at {python}; provision it first"
        )
    cache = FineTuneDigestCache() if digests is None else digests
    answer = cache.runtime_probe(root, python, lambda: _probe_runtime(python))
    pins = finetune_pretrained_pins() if pretrained_pin is None else pretrained_pin
    pretrained = root / "pretrained" / FINETUNE_PRETRAINED_FILE
    vocoder = root / "pretrained" / FINETUNE_VOCODER_DIRECTORY
    if not pretrained.is_file():
        raise FineTuneError(
            "FINETUNE.PRETRAINED_MISMATCH", f"pretrained weights missing at {pretrained}"
        )
    checked = (
        (vocoder / FINETUNE_VOCODER_CONFIG_FILE, pins.vocoder_config, "the Vocos vocoder config"),
        (vocoder / FINETUNE_VOCODER_MODEL_FILE, pins.vocoder_model, "the Vocos vocoder weights"),
    )
    for path, _pin, what in checked:
        if not path.is_file():
            raise FineTuneError(
                "FINETUNE.RUNTIME_UNAVAILABLE",
                f"{what} is missing at {path} ({FINETUNE_VOCODER_REPOSITORY} revision "
                f"{FINETUNE_VOCODER_REVISION}); fetch it with `voice-lab finetune "
                f"fetch-pretrained --runtime-root {root} --allow-downloads`",
            )
    for path, pin, what in checked:
        _require_pinned_file(path, pin, what, cache)
    digest = _require_pinned_file(pretrained, pins.weights, "the F5-TTS v1 Base weights", cache)
    return FineTuneRuntime(
        root=root,
        identity=FineTuneRuntimeIdentity(
            runtime_root=str(root),
            python_sha256=cache.sha256(python.resolve()),
            f5_tts_version=answer.f5_tts_version,
            torch_version=answer.torch_version,
            pretrained_path=str(pretrained),
            pretrained_sha256=digest,
        ),
        package_path=answer.package_path,
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


def _failed_run(
    run: FineTuneRun, checkpoints: Path, root: Path, message: str, finished_at: datetime
) -> FineTuneRun:
    """``run`` as failed with ``message``, listing the checkpoints it left behind."""
    return run.model_copy(
        update={
            "status": "failed",
            "finished_at": finished_at,
            "error": message[:4_000],
            "checkpoints": _checkpoints_in(checkpoints, root),
        }
    )


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
        failed = _failed_run(run, checkpoints, layout.root, message, clock())
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


# The synthesizer's input, written into its output directory (absolute paths and the texts).
SYNTHESIS_JOB_FILE: Final = "job.json"
# The synthesizer's private temporary directory inside its output directory: F5-TTS writes a
# converted copy of the reference clip to the temporary directory and never removes it.
SYNTHESIS_WORK_DIRECTORY: Final = "synthesis-tmp"


@dataclass(frozen=True, slots=True)
class _SynthesizedAudio:
    """One clip the synthesizer wrote, checked: private 24 kHz 16-bit mono PCM."""

    relative_path: str
    sha256: str
    frames: int
    text: str

    @property
    def seconds(self) -> float:
        return self.frames / FINETUNE_SAMPLE_RATE_HZ


def _checked_clip(path: Path) -> tuple[int, str]:
    """Make one synthesized clip private (0600), then return its header frames and SHA-256.

    Raises: ``FineTuneError`` FINETUNE.RUNTIME_FAILED when it is not a regular PCM WAV file,
    is not 24 kHz 16-bit mono (the message names what it is), or is empty or truncated.
    """
    try:
        with open_regular_no_follow(path) as stream:
            os.fchmod(stream.fileno(), 0o600)
            with wave.open(stream, "rb") as handle:
                frames = handle.getnframes()
                rate, channels, width = (
                    handle.getframerate(),
                    handle.getnchannels(),
                    handle.getsampwidth(),
                )
                data = handle.readframes(frames)
            stream.seek(0)
            digest, _ = sha256_stream(stream)
    except (SecurityError, wave.Error, EOFError) as error:
        raise FineTuneError(
            "FINETUNE.RUNTIME_FAILED",
            f"the synthesizer wrote {path.name}, which is not a PCM WAV file: {error}",
        ) from error
    if (rate, channels, width) != (FINETUNE_SAMPLE_RATE_HZ, 1, 2):
        raise FineTuneError(
            "FINETUNE.RUNTIME_FAILED",
            f"the synthesizer wrote {path.name} as {rate} Hz, {channels} channel(s), "
            f"{8 * width}-bit; it must be {FINETUNE_SAMPLE_RATE_HZ} Hz mono 16-bit",
        )
    if frames < 1 or len(data) != frames * channels * width:
        raise FineTuneError(
            "FINETUNE.RUNTIME_FAILED",
            f"the synthesizer wrote {path.name} empty or truncated ({frames} frames declared, "
            f"{len(data) // 2} present)",
        )
    return frames, digest


def _reported_clips(output: str, directory: Path, prefix: str, count: int) -> list[Path]:
    """The clip paths the synthesizer printed, one per text, each exactly where it belongs.

    Raises: ``FineTuneError`` FINETUNE.RUNTIME_FAILED for an unreadable report, a clip it
    did not ask for, or a missing one.
    """
    reported: dict[int, Path] = {}
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
            index, path = item["index"], item["path"]
        except (ValueError, KeyError) as error:
            raise FineTuneError(
                "FINETUNE.RUNTIME_FAILED", f"the synthesizer printed an unreadable report: {error}"
            ) from error
        expected = (
            directory / f"{prefix}-{index:02d}.wav"
            if type(index) is int and 1 <= index <= count
            else None
        )
        if expected is None or index in reported or path != str(expected):
            raise FineTuneError(
                "FINETUNE.RUNTIME_FAILED",
                f"the synthesizer reported clip {index!r} at {path!r}, which it was not asked for",
            )
        reported[index] = expected
    if len(reported) != count:
        raise FineTuneError(
            "FINETUNE.RUNTIME_FAILED", f"synthesizer reported {len(reported)} of {count} samples"
        )
    return [reported[index] for index in range(1, count + 1)]


def _synthesize(
    layout: AssetLayout,
    runtime: FineTuneRuntime,
    *,
    checkpoint: Path,
    reference_audio: Path,
    reference_text: str,
    output_directory: Path,
    texts: tuple[str, ...],
    file_prefix: str,
    use_ema: bool,
    nfe_steps: int,
    device: Literal["mps", "cpu", "cuda"],
    synthesizer: Path | None,
    what: str,
    keep_job: bool,
) -> tuple[_SynthesizedAudio, ...]:
    """Run the synthesizer once for ``texts``; return its checked clips, in order.

    Writes ``job.json`` (absolute paths) into ``output_directory`` and removes it afterwards
    unless ``keep_job``; the clips are ``<file_prefix>-NN.wav`` there, each made 0600 and
    checked from its header. The synthesizer loads the runtime's pinned Vocos vocoder and
    keeps its temporary files (F5-TTS's converted reference copy) in its private work
    directory, ``output_directory/synthesis-tmp``, which it removes before it exits.
    Raises: ``FineTuneError`` FINETUNE.RUNTIME_FAILED (the synthesizer failed, left its work
    directory behind, or wrote or reported something other than the clips asked for),
    FINETUNE.RUNTIME_UNAVAILABLE.
    """
    vocab = runtime.data_directory / _F5_PRETRAINED_VOCAB_DATASET / "vocab.txt"
    if not vocab.is_file():
        vocab = runtime.pretrained_vocab_path
    work_directory = output_directory / SYNTHESIS_WORK_DIRECTORY
    job = {
        "checkpoint_path": str(checkpoint),
        "vocab_path": str(vocab),
        "vocoder_path": str(runtime.vocoder_path),
        "use_ema": use_ema,
        "device": device,
        "nfe_steps": nfe_steps,
        "reference_audio_path": str(reference_audio),
        "reference_text": reference_text,
        "output_directory": str(output_directory),
        "work_directory": str(work_directory),
        "file_prefix": file_prefix,
        "texts": list(texts),
    }
    job_path = output_directory / SYNTHESIS_JOB_FILE
    write_canonical_json(job_path, job)
    try:
        output = _run_runtime(
            runtime.python,
            [str(synthesizer or resource_path("f5_synthesize.py")), str(job_path)],
            cwd=output_directory,
            log=None,
            what=what,
        )
    finally:
        if not keep_job:
            job_path.unlink(missing_ok=True)
    if os.path.lexists(work_directory):
        raise FineTuneError(
            "FINETUNE.RUNTIME_FAILED",
            f"{what} left its work directory {work_directory} behind; it may hold a copy of "
            "the reference clip",
        )
    clips: list[_SynthesizedAudio] = []
    for index, path in enumerate(
        _reported_clips(output, output_directory, file_prefix, len(texts)), start=1
    ):
        frames, digest = _checked_clip(path)
        clips.append(
            _SynthesizedAudio(
                relative_path=path.relative_to(layout.root).as_posix(),
                sha256=digest,
                frames=frames,
                text=texts[index - 1],
            )
        )
    return tuple(clips)


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
    digests: FineTuneDigestCache | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FineTuneSampleSet:
    """Generate ``texts`` with ``checkpoint`` (fine-tuned or the pinned base) into ``output_root``.

    The reference is the dataset's normalized copy of ``reference_prompt_id`` with its exact
    prompt text. The synthesizer loads the runtime's pinned Vocos vocoder from
    ``runtime.vocoder_path`` (the job's ``vocoder_path``), never from a download cache.
    Writes ``sample-NN.wav`` (0600, 24 kHz 16-bit mono, checked from the header), the
    synthesizer's ``job.json`` and ``samples.json``; the synthesizer's temporary work
    directory must be gone when it exits.
    Params: ``digests`` supplies the checkpoint's digest when an earlier call verified the
    unchanged file (``None`` hashes it).
    Raises: ``FineTuneError`` FINETUNE.REFERENCE_MISSING, FINETUNE.SOURCE_CHANGED (the
    reference clip changed), FINETUNE.CHECKPOINT_MISSING, FINETUNE.RUNTIME_FAILED with the
    synthesizer's last output when generation fails, when it left its work directory, or
    with the observed format when a sample is not 24 kHz 16-bit mono.
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
    output_directory = confined_path(layout.root, output_root)
    output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    samples = _synthesize(
        layout,
        runtime,
        checkpoint=checkpoint,
        reference_audio=reference_audio,
        reference_text=clip.text,
        output_directory=output_directory,
        texts=texts,
        file_prefix="sample",
        use_ema=use_ema,
        nfe_steps=nfe_steps,
        device=device,
        synthesizer=synthesizer,
        what=f"sample synthesis for {label}",
        keep_job=True,
    )
    cache = FineTuneDigestCache() if digests is None else digests
    sample_set = FineTuneSampleSet(
        label=label,
        checkpoint_path=str(checkpoint),
        checkpoint_sha256=cache.sha256(checkpoint),
        use_ema=use_ema,
        nfe_steps=nfe_steps,
        device=device,
        reference_prompt_id=reference_prompt_id,
        reference_audio_sha256=clip.sha256,
        samples=tuple(
            FineTuneSample(
                index=index,
                relative_path=sample.relative_path,
                sha256=sample.sha256,
                seconds=sample.seconds,
                text=sample.text,
            )
            for index, sample in enumerate(samples, start=1)
        ),
        created_at=clock(),
    )
    write_canonical_json(output_directory / "samples.json", sample_set)
    return sample_set


FINETUNE_SPEECH_SCHEMA: Final = "cole-voice-lab.finetune-speech/v1"
SPEECH_CLIP_FILE: Final = "speech-01.wav"
SPEECH_RECORD_FILE: Final = "speech.json"


class SpeechClip(ContractModel):
    """One Speak request's result: what spoke, with which reference, and the clip it wrote.

    Written last, as ``derived/speech/<operation_id>/speech.json`` beside the clip
    (``speech-01.wav``, 0600, 24 kHz 16-bit mono). ``model`` is ``base`` (the pinned
    pretrained weights) or the fine-tuned run's id; the reference clip is
    ``reference_prompt_id``'s normalized training copy in ``reference_run_id``'s dataset (the
    model's own run, or the newest finished run for ``base``). Relative paths only. A
    listening aid: never a reference, dataset or voice-release artifact.
    """

    schema_version: Literal["cole-voice-lab.finetune-speech/v1"] = FINETUNE_SPEECH_SCHEMA
    operation_id: StableId
    model: StableId
    reference_run_id: StableId
    reference_prompt_id: StableId
    reference_audio_sha256: Sha256
    checkpoint_sha256: Sha256
    use_ema: bool
    nfe_steps: int = Field(ge=SPEECH_MIN_NFE_STEPS, le=SPEECH_MAX_NFE_STEPS)
    text: SpeechText
    relative_path: RelativePath
    sha256: Sha256
    frames: int = Field(ge=1, le=MAX_RECORDING_FRAMES)
    sample_rate_hz: Literal[24_000] = FINETUNE_SAMPLE_RATE_HZ
    seconds: float = Field(gt=0.0, allow_inf_nan=False)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def _bound_clip(self) -> SpeechClip:
        if self.relative_path != f"{speech_directory(self.operation_id)}/{SPEECH_CLIP_FILE}":
            raise ValueError("a speech clip lies at derived/speech/<operation id>/speech-01.wav")
        if self.model not in {"base", self.reference_run_id}:
            raise ValueError("a fine-tuned run speaks with its own reference")
        if abs(self.seconds - self.frames / self.sample_rate_hz) > SPEECH_SECONDS_TOLERANCE:
            raise ValueError("a clip lasts its frames at its sample rate")
        return self


FINETUNE_SCHEMA_MODELS["finetune-speech"] = SpeechClip


def release_finetune_runtime_link(link: Path, checkpoints: Path) -> bool:
    """Remove the runtime's ``ckpts/<run id>`` symlink when it points at ``checkpoints``.

    ``run_f5_finetune`` creates the link (to ``<run root>/checkpoints``, resolved); a failed
    or cancelled run releases it so the runtime tree keeps no pointer into the library. A
    link that is absent, not a symlink, or points anywhere else belongs to another run and is
    left alone.
    Returns: whether the link was removed.
    Raises: ``OSError`` when the link cannot be read or removed.
    """
    if not link.is_symlink() or Path(os.readlink(link)) != checkpoints.resolve():
        return False
    link.unlink()
    return True


def mark_finetune_run_failed(
    layout: AssetLayout,
    run_root: str,
    error: str,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> FineTuneRun | None:
    """Rewrite ``<run_root>/run.json`` as failed with ``error`` when it still says running.

    A run whose trainer was stopped from outside (cancelled, or its service restarted) never
    reached ``run_f5_finetune``'s own failure record; this writes the same record, listing
    the checkpoints it left behind (hashing them can take seconds for real weights).
    Returns: the rewritten record, the unchanged record when it is not running, or ``None``
    when the run has no ``run.json``.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when ``run.json`` is unreadable or
    invalid; ``SecurityError``/``ValueError`` for an unsafe ``run_root``; ``OSError`` when
    the record cannot be written.
    """
    layout = AssetLayout.validate(layout.root)
    run_directory = confined_path(layout.root, run_root)
    path = run_directory / "run.json"
    if not os.path.lexists(path):
        return None
    run = _read_finetune_record(path, FineTuneRun)
    if run.status != "running":
        return run
    failed = _failed_run(run, run_directory / "checkpoints", layout.root, error, clock())
    write_canonical_json(path, failed)
    return failed


@dataclass(frozen=True, slots=True)
class FineTunePipelineStep:
    """One stage change of ``run_finetune_pipeline``.

    ``runtime_link`` is the runtime's ``ckpts/<run id>`` path once the runtime is described
    (from the ``dataset`` stage on): whoever stops the run releases it with
    ``release_finetune_runtime_link``.
    """

    stage: FineTuneStage
    run_id: str
    runtime_link: Path | None


@dataclass(frozen=True, slots=True)
class FineTunePipelineResult:
    """Everything one pipeline run produced: the corpus, its dataset, the run and the samples."""

    corpus: FineTuneCorpus
    dataset: FineTuneDataset
    run: FineTuneRun
    finetuned: FineTuneSampleSet
    base: FineTuneSampleSet


def run_finetune_pipeline(
    layout: AssetLayout,
    catalog: PromptSuite | CatalogSnapshot,
    subject_id: str,
    runtime_root: Path,
    hyperparameters: FineTuneHyperparameters,
    *,
    run_id: str,
    allow_missing: bool,
    reference: CatalogReference | None = None,
    output_root: str = FINETUNE_OUTPUT_ROOT,
    ffmpeg: Path = FINETUNE_FFMPEG,
    reference_prompt_id: str | None = None,
    sample_texts: tuple[str, ...] = FINETUNE_DEFAULT_SAMPLE_TEXTS,
    use_ema: bool = True,
    nfe_steps: int = 32,
    device: Literal["mps", "cpu"] = "mps",
    pretrained_pin: FineTunePretrainedPins | None = None,
    progress: Callable[[FineTunePipelineStep], None] | None = None,
) -> FineTunePipelineResult:
    """Select → describe the runtime → materialize → train → sample (fine-tuned and base).

    The one fine-tune chain: ``voice-lab finetune run`` and the recording service's job
    worker both call it. Everything is written under ``<output_root>/<run_id>/``: the
    dataset in ``dataset/``, the run in ``run.json``/``train.log``/``checkpoints/``, and the
    listening samples in ``samples/finetuned`` (``model_last.pt``, EMA unless ``use_ema`` is
    false) and ``samples/base`` (the pinned pretrained weights, EMA). ``progress`` hears each
    stage as it starts: ``selecting`` (corpus and runtime), ``dataset``, ``training``,
    ``sampling``. A failed or interrupted training releases the runtime's ``ckpts/<run_id>``
    link before the error propagates; a finished one keeps it.
    Params: ``catalog``/``reference``/``subject_id`` as ``select_finetune_corpus``;
    ``allow_missing`` as ``require_complete_corpus``; ``reference_prompt_id`` names the
    reference clip (default: the dataset's first); ``pretrained_pin`` substitutes the
    pretrained-file pins (tests only; ``None`` is ``finetune_pretrained_pins()``).
    Returns: the corpus, dataset, finished run and both sample sets.
    Raises: every ``FineTuneError`` of the steps it chains (FINETUNE.CORPUS_INCOMPLETE,
    FINETUNE.RUNTIME_UNAVAILABLE, FINETUNE.PRETRAINED_MISMATCH, FINETUNE.EMPTY_CORPUS,
    FINETUNE.TRAINING_FAILED, FINETUNE.RUNTIME_FAILED, ...); ``CorpusSnapshotSecurityError``
    as ``select_finetune_corpus``.
    """

    def report(stage: FineTuneStage, link: Path | None) -> None:
        if progress is not None:
            progress(FineTunePipelineStep(stage=stage, run_id=run_id, runtime_link=link))

    report("selecting", None)
    corpus = select_finetune_corpus(layout, catalog, subject_id, reference=reference)
    require_complete_corpus(corpus, allow_missing=allow_missing)
    # The base samples reuse the weights' digest the runtime check just verified.
    digests = FineTuneDigestCache()
    runtime = describe_finetune_runtime(
        runtime_root, pretrained_pin=pretrained_pin, digests=digests
    )
    link = runtime.checkpoints_directory / run_id
    run_root = f"{output_root}/{run_id}"
    report("dataset", link)
    dataset = materialize_finetune_dataset(layout, corpus, f"{run_root}/dataset", ffmpeg=ffmpeg)
    report("training", link)
    try:
        run = run_f5_finetune(
            layout,
            dataset,
            runtime,
            hyperparameters,
            run_root,
            run_id=run_id,
            subject_id=corpus.subject_id,
            catalog_sha256=corpus.catalog_sha256,
        )
    except BaseException:
        release_finetune_runtime_link(link, layout.root / run_root / "checkpoints")
        raise
    report("sampling", link)
    reference_clip = reference_prompt_id or dataset.clips[0].prompt_id

    def samples(label: str, checkpoint: Path, ema: bool) -> FineTuneSampleSet:
        return synthesize_finetune_samples(
            layout,
            dataset,
            runtime,
            checkpoint,
            f"{run_root}/samples/{label}",
            label=label,
            reference_prompt_id=reference_clip,
            texts=sample_texts,
            use_ema=ema,
            nfe_steps=nfe_steps,
            device=device,
            digests=digests,
        )

    finetuned = samples(
        "finetuned", layout.root / run_root / "checkpoints" / "model_last.pt", use_ema
    )
    base = samples("base", runtime.pretrained_path, True)
    return FineTunePipelineResult(
        corpus=corpus, dataset=dataset, run=run, finetuned=finetuned, base=base
    )


def _read_finetune_record[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    """One fine-tune record (run, dataset, corpus or samples) through a bounded no-follow read.

    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when the file is missing, not a regular
    file, larger than ``MAX_FINETUNE_RECORD_BYTES`` or not a valid ``model``.
    """
    try:
        with open_regular_no_follow(path, nonblocking=True) as stream:
            if os.fstat(stream.fileno()).st_size > MAX_FINETUNE_RECORD_BYTES:
                raise ValueError(f"larger than {MAX_FINETUNE_RECORD_BYTES} bytes")
            payload = stream.read(MAX_FINETUNE_RECORD_BYTES + 1)
        if len(payload) > MAX_FINETUNE_RECORD_BYTES:
            raise ValueError(f"larger than {MAX_FINETUNE_RECORD_BYTES} bytes")
        return model.model_validate_json(payload)
    except (SecurityError, OSError, ValueError) as error:
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED",
            f"fine-tune record {path} is missing, unreadable or invalid: {error}",
        ) from error


def load_finetune_dataset(
    layout: AssetLayout, run_root: str
) -> tuple[FineTuneDataset, FineTuneCorpus]:
    """A run directory's ``dataset/dataset.json`` and the ``dataset/corpus.json`` it binds.

    Needs no ``run.json`` (``voice-lab finetune train`` reads a dataset before its run
    exists).
    Params: ``run_root`` is the asset-root-relative run directory.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when a record is missing, unreadable
    or invalid, or the dataset does not bind the corpus; ``SecurityError``/``ValueError``
    for an unsafe ``run_root``.
    """
    directory = confined_path(layout.root, run_root) / "dataset"
    dataset = _read_finetune_record(directory / "dataset.json", FineTuneDataset)
    corpus = _read_finetune_record(directory / "corpus.json", FineTuneCorpus)
    if canonical_sha256(corpus) != dataset.corpus_sha256:
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED",
            f"fine-tune dataset {run_root}/dataset does not match its corpus record",
        )
    return dataset, corpus


def load_finetune_run_inputs(
    layout: AssetLayout, run_root: str
) -> tuple[FineTuneRun, FineTuneDataset, FineTuneCorpus]:
    """One run's ``run.json`` with the dataset and corpus it was trained on, bound together.

    Params: ``run_root`` is the asset-root-relative run directory
    (``derived/finetune/<run id>``).
    Returns: the run, its dataset and its corpus.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when a record is missing, unreadable
    or invalid, or ``run.json``, ``dataset/dataset.json`` and ``dataset/corpus.json`` do not
    bind each other and ``run_root``; ``SecurityError``/``ValueError`` for an unsafe
    ``run_root``.
    """
    run = _read_finetune_record(confined_path(layout.root, run_root) / "run.json", FineTuneRun)
    dataset, corpus = load_finetune_dataset(layout, run_root)
    if (
        run.run_root != run_root
        or canonical_sha256(dataset) != run.dataset_sha256
        or canonical_sha256(corpus) != run.corpus_sha256
        or dataset.corpus_sha256 != run.corpus_sha256
    ):
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED",
            f"fine-tune run {run_root} does not match its dataset and corpus records",
        )
    return run, dataset, corpus


def speech_reference_candidates(
    dataset: FineTuneDataset, corpus: FineTuneCorpus
) -> tuple[str, ...]:
    """The reference choices Speak offers for a run, in catalog order.

    Returns: the prompt ids of ``corpus`` entries whose prompt is a reference candidate and
    that have a clip in ``dataset``.
    """
    clips = {clip.prompt_id for clip in dataset.clips}
    return tuple(
        entry.prompt_id
        for entry in corpus.entries
        if entry.prompt.reference_candidate and entry.prompt_id in clips
    )


# FineTuneRunSummary bounds each sample and reference list; the default sentences are three.
_MAX_SUMMARY_SAMPLES: Final = 64


class FineTuneRunLocation(NamedTuple):
    """Where a run's checkpoints are, and whether the vault holds a verified copy."""

    location: Literal["local", "archived"]
    in_vault: bool


def finetune_run_location(layout: AssetLayout, run_root: str) -> FineTuneRunLocation:
    """Whether a run's checkpoints are gone, and whether the vault holds a verified copy.

    A run is ``archived`` while ``local-removal.json`` is beside its ``run.json`` **and** a
    file that record names is missing: those are the two halves of being archived, and a run
    whose files are all there can speak whatever its records say. The record travels with the
    run (a whole-library restore writes it back beside the checkpoints it also restores) and a
    crash can leave it written before the first unlink, so the record alone would refuse a run
    that is completely local. Only the file names are looked at, never their bytes, because a
    status reply asks this of every listed run; a record that cannot be read at all is taken
    as ``archived``, so a damaged record never offers Speak a run whose checkpoints are gone.
    ``in_vault`` is whether ``archived.json`` is there.
    Params: ``layout``; ``run_root``, the run's asset-root-relative directory.
    Returns: the location.
    Raises: ``SecurityError`` when ``run_root`` has a symlinked component.
    """
    directory = confined_path(layout.root, run_root)
    in_vault = os.path.lexists(directory / FINETUNE_ARCHIVE_NAME)
    record = directory / FINETUNE_REMOVAL_NAME
    if not os.path.lexists(record):
        return FineTuneRunLocation("local", in_vault)
    try:
        removal = _read_finetune_record(record, FineTuneRunRemoval)
    except (FineTuneError, SecurityError):
        return FineTuneRunLocation("archived", in_vault)
    gone = any(
        not os.path.lexists(_run_record(layout, run_root, file.path)) for file in removal.files
    )
    return FineTuneRunLocation("archived" if gone else "local", in_vault)


def finetune_run_summary(layout: AssetLayout, run_root: str) -> FineTuneRunSummary:
    """The app's view of one run: ``run.json`` plus its dataset, corpus and samples.

    ``prompt_count`` is the dataset's clip count, ``retained_seconds`` the corpus's retained
    audio (neither is in ``run.json``) and ``reference_prompt_ids`` the run's Speak
    reference choices (``speech_reference_candidates``). Sample paths come from
    ``samples/{finetuned,base}/samples.json`` and only for a finished run; a finished run
    whose sampling never completed has none. Every path is asset-root-relative.
    ``location`` and ``in_vault`` come from ``finetune_run_location``.
    Params: ``run_root`` is the run's asset-root-relative directory
    (``derived/finetune/<run id>``).
    Returns: the summary.
    Raises: as ``load_finetune_run_inputs``; ``FineTuneError`` FINETUNE.SOURCE_CHANGED when
    a samples record is unreadable or invalid, or the summary breaks its contract.
    """
    run, dataset, corpus = load_finetune_run_inputs(layout, run_root)
    directory = confined_path(layout.root, run_root)
    location, in_vault = finetune_run_location(layout, run_root)
    sample_paths: dict[str, tuple[str, ...]] = {"finetuned": (), "base": ()}
    if run.status == "finished":
        for label in sample_paths:
            path = directory / "samples" / label / "samples.json"
            if os.path.lexists(path):
                sample_set = _read_finetune_record(path, FineTuneSampleSet)
                sample_paths[label] = tuple(
                    sample.relative_path for sample in sample_set.samples[:_MAX_SUMMARY_SAMPLES]
                )
    try:
        return FineTuneRunSummary(
            run_id=run.run_id,
            run_root=run.run_root,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            epochs=run.hyperparameters.epochs,
            prompt_count=len(dataset.clips),
            retained_seconds=round(corpus.retained_seconds, 3),
            finetuned_sample_paths=sample_paths["finetuned"],
            base_sample_paths=sample_paths["base"],
            reference_prompt_ids=speech_reference_candidates(dataset, corpus)[
                :_MAX_SUMMARY_SAMPLES
            ],
            location=location,
            in_vault=in_vault,
            error=run.error,
        )
    except ValueError as error:
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED", f"fine-tune run {run_root} cannot be summarized: {error}"
        ) from error


_RUN_DIRECTORY_NAME: TypeAdapter[StableId] = TypeAdapter(StableId)


def finished_finetune_run_ids(
    layout: AssetLayout, *, output_root: str = FINETUNE_OUTPUT_ROOT
) -> tuple[str, ...]:
    """Every finished run under ``<output_root>``, newest first: the one run ordering.

    Every real directory whose name is a stable id and that holds a ``run.json`` is a run
    (a ``finetune dataset`` directory without one is not). Newest means the latest
    ``finished_at``; equal times are ordered by run id, the greater first. Only ``run.json``
    files are read, so this is cheap.
    Returns: the run ids.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when any ``run.json`` is unreadable,
    invalid or names another run or directory; ``SecurityError`` when ``output_root`` has a
    symlink component.
    """
    root = confined_path(layout.root, output_root)
    if not root.is_dir():
        return ()
    finished: list[tuple[datetime, str]] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                name = _RUN_DIRECTORY_NAME.validate_python(entry.name)
            except ValueError:
                continue
            path = root / name / "run.json"
            if not os.path.lexists(path):
                continue
            run = _read_finetune_record(path, FineTuneRun)
            if run.run_id != name or run.run_root != f"{output_root}/{name}":
                raise FineTuneError(
                    "FINETUNE.SOURCE_CHANGED",
                    f"fine-tune record {path} names run {run.run_id} at {run.run_root}",
                )
            if run.status != "finished":
                continue
            assert run.finished_at is not None  # FineTuneRun: finished runs are dated
            finished.append((run.finished_at, run.run_id))
    return tuple(run_id for _, run_id in sorted(finished, reverse=True))


# The records a run summary is read from (or, for the two archive records, looked for),
# relative to the run directory. Archiving and removing a run changes none of the first five,
# so without the last two the summary cache would keep reporting a removed run as local. They
# are not enough on their own: a removal resumed from a record an interrupted one left writes
# no record at all, it only unlinks, so the cache asks `finetune_run_location` as well.
_SUMMARY_RECORDS: Final = (
    "run.json",
    "dataset/dataset.json",
    "dataset/corpus.json",
    "samples/finetuned/samples.json",
    "samples/base/samples.json",
    FINETUNE_ARCHIVE_NAME,
    FINETUNE_REMOVAL_NAME,
)
# A file's identity: device, inode, type and mode, size, modification and change times.
type _RecordIdentity = tuple[int, int, int, int, int, int] | None


def _record_identities(directory: Path) -> tuple[_RecordIdentity, ...] | None:
    """The identity of each summary record under ``directory`` (``None`` when absent).

    Returns: ``None`` when a record's identity cannot be read at all (reading the record
    then names the failure).
    """
    identities: list[_RecordIdentity] = []
    for relative in _SUMMARY_RECORDS:
        try:
            found = os.lstat(directory / relative)
        except FileNotFoundError:
            identities.append(None)
            continue
        except OSError:
            return None
        identities.append(
            (
                found.st_dev,
                found.st_ino,
                found.st_mode,
                found.st_size,
                found.st_mtime_ns,
                found.st_ctime_ns,
            )
        )
    return tuple(identities)


class FineTuneSummaryCache:
    """Run summaries, reused while the records they were read from are unchanged.

    A finished run's ``run.json``, ``dataset/dataset.json``, ``dataset/corpus.json`` and
    ``samples/{finetuned,base}/samples.json`` rarely change, but a published catalog's corpus
    record is over a megabyte, so re-reading 32 runs for every status reply takes about a
    second. An entry is used only while every one of those files (or its absence) has the
    identity it had when the summary was read: device, inode, mode, size, modification and
    change time. Records alone would not see a run leave the Mac: a removal resumed from the
    record an interrupted one left writes no record, it only unlinks the checkpoints the
    record already names, so an entry is also used only while the run is still where the
    summary says it is (``finetune_run_location``, two metadata reads for a run that was never
    archived). A summary is kept only when no record changed while it was read, and only
    for the runs of the latest listing. A record that cannot be read is never cached, so a
    damaged record fails every reply until it is repaired. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[tuple[_RecordIdentity, ...], FineTuneRunSummary]] = {}

    def summaries(
        self, layout: AssetLayout, run_roots: tuple[str, ...]
    ) -> tuple[FineTuneRunSummary, ...]:
        """The summaries of ``run_roots``, in order; entries for other runs are dropped.

        Raises: as ``finetune_run_summary`` for a run whose records changed, were never read
        or cannot be identified, and as ``finetune_run_location`` for an unsafe ``run_root``.
        """
        with self._lock:
            known = {root: self._entries[root] for root in run_roots if root in self._entries}
        result: list[FineTuneRunSummary] = []
        fresh: dict[str, tuple[tuple[_RecordIdentity, ...], FineTuneRunSummary]] = {}
        for run_root in run_roots:
            directory = confined_path(layout.root, run_root)
            before = _record_identities(directory)
            entry = known.get(run_root)
            if (
                before is not None
                and entry is not None
                and entry[0] == before
                and finetune_run_location(layout, run_root)
                == (entry[1].location, entry[1].in_vault)
            ):
                summary = entry[1]
            else:
                summary = finetune_run_summary(layout, run_root)
                if before is None or _record_identities(directory) != before:
                    result.append(summary)
                    continue
            fresh[run_root] = (before, summary)
            result.append(summary)
        with self._lock:
            self._entries = fresh
        return tuple(result)


def finished_finetune_runs(
    layout: AssetLayout,
    *,
    limit: int = MAX_FINISHED_RUNS,
    output_root: str = FINETUNE_OUTPUT_ROOT,
    summaries: FineTuneSummaryCache | None = None,
) -> tuple[FineTuneRunSummary, ...]:
    """The newest ``limit`` finished runs, newest first (``finished_finetune_run_ids``).

    The recording service's fine-tune status lists them for the Speak page's model choice.
    Params: ``summaries`` reuses the summaries of runs whose records are unchanged (``None``
    reads every run).
    Returns: their summaries (``finetune_run_summary``).
    Raises: ``ValueError`` when ``limit`` is below 1; as ``finished_finetune_run_ids``, and
    as ``finetune_run_summary`` for each listed run.
    """
    if limit < 1:
        raise ValueError("list at least one finished run")
    run_ids = finished_finetune_run_ids(layout, output_root=output_root)[:limit]
    run_roots = tuple(f"{output_root}/{run_id}" for run_id in run_ids)
    if summaries is not None:
        return summaries.summaries(layout, run_roots)
    return tuple(finetune_run_summary(layout, run_root) for run_root in run_roots)


def latest_finished_finetune_run(
    layout: AssetLayout, *, output_root: str = FINETUNE_OUTPUT_ROOT
) -> FineTuneRunSummary | None:
    """The newest finished run: the one resolver of "the latest model".

    Returns: the first of ``finished_finetune_runs``, or ``None`` when no run has finished.
    Raises: as ``finished_finetune_runs``.
    """
    runs = finished_finetune_runs(layout, limit=1, output_root=output_root)
    return runs[0] if runs else None


# -- archiving a finished run to the library vault ---------------------------------------------


def _run_record(layout: AssetLayout, run_root: str, name: str) -> Path:
    return confined_path(layout.root, f"{run_root}/{name}")


def read_run_archive(layout: AssetLayout, run_root: str) -> FineTuneRunArchive | None:
    """A run's ``archived.json``, or ``None`` when it has none.

    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when the record is unreadable or not a
    valid archive record; ``SecurityError`` for a symlinked component.
    """
    path = _run_record(layout, run_root, FINETUNE_ARCHIVE_NAME)
    return None if not os.path.lexists(path) else _read_finetune_record(path, FineTuneRunArchive)


def read_run_removal(layout: AssetLayout, run_root: str) -> FineTuneRunRemoval | None:
    """A run's ``local-removal.json``, or ``None`` when its files are all local.

    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when the record is unreadable or not a
    valid removal record; ``SecurityError`` for a symlinked component.
    """
    path = _run_record(layout, run_root, FINETUNE_REMOVAL_NAME)
    return None if not os.path.lexists(path) else _read_finetune_record(path, FineTuneRunRemoval)


def require_finished_run(layout: AssetLayout, run_root: str, run_id: str) -> FineTuneRun:
    """The run's ``run.json``, which must name this run and be finished.

    Archiving copies a run that is done: an unfinished run's checkpoints are still being
    written, and ``run.json`` is written finished before the samples are, which is why the
    caller also refuses while the app holds a fine-tune job or a Speak request.
    Returns: the run record.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when the record is missing, invalid or
    names another run; FINETUNE.NO_FINISHED_RUN when the run is not finished.
    """
    run = _read_finetune_record(_run_record(layout, run_root, "run.json"), FineTuneRun)
    if run.run_id != run_id or run.run_root != run_root:
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED",
            f"fine-tune record {run_root}/run.json names run {run.run_id} at {run.run_root}",
        )
    if run.status != "finished":
        raise _no_finished_run(
            f"fine-tune run {run_id} is {run.status}, not finished; only a finished run is archived"
        )
    return run


def removable_run_files(
    files: Sequence[FineTuneArchivedFile],
) -> tuple[FineTuneArchivedFile, ...]:
    """The archived files ``--remove-local-copy`` removes: the run's checkpoints, nothing else.

    Every other file of a run is small and is read by the app on every status reply (the run
    record, the dataset and corpus records, the sample sets) or by Speak (the dataset clips),
    so removing one would turn every status reply into ``latest_run_error``.
    """
    prefix = f"{FINETUNE_REMOVABLE_DIRECTORY}/"
    return tuple(file for file in files if file.path.startswith(prefix))


def write_run_archive(
    layout: AssetLayout, run_root: str, archive: FineTuneRunArchive
) -> tuple[Path, str]:
    """Write ``archived.json`` beside the run's ``run.json``, once (0600).

    Returns: the path and the digest of the bytes written.
    Raises: ``FineTuneError`` FINETUNE.RUN_EXISTS when the run already has an archive record;
    ``OSError`` from the write; ``SecurityError`` for a symlinked component.
    """
    return _write_run_record(layout, run_root, FINETUNE_ARCHIVE_NAME, archive)


def write_run_removal(
    layout: AssetLayout, run_root: str, removal: FineTuneRunRemoval
) -> tuple[Path, str]:
    """Write ``local-removal.json`` beside the run's ``run.json``, once (0600).

    It is written before the first unlink, so a crash in the middle of a removal leaves the
    record and ``voice-lab finetune restore-run`` (or a second removal) can finish the job.
    Returns: the path and the digest of the bytes written.
    Raises: ``FineTuneError`` FINETUNE.RUN_EXISTS when the run already has a removal record;
    ``OSError`` from the write; ``SecurityError`` for a symlinked component.
    """
    return _write_run_record(layout, run_root, FINETUNE_REMOVAL_NAME, removal)


def _write_run_record(
    layout: AssetLayout, run_root: str, name: str, record: BaseModel
) -> tuple[Path, str]:
    path = _run_record(layout, run_root, name)
    data = canonical_json_bytes(record) + b"\n"
    try:
        write_new_private_file(path, data, mode=0o600)
    except FileExistsError as error:
        raise FineTuneError(
            "FINETUNE.RUN_EXISTS", f"{run_root}/{name} already exists; it is never replaced"
        ) from error
    return path, hashlib.sha256(data).hexdigest()


def _recorded_file_state(
    layout: AssetLayout, run_root: str, file: FineTuneArchivedFile
) -> Literal["gone", "recorded", "other-bytes"]:
    """Whether the named file is absent, exactly the record's file, or something else."""
    path = _run_record(layout, run_root, file.path)
    if not os.path.lexists(path):
        return "gone"
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != file.size:
        return "other-bytes"
    return "recorded" if sha256_file(path)[0] == file.sha256 else "other-bytes"


def run_files_are_the_recorded_ones(
    layout: AssetLayout, run_root: str, files: Sequence[FineTuneArchivedFile]
) -> bool:
    """Whether every named file is in the run with exactly the bytes the record names.

    A whole-library restore writes an archived run's files back beside its removal record, and
    a crash can leave the record written before the first unlink; in both the run is complete
    and ``voice-lab finetune restore-run`` has nothing to fetch, only the record to keep as
    history.
    Raises: ``SecurityError`` for a symlinked component; ``OSError`` from the filesystem.
    """
    return all(_recorded_file_state(layout, run_root, file) == "recorded" for file in files)


def remove_run_files(
    layout: AssetLayout, run_root: str, files: Sequence[FineTuneArchivedFile]
) -> tuple[str, ...]:
    """Unlink the named run-relative files, each proved first to be the file the record names.

    Every file is hashed before anything is unlinked and must have exactly the size and digest
    the removal record names. That check is the only thing binding these bytes to the bytes
    the vault was proved to hold: a run retrained (or otherwise rewritten) after its archive
    has newer checkpoints that no generation holds, and unlinking those on an older record's
    word would lose them for good. A file that is already gone is not an error.
    The directories stay: the runtime's ``ckpts/<run id>`` symlink points at
    ``checkpoints/``, and a restore writes the files back into the same place.
    Returns: the paths that were unlinked, in order.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when a named file is not the file the
    record names or cannot be unlinked; ``SecurityError`` for a symlinked component.
    """
    removable: list[FineTuneArchivedFile] = []
    for file in files:
        state = _recorded_file_state(layout, run_root, file)
        if state == "other-bytes":
            raise FineTuneError(
                "FINETUNE.SOURCE_CHANGED",
                f"{run_root}/{file.path} is not the file {run_root}/{FINETUNE_REMOVAL_NAME} "
                "names, so the vault holds other bytes than this Mac does; nothing was removed",
            )
        if state == "recorded":
            removable.append(file)
    removed: list[str] = []
    for file in removable:
        path = _run_record(layout, run_root, file.path)
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise FineTuneError(
                "FINETUNE.SOURCE_CHANGED", f"{run_root}/{file.path} cannot be removed: {error}"
            ) from error
        removed.append(file.path)
    return tuple(removed)


def keep_removal_history(layout: AssetLayout, run_root: str) -> Path:
    """Rename a finished restore's ``local-removal.json`` to the next kept history name.

    Returns: the path the record was renamed to.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED when the record cannot be renamed;
    ``SecurityError`` for a symlinked component.
    """
    directory = confined_path(layout.root, run_root)
    number = 1
    while True:
        target = directory / f"{FINETUNE_REMOVAL_RESTORED_PREFIX}{number}.json"
        try:
            rename_no_replace(directory / FINETUNE_REMOVAL_NAME, target)
        except FileExistsError:
            number += 1
            continue
        except OSError as error:
            raise FineTuneError(
                "FINETUNE.SOURCE_CHANGED",
                f"{run_root}/{FINETUNE_REMOVAL_NAME} cannot be kept as history: {error}",
            ) from error
        fsync_directory(directory)
        return target


@dataclass(frozen=True, slots=True)
class SpeechResolution:
    """What a Speak request resolved to: the model that speaks and its reference clip.

    ``run`` is the fine-tuned run that speaks (``None`` for the base weights);
    ``reference_run`` is the run whose dataset holds ``reference`` (the model's own run, or
    the newest finished run for ``base``).
    """

    run: FineTuneRun | None
    reference_run: FineTuneRun
    dataset: FineTuneDataset
    reference: FineTuneDatasetClip

    @property
    def model(self) -> str:
        """``base`` or the speaking run's id."""
        return "base" if self.run is None else self.run.run_id

    @property
    def run_id(self) -> str | None:
        return None if self.run is None else self.run.run_id


def _no_finished_run(message: str) -> FineTuneError:
    return FineTuneError("FINETUNE.NO_FINISHED_RUN", message)


def archived_run_message(run_id: str) -> str:
    """The one wording every archived-run refusal uses (Speak, the worker and the CLI)."""
    return (
        f"run {run_id} is archived; restore it with `voice-lab finetune restore-run --run {run_id}`"
    )


def _archived_run(run_id: str) -> FineTuneError:
    return FineTuneError("FINETUNE.RUN_ARCHIVED", archived_run_message(run_id))


def resolve_speech_model(
    layout: AssetLayout,
    model: str,
    *,
    reference_prompt_id: str | None = None,
    output_root: str = FINETUNE_OUTPUT_ROOT,
) -> SpeechResolution:
    """Resolve a Speak model choice and its reference clip.

    ``latest`` is the first of ``finished_finetune_run_ids``; ``base`` is the pinned
    pretrained weights with the reference from that same newest finished run (a reference
    needs recorded audio); anything else names a finished run. The reference is
    ``reference_prompt_id``'s clip in the reference run's dataset, else the first
    reference-candidate prompt with a clip there (``speech_reference_candidates``), else the
    run's first clip.
    Returns: the resolution; every record in it was read and bound here.
    Raises: ``FineTuneError`` FINETUNE.NO_FINISHED_RUN when no run has finished (whatever
    was chosen) or the named run is missing or not finished; FINETUNE.RUN_ARCHIVED when the
    run that would speak is archived (``base`` is not, it only borrows the newest run's
    reference clip); FINETUNE.REFERENCE_MISSING when
    ``reference_prompt_id`` has no clip in the reference run's dataset;
    FINETUNE.SOURCE_CHANGED as ``finished_finetune_run_ids`` and
    ``load_finetune_run_inputs``; ``SecurityError`` for a symlinked run directory;
    ``ValueError`` when ``model`` is neither keyword nor a stable id.
    """
    if model in {"latest", "base"}:
        run_ids = finished_finetune_run_ids(layout, output_root=output_root)
        if not run_ids:
            raise _no_finished_run(
                "no fine-tuned model has finished yet; fine-tune once before using Speak"
            )
        run_id = run_ids[0]
    else:
        run_id = _RUN_DIRECTORY_NAME.validate_python(model)
    run_root = f"{output_root}/{run_id}"
    if not os.path.lexists(confined_path(layout.root, run_root) / "run.json"):
        if not finished_finetune_run_ids(layout, output_root=output_root):
            raise _no_finished_run(
                f"no fine-tuned model has finished yet, so {run_id} cannot speak; fine-tune "
                "once before using Speak"
            )
        raise _no_finished_run(f"there is no fine-tune run {run_id} in {output_root}")
    run, dataset, corpus = load_finetune_run_inputs(layout, run_root)
    if run.run_id != run_id:
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED", f"fine-tune record {run_root}/run.json names {run.run_id}"
        )
    if run.status != "finished":
        raise _no_finished_run(f"fine-tune run {run_id} is {run.status}, not finished")
    # ``base`` speaks with the pinned pretrained weights and only borrows this run's dataset
    # clip as its reference, and a removal never takes the dataset; only the run itself
    # cannot speak without its checkpoints.
    if model != "base" and finetune_run_location(layout, run_root).location == "archived":
        raise _archived_run(run_id)
    clips = {clip.prompt_id: clip for clip in dataset.clips}
    if reference_prompt_id is not None:
        reference = clips.get(reference_prompt_id)
        if reference is None:
            raise FineTuneError(
                "FINETUNE.REFERENCE_MISSING",
                f"{reference_prompt_id} has no recording in fine-tune run {run_id}; choose one "
                "of its reference prompts",
            )
    else:
        candidates = speech_reference_candidates(dataset, corpus)
        reference = clips[candidates[0]] if candidates else dataset.clips[0]
    return SpeechResolution(
        run=None if model == "base" else run,
        reference_run=run,
        dataset=dataset,
        reference=reference,
    )


_SPEECH_TEXT: TypeAdapter[SpeechText] = TypeAdapter(SpeechText)


def synthesize_speech(
    layout: AssetLayout,
    resolution: SpeechResolution,
    runtime_root: Path,
    *,
    operation_id: str,
    text: str,
    nfe_steps: int = SPEECH_DEFAULT_NFE_STEPS,
    use_ema: bool = True,
    device: Literal["mps", "cpu", "cuda"] = "mps",
    pretrained_pin: FineTunePretrainedPins | None = None,
    digests: FineTuneDigestCache | None = None,
    synthesizer: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> SpeechClip:
    """Speak ``text`` with ``resolution``'s model into ``derived/speech/<operation_id>/``.

    Creates that directory (0700; it must not exist), checks the runtime
    (``describe_finetune_runtime``), the checkpoint (a fine-tuned run's recorded
    ``model_last.pt``, which must still hash to its record; the pinned weights for
    ``base``) and the reference clip (its dataset digest), runs the synthesizer core once,
    and writes ``speech-01.wav`` (0600, 24 kHz 16-bit mono, frames from its header) and,
    last, ``speech.json``. A failure can leave partial output behind:
    ``discard_speech_output`` removes it (the recording service does after every request
    that did not finish).
    Params: ``digests`` supplies digests an earlier request verified and records this
    request's (``None``: hash everything); ``pretrained_pin`` substitutes the pins (tests
    only).
    Returns: the clip record.
    Raises: ``FineTuneError`` FINETUNE.UNSUPPORTED_TEXT for text Speak refuses,
    FINETUNE.RUN_EXISTS when the operation's directory exists, FINETUNE.CHECKPOINT_MISSING
    when the run records no ``model_last.pt`` or it is gone, FINETUNE.SOURCE_CHANGED when
    the checkpoint or the reference clip no longer matches its record, every
    ``describe_finetune_runtime`` failure, and FINETUNE.RUNTIME_FAILED from the
    synthesizer; ``ValueError`` for an invalid ``operation_id`` or ``nfe_steps``;
    ``SecurityError`` for a symlinked path.
    """
    layout = AssetLayout.validate(layout.root)
    operation_id = _RUN_DIRECTORY_NAME.validate_python(operation_id)
    if not SPEECH_MIN_NFE_STEPS <= nfe_steps <= SPEECH_MAX_NFE_STEPS:
        raise ValueError(
            f"nfe_steps must be {SPEECH_MIN_NFE_STEPS} to {SPEECH_MAX_NFE_STEPS}, not {nfe_steps}"
        )
    try:
        text = _SPEECH_TEXT.validate_python(text)
    except ValidationError as error:
        raise FineTuneError(
            "FINETUNE.UNSUPPORTED_TEXT", f"Speak cannot say this text: {error.errors()[0]['msg']}"
        ) from error
    cache = FineTuneDigestCache() if digests is None else digests
    directory = confined_path(layout.root, speech_directory(operation_id))
    directory.parent.mkdir(mode=0o700, exist_ok=True)  # confined_path refused any link
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise FineTuneError(
            "FINETUNE.RUN_EXISTS",
            f"{speech_directory(operation_id)} already exists; each Speak request needs a new "
            "operation id",
        ) from error
    runtime = describe_finetune_runtime(runtime_root, pretrained_pin=pretrained_pin, digests=cache)
    if resolution.run is None:
        checkpoint = runtime.pretrained_path
        checkpoint_sha256 = cache.sha256(checkpoint)
    else:
        run = resolution.run
        recorded = next((item for item in run.checkpoints if item.kind == "last"), None)
        expected = f"{run.run_root}/checkpoints/model_last.pt"
        if recorded is None or recorded.relative_path != expected:
            raise FineTuneError(
                "FINETUNE.CHECKPOINT_MISSING", f"fine-tune run {run.run_id} records no {expected}"
            )
        checkpoint = confined_path(layout.root, recorded.relative_path)
        if not checkpoint.is_file():
            raise FineTuneError("FINETUNE.CHECKPOINT_MISSING", f"no checkpoint at {checkpoint}")
        checkpoint_sha256 = cache.sha256(checkpoint)
        if checkpoint_sha256 != recorded.sha256:
            raise FineTuneError(
                "FINETUNE.SOURCE_CHANGED",
                f"{recorded.relative_path} changed since fine-tune run {run.run_id} finished",
            )
    reference = resolution.reference
    dataset_root = confined_path(layout.root, resolution.dataset.dataset_root, must_exist=True)
    reference_audio = confined_path(dataset_root, reference.relative_path)
    what = (
        f"the reference clip {reference.prompt_id} of fine-tune run "
        f"{resolution.reference_run.run_id}"
    )
    try:
        reference_sha256 = sha256_file(reference_audio)[0]
    except SecurityError as error:
        raise FineTuneError(
            "FINETUNE.SOURCE_CHANGED", f"{what} is missing or not a regular file: {error}"
        ) from error
    if reference_sha256 != reference.sha256:
        raise FineTuneError("FINETUNE.SOURCE_CHANGED", f"{what} changed since packing")
    (spoken,) = _synthesize(
        layout,
        runtime,
        checkpoint=checkpoint,
        reference_audio=reference_audio,
        reference_text=reference.text,
        output_directory=directory,
        texts=(text,),
        file_prefix="speech",
        use_ema=use_ema,
        nfe_steps=nfe_steps,
        device=device,
        synthesizer=synthesizer,
        what=f"speech synthesis with {resolution.model}",
        keep_job=False,
    )
    clip = SpeechClip(
        operation_id=operation_id,
        model=resolution.model,
        reference_run_id=resolution.reference_run.run_id,
        reference_prompt_id=reference.prompt_id,
        reference_audio_sha256=reference.sha256,
        checkpoint_sha256=checkpoint_sha256,
        use_ema=use_ema,
        nfe_steps=nfe_steps,
        text=text,
        relative_path=spoken.relative_path,
        sha256=spoken.sha256,
        frames=spoken.frames,
        seconds=spoken.seconds,
        created_at=clock(),
    )
    write_canonical_json(directory / SPEECH_RECORD_FILE, clip)
    return clip


def discard_speech_output(layout: AssetLayout, operation_id: str) -> bool:
    """Remove ``derived/speech/<operation_id>/`` and everything in it.

    The recording service calls it for a Speak request that did not finish (failed,
    cancelled or past its deadline), whose directory only that request created.
    Returns: whether a directory was removed.
    Raises: ``SecurityError`` when the path or a directory above it is a link or the path is
    not a directory; ``OSError`` when it cannot be removed; ``ValueError`` for an invalid
    ``operation_id``.
    """
    operation_id = _RUN_DIRECTORY_NAME.validate_python(operation_id)
    directory = confined_path(layout.root, speech_directory(operation_id))
    if not os.path.lexists(directory):
        return False
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise SecurityError(f"{directory} is not a directory")
    shutil.rmtree(directory)
    return True


# What a Speak request can leave in its directory before speech.json: the synthesizer's job,
# the clip, and the canonical writer's temporary files (for job.json and speech.json).
_UNFINISHED_SPEECH_FILES: Final = frozenset({SYNTHESIS_JOB_FILE, SPEECH_CLIP_FILE})
_CANONICAL_TEMPORARY: Final = re.compile(r"\.(?:job|speech)\.json\.tmp-[0-9a-f]{16}")


def _raise(error: OSError) -> None:
    raise error


_FINDER_METADATA: Final = ".DS_Store"


def _unfinished_speech_problem(directory: Path) -> str | None:
    """Why ``directory`` holds more than an unfinished Speak request writes, or ``None``.

    The work directory may hold regular files and real directories (whatever F5-TTS and its
    libraries write as temporary files); everything else must be one of the known files or
    Finder's ``.DS_Store`` (written when the folder is viewed).
    """
    with os.scandir(directory) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        mode = os.lstat(directory / name).st_mode
        if name == SYNTHESIS_WORK_DIRECTORY and stat.S_ISDIR(mode):
            for current, directories, files in os.walk(directory / name, onerror=_raise):
                for member in sorted(directories + files):
                    member_mode = os.lstat(Path(current) / member).st_mode
                    if not (stat.S_ISDIR(member_mode) or stat.S_ISREG(member_mode)):
                        where = (Path(current) / member).relative_to(directory).as_posix()
                        return f"holds {where}, which is neither a regular file nor a directory"
        elif (
            name in _UNFINISHED_SPEECH_FILES
            or name == _FINDER_METADATA
            or (_CANONICAL_TEMPORARY.fullmatch(name))
        ):
            if not stat.S_ISREG(mode):
                return f"holds {name}, which is not a regular file"
        else:
            return f"holds {name}, which Speak does not write"
    return None


def discard_unfinished_speech(layout: AssetLayout) -> tuple[str, ...]:
    """Remove every Speak request directory that a stopped service left without its record.

    A request cut short by a service stop or crash leaves ``derived/speech/<operation id>/``
    without ``speech.json``, possibly holding ``job.json`` (absolute paths and the text), a
    partial ``speech-01.wav`` and the synthesizer's work directory (a copy of the reference
    clip). The recording service calls this when fine-tune supervision starts, before any
    request runs. A directory with ``speech.json`` is a finished request and is never
    touched. Only what an unfinished request writes is ever removed: an entry of
    ``derived/speech`` that is not a directory named like an operation id (a Finder
    ``.DS_Store``, a folder Cole made), and a directory without ``speech.json`` that holds
    anything else (a link, another file), are left in place untouched. Nothing reads
    them: the app lists only the clips of its own session.
    Returns: the operation ids whose directories were removed, sorted.
    Raises: ``SecurityError`` when ``derived/speech`` or a directory above it is a link, or
    ``derived/speech`` is not a directory; ``OSError`` when a directory cannot be read or
    removed.
    """
    root = confined_path(layout.root, SPEECH_OUTPUT_ROOT)
    if not os.path.lexists(root):
        return ()
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise SecurityError(f"{root} is not a directory")
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries)
    unfinished: list[str] = []
    for name in names:
        directory = root / name
        try:
            _RUN_DIRECTORY_NAME.validate_python(name)
        except ValueError:
            continue
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            continue
        if os.path.lexists(directory / SPEECH_RECORD_FILE):
            continue
        if _unfinished_speech_problem(directory) is not None:
            continue
        unfinished.append(name)
    for name in unfinished:
        shutil.rmtree(root / name)
    return tuple(unfinished)


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
    """Every pinned pretrained file, each from its immutable Hugging Face revision.

    The F5-TTS v1 Base weights and vocabulary go to ``pretrained/F5TTS_v1_Base/`` and the
    Vocos vocoder's config and weights to ``pretrained/vocos-mel-24khz/``.
    """
    base = f"{FINETUNE_HUGGINGFACE_BASE_URL}/{FINETUNE_PRETRAINED_REPOSITORY}/resolve"
    vocoder = (
        f"{FINETUNE_HUGGINGFACE_BASE_URL}/{FINETUNE_VOCODER_REPOSITORY}/resolve/"
        f"{FINETUNE_VOCODER_REVISION}"
    )
    pretrained = runtime_root / "pretrained"
    vocoder_directory = pretrained / FINETUNE_VOCODER_DIRECTORY
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
        FineTuneDownload(
            url=f"{vocoder}/{FINETUNE_VOCODER_CONFIG_FILE}",
            destination=vocoder_directory / FINETUNE_VOCODER_CONFIG_FILE,
            sha256=FINETUNE_VOCODER_CONFIG_SHA256,
            size_bytes=FINETUNE_VOCODER_CONFIG_SIZE_BYTES,
        ),
        FineTuneDownload(
            url=f"{vocoder}/{FINETUNE_VOCODER_MODEL_FILE}",
            destination=vocoder_directory / FINETUNE_VOCODER_MODEL_FILE,
            sha256=FINETUNE_VOCODER_MODEL_SHA256,
            size_bytes=FINETUNE_VOCODER_MODEL_SIZE_BYTES,
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

    return cast(IO[bytes], urlopen(Request(url, headers={"User-Agent": "voice-lab"})))


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
    """Ensure every pinned pretrained file (``pretrained_downloads``) exists under ``<root>``.

    The F5-TTS v1 Base weights and vocabulary and the Vocos vocoder's config and weights.
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
