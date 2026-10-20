"""One take of the prompt studio, as it travels to the PC (D-052, docs/design/RECORDING-V1.md §4).

@canonical corpus_takes -- ``cole-voice-lab.corpus-take/v1``: one take of one catalog prompt,
recorded by voice-lab-desktop's PortAudio worker (D-039) and packaged for the home host's
intake (D-021). A take package is a directory of exactly five files:

- ``manifest.json``, the ``CorpusTakeManifest``: the take id (``take-<SHA-256 of
  original.wav>``), the subject (``COLE_SUBJECT``), the exact prompt and its canonical digest,
  the catalog version it was recorded against, the request time, the client, host and device,
  the audio, and a summary of the continuity evidence with its digest;
- ``qc.json``, the client's ``QcReport`` of the one recording (never authoritative: intake
  recomputes it from the WAV);
- ``READY``, the ``ReadyMarker`` that binds the two, written last;
- ``original.wav``, the take: canonical mono 48 kHz s24le PCM;
- ``continuity.json``, the worker's ``PortAudioContinuityEvidence``, bound to that WAV.

Its control files are the capture package's, so the resumable upload (``intake_client``,
``api.staging``) carries it unchanged and the publisher publishes it into
``raw/<manifest_sha256>/`` like any package; ``intake.validate_package`` tells the two kinds
apart by the manifest's ``schema_version``. ``validate_take_package`` is as strict as
``intake.validate_capture_package``: the exact file set, no links or directories, canonical
JSON, every digest and byte count, the WAV's format, and evidence that succeeded and describes
exactly this WAV. ``admit_take`` adds the PC's own rules before anything is published: no
simulated audio, no diagnostic root, and a prompt the take's catalog version (a snapshot on
this server, digest-checked) counts.

``build_take_package`` is the client's side: it copies a finished take into a new package and
validates it with the same validator.

``CorpusTakeManifestV2`` is ``cole-voice-lab.corpus-take/v2`` (D-056): a take that declares its
capture profile (``capture_profiles``) and carries its backend's evidence (``alsa_evidence``),
in the same five files, so the upload, the staging and the publisher carry it unchanged. Its
WAV is what the device gave (the house panel's: 16 kHz, 2 or 6 channels, a plain 44-byte
header), not the canonical format. ``validate_profile_take_package`` is as strict as v1's
validator, recomputes the evidence's continuity rule, and judges the take (QC) by its
profile's primary channel; ``admit_take`` applies the same rules to both versions. Every
dispatch point knows v2: ``intake.validate_package``, ``_raw_session_id`` and ``_admit``,
``corpus_take_store.record_takes``, the vault's raw source and ``recording_library`` (through
``CORPUS_TAKE_SCHEMAS``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from .alsa_evidence import (
    ALSA_FORMAT_ENCODINGS,
    MAX_TAKE_SECONDS,
    MAX_TOLERANCE_NS,
    AlsaContinuityEvidence,
)
from .alsa_evidence import MAX_EVIDENCE_BYTES as MAX_ALSA_EVIDENCE_BYTES
from .audio import (
    CANONICAL_CHANNELS,
    CANONICAL_ENCODING,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
    WavInspection,
    inspect_canonical_wav,
    inspect_pcm_wav,
)
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    read_canonical_model,
    rename_no_replace,
    sha256_file,
    write_canonical_json,
)
from .capture import MANIFEST_NAME, QC_REPORT_NAME, READY_NAME, ReadyMarker
from .capture_profiles import XVF3800_SAMPLE_RATE_HZ, CaptureProfile
from .catalog_ports import CorpusStoreError
from .diagnostic_roots import read_diagnostic_root_marker
from .errors import ContractError, SecurityError, VoiceLabError
from .intake import (
    INTAKE_QC_REPORT_NAME,
    MAX_CONTROL_FILE_BYTES,
    RECEIPT_NAME,
    IntakeReceipt,
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
    utc_now,
)
from .paths import AssetLayout
from .portaudio_evidence import MAX_EVIDENCE_BYTES, WAV_HEADER_BYTES, PortAudioContinuityEvidence
from .recording_contracts import CatalogReference

TAKE_SCHEMA: Final = "cole-voice-lab.corpus-take/v1"
#: A take that declares its capture profile and carries its backend's evidence (D-056).
TAKE_V2_SCHEMA: Final = "cole-voice-lab.corpus-take/v2"
#: The Swift app's takes as the PC imports them (``corpus_take_imports``, D-053).
TAKE_IMPORT_SCHEMA: Final = "cole-voice-lab.corpus-take-import/v1"
#: Every package schema of the PC's corpus: its own takes, and the takes it imported.
CORPUS_TAKE_SCHEMAS: Final = frozenset({TAKE_SCHEMA, TAKE_V2_SCHEMA, TAKE_IMPORT_SCHEMA})
AUDIO_NAME: Final = "original.wav"
CONTINUITY_NAME: Final = "continuity.json"
TAKE_FILES: Final = frozenset(
    {MANIFEST_NAME, QC_REPORT_NAME, READY_NAME, AUDIO_NAME, CONTINUITY_NAME}
)
#: Eleven minutes of the canonical format, and with its header (the capture worker stops at
#: ten).
MAX_TAKE_FRAMES: Final = CANONICAL_SAMPLE_RATE_HZ * 660
MAX_TAKE_AUDIO_BYTES: Final = WAV_HEADER_BYTES + MAX_TAKE_FRAMES * CANONICAL_SAMPLE_WIDTH_BYTES
#: The largest v2 take: 660 s of the 6-channel image at 16 kHz in s32le, with its header.
MAX_PROFILE_TAKE_AUDIO_BYTES: Final = (
    WAV_HEADER_BYTES + XVF3800_SAMPLE_RATE_HZ * MAX_TAKE_SECONDS * 6 * 4
)
TAKE_ID_PATTERN: Final = r"^take-[0-9a-f]{64}$"
#: A UMIK-1 by the name its driver gives PortAudio: voice-lab-desktop's rule
#: (``measurement.chain.is_umik1``), so the PC admits what the app records with.
_UMIK1_NAME: Final = re.compile(r"umik[\s_-]?1(?!\d)", re.IGNORECASE)

type TakeId = Annotated[str, StringConstraints(pattern=TAKE_ID_PATTERN)]
type _Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]

#: Why the PC refused a valid take (``TakeRefusal.code``); each refusal's text starts with it.
type TakeRefusalCode = Literal[
    "take_simulated",
    "take_diagnostic_root",
    "take_catalog_invalid",
    "take_prompt_not_in_catalog",
]
#: Why the PC cannot judge a take yet (``TakeDeferral.code``): the server's state, not the take.
type TakeDeferralCode = Literal["take_catalog_missing", "take_catalog_unreadable"]
#: The store's answers that mean the take's catalog version is not on this server yet.
_NOT_HERE_YET: Final = frozenset(
    {"CORPUS.STORE_MISSING", "CORPUS.CATALOG_UNKNOWN", "CORPUS.VERSION_UNKNOWN"}
)


class TakeRefusal(ContractError):
    """A valid take package the PC does not take into its corpus, with a stable ``code``."""

    def __init__(self, code: TakeRefusalCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code: TakeRefusalCode = code


class TakeDeferral(VoiceLabError):
    """A take the PC cannot judge yet, because of its own state, with a stable ``code``.

    The catalog version the take names is not on this server yet, or this server's copy of
    it cannot be read. Neither is the take's fault, and a refusal would be final (the upload
    session is the take's audio digest), so intake leaves the package in ``incoming/``. The
    next pass (the path unit, or the intake timer within 15 minutes) judges it again.
    """

    def __init__(self, code: TakeDeferralCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code: TakeDeferralCode = code


def take_id_for(audio_sha256: str) -> str:
    """The id of the take whose ``original.wav`` has this SHA-256."""
    return f"take-{audio_sha256}"


def _utc(value: datetime, what: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} requires an explicit timezone")
    return value.astimezone(UTC)


class TakeClient(ContractModel):
    """The program that recorded the take: its name, version and platform, as it reports them."""

    name: _Text
    version: _Text
    platform: _Text


class TakeAudio(ContractModel):
    """The take's one recording."""

    path: Literal["original.wav"] = AUDIO_NAME
    sha256: Sha256
    size_bytes: int = Field(gt=WAV_HEADER_BYTES, le=MAX_TAKE_AUDIO_BYTES)
    audio: AudioSpec

    @model_validator(mode="after")
    def _canonical_format(self) -> TakeAudio:
        spec = self.audio
        if (
            spec.sample_rate_hz != CANONICAL_SAMPLE_RATE_HZ
            or spec.channels != CANONICAL_CHANNELS
            or spec.sample_width_bytes != CANONICAL_SAMPLE_WIDTH_BYTES
            or spec.encoding != CANONICAL_ENCODING
        ):
            raise ValueError("a take is canonical mono 48 kHz s24le PCM")
        return self


