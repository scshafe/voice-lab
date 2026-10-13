"""The Mac's takes to the PC (D-053, docs/design/RECORDING-V1.md step 9): ``voice-lab
corpus-import plan|approve|send``, run by the owner on the Mac.

@canonical corpus_import -- the Mac's side of step 9:

- ``plan_export`` reads the Mac library's native takes through the take store's read-only
  readers (``RecordingTakeStore(read_only=True)``) and selects every sealed take of
  ``cole-self`` whose exact prompt the PC's catalog version counts (a ``CatalogView``, as
  ``GET /api/v1/catalog`` answers it). It packages each one (``build_take_import_package``:
  ``corpus_take_imports``) under ``tmp/corpus-import/<take_id>/``, which never leaves the Mac
  (the vault already carries the takes it copies). A package already there is reused, so
  planning the same takes again gives the same digests. One never sent (the home-host
  ledger names no upload of it) is packaged again when the PC's version or the Mac's
  decision or trim has changed since; one already sent never is, and a later change on the
  Mac is reported (``mac_state_changed_since_sent``: the PC holds the take, and changes go to
  it as step 4's requests). It writes the **export plan** (``CorpusImportPlan``: the PC's
  catalog version, and every package's take ids and every file's digest and size; no time,
  so it is a function of the packages) as
  ``provenance/host-transfers/corpus-import-<plan_sha256>.plan.json`` (0400, write-once).
  Every other take is reported with its reason: another subject's, unsealed, damaged, a room
  tone or a prompt the version does not count, audio another take's package already holds,
  and the legacy capture sessions (Qt desktop, capture CLI, prepared packages) this step does
  not export.
- ``approve_plan`` records the owner's per-payload transfer approval (AGENTS.md: the
  destination and the payload digest): the plan's digest, its size, and the endpoint and
  node of the standing home-host approval (D-021), as
  ``provenance/host-transfers/corpus-import-<plan_sha256>.approval-<destination>.json``
  (0400, write-once; ``<destination>`` is the SHA-256 of the endpoint and node). A plan is
  approved for one destination at a time: after the home host's node changes, the same plan
  is approved again for the new one, beside the first approval.
- ``send_plan`` uploads only the packages an approved plan lists, through the standing
  approval, the node check and the ledger, as every upload (class ``capture``). Before each
  upload, inside ``upload_session``, ``PlanApproval`` reads the plan and its approval for the
  standing approval of that very upload again and requires the upload's declaration (its
  manifest and every file's digest and size) to be the plan's. ``upload_session`` refuses an
  imported take without such an approval, so no other upload path sends one. Uploads resume;
  a take the ledger already records as accepted is not sent again. It reports each take:
  ``accepted``, ``deferred`` (on the PC, not published yet: run ``send`` again later),
  ``refused``, ``invalid`` (the package is not the planned one), ``unapproved`` (the approval
  no longer covers it) or ``failed`` (run ``send`` again).

Every directory and file is reached through ``confined_path`` and made a private real
directory, never through a link.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from pydantic import Field, JsonValue, ValidationError, model_validator

from . import __version__
from .api.contracts import IntakeSessionRequest
from .api.corpus_contracts import CatalogView
from .audio import inspect_canonical_wav
from .canonical import (
    canonical_sha256,
    open_regular_no_follow,
    rename_no_replace,
    require_evidence_file,
    write_canonical_json,
    write_new_private_file,
)
from .capture import MANIFEST_NAME, QC_REPORT_NAME, READY_NAME, ReadyMarker
from .catalog_ports import CorpusSnapshotSecurityError
from .corpus_store import require_referenced_snapshot
from .corpus_take_imports import (
    GROUP_NAME,
    MAC_STATE_NAME,
    RESERVATION_NAME,
    SEAL_NAME,
    SWIFT_CLIENT_NAME,
    SWIFT_CLIENT_PLATFORM,
    TAKE_IMPORT_FILES,
    CorpusTakeImportManifest,
    ImportedRecord,
    ImportedRecords,
    MacTakeState,
    TakeExporter,
    ValidatedTakeImport,
    recorded_against,
    validate_take_import_package,
)
from .corpus_takes import (
    AUDIO_NAME,
    TakeAudio,
    TakeClient,
    TakeId,
    copy_private,
    take_id_for,
    take_qc_report,
)
from .errors import ContractError, SecurityError, VoiceLabError
from .home_host import HomeHostApproval, HomeHostLedger, load_approval, record_bytes
from .intake_client import (
    HomeHostClient,
    TransferNotApproved,
    UploadError,
    UploadPending,
    UploadRefused,
    upload_session,
)
from .models import COLE_SUBJECT, ContractModel, Sha256, StableId, utc_now
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    CaptureReservation,
    CaptureSealReceipt,
    CatalogReference,
    CatalogSnapshot,
    RecordingFrameRange,
    RecordingGroup,
    RecordingTake,
)
from .recording_takes import (
    GROUP_PROVENANCE_ROOT,
    TAKE_PROVENANCE_ROOT,
    RecordingMetadataBusyError,
    RecordingTakeStore,
    read_recording_evidence_bytes,
)
from .storage_contracts import Endpoint, NodeStableId, NonBlank, UtcTimestamp

PLAN_SCHEMA: Final = "cole-voice-lab.corpus-import-plan/v1"
APPROVAL_SCHEMA: Final = "cole-voice-lab.corpus-import-approval/v1"
#: Where the packages wait on the Mac (``library_classification``: Mac-only).
EXPORT_ROOT: Final = "tmp/corpus-import"
#: Where the plans and their approvals are kept (``library_classification``: records).
HOST_TRANSFERS: Final = "provenance/host-transfers"
MAX_RECORD_BYTES: Final = 64 * 1024 * 1024
MAX_VIEW_BYTES: Final = 64 * 1024 * 1024
#: How long ``send`` waits for the PC to publish one take before it moves on.
DEFAULT_SEND_WAIT_SECONDS: Final = 120.0

type SkipReason = Literal[
    "other-subject",
    "unsealed",
    "damaged",
    "room-tone",
    "prompt-not-in-catalog",
    "duplicate-audio",
    "legacy-capture-session",
]
type SendStatus = Literal["accepted", "deferred", "refused", "invalid", "unapproved", "failed"]
type _Declaration = tuple[tuple[str, str, int], ...]


# -- Records ---------------------------------------------------------------------------------


class PlannedFile(ContractModel):
    """One file of a planned package, as its upload declares it."""

    path: str = Field(min_length=1, max_length=64)
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class PlannedPackage(ContractModel):
    """One package of an export plan: the PC's take id, the Swift take's, the prompt, the
    package's manifest digest, every file's digest and size (by path), and its bytes."""

    take_id: TakeId
    source_take_id: TakeId
    prompt_id: StableId
    manifest_sha256: Sha256
    files: tuple[PlannedFile, ...]
    size_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def _pinned(self) -> PlannedPackage:
        if [item.path for item in self.files] != sorted(TAKE_IMPORT_FILES):
            raise ValueError("a planned package pins each file of an imported take, by path")
        manifest = next(item for item in self.files if item.path == MANIFEST_NAME)
        if manifest.sha256 != self.manifest_sha256:
            raise ValueError("the planned manifest digest is its manifest.json's")
        if self.size_bytes != sum(item.size_bytes for item in self.files):
            raise ValueError("size_bytes is the sum of the package's files")
        return self

    def declaration(self) -> _Declaration:
        """The files as an upload declares them: (path, SHA-256, size), by path."""
        return tuple((item.path, item.sha256, item.size_bytes) for item in self.files)


class CorpusImportPlan(ContractModel):
    """The payload an export approval names: which packages go to the PC, for which of its
    catalog versions. It carries no time, so the same packages always make the same plan."""

    schema_version: Literal["cole-voice-lab.corpus-import-plan/v1"] = PLAN_SCHEMA
    subject_id: StableId
    catalog: CatalogReference
    package_root: Literal["tmp/corpus-import"] = EXPORT_ROOT
    packages: tuple[PlannedPackage, ...] = Field(min_length=1)
    total_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> CorpusImportPlan:
        if self.subject_id != COLE_SUBJECT:
            raise ValueError(f"a plan exports takes of {COLE_SUBJECT} (D-050)")
        ids = [package.take_id for package in self.packages]
        if ids != sorted(set(ids)):
            raise ValueError("a plan lists its packages by take id, once each")
        sources = [package.source_take_id for package in self.packages]
        if len(set(sources)) != len(sources):
            raise ValueError("a plan lists each Swift take once")
        if self.total_bytes != sum(package.size_bytes for package in self.packages):
            raise ValueError("total_bytes is the sum of the packages' bytes")
        return self


class ImportDestination(ContractModel):
    """Where an approved plan may go: the standing home-host approval's endpoint and node
    (and, for the record, which standing approval named them when the plan was approved)."""

    endpoint: Endpoint
    node_stable_id: NodeStableId
    home_host_approval_id: StableId
    home_host_approval_sha256: Sha256

    @property
    def key(self) -> str:
        """The destination's name in an approval's file name: endpoint and node only, so a
        renewed standing approval of the same node keeps the plan's approval."""
        return destination_key(self.endpoint, self.node_stable_id)


