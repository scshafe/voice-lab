"""Strict models of the speech API and of the records it keeps (docs/SPEECH_API.md).

@canonical speech.contracts -- the request and status bodies of ``/api/v1/speech`` (JSON
Schemas ``api-v1-speech-*``), the webhook event, and the records written on disk: the job
(``speech-job``), its input (the text, deleted when the job ends), its state, the render
ledger entry (``speech-render``), the webhook outbox entry and delivery log, and the GPU
slot's holder. Every one is frozen and refuses unknown fields.

The model-family part of a release and of a render (D-048) is a discriminated union on
``family``: ``ReleaseModel`` (what a release binds beyond its files) and ``RenderSettings``
(the settings a render used, which also seed it). F5-TTS (``f5-tts``) is the only member; a
new family adds one member to each union and leaves every common field as it is.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from ..models import (
    F5_FAMILY,
    ContractModel,
    Sha256,
    StableId,
    has_hidden_control,
    historical_field,
    reject_explicit_null,
)
from ..recording_contracts import SpeechText, UtcTimestamp

JOB_ID_PATTERN: Final = re.compile(r"^sp-[0-9a-f]{24}$")
MAX_SEGMENTS: Final = 2000
MAX_CLIENT_REF: Final = 200
WEBHOOK_EVENT_PREFIX: Final = "msg_"

type JobId = Annotated[str, StringConstraints(pattern=JOB_ID_PATTERN.pattern)]
type SpeechFormat = Literal["wav", "ogg-opus"]
type JobState = Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "cancelled", "expired"})
# The API's one audio format, whatever the family renders natively (D-048): 24 kHz mono 16-bit.
API_SAMPLE_RATE_HZ: Final = 24_000
type FailureCode = Literal[
    "runtime_unavailable",
    "render_failed",
    "render_timeout",
    "queue_timeout",
    "voice_release_changed",
    "internal_error",
]
type EventType = Literal["speech.succeeded", "speech.failed", "speech.cancelled"]


def _client_ref(value: str) -> str:
    if not value.strip() or has_hidden_control(value) or "\n" in value:
        raise ValueError("a client reference is one line of visible text")
    return value


type ClientRef = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_CLIENT_REF), AfterValidator(_client_ref)
]
type IdempotencyKey = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]


# -- the API -------------------------------------------------------------------------------


class SpeechRequest(ContractModel):
    """``POST /api/v1/speech``: what to say, and how the caller wants to hear that it ended."""

    schema_version: Literal["cole-voice-lab.api.speech-request/v1"] = (
        "cole-voice-lab.api.speech-request/v1"
    )
    text: SpeechText
    format: SpeechFormat = "wav"
    callback_url: str | None = Field(default=None, min_length=1, max_length=2048)
    idempotency_key: IdempotencyKey | None = None
    client_ref: ClientRef | None = None


class SpeechLinks(ContractModel):
    self: str
    audio: str
    events: str
    cancel: str

    @classmethod
    def for_job(cls, job_id: str) -> SpeechLinks:
        base = f"/api/v1/speech/{job_id}"
        return cls(
            self=base, audio=f"{base}/audio", events=f"{base}/events", cancel=f"{base}/cancel"
        )


def segment_link(job_id: str, index: int) -> str:
    return f"/api/v1/speech/{job_id}/segments/{index}/audio"


class SpeechSegment(ContractModel):
    """One finished sentence, as a caller sees it (and fetches it at ``audio``)."""

    index: int = Field(ge=1, le=MAX_SEGMENTS)
    sha256: Sha256
    bytes: int = Field(ge=45)
    duration_ms: int = Field(ge=0)
    audio: str


class SpeechAudio(ContractModel):
    """The rendered audio: what a caller checks the bytes it fetched against."""

    sha256: Sha256
    bytes: int = Field(ge=45)
    duration_ms: int = Field(ge=0)
    format: Literal["wav"] = "wav"
    expires_at: UtcTimestamp


# What a caller is told of a failure: the code and one fixed sentence. The particulars
# (paths, a release's mismatch, a worker's error) go to the job runner's own log only.
FAILURE_DETAILS: Final[dict[str, str]] = {
    "runtime_unavailable": "the speech runtime is not available on this host",
    "render_failed": "a sentence could not be rendered",
    "render_timeout": "a sentence took longer than its deadline",
    "queue_timeout": "the job waited longer than the queue allows",
    "voice_release_changed": "the voice release changed or cannot be used",
    "internal_error": "the job runner failed",
}


class SpeechFailure(ContractModel):
    code: FailureCode
    detail: str = Field(max_length=2000)

    @classmethod
    def of(cls, code: FailureCode) -> SpeechFailure:
        """The failure a caller sees for ``code``: never a path or a reason."""
        return cls(code=code, detail=FAILURE_DETAILS[code])


class SpeechJobStatus(ContractModel):
    """A job as its caller sees it (the answer of ``POST`` and ``GET …/{id}``)."""

    schema_version: Literal["cole-voice-lab.api.speech-job/v1"] = "cole-voice-lab.api.speech-job/v1"
    id: JobId
    state: JobState
    client_ref: ClientRef | None
    format: Literal["wav"]
    text_characters: int = Field(ge=1)
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    finished_at: UtcTimestamp | None
    queue_ms: int | None = Field(ge=0)
    render_ms: int | None = Field(ge=0)
    segment_count: int | None = Field(ge=1, le=MAX_SEGMENTS)
    segments: tuple[SpeechSegment, ...] = Field(max_length=MAX_SEGMENTS)
    audio: SpeechAudio | None
    error: SpeechFailure | None
    links: SpeechLinks


class SpeechEventData(ContractModel):
    id: JobId
    state: Literal["succeeded", "failed", "cancelled"]
    client_ref: ClientRef | None
    segment_count: int | None = Field(ge=1, le=MAX_SEGMENTS)
    audio: SpeechAudio | None
    error: SpeechFailure | None
    links: SpeechLinks


class SpeechEvent(ContractModel):
    """A webhook's body: no text and no audio (Standard Webhooks' ``type``, ``timestamp``,
    ``data``)."""

    type: EventType
    timestamp: UtcTimestamp
    data: SpeechEventData


# -- records on disk -----------------------------------------------------------------------


class SpeechJobRecord(ContractModel):
    """``jobs/<id>/request.json``: the accepted request, without its text.

    The text and the whole request are named by keyed digests (HMAC-SHA256 under the
    install's text key, ``store.TEXT_KEY_NAME``), never by plain SHA-256: a short reply
    ("Yes.") would otherwise be found again by hashing guesses.
    """

    schema_version: Literal["cole-voice-lab.speech-job/v1"] = "cole-voice-lab.speech-job/v1"
    job_id: JobId
    caller_id: StableId
    created_at: UtcTimestamp
    format: Literal["wav"]
    callback_url: str | None = Field(max_length=2048)
    client_ref: ClientRef | None
    idempotency_key_sha256: Sha256 | None
    request_hmac_sha256: Sha256
    text_hmac_sha256: Sha256
    text_characters: int = Field(ge=1, le=2000)


class SpeechJobInput(ContractModel):
    """``jobs/<id>/input.json``: the text; deleted when the job ends."""

    schema_version: Literal["cole-voice-lab.speech-input/v1"] = "cole-voice-lab.speech-input/v1"
    job_id: JobId
    text: SpeechText


class RenderedAudio(ContractModel):
    sha256: Sha256
    bytes: int = Field(ge=45)
    frames: int = Field(ge=1)
    sample_rate_hz: Literal[24_000] = API_SAMPLE_RATE_HZ


class SegmentAudio(RenderedAudio):
    """One published sentence: ``jobs/<id>/segments/<index, 4 digits>.wav``."""

    index: int = Field(ge=1, le=MAX_SEGMENTS)


class SpeechJobState(ContractModel):
    """``jobs/<id>/status.json``: where the job is (replaced atomically at each change)."""

    schema_version: Literal["cole-voice-lab.speech-job-state/v1"] = (
        "cole-voice-lab.speech-job-state/v1"
    )
    job_id: JobId
    state: JobState
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    error: SpeechFailure | None = None
    segment_count: int | None = Field(default=None, ge=1, le=MAX_SEGMENTS)
    segments: tuple[SegmentAudio, ...] = Field(default=(), max_length=MAX_SEGMENTS)
    audio: RenderedAudio | None = None
    audio_expires_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SpeechJobState:
        if [segment.index for segment in self.segments] != list(range(1, len(self.segments) + 1)):
            raise ValueError("segments are published in order, from 1")
        if self.segments and (
            self.segment_count is None or len(self.segments) > self.segment_count
        ):
            raise ValueError("a job publishes at most the segments it planned")
        terminal = self.state in TERMINAL_STATES
        if terminal != (self.finished_at is not None):
            raise ValueError("an ended job carries its end time, and only an ended job")
        if (self.state == "failed") != (self.error is not None):
            raise ValueError("a failed job names its failure, and only a failed job")
        if self.state in {"succeeded", "expired"} and (
            self.audio is None or self.audio_expires_at is None
        ):
            raise ValueError("a job that rendered names its audio and when it expires")
        if self.state not in {"succeeded", "expired"} and self.audio is not None:
            raise ValueError("only a job that rendered names audio")
        if self.state == "running" and self.started_at is None:
            raise ValueError("a running job carries its start time")
        return self


class VoiceReleaseRef(ContractModel):
    release_id: StableId
    release_sha256: Sha256


# -- model families (D-048) -------------------------------------------------------------------


class ReleaseFile(ContractModel):
    """One file a release binds: its role in its family (``checkpoint``, ``reference``, a
    speaker embedding…), its path relative to the asset root, and its SHA-256."""

    role: StableId
    path: str = Field(min_length=1, max_length=4096)
    sha256: Sha256


class FileDigest(ContractModel):
    """A file a backend loaded, by role and digest (a render's fingerprint names no path)."""

    role: StableId
    sha256: Sha256


class F5RuntimeRef(ContractModel):
    """The F5-TTS runtime a run trained with, as its run record names it."""

    f5_tts_version: str = Field(min_length=1, max_length=64)
    torch_version: str = Field(min_length=1, max_length=64)
    pretrained_sha256: Sha256


class F5ReleaseModel(ContractModel):
    """F5-TTS's part of a release: the reference clip's prompt and transcript, and the
    runtime the run trained with. Its files are ``checkpoint`` (the run's ``model_last.pt``)
    and ``reference`` (the clip in the run's dataset)."""

    family: Literal["f5-tts"] = F5_FAMILY
    reference_prompt_id: StableId
    reference_text_sha256: Sha256
    runtime: F5RuntimeRef


class F5RenderSettings(ContractModel):
    """What an F5-TTS render was asked for: its flow steps (D-046)."""

    family: Literal["f5-tts"] = F5_FAMILY
    nfe_steps: int = Field(ge=1, le=256)


# One member per family; a new family adds its own (D-048).
type ReleaseModel = Annotated[F5ReleaseModel, Field(discriminator="family")]
type RenderSettings = Annotated[F5RenderSettings, Field(discriminator="family")]


class RenderFingerprint(ContractModel):
    """What spoke: the family, the files its backend loaded, the runtime and the settings.

    ``native_sample_rate_hz`` is the rate the model renders at; the backend delivers
    ``API_SAMPLE_RATE_HZ`` whatever it is. ``seeded`` says whether the family honours a seed
    (a family that does not cannot promise byte-identical reruns).
    """

    model_family: StableId
    backend: StableId
    files: tuple[FileDigest, ...] = Field(min_length=1, max_length=8)
    runtime: dict[str, str] = Field(max_length=16)
    device: StableId
    native_sample_rate_hz: int = Field(ge=8_000, le=192_000)
    seeded: bool
    settings: RenderSettings
    voice_lab_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _one_family(self) -> RenderFingerprint:
        if self.settings.family != self.model_family:
            raise ValueError("a fingerprint's settings are its family's")
        return self


class SpeechRenderRecord(ContractModel):
    """``provenance/speech-renders/<job id>.json``: one render, for ever (never the text; the
    text's digest is keyed with the install's text key, so it can be checked against a text
    on this host and guessed nowhere)."""

    schema_version: Literal["cole-voice-lab.speech-render/v1"] = "cole-voice-lab.speech-render/v1"
    job_id: JobId
    caller_id: StableId
    text_hmac_sha256: Sha256
    text_characters: int = Field(ge=1, le=2000)
    release: VoiceReleaseRef
    model_family: StableId
    # Retired with consent records (D-050): a render names none; the fields stay readable.
    consent_id: StableId | None = historical_field()
    consent_sha256: Sha256 | None = historical_field()
    outcome: Literal["succeeded", "failed", "cancelled"]
    failure: FailureCode | None
    fingerprint: RenderFingerprint | None
    seed: int = Field(ge=0, le=0x7FFFFFFF)
    segment_count: int = Field(ge=1, le=MAX_SEGMENTS)
    segment_sha256: tuple[Sha256, ...] = Field(max_length=MAX_SEGMENTS)
    output: RenderedAudio | None
    created_at: UtcTimestamp
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    queue_ms: int = Field(ge=0)
    render_ms: int = Field(ge=0)
    audio_ms: int | None = Field(ge=0)

    @model_validator(mode="after")
    def _outcome_matches(self) -> SpeechRenderRecord:
        reject_explicit_null(self, "consent_id", "consent_sha256")
        if (self.consent_id is None) != (self.consent_sha256 is None):
            raise ValueError("a stored consent id and digest are present together")
        if (self.outcome == "succeeded") != (self.output is not None):
            raise ValueError("only a render that succeeded names its output")
        if (self.outcome == "failed") != (self.failure is not None):
            raise ValueError("only a failed render names its failure")
        if self.fingerprint is not None and self.fingerprint.model_family != self.model_family:
            raise ValueError("a render's fingerprint is of the release's family")
        return self


class WebhookOutboxEntry(ContractModel):
    """``outbox/<event id>.json``: an event for the delivery process (no secret)."""

    schema_version: Literal["cole-voice-lab.speech-webhook-event/v1"] = (
        "cole-voice-lab.speech-webhook-event/v1"
    )
    event_id: Annotated[str, StringConstraints(pattern=r"^msg_sp-[0-9a-f]{24}$")]
    caller_id: StableId
    url: str = Field(min_length=1, max_length=2048)
    created_at: UtcTimestamp
    event: SpeechEvent


class DeliveryAttempt(ContractModel):
    number: int = Field(ge=1, le=16)
    at: UtcTimestamp
    status: int | None = Field(ge=100, le=599)
    error: str | None = Field(max_length=200)
    duration_ms: int = Field(ge=0)


type DeliveryOutcome = Literal["pending", "delivered", "gone", "given-up", "refused"]


class WebhookDeliveryLog(ContractModel):
    """``webhooks/<event id>/delivery.json``: every attempt; never the secret or the body."""

    schema_version: Literal["cole-voice-lab.speech-webhook-delivery/v1"] = (
        "cole-voice-lab.speech-webhook-delivery/v1"
    )
    event_id: str = Field(min_length=1, max_length=64)
    caller_id: StableId
    outcome: DeliveryOutcome
    attempts: tuple[DeliveryAttempt, ...] = Field(max_length=16)
    ended_at: UtcTimestamp | None = None


class GpuSlotHolder(ContractModel):
    """``gpu-slot.json``: who holds the one GPU slot, and until when it expects to."""

    schema_version: Literal["cole-voice-lab.gpu-slot-holder/v1"] = (
        "cole-voice-lab.gpu-slot-holder/v1"
    )
    kind: Literal["speech", "finetune", "audition", "speak", "benchmark"]
    job_id: StableId
    started_at: UtcTimestamp
    expected_end_at: UtcTimestamp | None = None


class GpuSlotWant(ContractModel):
    """``gpu-slot.wanted``: a long job waiting for the slot, and how to tell it is gone.

    The want is stale, and is removed by whoever reads it, when its process is not alive,
    the host booted since it was written, or it is past ``expires_at`` (the longest the job
    said it would wait for the lock, plus a minute).
    """

    schema_version: Literal["cole-voice-lab.gpu-slot-want/v1"] = "cole-voice-lab.gpu-slot-want/v1"
    holder: GpuSlotHolder
    pid: int = Field(ge=1)
    boot_id: str = Field(min_length=1, max_length=64)
    expires_at: UtcTimestamp


def event_type(state: str) -> EventType:
    """The webhook event type of a job that ended in ``state``."""
    if state == "succeeded":
        return "speech.succeeded"
    if state == "failed":
        return "speech.failed"
    return "speech.cancelled"


def milliseconds(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() * 1000))


SPEECH_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "api-v1-speech-request": SpeechRequest,
    "api-v1-speech-job": SpeechJobStatus,
    "api-v1-speech-event": SpeechEvent,
    "speech-job": SpeechJobRecord,
    "speech-job-state": SpeechJobState,
    "speech-render": SpeechRenderRecord,
    "speech-webhook-event": WebhookOutboxEntry,
    "speech-webhook-delivery": WebhookDeliveryLog,
    "gpu-slot-holder": GpuSlotHolder,
    "gpu-slot-want": GpuSlotWant,
}
