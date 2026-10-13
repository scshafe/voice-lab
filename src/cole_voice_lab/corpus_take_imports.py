"""The Swift app's takes, imported into the PC's corpus (D-053, docs/design/RECORDING-V1.md
step 9).

@canonical corpus_take_imports -- ``cole-voice-lab.corpus-take-import/v1``: one take that Cole
Voice Recorder (the Swift app, D-015) recorded on the Mac, packaged by ``voice-lab
corpus-import`` (``corpus_import``) for the home host's intake. The PortAudio take contract
(``corpus_takes``) stays strict: a Swift take has no PortAudio evidence, so it travels as its
own kind of package, with its Core Audio provenance byte for byte. A package is a directory of
exactly eight files:

- ``manifest.json``, the ``CorpusTakeImportManifest``: the PC's take id (``take-<SHA-256 of
  original.wav>``, the PC's own id scheme, so a recording is imported once), the Swift take's
  id (``source_take_id``), the subject (``COLE_SUBJECT``), the exact prompt and its digest,
  what the take's recording group was recorded against (``recorded_against``), the PC's
  catalog version that counts the prompt (``catalog``), the request and first-sample times
  (the seal's), the host, the Core Audio device, the client (the seal's recorder build), the
  audio, the digest of each of the four records below, and the exporter;
- ``qc.json`` and ``READY``, as for a take;
- ``original.wav``, the take: canonical mono 48 kHz s24le PCM, the seal's exact bytes;
- ``seal.json``, ``reservation.json`` and ``group.json``: the Swift take's
  ``CaptureSealReceipt`` (with its ``capture-continuity/v1`` evidence), its
  ``CaptureReservation`` and its ``RecordingGroup``, verbatim from the Mac's library;
- ``mac-state.json``, the ``MacTakeState``: the take's decision and trim on the Mac when it
  was exported.

``validate_take_import_package`` is as strict as the other validators: the exact file set, no
links, hard links or directories, canonical JSON for the files the exporter writes, every
digest and byte count, each verbatim record parsed by its own strict model, the WAV canonical
and equal to the seal's audio, the seal bound to the reservation (its digest), the
reservation to the group, the group's device to the seal's continuity evidence and to a
UMIK-1, the times in order, and the Mac's trim within the take's frames.
``admit_take_import`` adds the PC's own rules: no diagnostic root, and the ``catalog`` version
is the one this server published and it counts the exact prompt. The Swift app has no
simulated capture device, so there is no "simulated" rule.

On the PC the publisher publishes an admitted package into ``raw/<manifest_sha256>/`` like
any package, and ``corpus_take_store.record_takes`` records it with the Mac's state
(``corpus_take_store.imported_state``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .audio import inspect_canonical_wav
from .canonical import canonical_sha256, read_canonical_model, sha256_file
from .capture import MANIFEST_NAME, QC_REPORT_NAME, READY_NAME, ReadyMarker
from .corpus_takes import (
    AUDIO_NAME,
    TAKE_ID_PATTERN,
    TAKE_IMPORT_SCHEMA,
    TakeAudio,
    TakeClient,
    TakeId,
    admit_catalog,
    require_corpus_root,
    take_intake_qc,
)
from .errors import ContractError, SecurityError
from .intake import (
    INTAKE_QC_REPORT_NAME,
    MAX_CONTROL_FILE_BYTES,
    RECEIPT_NAME,
    PackageSnapshot,
    _snapshot_package,
)
from .models import (
    COLE_SUBJECT,
    AudioSpec,
    CaptureDevice,
    ContractModel,
    Prompt,
    QcReport,
    Sha256,
    StableId,
)
from .paths import AssetLayout
from .recording_contracts import (
    CaptureReservation,
    CaptureSealReceipt,
    CatalogReference,
    DecisionState,
    RecordingFrameRange,
    RecordingGroup,
    Revision,
)

MAC_STATE_SCHEMA: Final = "cole-voice-lab.corpus-take-import-state/v1"
SEAL_NAME: Final = "seal.json"
RESERVATION_NAME: Final = "reservation.json"
GROUP_NAME: Final = "group.json"
MAC_STATE_NAME: Final = "mac-state.json"
TAKE_IMPORT_FILES: Final = frozenset(
    {
        MANIFEST_NAME,
        QC_REPORT_NAME,
        READY_NAME,
        AUDIO_NAME,
        SEAL_NAME,
        RESERVATION_NAME,
        GROUP_NAME,
        MAC_STATE_NAME,
    }
)
#: The Swift app (apps/macos), as an imported take names its client.
SWIFT_CLIENT_NAME: Final = "Cole Voice Recorder"
SWIFT_CLIENT_PLATFORM: Final = "macOS"
EXPORTER_NAME: Final = "voice-lab corpus-import"
#: The Mac's own bound on a take's evidence files (``recording_takes.MAX_TAKE_EVIDENCE_BYTES``).
MAX_RECORD_BYTES: Final = MAX_CONTROL_FILE_BYTES
#: A UMIK-1 by the name Core Audio gives it: the native capture rule (``capture_native``),
#: which is also the capture packages' (``intake.validate_capture_package``).
_UMIK1: Final = "umik-1"

#: A Swift take's id: ``take-<SHA-256 of its reservation's operation id>``.
type SourceTakeId = Annotated[str, StringConstraints(pattern=TAKE_ID_PATTERN)]
type _Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type _Reason = Annotated[str, StringConstraints(min_length=1, max_length=160)]


def _utc(value: datetime, what: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} requires an explicit timezone")
    return value.astimezone(UTC)


class MacTakeState(ContractModel):
    """``mac-state.json``: what the Mac's library said about the take when it was exported.

    The Swift take's decision (``draft``, ``kept``, ``rejected`` or ``removed``) and its
    revision; a rejection's or removal's reason and the decision it would restore; the time
    the decision was made when the take's records say (an explicit reject, remove or restore;
    a Save records none on the take); and the take's frame recipe and edit revision."""

    schema_version: Literal["cole-voice-lab.corpus-take-import-state/v1"] = MAC_STATE_SCHEMA
    source_take_id: SourceTakeId
    decision: DecisionState
    decision_revision: Revision
    reason: _Reason | None
    decided_at: datetime | None
    restore_decision: Literal["draft", "kept"] | None
    frame_range: RecordingFrameRange
    edit_revision: Revision

    @field_validator("decided_at")
    @classmethod
    def _decided_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, "a decision's time")

    @model_validator(mode="after")
    def _consistent(self) -> MacTakeState:
        excluded = self.decision in {"rejected", "removed"}
        if (self.reason is not None) != excluded or (self.restore_decision is not None) != (
            excluded
        ):
            raise ValueError("a rejection or removal, and only one, has a reason and a restore")
        if self.decision_revision == 0 and (
            self.decision != "draft" or self.decided_at is not None
        ):
            raise ValueError("decision revision 0 is an undecided draft")
        frames = self.frame_range
        trimmed = frames.start_frame != 0 or frames.end_frame != frames.source_frames
        if trimmed and self.edit_revision == 0:
            raise ValueError("edit revision 0 keeps every frame")
        return self

    @property
    def trimmed(self) -> bool:
        """Whether the Mac's recipe keeps less than the whole take."""
        frames = self.frame_range
        return frames.start_frame != 0 or frames.end_frame != frames.source_frames


