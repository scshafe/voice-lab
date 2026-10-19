"""The PC's take store (D-052, docs/design/RECORDING-V1.md §4): each take's current state.

@canonical corpus_take_store -- ``provenance/corpus-takes/<take_id>.json``: one
``CorpusTakeState`` (``cole-voice-lab.corpus-take-state/v1``) per take package published into
``raw/``. It holds what the take is, copied from its immutable raw package so that the read
model never opens ``raw/`` (the exact prompt, the catalog version, the raw package, the audio,
intake's QC, the host and device), and what was decided about it: its decision and its trim,
each with its own revision.

Writer: the intake publisher only (the PC's one lab writer). ``record_takes`` runs on every
pass and writes the first record of every take package in ``raw/`` that has none: of each
take the pass accepted, and of any take an interrupted pass left without one. A take's first
record is an undecided, untrimmed draft at revision 0 (``initial_state``); a v2 take's (D-056)
also summarizes its capture profile (``capture_profile``). An imported Swift
take's (``corpus_take_imports``, D-053) carries the Mac's decision and trim instead, each at
revision 1 when it is not the draft or the whole take, and says where it came from
(``origin``; ``imported_state``). Either is built from the raw package alone, so the store's
first records are rebuildable from ``raw/``. Decisions and trims (``corpus_requests``, step 4)
are the only later writes; they raise their revision and cannot be rebuilt, so the vault
copies these records (``library_classification``: ``corpus-take-states``) and the requests'
outcomes.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .canonical import read_canonical_model, write_canonical_json
from .capture_profiles import XVF3800_SAMPLE_RATE_HZ, RoomSlug
from .corpus_take_imports import ValidatedTakeImport, validate_take_import_package
from .corpus_takes import (
    TAKE_ID_PATTERN,
    TAKE_IMPORT_SCHEMA,
    TAKE_SCHEMA,
    TAKE_V2_SCHEMA,
    ProfileTakeAudio,
    TakeAudio,
    TakeId,
    ValidatedProfileTake,
    ValidatedTake,
    package_schema,
    take_id_for,
    validate_profile_take_package,
    validate_take_package,
)
from .errors import ContractError, SecurityError
from .intake import MAX_CONTROL_FILE_BYTES, RECEIPT_NAME, IntakeReceipt
from .models import ContractModel, Prompt, Sha256, StableId
from .paths import AssetLayout
from .recording_contracts import CatalogReference

STATE_SCHEMA: Final = "cole-voice-lab.corpus-take-state/v1"
STATE_DIRECTORY: Final = "corpus-takes"
MAX_STATE_BYTES: Final = 64 * 1024

type TakeDecisionName = Literal["draft", "kept", "rejected", "removed"]
type _Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type _Finding = Annotated[str, StringConstraints(min_length=1, max_length=120)]
type _Byte = Annotated[int, Field(ge=0, le=255)]


def _utc(value: datetime, what: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} requires an explicit timezone")
    return value.astimezone(UTC)


class TakeDecision(ContractModel):
    """What the operator decided about a take, at which revision (0: nothing yet, a draft).

    ``reason`` is a rejection's, and only a rejection's; every decision after revision 0 says
    when it was made."""

    decision: TakeDecisionName
    revision: int = Field(ge=0)
    reason: _Text | None
    decided_at: datetime | None

    @field_validator("decided_at")
    @classmethod
    def _decided_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, "a decision's time")

    @model_validator(mode="after")
    def _consistent(self) -> TakeDecision:
        if self.revision == 0:
            if self.decision != "draft" or self.reason is not None or self.decided_at is not None:
                raise ValueError("revision 0 is an undecided draft")
            return self
        if self.decided_at is None:
            raise ValueError("a decision says when it was made")
        if (self.decision == "rejected") != (self.reason is not None):
            raise ValueError("a rejection, and only a rejection, has a reason")
        return self


class TakeTrim(ContractModel):
    """The frames a take keeps, ``[start_frame, end_frame)``, or all of them (both ``None``),
    at which revision (0: never trimmed)."""

    revision: int = Field(ge=0)
    start_frame: int | None = Field(ge=0)
    end_frame: int | None = Field(gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> TakeTrim:
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("a trim has both ends or neither")
        if self.start_frame is not None and self.end_frame is not None:
            if self.start_frame >= self.end_frame:
                raise ValueError("a trim keeps at least one frame")
            if self.revision == 0:
                raise ValueError("revision 0 is untrimmed")
        return self


class TakeOrigin(ContractModel):
    """Where a take the PC did not receive from a recording client came from.

    ``imported``: a take of the Swift app on the Mac (D-053), whose own id was
    ``source_take_id``; its raw package keeps its seal, reservation, group and the Mac's
    decision and trim as they were exported."""

    kind: Literal["imported"]
    source_take_id: TakeId


class TakeProfileSummary(ContractModel):
    """A v2 take's capture profile, as its record shows it (D-056): enough to tell the takes
    of one device and room from another's without opening ``raw/``."""

    profile: Literal["xvf3800-linear-4"]
    room: RoomSlug
    device_model: _Text
    #: The firmware's ``VERSION`` (three bytes).
    firmware_version: tuple[_Byte, _Byte, _Byte]
    #: The WAV channel (1-based) the take is judged by and derived from.
    primary_channel: int = Field(ge=1, le=32)