def destination_key(endpoint: str, node_stable_id: str) -> str:
    return canonical_sha256({"endpoint": endpoint, "node_stable_id": node_stable_id})


def destination_of(standing: HomeHostApproval, standing_sha256: str) -> ImportDestination:
    """The destination a standing home-host approval names."""
    return ImportDestination(
        endpoint=standing.endpoint,
        node_stable_id=standing.node_stable_id,
        home_host_approval_id=standing.approval_id,
        home_host_approval_sha256=standing_sha256,
    )


class CorpusImportApproval(ContractModel):
    """The owner's transfer approval of one export plan (AGENTS.md: a separate, explicit
    approval naming the destination and the payload digest)."""

    schema_version: Literal["cole-voice-lab.corpus-import-approval/v1"] = APPROVAL_SCHEMA
    plan_sha256: Sha256
    packages: int = Field(gt=0)
    total_bytes: int = Field(gt=0)
    destination: ImportDestination
    approved_at: UtcTimestamp
    approved_by: NonBlank = Field(max_length=256)
    statement: NonBlank = Field(max_length=4_096)


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"not a SHA-256: {value!r}")
    return value


def plan_path(layout: AssetLayout, plan_sha256: str) -> Path:
    """Raises: ``ValueError`` for a digest that is not one; ``SecurityError`` for a link."""
    name = f"corpus-import-{_sha256(plan_sha256)}.plan.json"
    return confined_path(layout.root, f"{HOST_TRANSFERS}/{name}")