class TakeContinuity(ContractModel):
    """The worker's continuity evidence: its file, and what the PC shows without reading it.

    ``started_at`` is when the worker's stream started (its clock); every field here equals
    the evidence file's own."""

    path: Literal["continuity.json"] = CONTINUITY_NAME
    sha256: Sha256
    size_bytes: int = Field(gt=0, le=MAX_EVIDENCE_BYTES)
    host_api: _Text
    device_name: _Text
    simulated: bool
    started_at: datetime
    stop_reason: Literal["requested", "maximum-duration"]
    max_abs_deviation_frames: float | None = Field(ge=0, allow_inf_nan=False)

    @field_validator("started_at")
    @classmethod
    def _started_utc(cls, value: datetime) -> datetime:
        return _utc(value, "the stream's start")


class CorpusTakeManifest(ContractModel):
    """``manifest.json`` of a take package: one take of one prompt, immutable."""

    schema_version: Literal["cole-voice-lab.corpus-take/v1"] = TAKE_SCHEMA
    take_id: TakeId
    subject_id: StableId
    #: The exact prompt the take was recorded against, and its canonical digest.
    prompt: Prompt
    prompt_sha256: Sha256
    catalog: CatalogReference
    #: When the operator asked for the take (UTC, the capturing host's clock).
    requested_at: datetime
    capture_host: _Text
    client: TakeClient
    capture_device: CaptureDevice
    audio: TakeAudio
    continuity: TakeContinuity

    @field_validator("requested_at")
    @classmethod
    def _requested_utc(cls, value: datetime) -> datetime:
        return _utc(value, "the request time")

    @model_validator(mode="after")
    def _bound(self) -> CorpusTakeManifest:
        _check_take(self)
        if self.capture_device.backend != "portaudio":
            raise ValueError("a take is recorded by the PortAudio capture worker (D-039)")
        if self.catalog.purpose == "physical-test":
            raise ValueError("a take is never recorded against a physical-test catalog")
        return self