class CorpusTakeState(ContractModel):
    """One take on the PC: what it is, and its current decision and trim.

    ``origin`` is absent for a take a recording client uploaded (every record written before
    D-053 included, byte for byte), and says ``imported`` for a Swift take.
    ``capture_profile`` is absent for a take of profile ``umik-1`` (every v1 and imported
    take, and so every record written before D-056, byte for byte), whose audio is the
    canonical format; a v2 take's record names its profile, and its audio is the profile's
    (``ProfileTakeAudio``)."""

    schema_version: Literal["cole-voice-lab.corpus-take-state/v1"] = STATE_SCHEMA
    take_id: TakeId
    subject_id: StableId
    #: The exact prompt the take was recorded against, and the catalog version it named.
    prompt: Prompt
    prompt_sha256: Sha256
    catalog: CatalogReference
    manifest_sha256: Sha256
    #: The published package: ``raw/<manifest_sha256>``.
    raw_path: str
    audio: TakeAudio | ProfileTakeAudio
    requested_at: datetime
    accepted_at: datetime
    capture_host: _Text
    #: The capture device's display name.
    device: _Text
    #: Intake's QC of the WAV (``intake-qc.json``).
    qc_status: Literal["pass", "review", "fail"]
    qc_findings: tuple[_Finding, ...]
    decision: TakeDecision
    trim: TakeTrim
    origin: TakeOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    capture_profile: TakeProfileSummary | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("requested_at", "accepted_at")
    @classmethod
    def _times_utc(cls, value: datetime) -> datetime:
        return _utc(value, "a take's times")

    @model_validator(mode="after")
    def _bound(self) -> CorpusTakeState:
        if self.take_id != take_id_for(self.audio.sha256):
            raise ValueError("the take id is take-<the SHA-256 of original.wav>")
        if self.raw_path != f"raw/{self.manifest_sha256}":
            raise ValueError("a take's raw package is raw/<its manifest digest>")
        if self.trim.end_frame is not None and self.trim.end_frame > self.audio.audio.frames:
            raise ValueError("a trim ends within the take")
        spec = self.audio.audio
        if self.capture_profile is None:
            if not isinstance(self.audio, TakeAudio):
                raise ValueError("a take without a capture profile is in the canonical format")
        elif self.origin is not None:
            raise ValueError("an imported take is a umik-1 take, which names no profile")
        elif (
            spec.sample_rate_hz != XVF3800_SAMPLE_RATE_HZ
            or self.capture_profile.primary_channel > spec.channels
        ):
            raise ValueError("a take's audio is in its capture profile's format")
        return self