def approval_path(layout: AssetLayout, plan_sha256: str, destination: str) -> Path:
    """The approval of a plan for one destination (``ImportDestination.key``)."""
    name = f"corpus-import-{_sha256(plan_sha256)}.approval-{_sha256(destination)}.json"
    return confined_path(layout.root, f"{HOST_TRANSFERS}/{name}")


def package_path(layout: AssetLayout, take_id: str) -> Path:
    if not take_id.startswith("take-"):
        raise ValueError(f"not a take id: {take_id!r}")
    _sha256(take_id.removeprefix("take-"))
    return confined_path(layout.root, f"{EXPORT_ROOT}/{take_id}")


def private_directory(layout: AssetLayout, relative: str) -> Path:
    """``relative`` under the asset root as a private real directory, made where missing.

    Raises: ``SecurityError`` when any component is a link or not a directory."""
    path = confined_path(layout.root, relative)
    current = layout.root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SecurityError(f"not a real directory of the asset root: {current}")
    return path


def _read_record[ModelT: ContractModel](
    path: Path, model: type[ModelT], *, sha256: str | None = None
) -> ModelT:
    require_evidence_file(path, max_bytes=MAX_RECORD_BYTES)
    with open_regular_no_follow(path) as stream:
        payload = stream.read(MAX_RECORD_BYTES + 1)
    if sha256 is not None and hashlib.sha256(payload).hexdigest() != sha256:
        raise SecurityError(f"the record is not the one its name pins: {path}")
    try:
        record = model.model_validate_json(payload)
    except ValidationError as error:
        raise ContractError(f"invalid record {path}: {error}") from error
    if payload != record_bytes(record):
        raise SecurityError(f"the record is not canonical: {path}")
    return record


def _write_once(layout: AssetLayout, path: Path, payload: bytes) -> bool:
    """Write ``payload`` at ``path`` (0400, in ``provenance/host-transfers/``) unless the same
    bytes are there; whether it wrote.

    Raises: ``SecurityError`` when other bytes are there, or for a link; ``OSError``."""
    private_directory(layout, HOST_TRANSFERS)
    if os.path.lexists(path):
        require_evidence_file(path, max_bytes=MAX_RECORD_BYTES)
        with open_regular_no_follow(path) as stream:
            if stream.read(MAX_RECORD_BYTES + 1) != payload:
                raise SecurityError(f"another record is already at {path}")
        return False
    write_new_private_file(path, payload, mode=0o400)
    return True


def read_plan(layout: AssetLayout, plan_sha256: str) -> CorpusImportPlan:
    """The export plan with this digest.

    Raises: ``ContractError`` when there is none (or it is invalid); ``SecurityError`` when
    its bytes are not that digest's or not canonical."""
    path = plan_path(layout, plan_sha256)
    if not os.path.lexists(path):
        raise ContractError(
            f"no export plan {plan_sha256} on this Mac; run `voice-lab corpus-import plan`"
        )
    return _read_record(path, CorpusImportPlan, sha256=plan_sha256)


def read_approval(
    layout: AssetLayout, plan_sha256: str, destination: ImportDestination
) -> CorpusImportApproval:
    """The transfer approval of the plan with this digest for ``destination`` (its endpoint
    and node).

    Raises: ``ContractError`` when the plan is not approved for that destination;
    ``SecurityError`` as ``_read_record``, or for an approval of another plan or
    destination."""
    path = approval_path(layout, plan_sha256, destination.key)
    if not os.path.lexists(path):
        others = len(list(path.parent.glob(f"corpus-import-{plan_sha256}.approval-*.json")))
        elsewhere = f" (it is approved for {others} other destination(s))" if others else ""
        raise ContractError(
            f"export plan {plan_sha256} is not approved for {destination.endpoint} "
            f"(node {destination.node_stable_id}){elsewhere}; review it and "
            "`voice-lab corpus-import approve` it for this destination"
        )
    approval = _read_record(path, CorpusImportApproval)
    if approval.plan_sha256 != plan_sha256:
        raise SecurityError(f"the approval names another plan: {path}")
    if (approval.destination.endpoint, approval.destination.node_stable_id) != (
        destination.endpoint,
        destination.node_stable_id,
    ):
        raise SecurityError(f"the approval names another destination: {path}")
    return approval


# -- The PC's catalog ------------------------------------------------------------------------