def _check_take(manifest: CorpusTakeManifest | CorpusTakeManifestV2) -> None:
    """The bindings every take's manifest has, whatever its version.

    Raises: ``ValueError``."""
    if manifest.take_id != take_id_for(manifest.audio.sha256):
        raise ValueError("the take id is take-<the SHA-256 of original.wav>")
    if manifest.subject_id != COLE_SUBJECT:
        raise ValueError(f"a take is of {COLE_SUBJECT} (D-050)")
    if manifest.prompt_sha256 != canonical_sha256(manifest.prompt):
        raise ValueError("prompt_sha256 is not the canonical digest of the prompt")


class ProfileTakeAudio(ContractModel):
    """A v2 take's one recording, in a format its capture profile allows."""

    path: Literal["original.wav"] = AUDIO_NAME
    sha256: Sha256
    size_bytes: int = Field(gt=WAV_HEADER_BYTES, le=MAX_PROFILE_TAKE_AUDIO_BYTES)
    audio: AudioSpec

    @model_validator(mode="after")
    def _plain_pcm(self) -> ProfileTakeAudio:
        spec = self.audio
        if self.size_bytes != WAV_HEADER_BYTES + spec.frames * spec.channels * (
            spec.sample_width_bytes
        ):
            raise ValueError("a v2 take's WAV is a 44-byte header and its frames")
        return self


class AlsaTakeContinuity(ContractModel):
    """A v2 take's ALSA continuity evidence (``alsa_evidence``): its file, and what the PC
    shows without reading it. Every field here equals the evidence file's own."""

    path: Literal["continuity.json"] = CONTINUITY_NAME
    sha256: Sha256
    size_bytes: int = Field(gt=0, le=MAX_ALSA_EVIDENCE_BYTES)
    pcm: _Text
    simulated: bool
    started_at: datetime
    stop_reason: Literal["requested", "maximum-duration"]
    tolerance_ns: int = Field(ge=1, le=MAX_TOLERANCE_NS)
    max_abs_deviation_ns: int | None = Field(ge=0)

    @field_validator("started_at")
    @classmethod
    def _started_utc(cls, value: datetime) -> datetime:
        return _utc(value, "the stream's start")


class CorpusTakeManifestV2(ContractModel):
    """``manifest.json`` of a v2 take package: one take of one prompt, by a capture profile.

    A v1 take's fields, but for its device: the capture profile names the device, the room,
    each channel's source and the primary channel, and the continuity evidence is the
    profile's backend's (ALSA for ``xvf3800-linear-4``)."""

    schema_version: Literal["cole-voice-lab.corpus-take/v2"] = TAKE_V2_SCHEMA
    take_id: TakeId
    subject_id: StableId
    prompt: Prompt
    prompt_sha256: Sha256
    catalog: CatalogReference
    requested_at: datetime
    capture_host: _Text
    client: TakeClient
    capture_profile: CaptureProfile
    audio: ProfileTakeAudio
    continuity: AlsaTakeContinuity

    @field_validator("requested_at")
    @classmethod
    def _requested_utc(cls, value: datetime) -> datetime:
        return _utc(value, "the request time")

    @model_validator(mode="after")
    def _bound(self) -> CorpusTakeManifestV2:
        _check_take(self)
        if self.catalog.purpose == "physical-test":
            raise ValueError("a take is never recorded against a physical-test catalog")
        spec = self.audio.audio
        if not self.capture_profile.allows(spec):
            raise ValueError(
                f"profile {self.capture_profile.profile} takes 16 kHz audio with one channel "
                f"per entry of its channels ({len(self.capture_profile.channels)}), not "
                f"{spec.sample_rate_hz} Hz with {spec.channels}"
            )
        if spec.frames > spec.sample_rate_hz * MAX_TAKE_SECONDS:
            raise ValueError(f"a take lasts at most {MAX_TAKE_SECONDS} s")
        return self