def states_directory(layout: AssetLayout) -> Path:
    return layout.directory("provenance") / STATE_DIRECTORY


def state_path(layout: AssetLayout, take_id: str) -> Path:
    """Raises: ``ValueError`` for a string that is not a take id."""
    if not re.fullmatch(TAKE_ID_PATTERN, take_id):
        raise ValueError(f"not a take id: {take_id!r}")
    return states_directory(layout) / f"{take_id}.json"


def read_take_state(layout: AssetLayout, take_id: str) -> CorpusTakeState | None:
    """The take's record, or ``None`` when the PC has none.

    Raises: ``ValueError`` for a string that is not a take id; ``SecurityError`` for a link or
    a hard-linked file; ``ContractError`` for a record that is too large, not canonical, or
    of another take."""
    path = state_path(layout, take_id)
    if not os.path.lexists(path):
        return None
    state = read_canonical_model(path, CorpusTakeState, max_bytes=MAX_STATE_BYTES)
    if state.take_id != take_id:
        raise ContractError(f"the take record is not named for its take: {path}")
    return state


def read_take_states(layout: AssetLayout) -> tuple[CorpusTakeState, ...]:
    """Every take's record, by take id; nothing when the store does not exist yet.

    A dot entry (a writer's temporary) is skipped. Raises: as ``read_take_state``, and
    ``ContractError`` for any other entry that is not a take's record."""
    directory = states_directory(layout)
    if not os.path.lexists(directory):
        return ()
    states: list[CorpusTakeState] = []
    for name in sorted(os.listdir(directory)):
        if name.startswith("."):
            continue
        take_id = name.removesuffix(".json")
        if take_id == name or not re.fullmatch(TAKE_ID_PATTERN, take_id):
            raise ContractError(f"unexpected entry in the take store: {directory / name}")
        state = read_take_state(layout, take_id)
        if state is None:
            raise ContractError(f"a take record disappeared while it was read: {directory / name}")
        states.append(state)
    return tuple(states)


_UNDECIDED: Final = TakeDecision(decision="draft", revision=0, reason=None, decided_at=None)
_UNTRIMMED: Final = TakeTrim(revision=0, start_frame=None, end_frame=None)


def _state(
    take: ValidatedTake | ValidatedProfileTake | ValidatedTakeImport,
    receipt: IntakeReceipt,
    *,
    decision: TakeDecision,
    trim: TakeTrim,
    origin: TakeOrigin | None,
) -> CorpusTakeState:
    manifest = take.manifest
    findings = sorted({finding for metric in take.qc_report.metrics for finding in metric.findings})
    profile: TakeProfileSummary | None = None
    if isinstance(take, ValidatedProfileTake):
        captured = take.manifest.capture_profile
        device = captured.device.model
        profile = TakeProfileSummary(
            profile=captured.profile,
            room=captured.room,
            device_model=captured.device.model,
            firmware_version=captured.device.firmware.version,
            primary_channel=captured.primary_channel,
        )
    else:
        device = take.manifest.capture_device.display_name
    return CorpusTakeState(
        take_id=manifest.take_id,
        subject_id=manifest.subject_id,
        prompt=manifest.prompt,
        prompt_sha256=manifest.prompt_sha256,
        catalog=manifest.catalog,
        manifest_sha256=take.manifest_sha256,
        raw_path=receipt.raw_path,
        audio=manifest.audio,
        requested_at=manifest.requested_at,
        accepted_at=receipt.accepted_at,
        capture_host=manifest.capture_host,
        device=device,
        qc_status=take.qc_report.status,
        qc_findings=tuple(findings),
        decision=decision,
        trim=trim,
        origin=origin,
        capture_profile=profile,
    )


def initial_state(
    take: ValidatedTake | ValidatedProfileTake, receipt: IntakeReceipt
) -> CorpusTakeState:
    """A published take's revision-0 record (of either version): an undecided, untrimmed
    draft."""
    return _state(take, receipt, decision=_UNDECIDED, trim=_UNTRIMMED, origin=None)