def catalog_from_view(view: CatalogView) -> tuple[CatalogReference, CatalogSnapshot]:
    """The PC's catalog version and its snapshot, from its ``GET /api/v1/catalog`` answer.

    The snapshot is rebuilt from the view's suite and aliases and must be byte for byte the
    file the reference seals (``require_referenced_snapshot``).
    Raises: ``ContractError`` for another subject, a physical-test catalog or a view whose
    prompts are not its reference's."""
    if view.subject_id != COLE_SUBJECT:
        raise ContractError(f"the catalog is {view.subject_id}'s, not {COLE_SUBJECT}'s")
    reference = view.reference
    if reference.purpose == "physical-test":
        raise ContractError("a physical-test catalog never counts a take")
    try:
        snapshot = CatalogSnapshot(
            suite=view.suite,
            catalog_sha256=canonical_sha256(view.suite),
            prompt_aliases=view.prompt_aliases,
        )
        require_referenced_snapshot(snapshot, reference)
    except (ValidationError, CorpusSnapshotSecurityError) as error:
        raise ContractError(f"the catalog view is not the version it names: {error}") from error
    return reference, snapshot


def read_catalog_view(path: Path) -> CatalogView:
    """A ``CatalogView`` saved from the PC's ``GET /api/v1/catalog``.

    Raises: ``ContractError`` when it is not one; ``SecurityError`` for a link or a file
    that is not regular."""
    require_evidence_file(path, max_bytes=MAX_VIEW_BYTES)
    with open_regular_no_follow(path) as stream:
        payload = stream.read(MAX_VIEW_BYTES + 1)
    try:
        return CatalogView.model_validate_json(payload)
    except ValidationError as error:
        raise ContractError(f"not a catalog view (GET /api/v1/catalog): {path}") from error


def fetch_catalog_view(client: HomeHostClient, approval: HomeHostApproval) -> CatalogView:
    """The PC's ``GET /api/v1/catalog``, from the approved node (identity checked first).

    Raises: ``SecurityError`` when the node is not the approved one; ``UploadError`` when the
    PC cannot be reached or has no catalog yet; ``ContractError`` for an invalid answer."""
    client.check_identity(approval)
    answer = client.request("GET", "/api/v1/catalog")
    if answer.status != 200:
        error = answer.document.get("error", f"status-{answer.status}")
        raise UploadError(f"the PC's catalog is not available: {error}")
    try:
        return CatalogView.model_validate_json(json.dumps(answer.document))
    except ValidationError as failure:
        raise ContractError("the PC answered with an invalid catalog view") from failure


# -- Packages --------------------------------------------------------------------------------


def mac_state_of(take: RecordingTake) -> MacTakeState:
    """The Swift take's decision and trim as its head says now.

    Raises: ``ValidationError`` for an unsealed take or one whose decision has no evidence
    where the Mac always writes it."""
    assert take.seal is not None
    evidence = take.decision_evidence
    excluded = take.decision in {"rejected", "removed"}
    frames = take.frame_range or RecordingFrameRange(
        source_frames=take.seal.audio.frames, start_frame=0, end_frame=take.seal.audio.frames
    )
    return MacTakeState(
        source_take_id=take.reservation.take_id,
        decision=take.decision,
        decision_revision=take.decision_revision,
        reason=evidence.reason if evidence is not None and excluded else None,
        decided_at=None if evidence is None else evidence.committed_at,
        restore_decision=evidence.restore_decision if evidence is not None and excluded else None,
        frame_range=frames,
        edit_revision=take.edit_revision,
    )