class EmbeddedSuite(ContractModel):
    """A recording-group/v1's catalog: the suite it embeds, by id and digest."""

    kind: Literal["embedded-suite"] = "embedded-suite"
    suite_id: StableId
    catalog_sha256: Sha256


class PublishedVersion(ContractModel):
    """A recording-group/v2's catalog: the published corpus-store version it references."""

    kind: Literal["catalog-reference"] = "catalog-reference"
    reference: CatalogReference


type RecordedAgainst = Annotated[EmbeddedSuite | PublishedVersion, Field(discriminator="kind")]


def recorded_against(group: RecordingGroup) -> EmbeddedSuite | PublishedVersion:
    """What ``group`` names as the catalog its takes were recorded against. Raises: nothing."""
    if group.catalog is not None:
        return EmbeddedSuite(suite_id=group.catalog.suite_id, catalog_sha256=group.catalog_sha256)
    assert group.catalog_reference is not None  # exactly one form (RecordingGroup)
    return PublishedVersion(reference=group.catalog_reference)


class ImportedRecord(ContractModel):
    """One record file of the package: its SHA-256 and byte count."""

    sha256: Sha256
    size_bytes: int = Field(gt=0, le=MAX_RECORD_BYTES)


class ImportedRecords(ContractModel):
    """The digests of the package's four records (``seal.json``, ``reservation.json``,
    ``group.json``, ``mac-state.json``)."""

    seal: ImportedRecord
    reservation: ImportedRecord
    group: ImportedRecord
    mac_state: ImportedRecord

    def by_name(self) -> dict[str, ImportedRecord]:
        return {
            SEAL_NAME: self.seal,
            RESERVATION_NAME: self.reservation,
            GROUP_NAME: self.group,
            MAC_STATE_NAME: self.mac_state,
        }


