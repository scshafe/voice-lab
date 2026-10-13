"""Decisions and trims of the PC's takes, as requests the one lab writer applies (D-052).

@canonical corpus_requests -- the network-facing API cannot write the corpus (its lab mount is
read-only except ``incoming/``, D-021), so a change to a take travels as a request
(docs/design/RECORDING-V1.md §3, step 4):

1. The API validates the change (``CorpusRequest``, ``cole-voice-lab.corpus-request/v1``)
   and writes it, canonical, to ``incoming/.corpus-request-tmp-<hex>`` (``O_EXCL``, 0600,
   fsynced), then renames it to ``incoming/.corpus-request-<received stamp>-<request_id>.json``
   (``queue_request``). The stamp is the receive time in UTC to the nanosecond, made later
   than every request still queued, so the names' order is the order requests arrived in. The
   rename fires the intake path unit; the package loop skips dot entries.
2. The intake publisher applies every queued request in name order (``apply_requests``). It
   reads each file bounded, without following a link and without blocking on a FIFO, and
   requires it canonical and named for its request. A change applies only when its
   ``expected_revision`` is the take's current revision of that kind (its decision or its
   trim); the record (``corpus_take_store``) is rewritten with the next revision. Nothing is
   ever deleted: ``removed`` is a decision like any other, and raw audio is untouched. The
   path unit does not start a pass for what changes while one runs, so the pass lists
   ``incoming/`` again until nothing new arrived.
3. The publisher writes the outcome (``CorpusRequestOutcome``,
   ``cole-voice-lab.corpus-request-outcome/v1``) to
   ``provenance/corpus-requests/<request_id>.json``, then the take's record, then removes
   the request file. The outcome is the commit point: a pass interrupted after it finishes the
   record from the outcome on the next pass, so a request is applied exactly once.

A request whose take has no record yet waits in ``incoming/`` while the take's package is
still uploading or waiting there (deferred), and so does every later request of that take, so
a take's requests apply in the order they were sent. A request whose take the PC never
recorded and is not on its way is refused ``take_not_found``. A request file that cannot be
read safely is set aside in ``incoming/.refused/`` with an ``invalid_request`` outcome (no
take, no change) unless its id already has one.

Writers: the API writes request files only (``queue_request``); the intake publisher writes
outcomes and take records, and removes request files it has decided.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from .canonical import (
    canonical_json_bytes,
    fsync_directory,
    open_regular_no_follow,
    rename_no_replace,
    write_canonical_json,
    write_new_private_file,
)
from .corpus_take_store import (
    CorpusTakeState,
    TakeDecision,
    TakeDecisionName,
    TakeTrim,
    read_take_state,
    state_path,
)
from .corpus_takes import MAX_TAKE_FRAMES, TakeId
from .errors import ContractError, SecurityError
from .models import ContractModel, utc_now
from .paths import AssetLayout

REQUEST_SCHEMA: Final = "cole-voice-lab.corpus-request/v1"
OUTCOME_SCHEMA: Final = "cole-voice-lab.corpus-request-outcome/v1"
OUTCOME_DIRECTORY: Final = "corpus-requests"
REQUEST_PREFIX: Final = ".corpus-request-"
TEMPORARY_PREFIX: Final = ".corpus-request-tmp-"
REQUEST_ID_PATTERN: Final = r"^req-[0-9a-f]{32}$"
#: The most requests that may wait in ``incoming/``; the API refuses more (503).
MAX_QUEUED_REQUESTS: Final = 10_000
#: How many times one call of ``apply_requests`` lists ``incoming/`` for new requests.
MAX_LISTINGS: Final = 100
MAX_REQUEST_BYTES: Final = 16 * 1024
MAX_OUTCOME_BYTES: Final = 32 * 1024
#: The upload staging of ``api.staging`` (``incoming/.uploads/<session_id>``, marked
#: ``FINALIZED`` once the package moved to ``incoming/<session_id>``, and kept); a take's
#: upload session is its take id.
UPLOADS_NAME: Final = ".uploads"
FINALIZED_NAME: Final = "FINALIZED"
_REQUEST_NAME: Final = re.compile(
    r"\.corpus-request-(?P<stamp>[0-9]{8}T[0-9]{15}Z)-(?P<request_id>req-[0-9a-f]{32})\.json"
)
_STAMP_SECONDS: Final = "%Y%m%dT%H%M%S"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

type RequestId = Annotated[str, StringConstraints(pattern=REQUEST_ID_PATTERN)]
type _Reason = Annotated[str, StringConstraints(min_length=1, max_length=500)]
#: Why the publisher refused a request (``CorpusRequestOutcome.code``).
type RequestRefusalCode = Literal[
    "revision_conflict", "take_not_found", "trim_out_of_bounds", "invalid_request"
]
type RequestStatus = Literal["applied", "refused"]


def _utc(value: datetime | None, what: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} requires an explicit timezone")
    return value.astimezone(UTC)


def check_decision(decision: str, reason: str | None) -> None:
    """``TakeDecision``'s rule for a change: a rejection, and only a rejection, has a reason.

    Raises: ``ValueError`` when it does not hold."""
    if (decision == "rejected") != (reason is not None):
        raise ValueError("a rejection, and only a rejection, has a reason")


def check_trim(start_frame: int | None, end_frame: int | None) -> None:
    """``TakeTrim``'s rule for a change: both ends or neither, and at least one frame kept.

    Raises: ``ValueError`` when it does not hold."""
    if (start_frame is None) != (end_frame is None):
        raise ValueError("a trim has both ends or neither")
    if start_frame is not None and end_frame is not None and start_frame >= end_frame:
        raise ValueError("a trim keeps at least one frame")


class DecisionChange(ContractModel):
    """Set the take's decision, if its decision revision is still ``expected_revision``.

    ``kept``, ``rejected`` (with a reason) and ``removed`` are the operator's actions; a
    restore sets the decision the take had before (``kept``, or ``draft`` for a take never
    kept). ``removed`` is a state, never a deletion."""

    kind: Literal["decision"] = "decision"
    decision: TakeDecisionName
    reason: _Reason | None
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> DecisionChange:
        check_decision(self.decision, self.reason)
        return self


class TrimChange(ContractModel):
    """Keep the frames ``[start_frame, end_frame)`` of the take, or all of them (both
    ``None``: a reset), if its trim revision is still ``expected_revision``."""

    kind: Literal["trim"] = "trim"
    start_frame: int | None = Field(ge=0, lt=MAX_TAKE_FRAMES)
    end_frame: int | None = Field(gt=0, le=MAX_TAKE_FRAMES)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> TrimChange:
        check_trim(self.start_frame, self.end_frame)
        return self


type CorpusChange = Annotated[DecisionChange | TrimChange, Field(discriminator="kind")]


class CorpusRequest(ContractModel):
    """One change to one take, as the API queued it: ``incoming/.corpus-request-…json``.

    ``request_id`` is the client's own id for the change (``req-`` and 32 lower-case hex
    digits), stable across its retries; ``received_at`` is when the API received it."""

    schema_version: Literal["cole-voice-lab.corpus-request/v1"] = REQUEST_SCHEMA
    request_id: RequestId
    take_id: TakeId
    received_at: datetime
    change: CorpusChange

    @field_validator("received_at")
    @classmethod
    def _received_utc(cls, value: datetime) -> datetime:
        received = _utc(value, "a request's receive time")
        assert received is not None
        return received


class CorpusRequestOutcome(ContractModel):
    """What the publisher did with a request: ``provenance/corpus-requests/<id>.json``.

    ``revision`` is the take's revision of the change's kind after the request: the new one
    when applied, the current one, unchanged, when refused once the take was read (a
    ``revision_conflict`` names the revision the client should have expected), and ``None``
    otherwise. ``decided_at`` is the publisher's time; an applied decision has it as its own.
    A request file that could not be read is refused ``invalid_request`` with no take, change
    or receive time."""

    schema_version: Literal["cole-voice-lab.corpus-request-outcome/v1"] = OUTCOME_SCHEMA
    request_id: RequestId
    take_id: TakeId | None
    change: CorpusChange | None
    received_at: datetime | None
    status: RequestStatus
    code: RequestRefusalCode | None
    detail: str | None = Field(max_length=2000)
    revision: int | None = Field(ge=0)
    decided_at: datetime

    @field_validator("received_at", "decided_at")
    @classmethod
    def _times_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value, "an outcome's times")

    @model_validator(mode="after")
    def _consistent(self) -> CorpusRequestOutcome:
        unread = self.take_id is None
        if unread != (self.change is None) or unread != (self.received_at is None):
            raise ValueError("an outcome names its take, change and receive time, or none")
        if unread and self.code != "invalid_request":
            raise ValueError("only an unreadable request is without its take")
        if self.status == "applied":
            if self.code is not None or self.detail is not None:
                raise ValueError("an applied request has no refusal")
            if unread or self.revision is None or self.revision < 1:
                raise ValueError("an applied request names its take and the revision it made")
        elif self.code is None or self.detail is None:
            raise ValueError("a refused request says why")
        return self

    def answers(self, take_id: str, change: DecisionChange | TrimChange) -> bool:
        """Whether this outcome is of the same request: the same take and change (an
        unreadable request, whose body is unknown, answers any body of its id)."""
        return self.take_id is None or (self.take_id == take_id and self.change == change)


# -- the queue (incoming/) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueuedName:
    """A request file waiting in ``incoming/``: its name, and its stamp in nanoseconds."""

    name: str
    request_id: str
    stamp_ns: int


def _stamp(nanoseconds: int) -> str:
    seconds, fraction = divmod(nanoseconds, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, UTC)
    return f"{moment.strftime(_STAMP_SECONDS)}{fraction:09d}Z"


def _stamp_ns(stamp: str) -> int:
    moment = datetime.strptime(stamp[:15], _STAMP_SECONDS).replace(tzinfo=UTC)
    return nanoseconds(moment) + int(stamp[15:24])


def nanoseconds(moment: datetime) -> int:
    """``moment`` in nanoseconds since the epoch (exact to its microseconds)."""
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def request_name(request_id: str, stamp_ns: int) -> str:
    """The queued file's name: ``.corpus-request-<UTC stamp, ns>-<request_id>.json``."""
    return f"{REQUEST_PREFIX}{_stamp(stamp_ns)}-{request_id}.json"