def build_take_import_package(
    directory: Path,
    *,
    layout: AssetLayout,
    take: RecordingTake,
    catalog: CatalogReference,
    clock: Callable[[], datetime] = utc_now,
    exporter_version: str = __version__,
) -> ValidatedTakeImport:
    """Package one sealed Swift take of the Mac library ``layout`` in the new ``directory``.

    The WAV is copied, and the seal, reservation and group are copied byte for byte from
    the take's records after each is checked against the take's head; ``mac-state.json`` is
    the head's decision and trim. The package is assembled in a private sibling directory,
    validated with ``validate_take_import_package`` and renamed into place complete, never
    over an existing entry; on failure nothing is left behind. The take stays where it is.
    Returns: the validated package.
    Raises: ``FileExistsError`` when ``directory`` exists; ``ContractError``/``SecurityError``
    when the take's records disagree with its head or the package does not validate;
    ``OSError``.
    """
    seal = take.seal
    if seal is None:
        raise ContractError("an unsealed take has no recording to export")
    reservation = take.reservation
    group = reservation.group
    source = reservation.take_id
    if os.path.lexists(directory):
        raise FileExistsError(f"the package already exists: {directory}")
    work = directory.parent / f".{directory.name}.building-{secrets.token_hex(8)}"
    work.mkdir(mode=0o700)
    try:
        copy_private(confined_path(layout.root, seal.original_path), work / AUDIO_NAME)
        inspection = inspect_canonical_wav(work / AUDIO_NAME)
        if (inspection.sha256, inspection.size_bytes) != (
            seal.original_sha256,
            seal.original_size_bytes,
        ):
            raise SecurityError(f"{seal.original_path} is not the audio its seal binds")
        sources: dict[str, tuple[str, type[ContractModel], ContractModel]] = {
            SEAL_NAME: (f"{TAKE_PROVENANCE_ROOT}/{source}/seal.json", CaptureSealReceipt, seal),
            RESERVATION_NAME: (
                f"{TAKE_PROVENANCE_ROOT}/{source}/reservation.json",
                CaptureReservation,
                reservation,
            ),
            GROUP_NAME: (f"{GROUP_PROVENANCE_ROOT}/{group.group_id}.json", RecordingGroup, group),
        }
        records: dict[str, ImportedRecord] = {}
        for name, (relative, model, expected) in sources.items():
            record, payload = read_recording_evidence_bytes(
                confined_path(layout.root, relative), model
            )
            if record != expected:
                raise SecurityError(f"{relative} is not the record the take's head binds")
            write_new_private_file(work / name, payload)
            records[name] = ImportedRecord(
                sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
            )
        state_sha256 = write_canonical_json(work / MAC_STATE_NAME, mac_state_of(take))
        records[MAC_STATE_NAME] = ImportedRecord(
            sha256=state_sha256, size_bytes=(work / MAC_STATE_NAME).stat().st_size
        )
        exported_at = clock()
        try:
            manifest = CorpusTakeImportManifest(
                take_id=take_id_for(inspection.sha256),
                source_take_id=source,
                subject_id=group.subject_id,
                prompt=reservation.prompt,
                prompt_sha256=canonical_sha256(reservation.prompt),
                recorded_against=recorded_against(group),
                catalog=catalog,
                requested_at=seal.requested_at,
                first_sample_at=seal.first_sample_at,
                capture_host=group.capture_host,
                capture_device=group.capture_device,
                client=TakeClient(
                    name=SWIFT_CLIENT_NAME,
                    version=seal.recorder_build.build_id,
                    platform=SWIFT_CLIENT_PLATFORM,
                ),
                audio=TakeAudio(
                    sha256=inspection.sha256,
                    size_bytes=inspection.size_bytes,
                    audio=inspection.audio,
                ),
                records=ImportedRecords(
                    seal=records[SEAL_NAME],
                    reservation=records[RESERVATION_NAME],
                    group=records[GROUP_NAME],
                    mac_state=records[MAC_STATE_NAME],
                ),
                exporter=TakeExporter(version=exporter_version, exported_at=exported_at),
            )
        except ValidationError as error:
            raise ContractError(f"the take cannot be described: {error}") from error
        manifest_sha256 = write_canonical_json(work / MANIFEST_NAME, manifest)
        report = take_qc_report(
            manifest.take_id, manifest.prompt, manifest_sha256, inspection, exported_at
        )
        qc_sha256 = write_canonical_json(work / QC_REPORT_NAME, report)
        write_canonical_json(
            work / READY_NAME,
            ReadyMarker(
                session_id=manifest.take_id,
                manifest_sha256=manifest_sha256,
                qc_report_sha256=qc_sha256,
                finalized_at=exported_at,
            ),
        )
        validate_take_import_package(work)
        rename_no_replace(work, directory)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return validate_take_import_package(directory)


def _declaration(package: ValidatedTakeImport) -> tuple[PlannedFile, ...]:
    """Every file of a validated package as its upload declares it, by path."""
    return tuple(
        PlannedFile(path=path, sha256=item.sha256, size_bytes=item.size_bytes)
        for path, item in sorted(package._snapshot.files.items())
    )


def _replace_package(
    directory: Path,
    *,
    layout: AssetLayout,
    take: RecordingTake,
    catalog: CatalogReference,
    clock: Callable[[], datetime],
    exporter_version: str,
) -> ValidatedTakeImport:
    """Package ``take`` again in place of its never-sent package ``directory``.

    The old package is set aside first and removed once the new one is in place; when the
    new one cannot be built, the old one is put back. Raises: as
    ``build_take_import_package``; ``OSError``."""
    aside = directory.parent / f".{directory.name}.replaced-{secrets.token_hex(8)}"
    os.rename(directory, aside)
    try:
        package = build_take_import_package(
            directory,
            layout=layout,
            take=take,
            catalog=catalog,
            clock=clock,
            exporter_version=exporter_version,
        )
    except BaseException:
        os.rename(aside, directory)
        raise
    metadata = aside.lstat()
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(aside)
    return package


# -- Planning --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkippedTake:
    """A take (or a legacy session's directory) the plan does not export, and why."""

    source: str
    reason: SkipReason
    detail: str