class TakeExporter(ContractModel):
    """The program that packaged the take, its version, and when (UTC, the Mac's clock)."""

    name: Literal["voice-lab corpus-import"] = EXPORTER_NAME
    version: _Text
    exported_at: datetime

    @field_validator("exported_at")
    @classmethod
    def _exported_utc(cls, value: datetime) -> datetime:
        return _utc(value, "the export time")


class CorpusTakeImportManifest(ContractModel):
    """``manifest.json`` of an imported take: one Swift take of one prompt, immutable."""

    schema_version: Literal["cole-voice-lab.corpus-take-import/v1"] = TAKE_IMPORT_SCHEMA
    take_id: TakeId
    source_take_id: SourceTakeId
    subject_id: StableId
    #: The exact prompt the take was recorded against (its reservation's), and its digest.
    prompt: Prompt
    prompt_sha256: Sha256
    #: What the take's recording group names: its embedded suite or its published version.
    recorded_against: RecordedAgainst
    #: The PC's catalog version that counts the exact prompt (``GET /api/v1/catalog``).
    catalog: CatalogReference
    #: The seal's request and first-sample times (UTC, the Mac's clock).
    requested_at: datetime
    first_sample_at: datetime
    capture_host: _Text
    capture_device: CaptureDevice
    client: TakeClient
    audio: TakeAudio
    records: ImportedRecords
    exporter: TakeExporter

    @field_validator("requested_at", "first_sample_at")
    @classmethod
    def _times_utc(cls, value: datetime) -> datetime:
        return _utc(value, "a take's times")

    @model_validator(mode="after")
    def _bound(self) -> CorpusTakeImportManifest:
        if self.take_id != f"take-{self.audio.sha256}":
            raise ValueError("the take id is take-<the SHA-256 of original.wav>")
        if self.subject_id != COLE_SUBJECT:
            raise ValueError(f"a take is of {COLE_SUBJECT} (D-050)")
        if self.prompt_sha256 != canonical_sha256(self.prompt):
            raise ValueError("prompt_sha256 is not the canonical digest of the prompt")
        if self.capture_device.backend != "coreaudio":
            raise ValueError("an imported take was recorded through Core Audio (D-015)")
        if self.catalog.purpose == "physical-test":
            raise ValueError("a take is never counted by a physical-test catalog")
        if (self.client.name, self.client.platform) != (SWIFT_CLIENT_NAME, SWIFT_CLIENT_PLATFORM):
            raise ValueError(f"an imported take was recorded by {SWIFT_CLIENT_NAME} on macOS")
        if not self.requested_at <= self.first_sample_at <= self.exporter.exported_at:
            raise ValueError("the request, the first sample and the export are in that order")
        return self


@dataclass(frozen=True, slots=True)
class ValidatedTakeImport:
    """An imported-take package that passed ``validate_take_import_package``."""

    manifest: CorpusTakeImportManifest
    manifest_sha256: str
    source_qc_report: QcReport
    source_qc_report_sha256: str
    #: Intake's own QC of the WAV (the persisted ``intake-qc.json`` of a published package).
    qc_report: QcReport
    qc_report_sha256: str
    ready: ReadyMarker
    seal: CaptureSealReceipt
    reservation: CaptureReservation
    group: RecordingGroup
    mac_state: MacTakeState
    _snapshot: PackageSnapshot

    @property
    def session_id(self) -> str:
        """The id the intake protocol knows the package by: the PC's take id."""
        return self.manifest.take_id


def _format(spec: AudioSpec) -> tuple[int, int, int, str, int]:
    return (
        spec.sample_rate_hz,
        spec.channels,
        spec.sample_width_bytes,
        spec.encoding,
        spec.frames,
    )