def imported_state(take: ValidatedTakeImport, receipt: IntakeReceipt) -> CorpusTakeState:
    """A published imported take's first record: the Mac's decision and trim (D-053).

    The decision (``draft``, ``kept``, ``rejected``, ``removed``) is the Mac's, at revision 1
    unless it is ``draft`` (revision 0, undecided, whatever the Mac's revision was). A
    rejection keeps its reason; a removal's stays in the raw package's ``mac-state.json``, since
    only a rejection has one here. It was decided when the Mac's records say, else when the PC
    accepted the take (a Save records no time on the Swift take). The trim is the Mac's
    recipe at revision 1, unless it keeps the whole take (revision 0, untrimmed).
    Raises: ``ValidationError`` only for a package that was never validated."""
    mac = take.mac_state
    decision = _UNDECIDED
    if mac.decision != "draft":
        decision = TakeDecision(
            decision=mac.decision,
            revision=1,
            reason=mac.reason if mac.decision == "rejected" else None,
            decided_at=receipt.accepted_at if mac.decided_at is None else mac.decided_at,
        )
    trim = _UNTRIMMED
    if mac.trimmed:
        trim = TakeTrim(
            revision=1,
            start_frame=mac.frame_range.start_frame,
            end_frame=mac.frame_range.end_frame,
        )
    origin = TakeOrigin(kind="imported", source_take_id=take.manifest.source_take_id)
    return _state(take, receipt, decision=decision, trim=trim, origin=origin)


def record_takes(layout: AssetLayout) -> tuple[tuple[CorpusTakeState, ...], tuple[str, ...]]:
    """Write the first record of every take package in ``raw/`` that has none.

    A raw package that a record already names is not read again; any other is read only as
    far as its manifest's schema, and a take's or an imported take's package is validated in
    full (with its receipt) before its record is written (``initial_state``,
    ``imported_state``). Capture packages are left alone.
    Returns: every take's record, by take id, and the ids recorded now.
    Raises: ``SecurityError`` for a raw take whose directory is not its manifest digest, or a
    take whose record names another raw package; ``ContractError``/``SecurityError`` as
    ``validate_take_package``, ``validate_take_import_package`` and ``read_take_states``;
    ``OSError``.
    """
    states = {state.take_id: state for state in read_take_states(layout)}
    published = {state.manifest_sha256 for state in states.values()}
    recorded: list[str] = []
    raw_root = layout.directory("raw")
    for name in sorted(os.listdir(raw_root)):
        package = raw_root / name
        if name in published or name.startswith("."):
            continue
        schema = package_schema(package)
        take: ValidatedTake | ValidatedProfileTake | ValidatedTakeImport
        if schema == TAKE_SCHEMA:
            take = validate_take_package(package, allow_intake_receipt=True)
        elif schema == TAKE_V2_SCHEMA:
            take = validate_profile_take_package(package, allow_intake_receipt=True)
        elif schema == TAKE_IMPORT_SCHEMA:
            take = validate_take_import_package(package, allow_intake_receipt=True)
        else:
            continue
        if name != take.manifest_sha256:
            raise SecurityError(f"raw take is not content-addressed: {package}")
        existing = states.get(take.manifest.take_id)
        if existing is not None:
            raise SecurityError(
                f"take {take.manifest.take_id} is recorded for raw/{existing.manifest_sha256}, "
                f"not {name}"
            )
        receipt = read_canonical_model(
            package / RECEIPT_NAME, IntakeReceipt, max_bytes=MAX_CONTROL_FILE_BYTES
        )
        state = (
            imported_state(take, receipt)
            if isinstance(take, ValidatedTakeImport)
            else initial_state(take, receipt)
        )
        write_canonical_json(state_path(layout, state.take_id), state)
        states[state.take_id] = state
        recorded.append(state.take_id)
    return tuple(states[take_id] for take_id in sorted(states)), tuple(recorded)