@dataclass(frozen=True, slots=True)
class ExportPlanning:
    """What ``plan_export`` did: the plan (``None`` when nothing is to be exported), the
    packages it built, rebuilt (never sent, and stale) and reused, the reused packages
    already sent whose Mac state changed since, and every take it skipped."""

    plan: CorpusImportPlan | None
    plan_sha256: str | None
    built: tuple[str, ...]
    rebuilt: tuple[str, ...]
    reused: tuple[str, ...]
    state_changed_since_sent: tuple[str, ...]
    skipped: tuple[SkippedTake, ...]

    def report(self, layout: AssetLayout) -> dict[str, JsonValue]:
        """The owner's review of the plan, as ``plan`` prints it."""
        plan = self.plan
        counts = Counter(item.reason for item in self.skipped)
        return {
            "plan_sha256": self.plan_sha256,
            "plan_path": (
                None
                if self.plan_sha256 is None
                else str(plan_path(layout, self.plan_sha256).relative_to(layout.root))
            ),
            "catalog": None
            if plan is None
            else {
                "catalog_id": plan.catalog.catalog_id,
                "catalog_version": plan.catalog.catalog_version,
                "catalog_sha256": plan.catalog.catalog_sha256,
            },
            "packages": 0 if plan is None else len(plan.packages),
            "total_bytes": 0 if plan is None else plan.total_bytes,
            "built": list(self.built),
            "rebuilt": list(self.rebuilt),
            "reused": list(self.reused),
            "mac_state_changed_since_sent": list(self.state_changed_since_sent),
            "skipped_counts": dict(sorted(counts.items())),
            "skipped": [
                {"source": item.source, "reason": item.reason, "detail": item.detail}
                for item in self.skipped
            ],
        }


def _take_ids(store: RecordingTakeStore) -> Iterator[str]:
    after: str | None = None
    while page := store.iter_take_ids_for_projection(after_take_id=after):
        yield from page
        after = page[-1]


def _legacy_sessions(layout: AssetLayout) -> Iterator[SkippedTake]:
    """The capture sessions of the Mac library (``tmp/capture-*``, ``raw/``), unread."""
    detail = (
        "a capture session (Qt desktop, capture CLI or a prepared package); this step exports "
        "the Swift app's takes only"
    )
    for name in sorted(os.listdir(layout.directory("tmp"))):
        if name.startswith("capture-") and not name.endswith(".rejected"):
            yield SkippedTake(f"tmp/{name}", "legacy-capture-session", detail)
    for name in sorted(os.listdir(layout.directory("raw"))):
        if not name.startswith("."):
            yield SkippedTake(f"raw/{name}", "legacy-capture-session", detail)


def ledger_sessions(layout: AssetLayout) -> frozenset[str]:
    """The upload sessions this library's home-host ledger names (any event): the packages
    that may have left the Mac. Raises: ``SecurityError`` for a broken ledger."""
    if not os.path.lexists(layout.root / "provenance" / "home-host" / "ledger"):
        return frozenset()
    return frozenset(entry.session_id for entry, _digest in HomeHostLedger(layout).entries())


def _why_not(take: RecordingTake, snapshot: CatalogSnapshot) -> tuple[SkipReason, str] | None:
    group = take.reservation.group
    if group.subject_id != COLE_SUBJECT:
        return "other-subject", f"a take of {group.subject_id}"
    if take.seal is None:
        return "unsealed", f"transport {take.transport}: the take was never sealed"
    prompt = take.reservation.prompt
    if snapshot.current_prompt_for(prompt) is None:
        if prompt.category == "room-tone":
            return "room-tone", f"{prompt.prompt_id}: no cole-corpus version holds room tones"
        return "prompt-not-in-catalog", f"the PC's version does not count {prompt.prompt_id}"
    return None


@dataclass(slots=True)
class _Planning:
    sent: frozenset[str]
    entries: list[PlannedPackage] = field(default_factory=list)
    built: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    skipped: list[SkippedTake] = field(default_factory=list)


def _plan_take(
    planning: _Planning,
    layout: AssetLayout,
    store: RecordingTakeStore,
    source: str,
    reference: CatalogReference,
    snapshot: CatalogSnapshot,
    *,
    clock: Callable[[], datetime],
    exporter_version: str,
) -> None:
    """Plan one Swift take: skip it with its reason, or package it (or reuse its package, or
    package it again when it was never sent and is stale)."""
    try:
        take = store.get_take_for_projection(source)
        store.group_catalog(take.reservation.group)
    except RecordingMetadataBusyError:
        raise
    except (VoiceLabError, ValidationError, OSError) as error:
        planning.skipped.append(SkippedTake(source, "damaged", str(error)))
        return
    why = _why_not(take, snapshot)
    if why is not None:
        planning.skipped.append(SkippedTake(source, *why))
        return
    assert take.seal is not None
    take_id = take_id_for(take.seal.original_sha256)
    try:
        directory = package_path(layout, take_id)
        if os.path.lexists(directory):
            try:
                package = validate_take_import_package(directory)
            except (VoiceLabError, ValidationError, OSError) as error:
                raise ContractError(
                    f"its package fails its checks ({error}); remove {EXPORT_ROOT}/{take_id} "
                    "to package the take again"
                ) from error
            if package.manifest.source_take_id != source:
                planning.skipped.append(
                    SkippedTake(
                        source,
                        "duplicate-audio",
                        f"take {package.manifest.source_take_id} has the same audio ({take_id})",
                    )
                )
                return
            state = mac_state_of(take)
            stale = package.manifest.catalog != reference or package.mac_state != state
            if stale and take_id not in planning.sent:
                package = _replace_package(
                    directory,
                    layout=layout,
                    take=take,
                    catalog=reference,
                    clock=clock,
                    exporter_version=exporter_version,
                )
                planning.rebuilt.append(take_id)
            else:
                planning.reused.append(take_id)
                if package.mac_state != state:
                    planning.changed.append(take_id)
        else:
            package = build_take_import_package(
                directory,
                layout=layout,
                take=take,
                catalog=reference,
                clock=clock,
                exporter_version=exporter_version,
            )
            planning.built.append(take_id)
    except (VoiceLabError, ValidationError, OSError) as error:
        planning.skipped.append(SkippedTake(source, "damaged", f"{EXPORT_ROOT}/{take_id}: {error}"))
        return
    files = _declaration(package)
    planning.entries.append(
        PlannedPackage(
            take_id=take_id,
            source_take_id=source,
            prompt_id=package.manifest.prompt.prompt_id,
            manifest_sha256=package.manifest_sha256,
            files=files,
            size_bytes=sum(item.size_bytes for item in files),
        )
    )