def require_bound_records(
    manifest: CorpusTakeImportManifest,
    seal: CaptureSealReceipt,
    reservation: CaptureReservation,
    group: RecordingGroup,
    mac_state: MacTakeState,
) -> None:
    """The manifest and the Swift take's records describe one take, as the Mac binds it
    (``RecordingTake``'s own rule) and as the manifest says. The seal's continuity evidence is
    a checked success by its own contract (``CaptureContinuityEvidence``).

    Raises: ``SecurityError`` when the seal is not bound to the reservation, or the group or
    device to them; ``ContractError`` for any other disagreement."""
    source = manifest.source_take_id
    if (seal.take_id, reservation.take_id, mac_state.source_take_id) != (source, source, source):
        raise ContractError("the take's records name another Swift take than the manifest")
    if seal.reservation_sha256 != canonical_sha256(reservation):
        raise SecurityError("the seal is not bound to this reservation")
    if (
        seal.process_generation != reservation.process_generation
        or seal.requested_at != reservation.requested_at
    ):
        raise SecurityError("the seal is not of this reservation's capture")
    if seal.audio.frames > reservation.maximum_frames:
        raise ContractError("the take exceeds its reservation's maximum duration")
    if reservation.group != group:
        raise SecurityError("group.json is not the group the reservation names")
    if seal.continuity.device_uid != group.capture_device.device_id:
        raise SecurityError("the seal's device is not the group's capture device")
    if _UMIK1 not in group.capture_device.display_name.casefold():
        raise ContractError("the take's group does not identify the required UMIK-1 microphone")
    if reservation.prompt != manifest.prompt:
        raise ContractError("the manifest's prompt is not the reservation's")
    if group.subject_id != manifest.subject_id:
        raise ContractError("the take's group is of another subject")
    if group.capture_device != manifest.capture_device or group.capture_host != (
        manifest.capture_host
    ):
        raise ContractError("the manifest's device or host is not the group's")
    if recorded_against(group) != manifest.recorded_against:
        raise ContractError("the manifest's recorded_against is not what the group names")
    if (manifest.requested_at, manifest.first_sample_at) != (
        seal.requested_at,
        seal.first_sample_at,
    ):
        raise ContractError("the manifest's times are not the seal's")
    if manifest.client.version != seal.recorder_build.build_id:
        raise ContractError("the manifest's client version is not the seal's recorder build")
    if mac_state.frame_range.source_frames != seal.audio.frames:
        raise ContractError("the Mac's trim is not within the take's frames")
    exported_at = manifest.exporter.exported_at
    if exported_at < seal.sealed_at:
        raise ContractError("the take was exported before it was sealed")
    if mac_state.decided_at is not None and not (
        seal.sealed_at <= mac_state.decided_at <= exported_at
    ):
        raise ContractError("the Mac's decision is not between the seal and the export")


