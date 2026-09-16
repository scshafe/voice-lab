"""Strict evidence and process contracts for the native recording experience.

@canonical recording_contracts -- Python owns validation and durable JSON; native
transport is bounded big-endian length-prefixed UTF-8 JSON, never executable input.
Related: models.py, capture.CaptureContinuityEvidence, docs/NATIVE_RECORDING_UX.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cached_property
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from .canonical import canonical_json_bytes, canonical_sha256
from .capture import CaptureContinuityEvidence
from .errors import ContractError
from .models import (
    AudioQcMetrics,
    AudioSpec,
    CaptureDevice,
    ContractModel,
    Prompt,
    PromptSuite,
    PromptSuitePurpose,
    Sha256,
    StableId,
)
from .paths import validate_relative_path

PROTOCOL_VERSION: Literal["cole-voice-lab.recording/v1"] = "cole-voice-lab.recording/v1"
MAX_CONTROL_FRAME_BYTES = 1_048_576
# Room a CatalogEvent reply needs beside its suite: the response envelope and meta, the
# event keys, its operation id and the reference. With every id and revision at its maximum
# that is 1,291 bytes, under a third of the reserve (tests/test_recording_contracts.py).
CATALOG_EVENT_RESERVE_BYTES: Final = 4_096
# The service sends a catalog whole, in one CatalogEvent control frame. A suite whose
# canonical bytes stay within this budget always fits; the corpus store refuses to publish
# a larger version and the service refuses to serve one.
MAX_CATALOG_SUITE_BYTES: Final = MAX_CONTROL_FRAME_BYTES - CATALOG_EVENT_RESERVE_BYTES
MAX_TELEMETRY_FRAME_BYTES = 262_144
MAX_WAVEFORM_BUCKETS = 4096
MAX_HISTORY_PAGE_SIZE = 200
MAX_RECORDING_FRAMES = 28_800_000
FRAME_PREFIX_BYTES = 4
RECORDING_SAMPLE_RATE = 48_000
RECORDING_FRAME_BYTES = 3
MAX_RECORDING_REVISION = 9_223_372_036_854_775_807


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("recording timestamps require an explicit timezone")
    return value.astimezone(UTC)


type UtcTimestamp = Annotated[datetime, AfterValidator(_utc)]
type RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=1024),
    AfterValidator(validate_relative_path),
]
type Revision = Annotated[int, Field(ge=0, le=MAX_RECORDING_REVISION)]
type PositiveRevision = Annotated[int, Field(ge=1, le=MAX_RECORDING_REVISION)]
type FrameCount = Annotated[int, Field(ge=0, le=MAX_RECORDING_FRAMES)]
type TransportPhase = Literal[
    "ready", "starting", "recording", "stopping", "draft", "saving", "error"
]
type DecisionState = Literal["draft", "kept", "rejected", "removed"]
type AnalysisState = Literal["pending", "running", "verified", "needs-review", "invalid", "failed"]
type ExportState = Literal["unprepared", "preparing", "prepared", "accepted-raw", "failed"]
type ErrorCode = Literal[
    "RECORDING.INVALID_MESSAGE",
    "RECORDING.VERSION_MISMATCH",
    "RECORDING.BUILD_MISMATCH",
    "RECORDING.STALE_REVISION",
    "RECORDING.STALE_GENERATION",
    "RECORDING.OWNERSHIP_CONFLICT",
    "RECORDING.INVALID_TRANSITION",
    "RECORDING.DEVICE_UNAVAILABLE",
    "RECORDING.PERMISSION_DENIED",
    "RECORDING.PLAYBACK_ACTIVE",
    "RECORDING.DISK_SPACE",
    "RECORDING.WRITE_FAILED",
    "RECORDING.WAVEFORM_CACHE_UNAVAILABLE",
    "RECORDING.CONTINUITY_FAILED",
    "RECORDING.CAPTURE_OVERFLOW",
    "RECORDING.PARENT_LOST",
    "RECORDING.SOURCE_CHANGED",
    "RECORDING.HISTORY_UNKNOWN",
    "RECORDING.GATE_NOT_READY",
    "RECORDING.ANALYSIS_FAILED",
    "RECORDING.PLAYBACK_FAILED",
    "RECORDING.EDIT_FAILED",
    "RECORDING.PACKAGE_INCOMPLETE",
    "RECORDING.CANCELLED",
    # The corpus-store failures the recording service sends under their own code
    # (recording_service maps corpus_store.CorpusStoreError; SNAPSHOT_MISSING arrives as
    # SNAPSHOT_MISMATCH and every other store failure as RECORDING.GATE_NOT_READY).
    "CORPUS.NO_ACTIVE_CATALOG",
    "CORPUS.SNAPSHOT_MISMATCH",
    "CORPUS.STORE_MISSING",
    # Exactly finetune.FineTuneErrorCode (tests/test_finetune.py holds the two equal), so a
    # fine-tune job can send any FineTuneError under its own code.
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


class RecordingFileIdentity(ContractModel):
    """Prepared authorization identity, including ctime to detect preserved-mtime changes."""

    relative_path: RelativePath
    device: Revision
    inode: Revision
    size_bytes: Revision
    mode: int = Field(ge=0, le=0xFFFFFFFF)
    mtime_ns: int = Field(ge=-MAX_RECORDING_REVISION - 1, le=MAX_RECORDING_REVISION)
    ctime_ns: int = Field(ge=-MAX_RECORDING_REVISION - 1, le=MAX_RECORDING_REVISION)


class RecordingBuildIdentity(ContractModel):
    """Exact executable, source-tree and environment identities; mutable tags are insufficient."""

    build_id: StableId
    source_sha256: Sha256
    executable_sha256: Sha256
    environment_sha256: Sha256


class RecordingError(ContractModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=1024)
    details: tuple[str, ...] = Field(default=(), max_length=16)
    retryable: bool

    @model_validator(mode="after")
    def _bounded_details(self) -> RecordingError:
        if any(not item.strip() or len(item) > 1024 for item in self.details):
            raise ValueError("error details must be nonblank strings of at most 1024 characters")
        return self


class PreparedCaptureGate(ContractModel):
    """Previously verified corpus approval identity, invalidated when its evidence changes."""

    state: Literal["not-required", "ready", "unprepared", "invalid"]
    approval_path: RelativePath | None = None
    approval_sha256: Sha256 | None = None
    verified_at: UtcTimestamp | None = None
    evidence_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _readiness_evidence(self) -> PreparedCaptureGate:
        fields = (
            self.approval_path,
            self.approval_sha256,
            self.verified_at,
            self.evidence_sha256,
        )
        if self.state == "ready" and any(value is None for value in fields):
            raise ValueError("ready corpus gate requires complete verified evidence")
        if self.state == "not-required" and any(value is not None for value in fields):
            raise ValueError("a gate that is not required cannot carry approval evidence")
        if (self.approval_path is None) != (self.approval_sha256 is None):
            raise ValueError("approval path and digest must be supplied together")
        return self


CATALOG_SNAPSHOT_ROOT = "provenance/corpus/snapshots"
CATALOG_SNAPSHOT_VERSION: Literal["cole-voice-lab.catalog-snapshot/v1"] = (
    "cole-voice-lab.catalog-snapshot/v1"
)
MAX_CATALOG_SNAPSHOT_ALIASES = 100_000


def catalog_snapshot_path(catalog_sha256: str) -> str:
    """The one relative path a published catalog's snapshot may occupy under the root."""
    return f"{CATALOG_SNAPSHOT_ROOT}/{catalog_sha256}.json"


def keep_recordings_identity(prompt: Prompt) -> str:
    """What a keep-recordings revision must preserve for earlier takes to keep counting.

    The one statement of the alias rule: a take recorded against an earlier revision counts
    for the current prompt only when both share this identity -- the prompt id, the held-out
    flag (evaluation audio never becomes training audio) and the behaviour (spoken, or the
    exact silent category; silence never covers speech). ``CatalogSnapshot`` applies it in
    memory and the recording index applies it in SQL through this same function.
    Params: ``prompt`` is a recorded or current prompt.
    Returns: a canonical JSON string; equal strings mean the same identity.
    Raises: nothing.
    """
    behaviour = "spoken" if prompt.spoken else prompt.category
    return canonical_json_bytes([prompt.prompt_id, prompt.holdout, behaviour]).decode()


class RecordingPromptNotCountedError(ContractError):
    """A take's recorded prompt counts for no prompt of the open catalog.

    The prompt changed (or left the catalog) without keeping its recordings, so the take is
    history of an earlier text rather than coverage. The audio itself is unchanged, so this
    is not a security failure; the recording service reports it as
    ``RECORDING.STALE_REVISION`` (the displayed history is older than the catalog).
    """

    def __init__(self, take_id: str, prompt_id: str) -> None:
        self.take_id = take_id
        self.prompt_id = prompt_id
        super().__init__(
            f"take {take_id} was recorded against a text of prompt {prompt_id} that this "
            "exact catalog no longer counts; the prompt changed without keeping its recordings"
        )

    def __reduce__(self) -> tuple[type[RecordingPromptNotCountedError], tuple[str, str]]:
        return (type(self), (self.take_id, self.prompt_id))


class CatalogReference(ContractModel):
    """A published catalog version by id, version, exact suite digest and snapshot digest.

    ``catalog_sha256`` is the canonical digest of the suite alone (the digest every take
    and coverage projection keys on); ``snapshot_sha256`` is the sha256 of the exact
    snapshot file bytes, so the alias half of the file is pinned too. ``suite_id`` and
    ``purpose`` repeat what the snapshot's suite declares so contract-level checks (purpose
    against the capture gate) need no file read; the store verifies all of them against the
    snapshot whenever a reference is materialized.
    """

    catalog_id: StableId
    catalog_version: PositiveRevision
    catalog_sha256: Sha256
    snapshot_sha256: Sha256
    suite_id: StableId
    purpose: PromptSuitePurpose
    snapshot_path: RelativePath

    @model_validator(mode="after")
    def _content_addressed_snapshot(self) -> CatalogReference:
        if self.snapshot_path != catalog_snapshot_path(self.catalog_sha256):
            raise ValueError("catalog snapshot path must be the content-addressed one")
        return self