@dataclass(frozen=True, slots=True)
class ValidatedTake:
    """A take package that passed ``validate_take_package``, and what it showed."""

    manifest: CorpusTakeManifest
    manifest_sha256: str
    source_qc_report: QcReport
    source_qc_report_sha256: str
    #: Intake's own QC of the WAV (the persisted ``intake-qc.json`` of a published package).
    qc_report: QcReport
    qc_report_sha256: str
    ready: ReadyMarker
    evidence: PortAudioContinuityEvidence
    _snapshot: PackageSnapshot

    @property
    def session_id(self) -> str:
        """The id the intake protocol knows the package by: the take id."""
        return self.manifest.take_id


@dataclass(frozen=True, slots=True)
class ValidatedProfileTake:
    """A v2 take package that passed ``validate_profile_take_package``, and what it showed."""

    manifest: CorpusTakeManifestV2
    manifest_sha256: str
    source_qc_report: QcReport
    source_qc_report_sha256: str
    #: Intake's own QC of the WAV's primary channel (the persisted ``intake-qc.json`` of a
    #: published package).
    qc_report: QcReport
    qc_report_sha256: str
    ready: ReadyMarker
    evidence: AlsaContinuityEvidence
    _snapshot: PackageSnapshot

    @property
    def session_id(self) -> str:
        """The id the intake protocol knows the package by: the take id."""
        return self.manifest.take_id


def package_schema(package: Path) -> str | None:
    """The ``schema_version`` that ``package/manifest.json`` declares, or ``None``.

    Reads at most the control-file bound through the no-follow boundary (never blocking on a
    FIFO) and decides nothing else: an unreadable or malformed manifest declares nothing, and
    the capture validator then refuses it. Raises: nothing."""
    try:
        with open_regular_no_follow(package / MANIFEST_NAME, nonblocking=True) as stream:
            payload = stream.read(MAX_CONTROL_FILE_BYTES + 1)
    except (SecurityError, OSError):
        return None
    return manifest_schema(payload)


def manifest_schema(manifest: bytes) -> str | None:
    """The ``schema_version`` string of a manifest's bytes when they are a JSON object.

    Raises: nothing."""
    try:
        document = json.loads(manifest)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    schema = document.get("schema_version")
    return schema if isinstance(schema, str) else None


def is_take_package(package: Path) -> bool:
    """Whether ``package/manifest.json`` declares a take (its ``schema_version``).

    Raises: nothing (``package_schema``)."""
    return package_schema(package) == TAKE_SCHEMA


def is_corpus_take_package(package: Path) -> bool:
    """Whether ``package`` declares a take of the PC's corpus: a take, or an imported one.

    Raises: nothing (``package_schema``)."""
    return package_schema(package) in CORPUS_TAKE_SCHEMAS


def declares_take(manifest: bytes) -> bool:
    """Whether a manifest's bytes are a JSON object whose ``schema_version`` is a take's.

    Raises: nothing."""
    return manifest_schema(manifest) == TAKE_SCHEMA


def take_qc_report(
    take_id: str,
    prompt: Prompt,
    manifest_sha256: str,
    inspection: WavInspection,
    generated_at: datetime,
) -> QcReport:
    """A take's QC report: capture's per-recording rule, quiet allowed for a silent prompt.

    The same for a take and an imported take (``corpus_take_imports``). Raises: nothing."""
    metric = inspection.qc_metrics(AUDIO_NAME, allow_quiet=not prompt.spoken)
    return QcReport(
        session_id=take_id,
        manifest_sha256=manifest_sha256,
        generated_at=generated_at,
        status="pass" if metric.passed else "review",
        metrics=(metric,),
    )


def _qc_report(
    manifest: CorpusTakeManifest,
    manifest_sha256: str,
    inspection: WavInspection,
    generated_at: datetime,
) -> QcReport:
    return take_qc_report(
        manifest.take_id, manifest.prompt, manifest_sha256, inspection, generated_at
    )