def parse_request_name(name: str) -> QueuedName | None:
    """The queued request a directory entry is, or ``None`` for any other entry (a temporary
    included)."""
    match = _REQUEST_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        stamp_ns = _stamp_ns(match["stamp"])
    except ValueError:
        return None
    return QueuedName(name=name, request_id=match["request_id"], stamp_ns=stamp_ns)


def queued_requests(layout: AssetLayout) -> tuple[QueuedName, ...]:
    """Every request file waiting in ``incoming/``, in the order the publisher applies them.

    Raises: ``OSError`` when ``incoming/`` cannot be listed."""
    found = (parse_request_name(name) for name in os.listdir(layout.directory("incoming")))
    return tuple(sorted((entry for entry in found if entry is not None), key=lambda e: e.name))


def read_request_file(path: Path) -> CorpusRequest:
    """One queued request: a regular single-link file within its bound, opened without
    following a link or blocking on a FIFO, holding exactly its canonical JSON.

    Raises: ``FileNotFoundError`` when it is gone; ``SecurityError`` for a link, a FIFO, a
    device, a directory or a hard-linked file; ``ContractError`` when it is too large, not a
    valid ``CorpusRequest`` or not canonical; ``OSError`` when it cannot be read."""
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SecurityError(f"a corpus request is not a plain regular file: {path.name}")
    if metadata.st_size > MAX_REQUEST_BYTES:
        raise ContractError(f"a corpus request is larger than {MAX_REQUEST_BYTES} bytes")
    with open_regular_no_follow(path, nonblocking=True) as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SecurityError(f"a corpus request changed while it was opened: {path.name}")
        payload = stream.read(MAX_REQUEST_BYTES + 1)
    if len(payload) > MAX_REQUEST_BYTES:
        raise ContractError(f"a corpus request is larger than {MAX_REQUEST_BYTES} bytes")
    try:
        request = CorpusRequest.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"not a valid corpus request: {error}") from error
    if payload != canonical_json_bytes(request) + b"\n":
        raise ContractError("a corpus request is not canonical JSON")
    return request