def plan_export(
    layout: AssetLayout,
    view: CatalogView,
    *,
    clock: Callable[[], datetime] = utc_now,
    exporter_version: str = __version__,
) -> ExportPlanning:
    """Package the Mac library's exportable takes for the PC's catalog version ``view`` and
    write the export plan (see the module docstring).

    Returns: what was planned, built, rebuilt, reused and skipped.
    Raises: ``ContractError`` for a catalog view that is not its version's;
    ``RecordingMetadataBusyError`` while the recording app commits a change (run it again);
    ``SecurityError`` for a link in the export's directories, a broken home-host ledger, or
    when the plan's path holds another record; ``OSError``.
    """
    reference, snapshot = catalog_from_view(view)
    private_directory(layout, EXPORT_ROOT)
    planning = _Planning(sent=ledger_sessions(layout))
    # A root that never held native takes has no take store; the read-only store would
    # otherwise wait for a service owner to create its directories.
    if os.path.lexists(layout.root / TAKE_PROVENANCE_ROOT):
        store = RecordingTakeStore(layout, read_only=True)
        for source in _take_ids(store):
            _plan_take(
                planning,
                layout,
                store,
                source,
                reference,
                snapshot,
                clock=clock,
                exporter_version=exporter_version,
            )
    skipped = [*planning.skipped, *_legacy_sessions(layout)]
    if not planning.entries:
        return ExportPlanning(None, None, (), (), (), (), tuple(skipped))
    entries = sorted(planning.entries, key=lambda entry: entry.take_id)
    plan = CorpusImportPlan(
        subject_id=COLE_SUBJECT,
        catalog=reference,
        packages=tuple(entries),
        total_bytes=sum(entry.size_bytes for entry in entries),
    )
    payload = record_bytes(plan)
    plan_sha256 = hashlib.sha256(payload).hexdigest()
    _write_once(layout, plan_path(layout, plan_sha256), payload)
    return ExportPlanning(
        plan,
        plan_sha256,
        tuple(planning.built),
        tuple(planning.rebuilt),
        tuple(planning.reused),
        tuple(planning.changed),
        tuple(skipped),
    )


# -- Approval --------------------------------------------------------------------------------


def _standing_destination(layout: AssetLayout, now: datetime) -> ImportDestination:
    standing, standing_sha256 = load_approval(layout, now=now)
    if "capture" not in standing.classes:
        raise SecurityError("the standing home-host approval does not cover capture packages")
    return destination_of(standing, standing_sha256)


def approve_plan(
    layout: AssetLayout,
    plan_sha256: str,
    *,
    approved_by: str,
    statement: str,
    now: datetime,
) -> tuple[CorpusImportApproval, bool]:
    """Record the owner's transfer approval of the plan with this digest, to the destination
    of the standing home-host approval (0400, write-once, one per destination).

    Returns: the approval, and whether it was written now (``False``: the plan was approved
    for this destination before, and that approval stands as it is).
    Raises: ``ContractError`` when there is no such plan; ``SecurityError`` when the standing
    approval does not stand now or does not cover capture packages; ``OSError``.
    """
    plan = read_plan(layout, plan_sha256)
    destination = _standing_destination(layout, now)
    path = approval_path(layout, plan_sha256, destination.key)
    if os.path.lexists(path):
        return read_approval(layout, plan_sha256, destination), False
    approval = CorpusImportApproval(
        plan_sha256=plan_sha256,
        packages=len(plan.packages),
        total_bytes=plan.total_bytes,
        destination=destination,
        approved_at=now,
        approved_by=approved_by,
        statement=statement,
    )
    _write_once(layout, path, record_bytes(approval))
    return approval, True