def take_intake_qc(
    package: Path,
    *,
    take_id: str,
    prompt: Prompt,
    manifest_sha256: str,
    source_report_sha256: str,
    inspection: WavInspection,
    ready: ReadyMarker,
    published: bool,
) -> tuple[QcReport, str]:
    """Intake's QC report of a take package and its digest.

    For a package being accepted (``published`` false) it is computed from the WAV, as of the
    READY marker's time. For a published package in ``raw/`` the persisted ``intake-qc.json``
    must equal the report recomputed from the WAV, and the intake receipt must bind the take,
    its manifest and both reports.
    Raises: ``SecurityError`` when the persisted report or the receipt disagrees;
    ``ContractError`` for a persisted file that is not canonical."""
    if not published:
        report = take_qc_report(take_id, prompt, manifest_sha256, inspection, ready.finalized_at)
        return report, hashlib.sha256(canonical_json_bytes(report) + b"\n").hexdigest()
    persisted = read_canonical_model(
        package / INTAKE_QC_REPORT_NAME, QcReport, max_bytes=MAX_CONTROL_FILE_BYTES
    )
    report = take_qc_report(take_id, prompt, manifest_sha256, inspection, persisted.generated_at)
    if persisted != report:
        raise SecurityError(
            "persisted intake QC does not match independently recomputed WAV metrics"
        )
    report_sha256, _ = sha256_file(package / INTAKE_QC_REPORT_NAME)
    receipt = read_canonical_model(
        package / RECEIPT_NAME, IntakeReceipt, max_bytes=MAX_CONTROL_FILE_BYTES
    )
    if (
        receipt.session_id != take_id
        or receipt.manifest_sha256 != manifest_sha256
        or receipt.source_qc_report_sha256 != source_report_sha256
        or receipt.intake_qc_report_sha256 != report_sha256
    ):
        raise SecurityError("published intake receipt disagrees with the raw take's evidence")
    return report, report_sha256


def _require_bound_evidence(
    manifest: CorpusTakeManifest, evidence: PortAudioContinuityEvidence
) -> None:
    if not evidence.success:
        raise ContractError(f"the capture worker did not succeed: {evidence.error or 'no reason'}")
    audio = manifest.audio
    if (
        evidence.wav_sha256 != audio.sha256
        or evidence.wav_size_bytes != audio.size_bytes
        or evidence.frames != audio.audio.frames
    ):
        raise SecurityError("the continuity evidence describes other audio than original.wav")
    summary = manifest.continuity
    if (
        summary.host_api != evidence.host_api
        or summary.device_name != evidence.device_name
        or summary.simulated != evidence.simulated
        or summary.started_at != evidence.started_at
        or summary.stop_reason != evidence.stop_reason
        or summary.max_abs_deviation_frames != evidence.max_abs_deviation_frames
    ):
        raise ContractError("the manifest's continuity summary disagrees with the evidence")


def _take_controls[ManifestT: (CorpusTakeManifest, CorpusTakeManifestV2)](
    package: Path, manifest_model: type[ManifestT], *, allow_intake_receipt: bool
) -> tuple[PackageSnapshot, ReadyMarker, ManifestT, QcReport, str, str]:
    """A take package's exact file set and its three control files, bound to each other.

    Returns: the package's snapshot, its READY marker, manifest and QC report, and the
    manifest's and the report's digests.
    Raises: as ``validate_take_package``."""
    snapshot = _snapshot_package(package)
    files = set(snapshot.files)
    if READY_NAME not in files:
        raise ContractError(f"partial take has no {READY_NAME} marker: {package}")
    expected = set(TAKE_FILES)
    if allow_intake_receipt:
        expected.update({INTAKE_QC_REPORT_NAME, RECEIPT_NAME})
    directories = sorted(set(snapshot.directories) - {"."})
    if files != expected or directories:
        raise SecurityError(
            f"take package files differ from a take's; missing={sorted(expected - files)}, "
            f"undeclared={sorted(files - expected)}, directories={directories}"
        )

    marker = read_canonical_model(
        package / READY_NAME, ReadyMarker, max_bytes=MAX_CONTROL_FILE_BYTES
    )
    manifest = read_canonical_model(
        package / MANIFEST_NAME, manifest_model, max_bytes=MAX_CONTROL_FILE_BYTES
    )
    source_report = read_canonical_model(
        package / QC_REPORT_NAME, QcReport, max_bytes=MAX_CONTROL_FILE_BYTES
    )
    manifest_sha256, _ = sha256_file(package / MANIFEST_NAME)
    source_report_sha256, _ = sha256_file(package / QC_REPORT_NAME)
    if manifest_sha256 != marker.manifest_sha256:
        raise SecurityError("READY manifest digest does not match manifest.json")
    if source_report_sha256 != marker.qc_report_sha256:
        raise SecurityError("READY QC digest does not match qc.json")
    if marker.session_id != manifest.take_id or source_report.session_id != manifest.take_id:
        raise ContractError("take IDs disagree across the take's evidence")
    if source_report.manifest_sha256 != manifest_sha256:
        raise SecurityError("QC report is not bound to the take's manifest")
    if [metric.path for metric in source_report.metrics] != [AUDIO_NAME]:
        raise ContractError(f"QC metrics must cover {AUDIO_NAME} exactly once")
    return snapshot, marker, manifest, source_report, manifest_sha256, source_report_sha256