def queue_request(layout: AssetLayout, request: CorpusRequest, stamp_ns: int) -> str:
    """Write ``request`` into ``incoming/`` under the name its ``stamp_ns`` orders (the API).

    The canonical bytes go to a new private temporary (``.corpus-request-tmp-<hex>``,
    ``O_EXCL``, 0600, fsynced), which is renamed, without replacing anything, to the request's
    name; the directory is fsynced. The publisher never takes a temporary.
    Returns: the request file's name.
    Raises: ``FileExistsError`` when that name exists; ``OSError`` when it cannot be written
    (the temporary is removed)."""
    incoming = layout.directory("incoming")
    name = request_name(request.request_id, stamp_ns)
    temporary = incoming / f"{TEMPORARY_PREFIX}{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(canonical_json_bytes(request) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        rename_no_replace(temporary, incoming / name)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    fsync_directory(incoming)
    return name


def take_on_its_way(layout: AssetLayout, take_id: str) -> bool:
    """Whether the take's package waits for the publisher (``incoming/<take_id>``: finalized,
    or deferred) or is still uploading (``incoming/.uploads/<take_id>``, not finalized: a
    finalized session's directory stays after its package moved on, or was refused)."""
    incoming = layout.directory("incoming")
    session = incoming / UPLOADS_NAME / take_id
    return os.path.lexists(incoming / take_id) or (
        os.path.lexists(session) and not os.path.lexists(session / FINALIZED_NAME)
    )


