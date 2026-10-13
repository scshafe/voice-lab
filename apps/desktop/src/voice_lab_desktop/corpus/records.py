"""The corpus client's records on this machine (D-052, docs/design/RECORDING-V1.md step 5).

Every record is a strict contract (the backend's ``ContractModel``: no extra fields, frozen,
strict types), written as canonical JSON, atomically, mode 0600 (``write_canonical_json``),
and read back only when it is exactly that canonical form (``read_canonical_model``). The
PC's own contracts are imported, never copied: a take names the backend's ``Prompt`` and
``CatalogReference``, an operation builds the backend's ``TakeDecisionRequest`` or
``TakeTrimRequest``, and a decision is the backend's ``TakeDecisionName``. A change to the
PC's contracts is a type or test failure here.

- ``LocalTake`` (``take.json``): what a take is, written once when it is saved.
- ``RemoteTake`` (``remote.json``): a take this machine did not record, learned from the PC's
  history so that it can be decided and trimmed here; it has no audio here.
- ``TakeSync`` (``state.json``): what was decided about it here, the revisions the PC last
  confirmed, its upload, and its outbox: the operations still to send, in order.
- ``Notices``, ``SyncRecord``, ``CatalogCacheState``: the client's notices for the UI, its
  last contact with the PC, and the catalog it last saw there.
- ``CachedLibrary``: the PC's library (coverage and each prompt's status) as last read.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from cole_voice_lab.api.corpus_contracts import (
    CorpusLibrary,
    TakeDecisionRequest,
    TakeTrimRequest,
)
from cole_voice_lab.corpus_requests import RequestId, check_decision, check_trim
from cole_voice_lab.corpus_take_store import TakeDecisionName, TakeOrigin
from cole_voice_lab.corpus_takes import MAX_TAKE_FRAMES, TakeId
from cole_voice_lab.models import ContractModel, Prompt, Sha256
from cole_voice_lab.recording_contracts import CatalogReference

TAKE_SCHEMA: Final = "voice-lab-desktop.corpus-local-take/v1"
SYNC_SCHEMA: Final = "voice-lab-desktop.corpus-take-sync/v1"
NOTICES_SCHEMA: Final = "voice-lab-desktop.corpus-notices/v1"
SYNC_RECORD_SCHEMA: Final = "voice-lab-desktop.corpus-sync/v1"
CATALOG_STATE_SCHEMA: Final = "voice-lab-desktop.corpus-catalog-cache/v1"
LIBRARY_CACHE_SCHEMA: Final = "voice-lab-desktop.corpus-library-cache/v1"
PENDING_SCHEMA: Final = "voice-lab-desktop.corpus-pending-take/v1"
REMOTE_SCHEMA: Final = "voice-lab-desktop.corpus-remote-take/v1"
#: The finished operations a take's record keeps (its counters count all of them).
JOURNAL_LIMIT: Final = 20
#: The notices kept for the UI, newest last.
NOTICE_LIMIT: Final = 100

type _Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type _Message = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
type NoticeId = Annotated[str, StringConstraints(pattern=r"^notice-[0-9a-f]{16}$")]
type OperationKind = Literal["upload", "decision", "trim"]
#: ``local-only``: never sent (simulated audio, or a take that could not be packaged);
#: ``queued``: its upload waits in the outbox; ``accepted``: the PC published it (the
#: receipt); ``refused``: the PC refused it, so nothing more of it is sent; ``remote``: the
#: PC's take, recorded elsewhere (a ``RemoteTake``): nothing to upload, its changes are sent;
#: ``gone``: such a take the PC no longer has (``take_not_found``): never sent, never counted.
type UploadState = Literal["local-only", "queued", "accepted", "refused", "remote", "gone"]
type NoticeKind = Literal[
    "changed-elsewhere", "take-refused", "change-refused", "change-blocked", "local-only"
]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a time needs an explicit timezone")
    return value.astimezone(UTC)


def new_request_id() -> str:
    """A request id for one change: ``req-`` and 32 hex digits, made once, kept for retries."""
    return f"req-{secrets.token_hex(16)}"


def new_notice_id() -> str:
    return f"notice-{secrets.token_hex(8)}"


class LocalTake(ContractModel):
    """``takes/<take_id>/take.json``: one take as it was saved here, never rewritten.

    ``prompt`` and ``catalog`` are the exact prompt and catalog version it was recorded
    against (the session's pinned version). ``local_only`` says why it is never sent
    (``simulated``, or why it could not be packaged); otherwise ``package_manifest_sha256``
    names its take package, built once (``takes/<take_id>/package/``)."""

    schema_version: Literal["voice-lab-desktop.corpus-local-take/v1"] = TAKE_SCHEMA
    take_id: TakeId
    prompt: Prompt
    prompt_sha256: Sha256
    catalog: CatalogReference
    requested_at: datetime
    saved_at: datetime
    device: _Text
    audio_sha256: Sha256
    size_bytes: int = Field(gt=44)
    frames: int = Field(gt=0, le=MAX_TAKE_FRAMES)
    simulated: bool
    local_only: _Message | None
    package_manifest_sha256: Sha256 | None

    @field_validator("requested_at", "saved_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment

    @model_validator(mode="after")
    def _consistent(self) -> LocalTake:
        if self.take_id != f"take-{self.audio_sha256}":
            raise ValueError("the take id is take-<the SHA-256 of original.wav>")
        if (self.local_only is None) == (self.package_manifest_sha256 is None):
            raise ValueError("a take is local-only, or has its package")
        if self.simulated and self.local_only is None:
            raise ValueError("a simulated take is never sent")
        return self


class RemoteTake(ContractModel):
    """``takes/<take_id>/remote.json``: a take on the PC that this machine did not record,
    learned from the PC's history (``GET /api/v1/corpus/prompts/{id}/takes``) so that the
    operator can decide and trim it here. It names the prompt it counts for and the prompt
    it was recorded against, never audio: its audio stays on the PC and is played from there.
    Its decision and trim are in ``state.json``, as for any take (``upload = "remote"``)."""

    schema_version: Literal["voice-lab-desktop.corpus-remote-take/v1"] = REMOTE_SCHEMA
    take_id: TakeId
    prompt_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    recorded_prompt_sha256: Sha256
    catalog_version: int = Field(ge=1)
    requested_at: datetime
    frames: int = Field(gt=0, le=MAX_TAKE_FRAMES)
    capture_host: _Text
    device: _Text
    #: The PC's ``origin`` of the take (D-053: ``imported``, a Swift take of the Mac, with
    #: the Mac's own take id); absent for another client's upload.
    origin: TakeOrigin | None = None
    learned_at: datetime

    @field_validator("requested_at", "learned_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment


class _Operation(ContractModel):
    """What every queued operation keeps about its tries."""

    queued_at: datetime
    attempts: int = Field(default=0, ge=0)
    last_attempt_at: datetime | None = None
    last_error: _Message | None = None
    #: Not tried again before this time (a backoff after a failed try).
    not_before: datetime | None = None

    @field_validator("queued_at", "last_attempt_at", "not_before")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class UploadOp(_Operation):
    """Upload the take's package through ``/api/v1/intake`` and wait for its receipt."""

    kind: Literal["upload"] = "upload"
    #: The PC has the whole package but has not published it yet (it defers a take whose
    #: catalog version it does not hold): its receipt is collected by a later try.
    awaiting_receipt: bool = False


class _Change(_Operation):
    """A decision or trim request: its id (made once, the same on every retry) and the
    revision it expects (chained: a second queued change expects the first's + 1)."""

    request_id: RequestId
    expected_revision: int = Field(ge=0)
    #: When the PC first answered it (it is then only polled until it is decided).
    submitted_at: datetime | None = None
    #: How often the take's upload was checked again because the PC had no record of it.
    rechecks: int = Field(default=0, ge=0)
    #: How often it was submitted again because the PC had lost a request it had answered.
    resubmits: int = Field(default=0, ge=0)

    @field_validator("submitted_at")
    @classmethod
    def _submitted(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class DecisionOp(_Change):
    kind: Literal["decision"] = "decision"
    decision: TakeDecisionName
    reason: _Text | None

    @model_validator(mode="after")
    def _rule(self) -> DecisionOp:
        check_decision(self.decision, self.reason)
        return self

    def body(self) -> TakeDecisionRequest:
        return TakeDecisionRequest(
            request_id=self.request_id,
            decision=self.decision,
            reason=self.reason,
            expected_revision=self.expected_revision,
        )


class TrimOp(_Change):
    kind: Literal["trim"] = "trim"
    start_frame: int | None = Field(ge=0, lt=MAX_TAKE_FRAMES)
    end_frame: int | None = Field(gt=0, le=MAX_TAKE_FRAMES)

    @model_validator(mode="after")
    def _rule(self) -> TrimOp:
        check_trim(self.start_frame, self.end_frame)
        return self

    def body(self) -> TakeTrimRequest:
        return TakeTrimRequest(
            request_id=self.request_id,
            start_frame=self.start_frame,
            end_frame=self.end_frame,
            expected_revision=self.expected_revision,
        )


type Operation = Annotated[UploadOp | DecisionOp | TrimOp, Field(discriminator="kind")]
type ChangeOp = DecisionOp | TrimOp


class Finished(ContractModel):
    """One operation that left the outbox: sent (``accepted``, ``applied``), ``refused`` by
    the PC, or ``dropped`` after a refusal of an earlier one."""

    kind: OperationKind
    request_id: RequestId | None
    result: Literal["accepted", "applied", "refused", "dropped"]
    code: _Text | None = None
    revision: int | None = Field(default=None, ge=0)
    at: datetime

    @field_validator("at")
    @classmethod
    def _at(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment


class LocalDecision(ContractModel):
    """The take's decision here and the decision revision the PC last confirmed.

    The decision is ahead of the PC while a change of it waits in the outbox. A rejection
    made here has its reason; one adopted from the PC may have none (its library does not
    serve reasons)."""

    decision: TakeDecisionName
    reason: _Text | None = None
    changed_at: datetime | None = None
    revision: int = Field(default=0, ge=0)

    @field_validator("changed_at")
    @classmethod
    def _changed(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _reason(self) -> LocalDecision:
        if self.reason is not None and self.decision != "rejected":
            raise ValueError("only a rejection has a reason")
        return self


class LocalTrim(ContractModel):
    """The take's trim here (``[start_frame, end_frame)``, both ``None`` for the whole take)
    and the trim revision the PC last confirmed."""

    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, gt=0)
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _rule(self) -> LocalTrim:
        check_trim(self.start_frame, self.end_frame)
        return self


class DecisionEvent(ContractModel):
    """One decision in the take's history here (``source``: made here, adopted from the PC
    after another client changed it, or ``learned`` with a take recorded elsewhere); Restore
    goes back to the last ``kept`` or ``draft`` before a rejection or removal."""

    decision: TakeDecisionName
    at: datetime
    source: Literal["here", "pc", "learned"] = "here"

    @field_validator("at")
    @classmethod
    def _at(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment


class PendingRefresh(ContractModel):
    """The PC refused a change of the take, and its state for the take must be read again
    before anything more is sent for it: the PC is known to be at least at these revisions
    (``reason``: the refusal's code). Until then the state here is left as it was, and the
    operator's changes wait in the outbox."""

    since: datetime
    decision_at_least: int = Field(ge=0)
    trim_at_least: int = Field(ge=0)
    reason: _Text

    @field_validator("since")
    @classmethod
    def _since(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment


class UploadReceipt(ContractModel):
    """The PC's intake receipt for the take package, as the upload checked it."""

    manifest_sha256: Sha256
    approval_id: _Text
    server: _Text
    accepted_at: datetime


class TakeSync(ContractModel):
    """``takes/<take_id>/state.json``: the take's state here and its outbox, in one file so
    that each change is one atomic write.

    ``outbox`` is the ordered list of what is still to send: the upload first (while it is
    queued), then each decision and trim in the order they were made. ``sent``, ``refused``
    and ``dropped`` count the operations that left it; ``journal`` keeps the last few."""

    schema_version: Literal["voice-lab-desktop.corpus-take-sync/v1"] = SYNC_SCHEMA
    take_id: TakeId
    decision: LocalDecision
    trim: LocalTrim
    history: tuple[DecisionEvent, ...] = ()
    upload: UploadState
    receipt: UploadReceipt | None = None
    #: The PC's state must be read again before more is sent (``PendingRefresh``).
    refresh: PendingRefresh | None = None
    outbox: tuple[Operation, ...] = ()
    sent: int = Field(default=0, ge=0)
    refused: int = Field(default=0, ge=0)
    dropped: int = Field(default=0, ge=0)
    journal: tuple[Finished, ...] = Field(default=(), max_length=JOURNAL_LIMIT)

    @model_validator(mode="after")
    def _consistent(self) -> TakeSync:
        uploads = [index for index, op in enumerate(self.outbox) if op.kind == "upload"]
        if (self.upload == "accepted") != (self.receipt is not None):
            raise ValueError("an accepted upload, and only one, has its receipt")
        if self.upload in ("local-only", "refused", "gone") and self.outbox:
            raise ValueError("nothing is queued for a take that is never sent")
        if self.upload in ("remote", "gone") and (uploads or self.receipt is not None):
            raise ValueError("a take recorded elsewhere is never uploaded from here")
        if self.upload == "queued" and uploads != [0]:
            raise ValueError("a queued upload is the first operation of its take")
        if self.upload == "accepted" and uploads:
            raise ValueError("an accepted take is not uploaded again")
        ids = [op.request_id for op in self.outbox if not isinstance(op, UploadOp)]
        if len(ids) != len(set(ids)):
            raise ValueError("each queued change has its own request id")
        return self

    @property
    def changes(self) -> tuple[ChangeOp, ...]:
        return tuple(op for op in self.outbox if not isinstance(op, UploadOp))


class PendingRecording(ContractModel):
    """``recording/rec-….json``, beside a recording directory: what the take being recorded
    there is of (the prompt and the catalog version it was bound to when it started). It
    lets a take the worker finished, but that a crash kept from being saved, be saved at the
    next start."""

    schema_version: Literal["voice-lab-desktop.corpus-pending-take/v1"] = PENDING_SCHEMA
    prompt: Prompt
    catalog: CatalogReference
    begun_at: datetime

    @field_validator("begun_at")
    @classmethod
    def _begun(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment


class Notice(ContractModel):
    """Something the UI should tell the operator about a take (``changed-elsewhere``: the
    PC's state was adopted after another client changed the take)."""

    notice_id: NoticeId
    take_id: TakeId
    prompt_id: _Text
    kind: NoticeKind
    text: _Message
    at: datetime

    @field_validator("at")
    @classmethod
    def _at(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment


class Notices(ContractModel):
    """``notices.json``: the notices not dismissed yet, oldest first."""

    schema_version: Literal["voice-lab-desktop.corpus-notices/v1"] = NOTICES_SCHEMA
    notices: tuple[Notice, ...] = Field(default=(), max_length=NOTICE_LIMIT)


class SyncRecord(ContractModel):
    """``sync.json``: when the client last reached the PC, and what went wrong last."""

    schema_version: Literal["voice-lab-desktop.corpus-sync/v1"] = SYNC_RECORD_SCHEMA
    last_sync_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: _Message | None = None

    @field_validator("last_sync_at", "last_attempt_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class CatalogCacheState(ContractModel):
    """``catalog/state.json``: the PC's active catalog version as last seen (its snapshot
    digest; the view is ``catalog/views/<digest>.json``), and when the PC last answered."""

    schema_version: Literal["voice-lab-desktop.corpus-catalog-cache/v1"] = CATALOG_STATE_SCHEMA
    latest: Sha256 | None = None
    checked_at: datetime | None = None
    #: The PC answered that no catalog is active there (``catalog_unavailable``).
    pc_unavailable: bool = False

    @field_validator("checked_at")
    @classmethod
    def _checked(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class CachedLibrary(ContractModel):
    """``library/library.json``: the PC's ``CorpusLibrary`` of its active catalog version as
    last read, with its ``ETag`` (``tag``, the PC's read-model digest) and when it was read.
    It names prompts by id and digest, with their status and take counts: no audio and no
    prompt text."""

    schema_version: Literal["voice-lab-desktop.corpus-library-cache/v1"] = LIBRARY_CACHE_SCHEMA
    tag: Sha256
    fetched_at: datetime
    library: CorpusLibrary

    @field_validator("fetched_at")
    @classmethod
    def _fetched(cls, value: datetime) -> datetime:
        moment = _utc(value)
        assert moment is not None
        return moment