class PromptAlias(ContractModel):
    """One earlier prompt digest that still counts as a recording of the current prompt."""

    prompt_sha256: Sha256
    accepted_prompt_sha256: Sha256

    @model_validator(mode="after")
    def _distinct_digests(self) -> PromptAlias:
        if self.prompt_sha256 == self.accepted_prompt_sha256:
            raise ValueError("a prompt alias cannot accept the current digest")
        return self


class CatalogSnapshot(ContractModel):
    """The immutable file behind one published catalog version.

    ``suite`` is the exact PromptSuite whose canonical digest is ``catalog_sha256`` (the
    file is named after it); ``prompt_aliases`` lists, sorted and unique, every earlier
    digest a keep-recordings revision still accepts, keyed by the current prompt digest.
    """

    schema_version: Literal["cole-voice-lab.catalog-snapshot/v1"] = CATALOG_SNAPSHOT_VERSION
    suite: PromptSuite
    catalog_sha256: Sha256
    prompt_aliases: tuple[PromptAlias, ...] = Field(max_length=MAX_CATALOG_SNAPSHOT_ALIASES)

    @classmethod
    def embedded(cls, suite: PromptSuite) -> CatalogSnapshot:
        """The snapshot a recording-group/v1 implies: its embedded suite and no aliases.

        Params: ``suite`` is the exact catalog a v1 group (or a built-in pin) embeds.
        Returns: a snapshot whose ``catalog_sha256`` is the suite's canonical digest.
        Raises: nothing for a valid ``PromptSuite``; a malformed suite fails when it was built.
        """
        return cls(suite=suite, catalog_sha256=canonical_sha256(suite), prompt_aliases=())

    @model_validator(mode="after")
    def _aliases_bind_to_current_prompts(self) -> CatalogSnapshot:
        if canonical_sha256(self.suite) != self.catalog_sha256:
            raise ValueError("catalog snapshot digest must match the exact suite")
        current = {canonical_sha256(prompt) for prompt in self.suite.prompts}
        keys = [
            (alias.prompt_sha256, alias.accepted_prompt_sha256) for alias in self.prompt_aliases
        ]
        if keys != sorted(set(keys)):
            raise ValueError("prompt aliases must be sorted and unique")
        if len({accepted for _, accepted in keys}) != len(keys):
            raise ValueError("an accepted digest cannot belong to more than one prompt")
        for prompt_sha256, accepted_prompt_sha256 in keys:
            if prompt_sha256 not in current:
                raise ValueError("prompt aliases must belong to prompts of the suite")
            if accepted_prompt_sha256 in current:
                raise ValueError("an accepted digest cannot also be a current prompt digest")
        return self

    @cached_property
    def _prompts_by_digest(self) -> dict[str, Prompt]:
        return {canonical_sha256(prompt): prompt for prompt in self.suite.prompts}

    @cached_property
    def _prompts_by_id(self) -> dict[str, Prompt]:
        return {prompt.prompt_id: prompt for prompt in self.suite.prompts}

    @cached_property
    def _current_digests(self) -> frozenset[str]:
        return frozenset(self._prompts_by_digest)

    @cached_property
    def _accepted_by_current(self) -> dict[str, frozenset[str]]:
        accepted: dict[str, set[str]] = {}
        for alias in self.prompt_aliases:
            accepted.setdefault(alias.prompt_sha256, set()).add(alias.accepted_prompt_sha256)
        return {key: frozenset(value) for key, value in accepted.items()}

    @cached_property
    def _current_by_recorded(self) -> dict[str, str]:
        current = {digest: digest for digest in self._current_digests}
        for alias in self.prompt_aliases:
            current[alias.accepted_prompt_sha256] = alias.prompt_sha256
        return current

    def accepted_hashes(self, prompt_sha256: str) -> frozenset[str]:
        """Every recorded digest that covers the prompt with the given current digest.

        Params: ``prompt_sha256`` is the canonical digest of a prompt in ``suite``.
        Returns: that digest plus every alias it accepts.
        Raises: ``KeyError`` when no prompt of the suite has that digest.
        """
        if prompt_sha256 not in self._current_digests:
            raise KeyError(prompt_sha256)
        return frozenset({prompt_sha256}) | self._accepted_by_current.get(
            prompt_sha256, frozenset()
        )

    def current_for(self, recorded_sha256: str) -> str | None:
        """Map a take's recorded prompt digest to the current prompt digest it counts for.

        Params: ``recorded_sha256`` is the digest the take was recorded against.
        Returns: the current digest (itself when it is current, the owning prompt's when it
        is an accepted alias) or ``None`` when no prompt of this snapshot counts it.
        """
        return self._current_by_recorded.get(recorded_sha256)

    def prompt(self, prompt_id: str) -> Prompt | None:
        """The suite prompt with this id.

        Params: ``prompt_id`` is a prompt id (prompt ids are unique within a suite).
        Returns: that prompt, or ``None`` when this catalog version has no such prompt.
        Raises: nothing.
        """
        return self._prompts_by_id.get(prompt_id)

    def current_prompt_for(self, recorded: Prompt) -> Prompt | None:
        """The current suite prompt that a take recorded against ``recorded`` counts for.

        A recorded prompt counts when its canonical digest is a current digest or an alias
        ``current_for`` accepts, and the current prompt keeps the recorded prompt's
        ``keep_recordings_identity`` (same prompt id, held-out flag and spoken/silent
        behaviour: the store only accepts such revisions, a take's cursor selects its prompt
        by id, and an alias never turns evaluation or silent audio into training speech).
        Params: ``recorded`` is the exact prompt a take bound when it was reserved.
        Returns: the current prompt (``recorded`` itself for an unchanged prompt), or ``None``
        when this catalog version does not count the recording for any prompt.
        Raises: nothing.
        """
        current = self.current_for(canonical_sha256(recorded))
        if current is None:
            return None
        prompt = self._prompts_by_digest[current]
        if keep_recordings_identity(prompt) != keep_recordings_identity(recorded):
            return None
        return prompt

    def require_current_prompt_for(self, recorded: Prompt, *, take_id: str) -> Prompt:
        """``current_prompt_for``, for a reader that must not proceed without a count.

        Params: ``recorded`` is the take's recorded prompt; ``take_id`` names the take.
        Returns: the current prompt the take counts for.
        Raises: ``RecordingPromptNotCountedError`` (a ``ContractError``) when it counts for
        no prompt of this catalog version.
        """
        current = self.current_prompt_for(recorded)
        if current is None:
            raise RecordingPromptNotCountedError(take_id, recorded.prompt_id)
        return current


RECORDING_GROUP_V1: Final = "cole-voice-lab.recording-group/v1"
RECORDING_GROUP_V2: Final = "cole-voice-lab.recording-group/v2"