# -- outcomes (provenance/corpus-requests/) -----------------------------------------------------


def outcomes_directory(layout: AssetLayout) -> Path:
    return layout.directory("provenance") / OUTCOME_DIRECTORY


def outcome_path(layout: AssetLayout, request_id: str) -> Path:
    """Raises: ``ValueError`` for a string that is not a request id."""
    if not re.fullmatch(REQUEST_ID_PATTERN, request_id):
        raise ValueError(f"not a request id: {request_id!r}")
    return outcomes_directory(layout) / f"{request_id}.json"


def read_outcome(layout: AssetLayout, request_id: str) -> CorpusRequestOutcome | None:
    """The request's outcome, or ``None`` while it has none.

    Raises: ``ValueError`` for a string that is not a request id; ``SecurityError`` for a
    link or a hard-linked file; ``ContractError`` for a record that is too large, not
    canonical or of another request."""
    path = outcome_path(layout, request_id)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SecurityError(f"a request outcome is not a plain regular file: {path}")
    if metadata.st_size > MAX_OUTCOME_BYTES:
        raise ContractError(f"a request outcome is larger than {MAX_OUTCOME_BYTES} bytes")
    with open_regular_no_follow(path, nonblocking=True) as stream:
        payload = stream.read(MAX_OUTCOME_BYTES + 1)
    try:
        outcome = CorpusRequestOutcome.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"invalid request outcome {path}: {error}") from error
    if payload != canonical_json_bytes(outcome) + b"\n" or outcome.request_id != request_id:
        raise ContractError(f"the request outcome is not this request's canonical record: {path}")
    return outcome


# -- the publisher's pass ---------------------------------------------------------------------


@dataclass(slots=True)
class RequestPass:
    """What one pass did with the queued requests.

    ``applied`` and ``refused`` hold the requests decided now (``{"request", "take", "code"}``
    for a refusal), ``waiting`` those left queued (``{"request", "take", "reason"}``), and
    ``updated`` the take records the pass rewrote. ``seen`` names the request files the pass
    handled, and ``held`` the takes whose request waits: their later requests wait too."""

    applied: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    waiting: list[dict[str, str]] = field(default_factory=list)
    updated: dict[str, CorpusTakeState] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    held: set[str] = field(default_factory=set)

    def wait(self, request_id: str, take_id: str, reason: str) -> None:
        if take_id:
            self.held.add(take_id)
        self.waiting.append({"request": request_id, "take": take_id, "reason": reason[:2000]})


def _refused(
    request: CorpusRequest,
    code: RequestRefusalCode,
    detail: str,
    revision: int | None,
    now: datetime,
) -> CorpusRequestOutcome:
    return CorpusRequestOutcome(
        request_id=request.request_id,
        take_id=request.take_id,
        change=request.change,
        received_at=request.received_at,
        status="refused",
        code=code,
        detail=detail[:2000],
        revision=revision,
        decided_at=now,
    )


def _changed(
    state: CorpusTakeState, change: DecisionChange | TrimChange, revision: int, at: datetime
) -> CorpusTakeState:
    """``state`` with ``change`` applied at ``revision``.

    Raises: ``ValidationError`` when the record's rules refuse the result."""
    if isinstance(change, DecisionChange):
        update: dict[str, object] = {
            "decision": TakeDecision(
                decision=change.decision, revision=revision, reason=change.reason, decided_at=at
            )
        }
    else:
        update = {
            "trim": TakeTrim(
                revision=revision, start_frame=change.start_frame, end_frame=change.end_frame
            )
        }
    return CorpusTakeState.model_validate({**dict(state), **update})


