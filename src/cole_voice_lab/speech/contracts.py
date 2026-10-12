"""Strict models of the speech API and of the records it keeps (docs/SPEECH_API.md).

@canonical speech.contracts -- the request and status bodies of ``/api/v1/speech`` (JSON
Schemas ``api-v1-speech-*``), the webhook event, and the records written on disk: the job
(``speech-job``), its input (the text, deleted when the job ends), its state, the render
ledger entry (``speech-render``), the webhook outbox entry and delivery log, and the GPU
slot's holder. Every one is frozen and refuses unknown fields.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from ..models import ContractModel, Sha256, StableId, has_hidden_control
from ..recording_contracts import SpeechText, UtcTimestamp

JOB_ID_PATTERN: Final = re.compile(r"^sp-[0-9a-f]{24}$")
MAX_SEGMENTS: Final = 2000
MAX_CLIENT_REF: Final = 200
WEBHOOK_EVENT_PREFIX: Final = "msg_"

type JobId = Annotated[str, StringConstraints(pattern=JOB_ID_PATTERN.pattern)]
type SpeechFormat = Literal["wav", "ogg-opus"]
type JobState = Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "cancelled", "expired"})
type FailureCode = Literal[
    "runtime_unavailable",
    "render_failed",
    "render_timeout",
    "queue_timeout",
    "voice_release_changed",
    "consent_changed",
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


class SpeechFailure(ContractModel):
    code: FailureCode
    detail: str = Field(max_length=2000)


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
    """``jobs/<id>/request.json``: the accepted request, without its text."""

    schema_version: Literal["cole-voice-lab.speech-job/v1"] = "cole-voice-lab.speech-job/v1"
    job_id: JobId
    caller_id: StableId
    created_at: UtcTimestamp
    format: Literal["wav"]
    callback_url: str | None = Field(max_length=2048)
    client_ref: ClientRef | None
    idempotency_key_sha256: Sha256 | None
    request_sha256: Sha256
    text_sha256: Sha256
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
    sample_rate_hz: Literal[24_000] = 24_000


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


class RenderFingerprint(ContractModel):
    """What spoke: the model, its reference and the runtime, as the backend verified them."""

    checkpoint_sha256: Sha256
    reference_sha256: Sha256
    backend: StableId
    runtime: dict[str, str] = Field(max_length=16)
    device: StableId
    nfe_steps: int = Field(ge=1, le=256)
    voice_lab_version: str = Field(min_length=1, max_length=64)


class SpeechRenderRecord(ContractModel):
    """``provenance/speech-renders/<job id>.json``: one render, for ever (never the text)."""

    schema_version: Literal["cole-voice-lab.speech-render/v1"] = "cole-voice-lab.speech-render/v1"
    job_id: JobId
    caller_id: StableId
    text_sha256: Sha256
    text_characters: int = Field(ge=1, le=2000)
    release: VoiceReleaseRef
    consent_id: StableId
    consent_sha256: Sha256
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
        if (self.outcome == "succeeded") != (self.output is not None):
            raise ValueError("only a render that succeeded names its output")
        if (self.outcome == "failed") != (self.failure is not None):
            raise ValueError("only a failed render names its failure")
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
    kind: Literal["speech", "finetune", "audition", "speak"]
    job_id: StableId
    started_at: UtcTimestamp
    expected_end_at: UtcTimestamp | None = None


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
}