def validate_take_package(package: Path, *, allow_intake_receipt: bool = False) -> ValidatedTake:
    """Independently verify every byte and contract of a complete take package.

    ``allow_intake_receipt`` is for re-validating a published package in ``raw/``, which also
    holds intake's ``intake-qc.json`` and ``intake-receipt.json``; an incoming package that
    supplies them itself fails closed.
    Returns: the validated take.
    Raises: ``SecurityError`` for links, hard links, a file set other than the take's, a
    digest or byte count that disagrees, or a package that changed while it was read;
    ``ContractError`` for a missing READY, a contract or canonical-form failure, a WAV that is
    not canonical, or evidence that is unsuccessful or summarized wrongly.
    """
    (
        snapshot_before,
        marker,
        manifest,
        source_report,
        manifest_sha256,
        source_report_sha256,
    ) = _take_controls(package, CorpusTakeManifest, allow_intake_receipt=allow_intake_receipt)
    if "umik-1" not in manifest.capture_device.display_name.casefold():
        raise ContractError("take manifest does not identify the required UMIK-1 microphone")

    inspection = inspect_canonical_wav(package / AUDIO_NAME)
    if (
        inspection.sha256 != manifest.audio.sha256
        or inspection.size_bytes != manifest.audio.size_bytes
    ):
        raise SecurityError(f"recording hash or byte count was tampered: {AUDIO_NAME}")
    if inspection.audio != manifest.audio.audio:
        raise ContractError(f"recording format evidence disagrees: {AUDIO_NAME}")

    evidence = read_canonical_model(
        package / CONTINUITY_NAME, PortAudioContinuityEvidence, max_bytes=MAX_EVIDENCE_BYTES
    )
    evidence_digest = sha256_file(package / CONTINUITY_NAME)
    if evidence_digest != (manifest.continuity.sha256, manifest.continuity.size_bytes):
        raise SecurityError(
            f"continuity evidence hash or byte count was tampered: {CONTINUITY_NAME}"
        )
    _require_bound_evidence(manifest, evidence)
    # The worker's own device name, not only the manifest's label (a simulated take is
    # refused by ``admit_take`` under its own code).
    if not evidence.simulated and _UMIK1_NAME.search(evidence.device_name) is None:
        raise ContractError(
            f"the take was recorded from {evidence.device_name!r}, not the required UMIK-1"
        )

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
        raise SecurityError("take package identities or bytes changed while being validated")
    return ValidatedTake(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        source_qc_report=source_report,
        source_qc_report_sha256=source_report_sha256,
        qc_report=report,
        qc_report_sha256=report_sha256,
        ready=marker,
        evidence=evidence,
        _snapshot=snapshot_after,
    )


def _require_bound_alsa_evidence(
    manifest: CorpusTakeManifestV2, evidence: AlsaContinuityEvidence
) -> None:
    if not evidence.success:
        raise ContractError(f"the capture client did not succeed: {evidence.error or 'no reason'}")
    audio = manifest.audio
    spec = audio.audio
    if (
        evidence.wav is None
        or evidence.wav.sha256 != audio.sha256
        or evidence.wav.size_bytes != audio.size_bytes
        or evidence.frames != spec.frames
    ):
        raise SecurityError("the continuity evidence describes other audio than original.wav")
    if (
        evidence.rate != spec.sample_rate_hz
        or evidence.channels != spec.channels
        or ALSA_FORMAT_ENCODINGS[evidence.format] != spec.encoding
    ):
        raise ContractError("original.wav is not in the format the PCM was opened with")
    summary = manifest.continuity
    if (
        summary.pcm != evidence.pcm
        or summary.simulated != evidence.simulated
        or summary.started_at != evidence.started_at
        or summary.stop_reason != evidence.stop_reason
        or summary.tolerance_ns != evidence.tolerance_ns
        or summary.max_abs_deviation_ns != evidence.max_abs_deviation_ns
    ):
        raise ContractError("the manifest's continuity summary disagrees with the evidence")