def _revision(state: CorpusTakeState, change: DecisionChange | TrimChange) -> int:
    return state.decision.revision if isinstance(change, DecisionChange) else state.trim.revision


def judge(
    state: CorpusTakeState, request: CorpusRequest, now: datetime
) -> tuple[CorpusRequestOutcome, CorpusTakeState | None]:
    """The outcome of ``request`` against the take's current ``state``, and the new record
    when it applies (``None`` when refused). Raises: nothing for a validated request."""
    change = request.change
    current = _revision(state, change)
    if change.expected_revision != current:
        kind = "decision" if isinstance(change, DecisionChange) else "trim"
        detail = f"the take's {kind} is at revision {current}, not {change.expected_revision}"
        return _refused(request, "revision_conflict", detail, current, now), None
    frames = state.audio.audio.frames
    if (
        isinstance(change, TrimChange)
        and change.end_frame is not None
        and change.end_frame > frames
    ):
        detail = f"the take has {frames} frames; the trim ends at {change.end_frame}"
        return _refused(request, "trim_out_of_bounds", detail, current, now), None
    # A change holds the record's own rules (``check_decision``, ``check_trim``), and its end
    # is within the take now, so the new record is valid.
    updated = _changed(state, change, current + 1, now)
    applied = CorpusRequestOutcome(
        request_id=request.request_id,
        take_id=request.take_id,
        change=change,
        received_at=request.received_at,
        status="applied",
        code=None,
        detail=None,
        revision=current + 1,
        decided_at=now,
    )
    return applied, updated


def _write_outcome(layout: AssetLayout, outcome: CorpusRequestOutcome) -> None:
    directory = outcomes_directory(layout)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = canonical_json_bytes(outcome) + b"\n"
    write_new_private_file(outcome_path(layout, outcome.request_id), payload)