@dataclass(frozen=True, slots=True)
class PlanApproval:
    """The transfer approval of one export plan, as ``upload_session`` checks it for each
    upload (``intake_client.PayloadApproval``).

    Every check reads the plan and the approval from disk again: the plan must still be the
    one its digest names, it must be approved for the destination of the standing approval
    this very upload goes under (endpoint and node), and the upload's declaration (its
    session id, manifest and every file's digest and size) must be a package the plan lists,
    exactly as it lists it."""

    layout: AssetLayout
    plan_sha256: str

    def require(
        self,
        request: IntakeSessionRequest,
        standing: HomeHostApproval,
        standing_sha256: str,
    ) -> None:
        """Raises: ``TransferNotApproved`` unless the plan's approval covers this upload."""
        try:
            plan = read_plan(self.layout, self.plan_sha256)
            read_approval(self.layout, self.plan_sha256, destination_of(standing, standing_sha256))
        except (VoiceLabError, ValueError, OSError) as error:
            raise TransferNotApproved(f"export plan {self.plan_sha256}: {error}") from error
        entry = next((item for item in plan.packages if item.take_id == request.session_id), None)
        declared = tuple((item.path, item.sha256, item.size_bytes) for item in request.files)
        if (
            entry is None
            or entry.manifest_sha256 != request.manifest_sha256
            or entry.declaration() != declared
        ):
            raise TransferNotApproved(
                f"{request.session_id} is not a package export plan {self.plan_sha256} lists, "
                "as it lists it"
            )


# -- Sending ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SentTake:
    """What became of one package of the plan. ``before`` is true for a take the ledger
    already recorded as accepted, which was not sent again."""

    take_id: str
    source_take_id: str
    status: SendStatus
    detail: str | None = None
    before: bool = False


def _accepted_before(layout: AssetLayout) -> set[tuple[str, str]]:
    return {
        (entry.session_id, entry.manifest_sha256)
        for entry, _digest in HomeHostLedger(layout).entries()
        if entry.event == "upload-completed"
    }


def send_plan(
    layout: AssetLayout,
    plan_sha256: str,
    *,
    client: HomeHostClient,
    clock: Callable[[], datetime] = utc_now,
    wait_seconds: float = DEFAULT_SEND_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[SentTake, ...]:
    """Upload the packages of the approved plan with this digest (see the module docstring).

    The plan's approval for the standing destination is checked here first, for a clear
    refusal before anything is sent, and again inside ``upload_session`` before each upload
    (``PlanApproval``). After the first take the PC does not publish within ``wait_seconds``
    (deferred: its catalog version is not there yet, say, or the publisher is not running),
    the remaining takes are uploaded without waiting; running ``send`` again collects their
    receipts.
    Returns: every take of the plan, in the plan's order, with what became of it.
    Raises: ``ContractError`` when the plan does not exist or is not approved for the
    standing destination; ``SecurityError`` when the standing approval does not stand, or
    the node is not the approved node.
    """
    plan = read_plan(layout, plan_sha256)
    read_approval(layout, plan_sha256, _standing_destination(layout, clock()))
    standing, _ = load_approval(layout, now=clock())
    client.check_identity(standing)
    gate = PlanApproval(layout, plan_sha256)
    accepted = _accepted_before(layout)
    results: list[SentTake] = []
    wait = wait_seconds
    for entry in plan.packages:
        name = (entry.take_id, entry.source_take_id)
        if (entry.take_id, entry.manifest_sha256) in accepted:
            results.append(SentTake(*name, "accepted", before=True))
            continue
        try:
            directory = package_path(layout, entry.take_id)
            package = validate_take_import_package(directory)
            if (
                package.manifest_sha256,
                package.manifest.source_take_id,
                _declaration(package),
            ) != (entry.manifest_sha256, entry.source_take_id, entry.files):
                raise SecurityError("the package is not the one the approved plan lists")
        except (VoiceLabError, ValidationError, ValueError, OSError) as error:
            results.append(SentTake(*name, "invalid", str(error)))
            continue
        try:
            upload_session(
                directory,
                layout=layout,
                client=client,
                wait_seconds=wait,
                clock=clock,
                sleep=sleep,
                monotonic=monotonic,
                package_class="capture",
                payload_approval=gate,
            )
        except UploadPending as pending:
            results.append(SentTake(*name, "deferred", str(pending)))
            wait = 0.0
        except TransferNotApproved as refused:
            results.append(SentTake(*name, "unapproved", str(refused)))
        except UploadRefused as refused:
            results.append(SentTake(*name, "refused", str(refused)))
        except (VoiceLabError, OSError) as error:
            results.append(SentTake(*name, "failed", str(error)))
        else:
            results.append(SentTake(*name, "accepted"))
    return tuple(results)


__all__ = [
    "APPROVAL_SCHEMA",
    "DEFAULT_SEND_WAIT_SECONDS",
    "EXPORT_ROOT",
    "PLAN_SCHEMA",
    "CorpusImportApproval",
    "CorpusImportPlan",
    "ExportPlanning",
    "ImportDestination",
    "PlanApproval",
    "PlannedFile",
    "PlannedPackage",
    "SentTake",
    "SkippedTake",
    "approval_path",
    "approve_plan",
    "build_take_import_package",
    "catalog_from_view",
    "destination_key",
    "destination_of",
    "fetch_catalog_view",
    "ledger_sessions",
    "mac_state_of",
    "package_path",
    "plan_export",
    "plan_path",
    "private_directory",
    "read_approval",
    "read_catalog_view",
    "read_plan",
    "send_plan",
]