def validate_profile_take_package(
    package: Path, *, allow_intake_receipt: bool = False
) -> ValidatedProfileTake:
    """Independently verify every byte and contract of a complete v2 take package (D-056).

    As strict as ``validate_take_package``: the exact file set, no links or directories,
    canonical JSON, and every digest and byte count. The WAV is the one the manifest
    describes, in a format its capture profile allows (the manifest's own rules), with a plain
    44-byte header. The ALSA evidence succeeded, describes exactly this WAV and its format,
    and passes the continuity rule the PC recomputes (``alsa_evidence``). Intake's QC is of
    the profile's primary channel, with a desktop take's thresholds.
    Params and returns: as ``validate_take_package``, for a v2 take.
    Raises: ``SecurityError``, ``ContractError``: as ``validate_take_package``.
    """
    (
        snapshot_before,
        marker,
        manifest,
        source_report,
        manifest_sha256,
        source_report_sha256,
    ) = _take_controls(package, CorpusTakeManifestV2, allow_intake_receipt=allow_intake_receipt)

    inspection = inspect_pcm_wav(
        package / AUDIO_NAME, channel=manifest.capture_profile.primary_channel
    )
    if (
        inspection.sha256 != manifest.audio.sha256
        or inspection.size_bytes != manifest.audio.size_bytes
    ):
        raise SecurityError(f"recording hash or byte count was tampered: {AUDIO_NAME}")
    if inspection.audio != manifest.audio.audio:
        raise ContractError(f"recording format evidence disagrees: {AUDIO_NAME}")

    evidence = read_canonical_model(
        package / CONTINUITY_NAME, AlsaContinuityEvidence, max_bytes=MAX_ALSA_EVIDENCE_BYTES
    )
    evidence_digest = sha256_file(package / CONTINUITY_NAME)
    if evidence_digest != (manifest.continuity.sha256, manifest.continuity.size_bytes):
        raise SecurityError(
            f"continuity evidence hash or byte count was tampered: {CONTINUITY_NAME}"
        )
    _require_bound_alsa_evidence(manifest, evidence)

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
        raise SecurityError("take package identities or bytes changed while being validated")
    return ValidatedProfileTake(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        source_qc_report=source_report,
        source_qc_report_sha256=source_report_sha256,
        qc_report=report,
        qc_report_sha256=report_sha256,
        ready=marker,
        evidence=evidence,
        _snapshot=snapshot_after,
    )


#: Set only by the contract fixture (``contract_fixture``, D-056): its publisher pass admits a
#: simulated take, so a client's live check needs no microphone. No production entry point sets
#: it, and a test holds every other module to that (``test_contract_fixture``).
_ADMIT_SIMULATED: ContextVar[bool] = ContextVar("admit_simulated_takes", default=False)


@contextmanager
def admitting_simulated_takes() -> Iterator[None]:
    """Within this block (this thread's context), ``admit_take`` admits simulated audio.

    For the contract fixture's server alone."""
    token = _ADMIT_SIMULATED.set(True)
    try:
        yield
    finally:
        _ADMIT_SIMULATED.reset(token)


def admit_take(layout: AssetLayout, take: ValidatedTake | ValidatedProfileTake) -> Prompt:
    """The PC's rules for a valid take of either version, checked before intake publishes it.

    The take's audio is real (not the simulated device's), the asset root is not a
    diagnostic root, the catalog version the take names is the one this server's store
    published under that id and number (every field of the reference), and that version's
    digest-checked snapshot counts the take's exact prompt
    (``CatalogSnapshot.current_prompt_for``: the prompt itself, or an earlier revision its
    alias accepts). The publisher is the store's writer (D-052), so it may open the store.
    Returns: the prompt of that catalog version the take counts for.
    Raises: ``TakeRefusal`` naming the rule; ``TakeDeferral`` when the store does not hold
    that version yet or cannot be read; ``OSError`` as the store's own.
    """
    manifest = take.manifest
    if take.evidence.simulated and not _ADMIT_SIMULATED.get():
        raise TakeRefusal(
            "take_simulated", "the simulated capture device's audio never enters the corpus"
        )
    require_corpus_root(layout)
    return admit_catalog(layout, manifest.catalog, manifest.prompt, manifest.prompt_sha256)


def require_corpus_root(layout: AssetLayout) -> None:
    """Refuse a take on an asset root marked for physical tests.

    Raises: ``TakeRefusal`` ``take_diagnostic_root``; as ``read_diagnostic_root_marker``."""
    if read_diagnostic_root_marker(layout) is not None:
        raise TakeRefusal(
            "take_diagnostic_root",
            "this asset root is marked for physical tests; it takes no takes",
        )