def validate_take_import_package(
    package: Path, *, allow_intake_receipt: bool = False
) -> ValidatedTakeImport:
    """Independently verify every byte and contract of a complete imported-take package.

    ``allow_intake_receipt`` is for re-validating a published package in ``raw/``, which also
    holds intake's ``intake-qc.json`` and ``intake-receipt.json``; an incoming package that
    supplies them itself fails closed.
    Returns: the validated package.
    Raises: ``SecurityError`` for links, hard links, a file set other than an imported take's,
    a digest or byte count that disagrees, records that are not bound to each other, or a
    package that changed while it was read; ``ContractError`` for a missing READY, a contract
    or canonical-form failure, a WAV that is not canonical, or records that disagree.
    """
    snapshot_before = _snapshot_package(package)
    files = set(snapshot_before.files)
    if READY_NAME not in files:
        raise ContractError(f"partial imported take has no {READY_NAME} marker: {package}")
    expected = set(TAKE_IMPORT_FILES)
    if allow_intake_receipt:
        expected.update({INTAKE_QC_REPORT_NAME, RECEIPT_NAME})
    directories = sorted(set(snapshot_before.directories) - {"."})
    if files != expected or directories:
        raise SecurityError(
            f"imported take files differ from an imported take's; "
            f"missing={sorted(expected - files)}, undeclared={sorted(files - expected)}, "
            f"directories={directories}"
        )

    bound = MAX_CONTROL_FILE_BYTES
    marker = read_canonical_model(package / READY_NAME, ReadyMarker, max_bytes=bound)
    manifest = read_canonical_model(
        package / MANIFEST_NAME, CorpusTakeImportManifest, max_bytes=bound
    )
    source_report = read_canonical_model(package / QC_REPORT_NAME, QcReport, max_bytes=bound)
    manifest_sha256, _ = sha256_file(package / MANIFEST_NAME)
    source_report_sha256, _ = sha256_file(package / QC_REPORT_NAME)
    if manifest_sha256 != marker.manifest_sha256:
        raise SecurityError("READY manifest digest does not match manifest.json")
    if source_report_sha256 != marker.qc_report_sha256:
        raise SecurityError("READY QC digest does not match qc.json")
    if marker.session_id != manifest.take_id or source_report.session_id != manifest.take_id:
        raise ContractError("take IDs disagree across the imported take's evidence")
    if source_report.manifest_sha256 != manifest_sha256:
        raise SecurityError("QC report is not bound to the imported take's manifest")
    if [metric.path for metric in source_report.metrics] != [AUDIO_NAME]:
        raise ContractError(f"QC metrics must cover {AUDIO_NAME} exactly once")

    for name, declared in manifest.records.by_name().items():
        if sha256_file(package / name) != (declared.sha256, declared.size_bytes):
            raise SecurityError(f"a record's hash or byte count was tampered: {name}")
    seal = read_canonical_model(package / SEAL_NAME, CaptureSealReceipt, max_bytes=bound)
    reservation = read_canonical_model(
        package / RESERVATION_NAME, CaptureReservation, max_bytes=bound
    )
    group = read_canonical_model(package / GROUP_NAME, RecordingGroup, max_bytes=bound)
    mac_state = read_canonical_model(package / MAC_STATE_NAME, MacTakeState, max_bytes=bound)
    require_bound_records(manifest, seal, reservation, group, mac_state)

    inspection = inspect_canonical_wav(package / AUDIO_NAME)
    if (inspection.sha256, inspection.size_bytes) != (
        manifest.audio.sha256,
        manifest.audio.size_bytes,
    ):
        raise SecurityError(f"recording hash or byte count was tampered: {AUDIO_NAME}")
    if inspection.audio != manifest.audio.audio:
        raise ContractError(f"recording format evidence disagrees: {AUDIO_NAME}")
    if (seal.original_sha256, seal.original_size_bytes) != (
        inspection.sha256,
        inspection.size_bytes,
    ):
        raise SecurityError(f"the seal describes other audio than {AUDIO_NAME}")
    if _format(seal.audio) != _format(inspection.audio):
        raise ContractError(f"the seal's audio format disagrees with {AUDIO_NAME}")

    report, report_sha256 = take_intake_qc(
        package,
        take_id=manifest.take_id,
        prompt=manifest.prompt,
        manifest_sha256=manifest_sha256,
        source_report_sha256=source_report_sha256,
        inspection=inspection,
        ready=marker,
        published=allow_intake_receipt,
    )
    snapshot_after = _snapshot_package(package)
    if snapshot_after != snapshot_before:
        raise SecurityError("imported take identities or bytes changed while being validated")
    return ValidatedTakeImport(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        source_qc_report=source_report,
        source_qc_report_sha256=source_report_sha256,
        qc_report=report,
        qc_report_sha256=report_sha256,
        ready=marker,
        seal=seal,
        reservation=reservation,
        group=group,
        mac_state=mac_state,
        _snapshot=snapshot_after,
    )


def admit_take_import(layout: AssetLayout, take: ValidatedTakeImport) -> Prompt:
    """The PC's rules for a valid imported take, checked before intake publishes it.

    The asset root is not a diagnostic root, and the ``catalog`` version the take names is the
    one this server's store published under that id and number (every field), and it counts
    the take's exact prompt (``corpus_takes.admit_catalog``). The Swift app has no simulated
    device, so no simulated rule applies (D-053).
    Returns: the prompt of that catalog version the take counts for.
    Raises: ``TakeRefusal`` naming the rule; ``TakeDeferral`` when the store does not hold
    that version yet or cannot be read; ``OSError`` as the store's own.
    """
    require_corpus_root(layout)
    manifest = take.manifest
    return admit_catalog(layout, manifest.catalog, manifest.prompt, manifest.prompt_sha256)