class RecordingGroup(ContractModel):
    """One capture context: subject, catalog, device, host and corpus gate.

    recording-group/v1 embeds the exact ``catalog`` (every group written before the corpus
    store; readable forever, byte-identical). recording-group/v2 carries ``catalog_reference``
    to a published corpus-store snapshot instead and embeds no catalog; its prompts are read
    through ``corpus_store.resolve_group_catalog``. Containers (capture-reservation/v1,
    recording-take/v1, recording-package-map/v1, responses) keep their own versions and nest
    either form. Absent optional fields are omitted so v1 canonical bytes never change.
    """

    schema_version: Literal[
        "cole-voice-lab.recording-group/v1", "cole-voice-lab.recording-group/v2"
    ] = RECORDING_GROUP_V1
    group_id: StableId
    context_revision: Revision
    subject_id: StableId
    catalog: PromptSuite | None = Field(default=None, exclude_if=lambda value: value is None)
    catalog_reference: CatalogReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    catalog_sha256: Sha256
    capture_device: CaptureDevice
    capture_host: str = Field(min_length=1, max_length=255)
    opened_at: UtcTimestamp
    reason: Literal["app-start", "device-change", "configuration-change", "new-setup"]
    setup_description: str = Field(default="", max_length=1024)
    gate: PreparedCaptureGate
    room_tone_take_ids: tuple[StableId, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _exact_group_context(self) -> RecordingGroup:
        if self.schema_version == RECORDING_GROUP_V1:
            if self.catalog is None or self.catalog_reference is not None:
                raise ValueError(
                    "recording-group/v1 embeds its exact catalog and carries no catalog reference"
                )
            if canonical_sha256(self.catalog) != self.catalog_sha256:
                raise ValueError("group catalog digest must match the exact catalog")
        else:
            if self.catalog_reference is None or self.catalog is not None:
                raise ValueError(
                    "recording-group/v2 carries a catalog reference and embeds no catalog"
                )
            if self.catalog_reference.catalog_sha256 != self.catalog_sha256:
                raise ValueError("group catalog digest must match its catalog reference")
        if self.capture_device.backend != "coreaudio":
            raise ValueError("native recording groups require Core Audio")
        if self.catalog_purpose == "capture-corpus":
            if self.gate.state == "not-required":
                raise ValueError("corpus catalog requires a prepared capture gate")
        elif self.gate.state != "not-required":
            raise ValueError("non-corpus catalog must not reuse corpus gate authorization")
        if len(self.room_tone_take_ids) != len(set(self.room_tone_take_ids)):
            raise ValueError("room-tone relationships must be unique")
        return self

    @property
    def catalog_purpose(self) -> PromptSuitePurpose:
        """The bound catalog's purpose: the embedded suite's (v1) or the reference's (v2)."""
        if self.catalog is not None:
            return self.catalog.purpose
        assert self.catalog_reference is not None  # exactly one form, validated above
        return self.catalog_reference.purpose


class CaptureReservation(ContractModel):
    schema_version: Literal["cole-voice-lab.capture-reservation/v1"] = (
        "cole-voice-lab.capture-reservation/v1"
    )
    take_id: StableId
    operation_id: StableId
    process_generation: StableId
    group: RecordingGroup
    prompt: Prompt
    requested_at: UtcTimestamp
    partial_path: RelativePath
    original_path: RelativePath
    maximum_frames: int = Field(default=MAX_RECORDING_FRAMES, ge=1, le=MAX_RECORDING_FRAMES)

    @model_validator(mode="after")
    def _reservation_binding(self) -> CaptureReservation:
        # A v2 group carries no prompts; reserve checks the prompt against its snapshot.
        catalog = self.group.catalog
        if catalog is not None and self.prompt not in catalog.prompts:
            raise ValueError("reservation prompt must exactly match the bound catalog")
        if self.requested_at < self.group.opened_at:
            raise ValueError("capture request cannot precede its group")
        if self.group.catalog_purpose == "capture-corpus" and self.group.gate.state != "ready":
            raise ValueError("corpus capture requires a ready verified gate")
        if self.partial_path != f"tmp/recording-takes/{self.take_id}/capture.partial.wav":
            raise ValueError("partial path must bind the reserved take")
        if self.original_path != f"tmp/recording-takes/{self.take_id}/original.wav":
            raise ValueError("original path must bind the reserved take")
        return self


class CaptureSealReceipt(ContractModel):
    schema_version: Literal["cole-voice-lab.capture-seal/v1"] = "cole-voice-lab.capture-seal/v1"
    take_id: StableId
    reservation_sha256: Sha256
    process_generation: StableId
    recorder_build: RecordingBuildIdentity
    original_path: RelativePath
    original_sha256: Sha256
    original_size_bytes: int = Field(gt=44)
    audio: AudioSpec
    requested_at: UtcTimestamp
    first_sample_at: UtcTimestamp
    last_sample_at: UtcTimestamp
    sealed_at: UtcTimestamp
    continuity: CaptureContinuityEvidence

    @model_validator(mode="after")
    def _sealed_counts_and_identity(self) -> CaptureSealReceipt:
        if self.original_path != f"tmp/recording-takes/{self.take_id}/original.wav":
            raise ValueError("sealed source path must bind its take")
        if (
            self.audio.sample_rate_hz,
            self.audio.channels,
            self.audio.sample_width_bytes,
            self.audio.encoding,
        ) != (48_000, 1, 3, "pcm_s24le"):
            raise ValueError("native original must be mono 48000 Hz 24-bit PCM")
        if self.audio.frames > MAX_RECORDING_FRAMES:
            raise ValueError("sealed take exceeds the independent maximum duration")
        if self.original_size_bytes != 44 + self.audio.frames * RECORDING_FRAME_BYTES:
            raise ValueError("native WAV size must match its fixed header and exact PCM count")
        if not self.requested_at <= self.first_sample_at <= self.last_sample_at <= self.sealed_at:
            raise ValueError("capture request, samples and sealing times must be ordered")
        if (
            self.continuity.frames != self.audio.frames
            or self.continuity.wav_sha256 != self.original_sha256
            or self.continuity.wav_size_bytes != self.original_size_bytes
            or self.continuity.output_name != "original.wav"
        ):
            raise ValueError("seal and continuity evidence identities must agree")
        return self


class NativeRecoveryReceipt(ContractModel):
    """Untrusted native emergency evidence; successful sealing requires a complete seal receipt.

    Wire bytes use the normal framed JSON contract. Python recovery validates this
    separate schema and writes authoritative canonical evidence only after checks.
    No byte order in native JSON is mistaken for canonical lab serialization.
    """

    schema_version: Literal["cole-voice-lab.native-recovery/v1"] = (
        "cole-voice-lab.native-recovery/v1"
    )
    take_id: StableId
    reservation_sha256: Sha256
    process_generation: StableId
    recorded_at: UtcTimestamp
    partial_path: RelativePath
    received_frames: FrameCount
    written_frames: FrameCount
    state: Literal["partial", "sealed"]
    seal: CaptureSealReceipt | None = None
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _recovery_is_honest(self) -> NativeRecoveryReceipt:
        if self.partial_path != f"tmp/recording-takes/{self.take_id}/capture.partial.wav":
            raise ValueError("recovery path must bind its take")
        if self.written_frames > self.received_frames:
            raise ValueError("written recovery frames cannot exceed received frames")
        if self.state == "sealed":
            if self.seal is None or self.error is not None:
                raise ValueError("sealed recovery requires a seal and no capture failure")
            if (
                self.seal.take_id != self.take_id
                or self.seal.reservation_sha256 != self.reservation_sha256
                or self.seal.process_generation != self.process_generation
                or self.seal.audio.frames != self.written_frames
                or self.received_frames != self.written_frames
            ):
                raise ValueError("recovery identity/counts must match sealed evidence")
        elif self.seal is not None or self.error is None:
            raise ValueError("partial recovery requires an explicit failure and no successful seal")
        return self


class RecordingFrameRange(ContractModel):
    """Source-frame recipe, independent from eventual derivative rendering."""

    source_frames: int = Field(ge=1, le=MAX_RECORDING_FRAMES)
    start_frame: FrameCount
    end_frame: int = Field(ge=1, le=MAX_RECORDING_FRAMES)

    @model_validator(mode="after")
    def _nonempty_range(self) -> RecordingFrameRange:
        if not self.start_frame < self.end_frame <= self.source_frames:
            raise ValueError("frame recipe must retain a nonempty range inside the source")
        return self


class RecordingSelection(ContractModel):
    catalog_sha256: Sha256
    prompt_id: StableId
    take_id: StableId | None
    selection_revision: Revision
    index_revision: Revision


class RecordingCursor(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-cursor/v1"] = (
        "cole-voice-lab.recording-cursor/v1"
    )
    subject_id: StableId
    group_id: StableId
    selection: RecordingSelection
    mode: Literal["selected-prompt", "continue-missing"]
    complete: bool
    operation_id: StableId


class RecordingNavigationResult(ContractModel):
    catalog_sha256: Sha256
    index_revision: Revision
    next_prompt: Prompt | None
    complete: bool
    # Explicit prompt selection only: the latest retained take resolved atomically with its
    # target. Omitted when absent, so earlier navigation evidence keeps its canonical bytes.
    selected_take_id: StableId | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _completion_target(self) -> RecordingNavigationResult:
        if self.complete != (self.next_prompt is None):
            raise ValueError("completed navigation must have no next target")
        if self.selected_take_id is not None and self.next_prompt is None:
            raise ValueError("a selected take requires its resolved prompt target")
        return self


class RecordingSaveOperation(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-save/v1"] = "cole-voice-lab.recording-save/v1"
    operation_id: StableId
    take_id: StableId
    receipt_sha256: Sha256
    selection: RecordingSelection
    decision: Literal["kept"] = "kept"
    decision_revision: PositiveRevision
    edit_revision: Revision
    frame_range: RecordingFrameRange
    advance: bool
    navigation: RecordingNavigationResult | None
    committed_at: UtcTimestamp

    @model_validator(mode="after")
    def _save_is_one_decision(self) -> RecordingSaveOperation:
        if self.selection.take_id != self.take_id:
            raise ValueError("Save must refer to the displayed current draft")
        if self.advance != (self.navigation is not None):
            raise ValueError("Save and Next requires a durable navigation result")
        if self.navigation and self.navigation.catalog_sha256 != self.selection.catalog_sha256:
            raise ValueError("save and navigation must bind the same catalog")
        if self.navigation and self.navigation.selected_take_id is not None:
            raise ValueError("Save and Next publishes its next prompt without a selected take")
        return self


class RecordingAnalysisResult(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-analysis/v1"] = (
        "cole-voice-lab.recording-analysis/v1"
    )
    take_id: StableId
    source_sha256: Sha256
    algorithm_version: StableId
    analyzer_build: RecordingBuildIdentity
    edit_revision: Revision
    context_revision: Revision
    state: AnalysisState
    queued_at: UtcTimestamp
    completed_at: UtcTimestamp | None = None
    metrics: AudioQcMetrics | None = None
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _analysis_state_evidence(self) -> RecordingAnalysisResult:
        finished = self.state not in {"pending", "running"}
        if finished != (self.completed_at is not None):
            raise ValueError("only completed analysis has a completion timestamp")
        if self.completed_at is not None and self.completed_at < self.queued_at:
            raise ValueError("analysis cannot complete before it was queued")
        if self.state in {"verified", "needs-review"}:
            if self.metrics is None or self.error is not None:
                raise ValueError("successful analysis requires metrics and no processing error")
            if (self.state == "verified") != self.metrics.passed:
                raise ValueError("analysis status must agree with metric findings")
        elif self.state in {"invalid", "failed"}:
            if self.error is None:
                raise ValueError("failed/invalid analysis requires a named error")
        elif self.metrics is not None or self.error is not None:
            raise ValueError("pending analysis cannot claim results")
        return self


class RecordingDecisionEvidence(ContractModel):
    operation_id: StableId
    revision: PositiveRevision
    action: Literal["reject", "remove", "restore"]
    previous: DecisionState
    current: DecisionState
    restore_decision: Literal["draft", "kept"] | None
    reason: str | None = Field(default=None, min_length=1, max_length=160)
    committed_at: UtcTimestamp

    @model_validator(mode="after")
    def _explicit_decision(self) -> RecordingDecisionEvidence:
        target = {"reject": "rejected", "remove": "removed"}.get(self.action)
        if self.action == "restore":
            if (
                self.previous not in {"rejected", "removed"}
                or self.current not in {"draft", "kept"}
                or self.restore_decision is not None
                or self.reason is not None
            ):
                raise ValueError("Restore must recover the previous retained decision")
        elif (
            self.current != target
            or self.restore_decision is None
            or self.reason is None
            or not self.reason.strip()
            or any(ord(character) < 32 for character in self.reason)
        ):
            raise ValueError("Reject/remove must retain a recoverable decision and readable reason")
        return self


class RecordingTake(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-take/v1"] = "cole-voice-lab.recording-take/v1"
    reservation: CaptureReservation
    seal: CaptureSealReceipt | None = None
    transport: TransportPhase
    decision: DecisionState
    decision_revision: Revision
    decision_evidence: RecordingDecisionEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    analysis: AnalysisState
    export: ExportState
    edit_revision: Revision
    frame_range: RecordingFrameRange | None = None
    state_revision: Revision

    @model_validator(mode="after")
    def _take_dimensions(self) -> RecordingTake:
        if self.seal is not None:
            if (
                self.seal.take_id != self.reservation.take_id
                or self.seal.process_generation != self.reservation.process_generation
                or self.seal.reservation_sha256 != canonical_sha256(self.reservation)
                or self.seal.requested_at != self.reservation.requested_at
                or self.seal.continuity.device_uid
                != self.reservation.group.capture_device.device_id
                or self.seal.audio.frames > self.reservation.maximum_frames
            ):
                raise ValueError("take seal must match its exact reservation")
        if self.transport in {"draft", "saving"} and self.seal is None:
            raise ValueError("draft review and Save require a successful seal")
        if self.decision == "kept" and (self.seal is None or self.decision_revision == 0):
            raise ValueError("kept takes require a seal and durable decision revision")
        if self.decision_evidence is not None and (
            self.decision_evidence.current != self.decision
            or self.decision_evidence.revision != self.decision_revision
        ):
            raise ValueError("take decision revision must bind its operator evidence")
        if (
            self.decision == "draft"
            and self.decision_revision != 0
            and (self.decision_evidence is None or self.decision_evidence.action != "restore")
        ):
            raise ValueError("only an explicit Restore can return a revised take to Draft")
        if self.frame_range is not None and (
            self.seal is None or self.frame_range.source_frames != self.seal.audio.frames
        ):
            raise ValueError("take edit recipe must match its sealed source frame count")
        if self.export in {"prepared", "accepted-raw"} and (
            self.decision != "kept" or self.analysis != "verified"
        ):
            raise ValueError("prepared/raw eligibility requires independently verified kept takes")
        return self

    @property
    def satisfies_coverage(self) -> bool:
        return self.decision == "kept" and self.analysis != "invalid"


class RecordingPackageEntry(ContractModel):
    take_id: StableId
    group_id: StableId
    subject_id: StableId
    catalog_sha256: Sha256
    prompt: Prompt
    package_relative_path: RelativePath
    original_sha256: Sha256
    recorded_at: UtcTimestamp
    edit_identity_sha256: Sha256
    decision_revision: Revision | None = Field(default=None, exclude_if=lambda value: value is None)
    edit_revision: Revision | None = Field(default=None, exclude_if=lambda value: value is None)
    frame_range: RecordingFrameRange | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    analysis_sha256: Sha256 | None = Field(default=None, exclude_if=lambda value: value is None)


class RecordingPackageMap(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-package-map/v1"] = (
        "cole-voice-lab.recording-package-map/v1"
    )
    session_id: StableId
    manifest_sha256: Sha256
    package_path: RelativePath
    group: RecordingGroup
    selected_suite: PromptSuite | None = Field(default=None, exclude_if=lambda value: value is None)
    entries: tuple[RecordingPackageEntry, ...] = Field(min_length=1, max_length=10_000)
    registered_at: UtcTimestamp

    @model_validator(mode="after")
    def _package_context(self) -> RecordingPackageMap:
        ids = [entry.take_id for entry in self.entries]
        paths = [entry.package_relative_path for entry in self.entries]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("package take identities and member paths must be unique")
        catalog = self.group.catalog
        if any(
            entry.group_id != self.group.group_id
            or entry.subject_id != self.group.subject_id
            or entry.catalog_sha256 != self.group.catalog_sha256
            or (catalog is not None and entry.prompt not in catalog.prompts)
            for entry in self.entries
        ):
            raise ValueError("package entries must share exact group and catalog context")
        if catalog is None and self.selected_suite is None:
            # A v2 group embeds no prompts, so its map must carry the exact selected suite.
            raise ValueError("a package of a catalog-reference group requires its selected suite")
        if self.selected_suite is not None:
            entry_prompts = tuple(entry.prompt for entry in self.entries)
            if catalog is None:
                # The snapshot subset (content and order) is checked where the map is built.
                expected = entry_prompts
            else:
                selected_ids = {prompt.prompt_id for prompt in entry_prompts}
                expected = tuple(
                    prompt for prompt in catalog.prompts if prompt.prompt_id in selected_ids
                )
            if (
                self.selected_suite.purpose != self.group.catalog_purpose
                or self.selected_suite.prompts != expected
                or entry_prompts != expected
            ):
                raise ValueError(
                    "package selected suite must preserve exact catalog purpose/content/order"
                )
        tones = {entry.take_id for entry in self.entries if entry.prompt.category == "room-tone"}
        if not tones or not tones.issubset(self.group.room_tone_take_ids):
            raise ValueError("package room tone requires explicit group relationships")
        return self


class RecordingMessageMeta(ContractModel):
    protocol_version: Literal["cole-voice-lab.recording/v1"] = PROTOCOL_VERSION
    request_id: StableId
    process_generation: StableId
    state_revision: Revision
    event_sequence: Revision
    take_id: StableId | None = None


class HandshakeCommand(ContractModel):
    command: Literal["handshake"] = "handshake"
    role: Literal["ui", "recorder", "service"]
    build: RecordingBuildIdentity
    expected_peer_build: RecordingBuildIdentity


class PrepareRecorderCommand(ContractModel):
    command: Literal["prepare"] = "prepare"
    device: CaptureDevice


class StartRecorderCommand(ContractModel):
    command: Literal["start"] = "start"
    reservation: CaptureReservation
    reservation_sha256: Sha256

    @model_validator(mode="after")
    def _canonical_reservation_binding(self) -> StartRecorderCommand:
        if canonical_sha256(self.reservation) != self.reservation_sha256:
            raise ValueError("Start reservation digest must match the exact canonical reservation")
        return self


class StopRecorderCommand(ContractModel):
    command: Literal["stop"] = "stop"
    take_id: StableId
    operation_id: StableId


class SnapshotCommand(ContractModel):
    command: Literal["snapshot"] = "snapshot"


class ShutdownCommand(ContractModel):
    command: Literal["shutdown"] = "shutdown"


class CatalogCommand(ContractModel):
    """Ask the service for the catalog it pinned for this process (the app's first request).

    The service resolves the subject's active corpus-store catalog once per process and
    answers with a ``CatalogEvent``; a later activation takes effect at the next launch.
    """

    command: Literal["catalog"] = "catalog"
    operation_id: StableId


class OpenContextCommand(ContractModel):
    """Open (or reuse) the native capture context of one subject on one catalog.

    The app names the catalog its ``CatalogEvent`` delivered by ``catalog_reference``; the
    service opens only the catalog it pinned for this process. The embedded form
    (``catalog`` plus ``catalog_sha256``) is legacy continuation only: it survives inside
    ``ContinueLegacyRecordingCommand``, which binds a desktop session's frozen catalog that no
    corpus-store version holds, and the service refuses it as a top-level command. Exactly
    one form is present; absent fields are omitted, so an embedded command serializes (and
    replays) byte-identically to the contract before the corpus store.
    """

    command: Literal["open-context"] = "open-context"
    operation_id: StableId
    subject_id: StableId
    catalog_reference: CatalogReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    catalog: PromptSuite | None = Field(default=None, exclude_if=lambda value: value is None)
    catalog_sha256: Sha256 | None = Field(default=None, exclude_if=lambda value: value is None)
    capture_device: CaptureDevice
    capture_host: str = Field(min_length=1, max_length=255)
    expected_selection_revision: Revision | None = None
    new_setup: bool = False
    setup_description: str = Field(default="", max_length=1024)
    pilot_approval_path: RelativePath | None = None
    pilot_approval_sha256: Sha256 | None = None

    @property
    def catalog_purpose(self) -> PromptSuitePurpose:
        """The purpose of the named catalog: the reference's, or the embedded suite's.

        Returns: that purpose.
        Raises: ``ContractError`` (uncoded) for an unvalidated command that names no catalog;
        validation guarantees one form, so a validated command never raises.
        """
        if self.catalog_reference is not None:
            return self.catalog_reference.purpose
        if self.catalog is None:
            raise ContractError("open-context names no catalog")
        return self.catalog.purpose

    @model_validator(mode="after")
    def _bound_context(self) -> OpenContextCommand:
        if self.catalog_reference is not None:
            if self.catalog is not None or self.catalog_sha256 is not None:
                raise ValueError("context names its catalog by reference or embeds it, not both")
        elif self.catalog is None or self.catalog_sha256 is None:
            raise ValueError(
                "context requires a catalog reference (legacy continuation embeds its catalog "
                "and digest)"
            )
        elif canonical_sha256(self.catalog) != self.catalog_sha256:
            raise ValueError("context must bind its exact canonical catalog")
        if self.capture_device.backend != "coreaudio":
            raise ValueError("native context requires Core Audio")
        if (self.pilot_approval_path is None) != (self.pilot_approval_sha256 is None):
            raise ValueError("pilot approval path and digest must be paired")
        if self.pilot_approval_path is not None and self.catalog_purpose != "capture-corpus":
            raise ValueError("pilot approval applies only to the corpus catalog")
        return self


class ReserveTakeCommand(ContractModel):
    command: Literal["reserve"] = "reserve"
    recorder_generation: StableId
    operation_id: StableId
    group_id: StableId
    selection: RecordingSelection


class RegisterSealCommand(ContractModel):
    command: Literal["register-seal"] = "register-seal"
    operation_id: StableId
    receipt: CaptureSealReceipt


class SaveTakeCommand(ContractModel):
    command: Literal["save"] = "save"
    operation_id: StableId
    selection: RecordingSelection
    expected_decision_revision: Revision
    expected_edit_revision: Revision
    frame_range: RecordingFrameRange
    advance: bool
    waveform_ready: Literal[True]


class HistoryCommand(ContractModel):
    command: Literal["history"] = "history"
    catalog_sha256: Sha256
    prompt_id: StableId | None = None
    cursor: StableId | None = None
    prompt_cursor: StableId | None = None
    group_cursor: StableId | None = None
    page_size: int = Field(default=50, ge=1, le=MAX_HISTORY_PAGE_SIZE)


class NavigateCommand(ContractModel):
    command: Literal["navigate"] = "navigate"
    operation_id: StableId
    selection: RecordingSelection
    mode: Literal["selected-prompt", "continue-missing"]
    target_prompt_id: StableId | None = None

    @model_validator(mode="after")
    def _selected_target(self) -> NavigateCommand:
        if (self.mode == "selected-prompt") != (self.target_prompt_id is not None):
            raise ValueError("only selected-prompt navigation requires an explicit target")
        return self


class ReviewTakeCommand(ContractModel):
    command: Literal["review-take"] = "review-take"
    operation_id: StableId
    take_id: StableId
    selection: RecordingSelection


class RetakeCommand(ContractModel):
    command: Literal["retake"] = "retake"
    operation_id: StableId
    selection: RecordingSelection


class EditTakeCommand(ContractModel):
    command: Literal["edit"] = "edit"
    selection: RecordingSelection
    operation_id: StableId
    take_id: StableId
    source_sha256: Sha256
    expected_edit_revision: Revision
    frame_range: RecordingFrameRange


class DecisionCommand(ContractModel):
    command: Literal["decision"] = "decision"
    selection: RecordingSelection
    operation_id: StableId
    take_id: StableId
    expected_decision_revision: Revision
    action: Literal["reject", "remove", "restore"]
    reason: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def _reason_binding(self) -> DecisionCommand:
        if (self.action in {"reject", "remove"}) != (self.reason is not None):
            raise ValueError("reject/remove require a reason; restore does not")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("decision reason must not be blank")
        return self


class RetryAnalysisCommand(ContractModel):
    command: Literal["retry-analysis"] = "retry-analysis"
    operation_id: StableId
    take_id: StableId
    source_sha256: Sha256


# An explicit package selection for recording_packages (plans, commits, the package queue).
# Not a service command: the app no longer prepares packages (corpus-first pipeline plan).
class PreparePackageCommand(ContractModel):
    command: Literal["prepare-package"] = "prepare-package"
    operation_id: StableId
    group_id: StableId
    take_ids: tuple[StableId, ...] = Field(min_length=1, max_length=10_000)
    room_tone_take_ids: tuple[StableId, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _unique_explicit_selection(self) -> PreparePackageCommand:
        for ids in (self.take_ids, self.room_tone_take_ids):
            if len(ids) != len(set(ids)):
                raise ValueError("package selection IDs must be unique")
        if not set(self.room_tone_take_ids).issubset(self.take_ids):
            raise ValueError("explicit room tone must belong to the selected takes")
        return self


class ContinueLegacyRecordingCommand(ContractModel):
    """Continue an unfinished desktop session in a new native context.

    ``context`` always embeds the session's frozen catalog and digest (the one place the
    embedded open-context form remains): a desktop session binds a catalog no corpus-store
    version holds, and its sidecar evidence must match that catalog exactly.
    """

    command: Literal["continue-legacy-recording"] = "continue-legacy-recording"
    operation_id: StableId
    session_id: StableId
    session_evidence_sha256: Sha256
    context: OpenContextCommand

    @property
    def legacy_catalog(self) -> PromptSuite:
        """The session's frozen catalog, embedded in ``context``.

        Returns: the embedded suite.
        Raises: ``ContractError`` for an unvalidated command whose context embeds none.
        """
        if self.context.catalog is None:
            raise ContractError("legacy continuation requires the session's embedded catalog")
        return self.context.catalog

    @model_validator(mode="after")
    def _explicit_new_native_context(self) -> ContinueLegacyRecordingCommand:
        if self.context.operation_id != self.operation_id or not self.context.new_setup:
            raise ValueError("legacy continuation requires its own explicit native setup operation")
        if self.context.catalog_reference is not None:
            raise ValueError(
                "legacy continuation binds the session's embedded catalog, not a catalog reference"
            )
        return self


class RecoverCommand(ContractModel):
    command: Literal["recover"] = "recover"
    operation_id: StableId
    take_id: StableId | None = None
    after_take_id: StableId | None = None
    page_size: int = Field(default=50, ge=1, le=50)

    @model_validator(mode="after")
    def _single_or_page(self) -> RecoverCommand:
        if self.take_id is not None and self.after_take_id is not None:
            raise ValueError("single-take recovery cannot include a page cursor")
        return self


class RecoverySnapshotCommand(ContractModel):
    """Read a bounded recovery inventory page without performing recovery."""

    command: Literal["recovery-snapshot"] = "recovery-snapshot"
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    page_size: int = Field(default=50, ge=1, le=50)


class CancelCommand(ContractModel):
    command: Literal["cancel"] = "cancel"
    operation_id: StableId
    cancelled_operation_id: StableId


FINETUNE_MAX_EPOCHS: Final = 10_000


class FineTuneCommand(ContractModel):
    """Train the voice model on the latest kept take of every spoken prompt (one job at a time).

    ``epochs`` overrides the default training length. ``allow_missing`` trains on the
    prompts that have a kept take even when others have none; without it the job is refused
    ``FINETUNE.CORPUS_INCOMPLETE`` unless every spoken prompt has one. The app always sends
    ``true`` because the active catalog lists unrecorded standard sentence sets.
    """

    command: Literal["fine-tune"] = "fine-tune"
    operation_id: StableId
    epochs: int | None = Field(default=None, ge=1, le=FINETUNE_MAX_EPOCHS)
    allow_missing: bool


class CancelFineTuneCommand(ContractModel):
    """Stop the fine-tune job ``cancelled_operation_id`` (a durable job, unlike ``cancel``)."""

    command: Literal["cancel-fine-tune"] = "cancel-fine-tune"
    operation_id: StableId
    cancelled_operation_id: StableId


class FineTuneStatusCommand(ContractModel):
    """Ask whether fine-tuning is available, which job is active and which run is latest."""

    command: Literal["fine-tune-status"] = "fine-tune-status"
    operation_id: StableId


type RecorderCommand = Annotated[
    HandshakeCommand
    | PrepareRecorderCommand
    | StartRecorderCommand
    | StopRecorderCommand
    | SnapshotCommand
    | ShutdownCommand,
    Field(discriminator="command"),
]
type ServiceCommand = Annotated[
    HandshakeCommand
    | CatalogCommand
    | OpenContextCommand
    | ReserveTakeCommand
    | RegisterSealCommand
    | SaveTakeCommand
    | HistoryCommand
    | NavigateCommand
    | EditTakeCommand
    | ReviewTakeCommand
    | RetakeCommand
    | DecisionCommand
    | RetryAnalysisCommand
    | RecoverCommand
    | RecoverySnapshotCommand
    | ContinueLegacyRecordingCommand
    | CancelCommand
    | FineTuneCommand
    | CancelFineTuneCommand
    | FineTuneStatusCommand
    | SnapshotCommand
    | ShutdownCommand,
    Field(discriminator="command"),
]


def _command_take_id(command: RecorderCommand | ServiceCommand) -> str | None:
    if isinstance(command, StartRecorderCommand):
        return command.reservation.take_id
    if isinstance(command, RegisterSealCommand):
        return command.receipt.take_id
    if isinstance(command, (ReserveTakeCommand, SaveTakeCommand, NavigateCommand, RetakeCommand)):
        return command.selection.take_id
    if isinstance(
        command,
        (
            StopRecorderCommand,
            EditTakeCommand,
            ReviewTakeCommand,
            DecisionCommand,
            RetryAnalysisCommand,
            RecoverCommand,
        ),
    ):
        return command.take_id
    return None


class RecorderRequest(ContractModel):
    data: RecorderCommand
    error: None = None
    meta: RecordingMessageMeta

    @model_validator(mode="after")
    def _request_identity(self) -> RecorderRequest:
        if self.meta.take_id != _command_take_id(self.data):
            raise ValueError("request metadata take ID must match its command")
        if isinstance(self.data, StartRecorderCommand):
            if self.data.reservation.process_generation != self.meta.process_generation:
                raise ValueError("Start generation must match its reservation")
        return self


class ServiceRequest(ContractModel):
    data: ServiceCommand
    error: None = None
    meta: RecordingMessageMeta

    @model_validator(mode="after")
    def _request_identity(self) -> ServiceRequest:
        if self.meta.take_id != _command_take_id(self.data):
            raise ValueError("request metadata take ID must match its command")
        if (
            isinstance(self.data, (EditTakeCommand, DecisionCommand))
            and self.data.selection.take_id != self.data.take_id
        ):
            raise ValueError("edit and decision commands must identify the displayed take")
        return self


class HandshakeEvent(ContractModel):
    event: Literal["handshake"] = "handshake"
    role: Literal["recorder", "service"]
    build: RecordingBuildIdentity


class RecorderStateEvent(ContractModel):
    event: Literal["recorder-state"] = "recorder-state"
    transport: TransportPhase
    take_id: StableId | None
    written_frames: FrameCount
    seal: CaptureSealReceipt | None = None

    @model_validator(mode="after")
    def _state_counts(self) -> RecorderStateEvent:
        if (
            self.transport in {"starting", "recording", "stopping", "draft"}
            and self.take_id is None
        ):
            raise ValueError("active capture state requires its take ID")
        if self.take_id is None and self.written_frames:
            raise ValueError("frames require an identified take")
        if self.seal and (
            self.take_id != self.seal.take_id or self.written_frames != self.seal.audio.frames
        ):
            raise ValueError("state must match its complete seal count and take")
        if self.transport == "draft" and self.seal is None:
            raise ValueError("draft state requires a seal")
        return self


class RecordingTransition(ContractModel):
    """Reliable legal transport edge; repeated commands return the existing result."""

    event: Literal["transition"] = "transition"
    take_id: StableId
    previous: TransportPhase
    current: TransportPhase

    @model_validator(mode="after")
    def _legal_transition(self) -> RecordingTransition:
        edges: dict[str, set[str]] = {
            "ready": {"starting", "error"},
            "starting": {"recording", "stopping", "error"},
            "recording": {"stopping", "error"},
            "stopping": {"draft", "ready", "error"},
            "draft": {"saving", "ready", "error"},
            "saving": {"ready", "draft", "error"},
            "error": {"ready", "draft"},
        }
        if self.current not in edges[self.previous]:
            raise ValueError("malformed recording transport transition")
        return self


class ReservationEvent(ContractModel):
    cursor: RecordingCursor | None = None
    event: Literal["reserved"] = "reserved"
    take: RecordingTake
    reservation_sha256: Sha256

    @model_validator(mode="after")
    def _reservation_identity(self) -> ReservationEvent:
        if (
            self.cursor is not None
            and self.cursor.selection.take_id != self.take.reservation.take_id
        ):
            raise ValueError("reserved cursor must identify the returned take")
        if self.take.seal is not None or self.take.decision != "draft":
            raise ValueError("reserved event must publish an unsealed draft")
        if canonical_sha256(self.take.reservation) != self.reservation_sha256:
            raise ValueError("reserved event digest must bind the canonical reservation")
        return self


class TakeEvent(ContractModel):
    event: Literal["take"] = "take"
    take: RecordingTake


class SaveEvent(ContractModel):
    cursor: RecordingCursor | None = None
    event: Literal["saved"] = "saved"
    operation: RecordingSaveOperation
    take: RecordingTake

    @model_validator(mode="after")
    def _saved_take_agrees(self) -> SaveEvent:
        if (
            self.take.reservation.take_id != self.operation.take_id
            or self.take.decision != "kept"
            or self.take.decision_revision != self.operation.decision_revision
        ):
            raise ValueError("saved event must publish its committed take and decision")
        if self.cursor is not None:
            if (
                self.cursor.operation_id != self.operation.operation_id
                or self.cursor.selection.catalog_sha256 != self.operation.selection.catalog_sha256
            ):
                raise ValueError("saved cursor must bind its operation and catalog")
            navigation = self.operation.navigation
            if navigation is not None and navigation.next_prompt is not None:
                if (
                    self.cursor.selection.prompt_id != navigation.next_prompt.prompt_id
                    or self.cursor.selection.take_id is not None
                ):
                    raise ValueError("saved cursor must publish its sole resolved next prompt")
            elif self.cursor.selection.take_id != self.take.reservation.take_id:
                raise ValueError("plain or completed Save must keep the last take visible")
        return self


class NavigationEvent(ContractModel):
    event: Literal["navigation"] = "navigation"
    result: RecordingNavigationResult
    cursor: RecordingCursor | None = None

    @model_validator(mode="after")
    def _cursor_result(self) -> NavigationEvent:
        if self.cursor is None:
            if self.result.selected_take_id is not None:
                raise ValueError("a selected take requires its committed cursor")
        else:
            if (
                self.cursor.selection.catalog_sha256 != self.result.catalog_sha256
                or self.cursor.complete != self.result.complete
            ):
                raise ValueError("navigation cursor must bind its catalog and completion")
            if self.result.next_prompt is not None and (
                self.cursor.selection.prompt_id != self.result.next_prompt.prompt_id
                or self.cursor.selection.take_id != self.result.selected_take_id
            ):
                raise ValueError("navigation cursor must identify only its resolved target")
            if self.result.selected_take_id is not None and self.cursor.mode != "selected-prompt":
                raise ValueError("only explicit prompt selection resolves a selected take")
        return self


class RecordingHistoryEntry(ContractModel):
    """Small indexed projection; history pages neither transmit catalogs nor read WAVs."""

    take_id: StableId
    subject_id: StableId
    group_id: StableId | None
    legacy_session_id: StableId | None
    catalog_sha256: Sha256
    prompt: Prompt
    recorded_at: UtcTimestamp | None
    date_precision: Literal["take", "session", "unknown"]
    source_sha256: Sha256 | None
    source_frames: FrameCount | None
    decision: DecisionState
    analysis: AnalysisState
    export: ExportState
    state_revision: Revision
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _honest_history_precision(self) -> RecordingHistoryEntry:
        if (self.date_precision == "unknown") != (self.recorded_at is None):
            raise ValueError("historical dates must retain their actual evidence precision")
        if self.group_id is None and self.legacy_session_id is None:
            raise ValueError("history requires a native group or legacy session identity")
        if (self.source_sha256 is None) != (self.source_frames is None):
            raise ValueError("indexed source digest and frame count must be supplied together")
        return self


class RecordingPromptSummary(ContractModel):
    """Bounded row projection; the client already owns exact catalog text."""

    prompt_id: StableId
    prompt_sha256: Sha256
    canonical_ordinal: Revision
    take_count: Revision
    kept_count: Revision
    status: Literal["missing", "unknown", "draft", "saved-checking", "recorded", "needs-review"]
    latest_recorded_at: UtcTimestamp | None
    date_precision: Literal["take", "session", "unknown"]

    @model_validator(mode="after")
    def _honest_prompt_summary(self) -> RecordingPromptSummary:
        if self.kept_count > self.take_count:
            raise ValueError("kept count cannot exceed total takes")
        if (self.date_precision == "unknown") != (self.latest_recorded_at is None):
            raise ValueError("prompt summary must preserve actual date precision")
        return self


class RecordingGroupSummary(ContractModel):
    """Bounded context summary, never a repeated catalog or complete take list."""

    group_id: StableId | None
    legacy_session_id: StableId | None
    opened_at: UtcTimestamp | None
    date_precision: Literal["take", "session", "unknown"]
    take_count: Revision
    kept_count: Revision

    @model_validator(mode="after")
    def _honest_group_summary(self) -> RecordingGroupSummary:
        if (self.group_id is None) == (self.legacy_session_id is None):
            raise ValueError("group summary requires exactly one native or legacy identity")
        if self.kept_count > self.take_count:
            raise ValueError("kept count cannot exceed total takes")
        if (self.date_precision == "unknown") != (self.opened_at is None):
            raise ValueError("group summary must preserve actual date precision")
        return self


class RecordingHistoryPage(ContractModel):
    event: Literal["history"] = "history"
    catalog_sha256: Sha256
    take_prompt_id: StableId | None = None
    index_revision: Revision
    completeness: Literal["complete", "building", "unknown"]
    takes: tuple[RecordingHistoryEntry, ...] = Field(default=(), max_length=MAX_HISTORY_PAGE_SIZE)
    next_cursor: StableId | None = None
    has_more: bool
    prompts: tuple[RecordingPromptSummary, ...] = Field(
        default=(), max_length=MAX_HISTORY_PAGE_SIZE
    )
    next_prompt_cursor: StableId | None = None
    has_more_prompts: bool = False
    groups: tuple[RecordingGroupSummary, ...] = Field(default=(), max_length=MAX_HISTORY_PAGE_SIZE)
    next_group_cursor: StableId | None = None
    has_more_groups: bool = False
    total_take_count: Revision = 0
    total_group_count: Revision = 0
    total_prompt_count: Revision = 0
    covered_prompt_count: Revision = 0
    missing_prompt_count: Revision | None = None
    errors: tuple[RecordingError, ...] = Field(default=(), max_length=16)
    error_count: Revision = 0

    @model_validator(mode="after")
    def _cursor_and_catalog(self) -> RecordingHistoryPage:
        for has_more, cursor in (
            (self.has_more, self.next_cursor),
            (self.has_more_prompts, self.next_prompt_cursor),
            (self.has_more_groups, self.next_group_cursor),
        ):
            if has_more != (cursor is not None):
                raise ValueError("paged history must state whether its next cursor exists")
        if self.take_prompt_id is not None and any(
            take.prompt.prompt_id != self.take_prompt_id for take in self.takes
        ):
            raise ValueError("filtered history takes must bind the selected exact prompt")
        if any(take.catalog_sha256 != self.catalog_sha256 for take in self.takes):
            raise ValueError("history page must bind one exact catalog")
        if self.completeness != "complete" and self.missing_prompt_count is not None:
            raise ValueError("incomplete history cannot claim missing coverage")
        if self.covered_prompt_count > self.total_prompt_count:
            raise ValueError("covered count cannot exceed catalog prompt count")
        return self


class RecordingReviewSource(ContractModel):
    """Authorization for one original; native playback verifies this exact descriptor/digest."""

    take_id: StableId
    subject_id: StableId
    catalog_sha256: Sha256
    prompt: Prompt
    source_path: RelativePath
    source_sha256: Sha256
    source_frames: FrameCount
    source_identity: RecordingFileIdentity
    frame_range: RecordingFrameRange
    decision: DecisionState
    decision_revision: Revision
    edit_revision: Revision
    analysis: AnalysisState
    export: ExportState
    recorded_at: UtcTimestamp | None
    date_precision: Literal["take", "session", "unknown"]
    original_kind: Literal["native-original", "legacy-staged", "accepted-raw"]
    edit_recipe_sha256: Sha256 | None = None
    eligibility_messages: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _source_binding(self) -> RecordingReviewSource:
        if (
            self.source_identity.relative_path != self.source_path
            or self.frame_range.source_frames != self.source_frames
            or (self.recorded_at is None) != (self.date_precision == "unknown")
        ):
            raise ValueError("review source must bind its original path, frames and date precision")
        return self


class ReviewEvent(ContractModel):
    event: Literal["review"] = "review"
    source: RecordingReviewSource
    cursor: RecordingCursor

    @model_validator(mode="after")
    def _selected_source(self) -> ReviewEvent:
        if (
            self.cursor.subject_id != self.source.subject_id
            or self.cursor.selection.take_id != self.source.take_id
            or self.cursor.selection.prompt_id != self.source.prompt.prompt_id
            or self.cursor.selection.catalog_sha256 != self.source.catalog_sha256
        ):
            raise ValueError("review cursor must select the authorized source")
        return self


class ReviewActionEvent(ContractModel):
    """Legacy actions bind an authorized source without inventing a native reservation."""

    event: Literal["review-action"] = "review-action"
    operation_id: StableId
    action: Literal["edit", "reject", "remove", "restore"]
    source: RecordingReviewSource
    cursor: RecordingCursor
    state: Literal["pending", "completed", "failed"]
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _legacy_action(self) -> ReviewActionEvent:
        ReviewEvent(source=self.source, cursor=self.cursor)
        if self.source.original_kind == "native-original":
            raise ValueError("legacy actions cannot substitute for a canonical native take")
        if (self.state == "failed") != (self.error is not None):
            raise ValueError("only a failed legacy action requires its error")
        return self


class EditEvent(ContractModel):
    event: Literal["edited"] = "edited"
    operation_id: StableId
    take: RecordingTake
    recipe_sha256: Sha256
    derivative_state: Literal["pending", "not-required"]

    @model_validator(mode="after")
    def _committed_recipe(self) -> EditEvent:
        if self.take.seal is None or self.take.frame_range is None or self.take.edit_revision < 1:
            raise ValueError("edited event requires a sealed versioned frame decision")
        trimmed = (
            self.take.frame_range.start_frame != 0
            or self.take.frame_range.end_frame != self.take.frame_range.source_frames
        )
        if (self.derivative_state == "pending") != trimmed:
            raise ValueError("only a trimmed frame selection needs a derivative")
        return self


class DecisionEvent(ContractModel):
    event: Literal["decision"] = "decision"
    take: RecordingTake
    decision: RecordingDecisionEvidence

    @model_validator(mode="after")
    def _bound_decision(self) -> DecisionEvent:
        if self.take.decision_evidence != self.decision:
            raise ValueError("decision response must publish the exact committed take intent")
        return self


class DerivativeEvent(ContractModel):
    event: Literal["derivative"] = "derivative"
    take_id: StableId
    source_sha256: Sha256
    edit_revision: PositiveRevision
    recipe_sha256: Sha256
    state: Literal["ready", "failed"]
    derived_path: RelativePath | None = None
    derived_sha256: Sha256 | None = None
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _render_outcome(self) -> DerivativeEvent:
        if self.state == "ready":
            if self.derived_path is None or self.derived_sha256 is None or self.error is not None:
                raise ValueError("rendered derivative requires bound output and no error")
            if (
                self.derived_path
                != f"derived/recording-edits/native/{self.recipe_sha256}/{self.derived_sha256}.wav"
            ):
                raise ValueError("derivative response must bind its exact recipe and output")
        elif self.error is None or self.derived_path is not None or self.derived_sha256 is not None:
            raise ValueError("failed derivative requires an error and cannot claim output")
        return self


class CatalogEvent(ContractModel):
    """The catalog the service pinned for this process: the exact suite and its reference.

    A plain correlated reply to ``CatalogCommand``, never a state snapshot. The app builds its
    prompts and Library from ``catalog`` and names ``reference`` when it opens a context.
    Validation fails (``ValidationError``) unless ``catalog`` hashes to
    ``reference.catalog_sha256`` and repeats its suite id and purpose; the recording service
    reports such a reply as the terminal ``RECORDING.INVALID_MESSAGE``.
    """

    event: Literal["catalog"] = "catalog"
    operation_id: StableId
    catalog: PromptSuite
    reference: CatalogReference

    @model_validator(mode="after")
    def _catalog_matches_reference(self) -> CatalogEvent:
        if canonical_sha256(self.catalog) != self.reference.catalog_sha256:
            raise ValueError("catalog event must deliver the exact suite its reference names")
        if (
            self.catalog.suite_id != self.reference.suite_id
            or self.catalog.purpose != self.reference.purpose
        ):
            raise ValueError("catalog event suite id and purpose must match its reference")
        return self


class ContextEvent(ContractModel):
    event: Literal["context"] = "context"
    group: RecordingGroup
    cursor: RecordingCursor
    history: RecordingHistoryPage | None = None

    @model_validator(mode="after")
    def _context_cursor(self) -> ContextEvent:
        if (
            self.cursor.subject_id != self.group.subject_id
            or self.cursor.group_id != self.group.group_id
            or self.cursor.selection.catalog_sha256 != self.group.catalog_sha256
        ):
            raise ValueError("context cursor must bind its exact group, subject and catalog")
        # A v2 group embeds no prompts; the service opens the cursor on a snapshot prompt.
        catalog = self.group.catalog
        if catalog is not None and not any(
            prompt.prompt_id == self.cursor.selection.prompt_id for prompt in catalog.prompts
        ):
            raise ValueError("context cursor prompt must belong to the exact catalog")
        if self.history is not None and self.history.catalog_sha256 != self.group.catalog_sha256:
            raise ValueError("context history must belong to the exact catalog")
        return self


class AnalysisEvent(ContractModel):
    event: Literal["analysis"] = "analysis"
    result: RecordingAnalysisResult


# Package queue state (recording_package_jobs); not a service event since fine-tuning.
class PackagePreparationEvent(ContractModel):
    event: Literal["package-preparation"] = "package-preparation"
    operation_id: StableId
    group_id: StableId
    state: Literal["queued", "running", "blocked", "failed"]
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _failure_detail(self) -> PackagePreparationEvent:
        if (self.state in {"blocked", "failed"}) != (self.error is not None):
            raise ValueError(
                "blocked/failed package preparation requires a named prerequisite/error"
            )
        return self


# A prepared package map (recording_package_jobs); not a service event since fine-tuning.
class PackageEvent(ContractModel):
    event: Literal["package"] = "package"
    package: RecordingPackageMap


type FineTuneState = Literal["queued", "running", "finished", "failed", "cancelled"]
type FineTuneStage = Literal["selecting", "dataset", "training", "sampling"]


class FineTuneProgressEvent(ContractModel):
    """Where one fine-tune job stands; sent as it moves and repeated in status replies.

    ``stage`` is the step a running job is in. Training progress (``epoch`` of ``epochs``,
    ``update``, ``loss``, ``eta_seconds`` remaining) is parsed from the trainer's log.
    ``error`` is present exactly when the job failed; a cancelled job carries none.
    """

    event: Literal["fine-tune-progress"] = "fine-tune-progress"
    operation_id: StableId
    state: FineTuneState
    stage: FineTuneStage | None = None
    run_id: StableId | None = None
    epoch: int | None = Field(default=None, ge=1, le=FINETUNE_MAX_EPOCHS)
    epochs: int | None = Field(default=None, ge=1, le=FINETUNE_MAX_EPOCHS)
    update: Revision | None = None
    loss: float | None = Field(default=None, allow_inf_nan=False)
    eta_seconds: Revision | None = None
    message: str | None = Field(default=None, min_length=1, max_length=1024)
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _state_evidence(self) -> FineTuneProgressEvent:
        if (self.state == "failed") != (self.error is not None):
            raise ValueError("a failed fine-tune names its error; other states carry none")
        if self.state == "finished" and self.run_id is None:
            raise ValueError("a finished fine-tune names its run")
        if self.state == "queued" and self.stage is not None:
            raise ValueError("a queued fine-tune has not started a stage")
        if (
            self.state == "running"
            and self.stage == "training"
            and (self.epoch is None or self.epochs is None)
        ):
            raise ValueError("training progress requires its pass and total")
        if self.epoch is not None and (self.epochs is None or self.epoch > self.epochs):
            raise ValueError("a training pass is counted against its total")
        return self


class FineTuneRunSummary(ContractModel):
    """One training run as the app sees it: ``finetune.FineTuneRun`` without absolute paths.

    Paths are relative to the asset root, and every sample lies under ``run_root``. Only a
    finished run has samples (``finetuned`` from the trained checkpoint, ``base`` from the
    pretrained weights, for comparison).
    """

    run_id: StableId
    run_root: RelativePath
    status: Literal["running", "finished", "failed"]
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    epochs: int = Field(ge=1, le=FINETUNE_MAX_EPOCHS)
    prompt_count: int = Field(ge=1, le=10_000)
    retained_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    finetuned_sample_paths: tuple[RelativePath, ...] = Field(default=(), max_length=64)
    base_sample_paths: tuple[RelativePath, ...] = Field(default=(), max_length=64)
    error: str | None = Field(default=None, min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def _run_evidence(self) -> FineTuneRunSummary:
        if (self.status == "running") == (self.finished_at is not None):
            raise ValueError("a finished or failed run carries its finish time; a running one none")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("a run cannot finish before it started")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("failed runs name their error; other runs carry none")
        samples = self.finetuned_sample_paths + self.base_sample_paths
        if samples and self.status != "finished":
            raise ValueError("only a finished run has samples")
        if not all(path.startswith(self.run_root + "/") for path in samples):
            raise ValueError("run samples must lie under the run's directory")
        return self


class FineTuneEvent(ContractModel):
    """A fine-tune job's run record changed (written, finished or failed)."""

    event: Literal["fine-tune"] = "fine-tune"
    operation_id: StableId
    run: FineTuneRunSummary


class FineTuneStatusEvent(ContractModel):
    """Reply to ``fine-tune-status``: the runtime, the active job and the latest finished run."""

    event: Literal["fine-tune-status"] = "fine-tune-status"
    operation_id: StableId
    runtime_configured: bool
    active: FineTuneProgressEvent | None = None
    latest_run: FineTuneRunSummary | None = None

    @model_validator(mode="after")
    def _current_jobs(self) -> FineTuneStatusEvent:
        if self.active is not None and self.active.state not in {"queued", "running"}:
            raise ValueError("the active fine-tune is queued or running")
        if self.latest_run is not None and self.latest_run.status != "finished":
            raise ValueError("the latest run is a finished run")
        return self


class RecordingRecoveryStatus(ContractModel):
    take_id: StableId
    status: Literal[
        "draft", "kept", "rejected", "removed", "partial", "empty", "busy", "unavailable"
    ]
    error: RecordingError | None = None
    recovered_seal: bool = False

    @model_validator(mode="after")
    def _recovery_status_evidence(self) -> RecordingRecoveryStatus:
        sealed = self.status in {"draft", "kept", "rejected", "removed"}
        if self.recovered_seal and not sealed:
            raise ValueError("recovered seal requires a sealed take status")
        if sealed and self.error is not None:
            raise ValueError("sealed recovery status cannot contain a recovery error")
        if self.status == "unavailable" and self.error is None:
            raise ValueError("unavailable recovery requires an explicit error")
        return self


class RecordingRecoveryPage(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-recovery-page/v1"] = (
        "cole-voice-lab.recording-recovery-page/v1"
    )
    entries: tuple[RecordingRecoveryStatus, ...] = Field(max_length=50)
    next_take_id: StableId | None = None

    @model_validator(mode="after")
    def _unique_take_entries(self) -> RecordingRecoveryPage:
        identifiers = [entry.take_id for entry in self.entries]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("recovery page cannot repeat a take identity")
        return self


class RecoveryEvent(ContractModel):
    event: Literal["recovery"] = "recovery"
    operation_id: StableId
    take_id: StableId | None = None
    page: RecordingRecoveryPage

    @model_validator(mode="after")
    def _single_take_identity(self) -> RecoveryEvent:
        if self.take_id is not None and (
            len(self.page.entries) != 1
            or self.page.entries[0].take_id != self.take_id
            or self.page.next_take_id is not None
        ):
            raise ValueError("single-take recovery must return exactly its requested identity")
        return self


class RecordingRecoveryFinding(ContractModel):
    """One observed result; pending results remain explicitly awaiting recheck."""

    take_id: StableId
    observed_take_revision: int | None = Field(default=None, ge=0)
    pending: bool
    result: RecordingRecoveryStatus | None = None

    @model_validator(mode="after")
    def _finding_is_bound(self) -> RecordingRecoveryFinding:
        if self.result is None and not self.pending:
            raise ValueError("a classified recovery finding requires its result")
        if self.result is not None and self.result.take_id != self.take_id:
            raise ValueError("recovery finding result belongs to another take")
        return self


class RecordingRecoveryInventoryPage(ContractModel):
    """Disposable inventory coverage, independent of canonical history completeness."""

    schema_version: Literal["cole-voice-lab.recording-recovery-inventory/v1"] = (
        "cole-voice-lab.recording-recovery-inventory/v1"
    )
    scan_generation: StableId
    inventory_revision: int = Field(ge=0)
    scan_state: Literal["scanning", "complete", "error"]
    enumeration_complete: bool
    pending_count: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    recovered_seal_count: int = Field(ge=0)
    covered_store_revision: int | None = Field(default=None, ge=0)
    entries: tuple[RecordingRecoveryFinding, ...] = Field(default=(), max_length=50)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _inventory_is_honest(self) -> RecordingRecoveryInventoryPage:
        identifiers = [entry.take_id for entry in self.entries]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("recovery inventory cannot repeat a take identity")
        if self.finding_count < len(self.entries):
            raise ValueError("recovery inventory count cannot omit visible findings")
        if (
            not sum(entry.pending for entry in self.entries)
            <= self.pending_count
            <= self.finding_count
        ):
            raise ValueError("recovery inventory pending count must cover visible pending findings")
        visible_recovered = sum(
            not entry.pending and entry.result is not None and entry.result.recovered_seal
            for entry in self.entries
        )
        if (
            not visible_recovered
            <= self.recovered_seal_count
            <= self.finding_count - self.pending_count
        ):
            raise ValueError("recovery inventory recovered count must cover classified findings")
        if (self.scan_state == "error") != (self.error is not None):
            raise ValueError("recovery inventory error state requires its named error")
        if self.scan_state == "complete":
            if (
                not self.enumeration_complete
                or self.pending_count != 0
                or self.covered_store_revision is None
            ):
                raise ValueError("complete recovery inventory requires classified coverage")
        elif self.covered_store_revision is not None:
            raise ValueError("unfinished recovery inventory cannot certify store coverage")
        return self


class RecoveryInventoryEvent(ContractModel):
    event: Literal["recovery-inventory"] = "recovery-inventory"
    page: RecordingRecoveryInventoryPage


class RecoveryInventoryChangedEvent(ContractModel):
    event: Literal["recovery-inventory-changed"] = "recovery-inventory-changed"
    scan_generation: StableId
    inventory_revision: int = Field(ge=0)


class RecordingRecoveryWork(ContractModel):
    """One claimed observation; a later recheck supersedes its token."""

    scan_generation: StableId
    take_id: StableId
    token: int = Field(ge=1)
    expected_take_revision: int | None = Field(default=None, ge=0)


class RecordingRecoveryObservation(ContractModel):
    """Bind a classification to the actual canonical head used to produce it."""

    result: RecordingRecoveryStatus
    observed_take_revision: int | None = Field(default=None, ge=0)
    subject_id: StableId | None = None


class AcknowledgedEvent(ContractModel):
    event: Literal["acknowledged"] = "acknowledged"
    operation_id: StableId
    action: Literal["prepared", "cancelled", "shutdown", "recovered"]


class WaveformCacheEvent(ContractModel):
    """Derived-cache status never changes the successful raw capture decision or transport."""

    event: Literal["waveform-cache"] = "waveform-cache"
    take_id: StableId
    source_sha256: Sha256
    status: Literal["stored", "needs-rebuild"]
    error: RecordingError | None

    @model_validator(mode="after")
    def _cache_failure_is_rebuildable(self) -> WaveformCacheEvent:
        if self.status == "stored" and self.error is not None:
            raise ValueError("stored waveform cache cannot contain an error")
        if self.status == "needs-rebuild" and (
            self.error is None
            or self.error.code != "RECORDING.WAVEFORM_CACHE_UNAVAILABLE"
            or not self.error.retryable
        ):
            raise ValueError("missing waveform cache requires an explicit retryable rebuild error")
        return self


type ReliableEvent = Annotated[
    HandshakeEvent
    | RecorderStateEvent
    | WaveformCacheEvent
    | RecordingTransition
    | ReservationEvent
    | TakeEvent
    | SaveEvent
    | NavigationEvent
    | CatalogEvent
    | ContextEvent
    | ReviewEvent
    | ReviewActionEvent
    | EditEvent
    | DecisionEvent
    | DerivativeEvent
    | RecordingHistoryPage
    | AnalysisEvent
    | FineTuneProgressEvent
    | FineTuneEvent
    | FineTuneStatusEvent
    | RecoveryEvent
    | RecoveryInventoryEvent
    | RecoveryInventoryChangedEvent
    | AcknowledgedEvent,
    Field(discriminator="event"),
]


class RecordingResponse(ContractModel):
    data: ReliableEvent | None
    error: RecordingError | None
    meta: RecordingMessageMeta

    @model_validator(mode="after")
    def _one_result(self) -> RecordingResponse:
        if (self.data is None) == (self.error is None):
            raise ValueError("response must contain exactly one result or error")
        take_id: str | None = None
        if isinstance(
            self.data, (RecorderStateEvent, RecordingTransition, WaveformCacheEvent, RecoveryEvent)
        ):
            take_id = self.data.take_id
        elif isinstance(
            self.data, (ReservationEvent, TakeEvent, SaveEvent, EditEvent, DecisionEvent)
        ):
            take_id = self.data.take.reservation.take_id
        elif isinstance(self.data, DerivativeEvent):
            take_id = self.data.take_id
        elif isinstance(self.data, (ReviewEvent, ReviewActionEvent)):
            take_id = self.data.source.take_id
        elif isinstance(self.data, ContextEvent):
            take_id = self.data.cursor.selection.take_id
        elif isinstance(self.data, NavigationEvent) and self.data.cursor is not None:
            take_id = self.data.cursor.selection.take_id
        elif isinstance(self.data, AnalysisEvent):
            take_id = self.data.result.take_id
        if self.data is not None and self.meta.take_id != take_id:
            raise ValueError("event metadata must match the event take identity")
        return self


class WaveformBucket(ContractModel):
    minimum: float = Field(ge=-1.0, le=1.0, allow_inf_nan=False)
    maximum: float = Field(ge=-1.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _ordered_extrema(self) -> WaveformBucket:
        if self.minimum > self.maximum:
            raise ValueError("waveform extrema must be ordered")
        return self


class WaveformSampleClock(ContractModel):
    """Current-generation captured frame binding to the same-boot native host clock."""

    anchor_frame: FrameCount
    host_time_ticks: Revision
    seconds_per_tick: float = Field(gt=0, allow_inf_nan=False)


class WaveformSnapshot(ContractModel):
    event: Literal["waveform"] = "waveform"
    take_id: StableId
    process_generation: StableId
    first_frame: FrameCount
    end_frame: FrameCount
    written_frames: FrameCount
    bucket_width_frames: int = Field(ge=1, le=MAX_RECORDING_FRAMES)
    buckets: tuple[WaveformBucket, ...] = Field(max_length=MAX_WAVEFORM_BUCKETS)
    sample_clock: WaveformSampleClock | None

    @model_validator(mode="after")
    def _bucket_frame_mapping(self) -> WaveformSnapshot:
        if not self.first_frame <= self.end_frame <= self.written_frames:
            raise ValueError("waveform range cannot exceed confirmed written frames")
        if self.sample_clock is not None and self.sample_clock.anchor_frame >= self.written_frames:
            raise ValueError("waveform sample clock must anchor an actually written frame")
        span = self.end_frame - self.first_frame
        expected = (span + self.bucket_width_frames - 1) // self.bucket_width_frames
        if len(self.buckets) != expected:
            raise ValueError("waveform bucket count must cover the exact frame range")
        return self


class RecordingTelemetry(ContractModel):
    data: WaveformSnapshot
    error: None = None
    meta: RecordingMessageMeta

    @model_validator(mode="after")
    def _visual_identity(self) -> RecordingTelemetry:
        if (
            self.meta.take_id != self.data.take_id
            or self.meta.process_generation != self.data.process_generation
        ):
            raise ValueError("telemetry metadata must bind its take and generation")
        return self


def recording_frame_bytes(message: ContractModel, *, telemetry: bool = False) -> bytes:
    """Encode one strict model with a four-byte network-order bounded length prefix."""
    payload = canonical_json_bytes(message)
    maximum = MAX_TELEMETRY_FRAME_BYTES if telemetry else MAX_CONTROL_FRAME_BYTES
    if not 1 <= len(payload) <= maximum:
        raise ValueError("recording message exceeds its frame size limit")
    return len(payload).to_bytes(FRAME_PREFIX_BYTES, "big") + payload


def native_recovery_bytes(receipt: NativeRecoveryReceipt) -> bytes:
    """Encode the separate emergency wire contract; this is not lab canonical evidence."""
    return recording_frame_bytes(receipt)


RECORDING_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "recording-group": RecordingGroup,
    # A recording-group/v2 nests the reference, so Swift validates it like the group itself.
    "catalog-reference": CatalogReference,
    "capture-reservation": CaptureReservation,
    "capture-seal": CaptureSealReceipt,
    "native-recovery": NativeRecoveryReceipt,
    "recording-take": RecordingTake,
    "recording-save": RecordingSaveOperation,
    "recording-analysis": RecordingAnalysisResult,
    "recording-package-map": RecordingPackageMap,
    "recording-recorder-request": RecorderRequest,
    "recording-service-request": ServiceRequest,
    "recording-response": RecordingResponse,
    "recording-recovery-page": RecordingRecoveryPage,
    "recording-recovery-inventory": RecordingRecoveryInventoryPage,
    "recording-telemetry": RecordingTelemetry,
    "recording-transition": RecordingTransition,
    "recording-history-entry": RecordingHistoryEntry,
}
# Snapshot files are read only by Python (corpus_store), so they stay out of the native
# conformance registry; the reference a group nests is registered above.
CORPUS_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "catalog-snapshot": CatalogSnapshot,
}