def _remove(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    fsync_directory(path.parent)


def _finish(layout: AssetLayout, outcome: CorpusRequestOutcome) -> CorpusTakeState | None:
    """Make the take's record hold an applied ``outcome`` that an interrupted pass wrote
    before the record: rewrite it at the outcome's revision unless it is already there.

    Returns: the record written now, or ``None``.
    Raises: ``ContractError`` when the take has no record; as ``read_take_state``;
    ``ValidationError``; ``OSError``."""
    assert outcome.take_id is not None and outcome.change is not None
    assert outcome.revision is not None
    state = read_take_state(layout, outcome.take_id)
    if state is None:
        raise ContractError(f"the take of an applied request has no record: {outcome.take_id}")
    if _revision(state, outcome.change) >= outcome.revision:
        return None
    updated = _changed(state, outcome.change, outcome.revision, outcome.decided_at)
    write_canonical_json(state_path(layout, outcome.take_id), updated)
    return updated


def _set_aside(
    layout: AssetLayout,
    entry: QueuedName,
    error: Exception,
    refused_root: Callable[[], Path],
    now: datetime,
    result: RequestPass,
    take_id: str = "",
) -> None:
    """Move an unreadable request file to ``refused_root`` and give its id an outcome, unless
    it has one. Raises: ``OSError``, ``SecurityError`` and ``ContractError`` (the outcome)."""
    incoming = layout.directory("incoming")
    refused = refused_root()
    target = f"{entry.name.removeprefix('.')}.{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    rename_no_replace(incoming / entry.name, refused / target)
    fsync_directory(refused)
    fsync_directory(incoming)
    detail = f"the request file was set aside as incoming/{refused.name}/{target}: {error}"
    if read_outcome(layout, entry.request_id) is None:
        _write_outcome(
            layout,
            CorpusRequestOutcome(
                request_id=entry.request_id,
                take_id=None,
                change=None,
                received_at=None,
                status="refused",
                code="invalid_request",
                detail=detail[:2000],
                revision=None,
                decided_at=now,
            ),
        )
    result.refused.append({"request": entry.request_id, "take": take_id, "code": "invalid_request"})


def apply_requests(
    layout: AssetLayout,
    *,
    refused_root: Callable[[], Path],
    records_complete: bool = True,
    clock: Callable[[], datetime] = utc_now,
    into: RequestPass | None = None,
) -> RequestPass:
    """Apply every request queued in ``incoming/``, in name order, once (the publisher).

    ``refused_root`` gives the directory an unreadable request file is set aside in (the
    publisher's ``incoming/.refused``, made when first needed). ``records_complete`` says
    whether the pass recorded every take in ``raw/``; when it did not, a request whose take
    has no record waits rather than being refused. A failure of one request (an ``OSError``,
    a damaged record) leaves it and every later request of its take queued for the next pass,
    and never stops the others. Requests that arrive meanwhile are applied too: ``incoming/``
    is listed again until it shows nothing new (at most ``MAX_LISTINGS`` times), since the
    path unit does not start a pass for what changes while one runs. ``into`` continues an
    earlier call of the same pass: what it handled is not handled again, and what waits there
    holds the later requests of its take.
    Returns: what the pass did (``into`` when given).
    Raises: ``OSError`` when ``incoming/`` cannot be listed."""
    result = into if into is not None else RequestPass()
    for _listing in range(MAX_LISTINGS):
        fresh = [entry for entry in queued_requests(layout) if entry.name not in result.seen]
        if not fresh:
            break
        for entry in fresh:
            result.seen.add(entry.name)
            _apply(layout, entry, refused_root, records_complete, clock(), result)
    return result


def _apply(
    layout: AssetLayout,
    entry: QueuedName,
    refused_root: Callable[[], Path],
    records_complete: bool,
    now: datetime,
    result: RequestPass,
) -> None:
    """Judge one queued request and record what became of it in ``result``."""
    incoming = layout.directory("incoming")
    try:
        request = read_request_file(incoming / entry.name)
        if request.request_id != entry.request_id:
            raise ContractError("the request file is not named for its request")
    except FileNotFoundError:
        return
    except (ContractError, SecurityError) as error:
        try:
            _set_aside(layout, entry, error, refused_root, now, result)
        except (OSError, ContractError, SecurityError) as failure:
            result.wait(entry.request_id, "", str(failure))
        return
    except OSError as error:
        result.wait(entry.request_id, "", str(error))
        return
    take_id = request.take_id
    if take_id in result.held:
        result.wait(request.request_id, take_id, "an earlier request of the take waits")
        return
    try:
        existing = read_outcome(layout, request.request_id)
        if existing is not None:
            _settle(layout, entry, request, existing, refused_root, now, result)
            return
        state = read_take_state(layout, take_id)
        if state is None:
            if not records_complete or take_on_its_way(layout, take_id):
                result.wait(request.request_id, take_id, "the take is not recorded on the PC yet")
                return
            outcome = _refused(
                request, "take_not_found", "the PC has no record of the take", None, now
            )
            updated = None
        else:
            outcome, updated = judge(state, request, now)
        _write_outcome(layout, outcome)  # the commit point
        if updated is not None:
            write_canonical_json(state_path(layout, take_id), updated)
            result.updated[take_id] = updated
        _remove(incoming / entry.name)
    except (OSError, ContractError, SecurityError, ValidationError) as error:
        result.wait(request.request_id, take_id, str(error))
        return
    if outcome.status == "applied":
        result.applied.append(request.request_id)
    else:
        assert outcome.code is not None
        result.refused.append(
            {"request": request.request_id, "take": take_id, "code": outcome.code}
        )


def _settle(
    layout: AssetLayout,
    entry: QueuedName,
    request: CorpusRequest,
    outcome: CorpusRequestOutcome,
    refused_root: Callable[[], Path],
    now: datetime,
    result: RequestPass,
) -> None:
    """A queued request whose id already has an outcome: an interrupted pass's, finished now
    (the record brought to an applied outcome's revision, the file removed), or another
    request under a spent id, set aside without touching the outcome.

    Raises: as ``_finish``, ``_set_aside`` and ``_remove``."""
    if outcome.take_id is None or not outcome.answers(request.take_id, request.change):
        error = ContractError("another request already has this id")
        _set_aside(layout, entry, error, refused_root, now, result, request.take_id)
        return
    if outcome.status == "applied":
        updated = _finish(layout, outcome)
        if updated is not None:
            result.updated[request.take_id] = updated
    _remove(layout.directory("incoming") / entry.name)
    if outcome.status == "applied":
        result.applied.append(request.request_id)
    else:
        assert outcome.code is not None
        result.refused.append(
            {"request": request.request_id, "take": request.take_id, "code": outcome.code}
        )