def admit_catalog(
    layout: AssetLayout, named: CatalogReference, prompt: Prompt, prompt_sha256: str
) -> Prompt:
    """The catalog rule of a take or an imported take: ``named`` is the version this server's
    store published under that id and number (every field of the reference), and that
    version's digest-checked snapshot counts ``prompt`` (``CatalogSnapshot.current_prompt_for``).

    Returns: the prompt of that catalog version the take counts for.
    Raises: ``TakeRefusal`` (``take_catalog_invalid``, ``take_prompt_not_in_catalog``);
    ``TakeDeferral`` when the store does not hold that version yet or cannot be read;
    ``OSError`` as the store's own.
    """
    from .storage_binding import open_catalog_reader  # the writer side only (D-052)

    version = f"catalog {named.catalog_id} version {named.catalog_version}"
    try:
        with open_catalog_reader(layout) as store:
            published = store.resolve_reference(named.catalog_id, named.catalog_version)
            if published != named:
                # A published version never changes, so this take can never count here.
                raise TakeRefusal(
                    "take_catalog_invalid",
                    f"{version} on this server is {published.catalog_sha256} (snapshot "
                    f"{published.snapshot_sha256}); the take names {named.catalog_sha256} "
                    f"(snapshot {named.snapshot_sha256})",
                )
            snapshot = store.materialize(published)
    except CorpusStoreError as error:
        if error.code in _NOT_HERE_YET:
            raise TakeDeferral(
                "take_catalog_missing", f"{version} is not on this server yet: {error}"
            ) from error
        raise TakeDeferral("take_catalog_unreadable", f"{version}: {error}") from error
    counted = snapshot.current_prompt_for(prompt)
    if counted is None:
        raise TakeRefusal(
            "take_prompt_not_in_catalog",
            f"catalog version {named.catalog_version} does not count prompt "
            f"{prompt.prompt_id} ({prompt_sha256})",
        )
    return counted


def copy_private(source: Path, destination: Path) -> None:
    """Copy one regular file into a new private file, synced."""
    with open_regular_no_follow(source) as reader:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())


def build_take_package(
    directory: Path,
    *,
    wav: Path,
    evidence: PortAudioContinuityEvidence,
    prompt: Prompt,
    catalog: CatalogReference,
    requested_at: datetime,
    capture_host: str,
    client: TakeClient,
    device: CaptureDevice,
    clock: Callable[[], datetime] = utc_now,
) -> ValidatedTake:
    """Package one finished take in the new directory ``directory``.

    ``wav`` is the take's ``original.wav`` and ``evidence`` the worker's evidence for it (the
    app has already checked the binding: ``voice_lab_desktop.capture.evidence``). Both are
    copied, never moved, so the take stays where the app keeps it. The package is assembled
    in a private sibling directory, validated with ``validate_take_package`` and renamed into
    place complete, never over an existing entry; on failure nothing is left behind.
    Returns: the validated package.
    Raises: ``FileExistsError`` when ``directory`` exists; ``ContractError``/``SecurityError``
    when the take cannot be described or does not validate; ``OSError``.
    """
    if os.path.lexists(directory):
        raise FileExistsError(f"the take package already exists: {directory}")
    work = directory.parent / f".{directory.name}.building-{secrets.token_hex(8)}"
    work.mkdir(mode=0o700)
    try:
        copy_private(wav, work / AUDIO_NAME)
        inspection = inspect_canonical_wav(work / AUDIO_NAME)
        continuity_sha256 = write_canonical_json(work / CONTINUITY_NAME, evidence)
        if evidence.started_at is None or evidence.stop_reason is None:
            raise ContractError("the continuity evidence is not of a finished take")
        try:
            manifest = CorpusTakeManifest(
                take_id=take_id_for(inspection.sha256),
                subject_id=COLE_SUBJECT,
                prompt=prompt,
                prompt_sha256=canonical_sha256(prompt),
                catalog=catalog,
                requested_at=requested_at,
                capture_host=capture_host,
                client=client,
                capture_device=device,
                audio=TakeAudio(
                    sha256=inspection.sha256,
                    size_bytes=inspection.size_bytes,
                    audio=inspection.audio,
                ),
                continuity=TakeContinuity(
                    sha256=continuity_sha256,
                    size_bytes=(work / CONTINUITY_NAME).stat().st_size,
                    host_api=evidence.host_api,
                    device_name=evidence.device_name,
                    simulated=evidence.simulated,
                    started_at=evidence.started_at,
                    stop_reason=evidence.stop_reason,
                    max_abs_deviation_frames=evidence.max_abs_deviation_frames,
                ),
            )
        except ValidationError as error:
            raise ContractError(f"the take cannot be described: {error}") from error
        manifest_sha256 = write_canonical_json(work / MANIFEST_NAME, manifest)
        finalized_at = clock()
        report = _qc_report(manifest, manifest_sha256, inspection, finalized_at)
        qc_sha256 = write_canonical_json(work / QC_REPORT_NAME, report)
        write_canonical_json(
            work / READY_NAME,
            ReadyMarker(
                session_id=manifest.take_id,
                manifest_sha256=manifest_sha256,
                qc_report_sha256=qc_sha256,
                finalized_at=finalized_at,
            ),
        )
        validate_take_package(work)
        rename_no_replace(work, directory)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return validate_take_package(directory)
