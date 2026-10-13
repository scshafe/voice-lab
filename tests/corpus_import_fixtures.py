"""A synthetic Mac library of Swift (native) takes for step 9's tests (D-053).

Takes are made through the take store itself (reserve, seal, save, decide, edit), over
generated tones in a test's temporary directory: never a person's voice, never a real library.
Each take has its own tone, so each has its own audio digest (the PC's take id).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from corpus_take_fixtures import NOW, write_tone

from cole_voice_lab.api.corpus_contracts import CatalogView
from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, write_canonical_json
from cole_voice_lab.capture import READY_NAME, CaptureContinuityEvidence, ReadyMarker
from cole_voice_lab.corpus_take_imports import CorpusTakeImportManifest
from cole_voice_lab.models import COLE_SUBJECT, CaptureDevice, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    CaptureReservation,
    CaptureSealReceipt,
    CatalogReference,
    CatalogSnapshot,
    DecisionCommand,
    RecordingBuildIdentity,
    RecordingFrameRange,
    RecordingSelection,
    RecordingTake,
    SaveTakeCommand,
)
from cole_voice_lab.recording_takes import RecordingTakeStore

MAC_DEVICE = CaptureDevice(
    backend="coreaudio", device_id="AppleUSBAudioEngine:miniDSP:Umik-1", display_name="Umik-1"
)
MAC_HOST = "coles-mac-synthetic"
BUILD = RecordingBuildIdentity(
    build_id="cole-voice-recorder-synthetic-1",
    source_sha256="1" * 64,
    executable_sha256="2" * 64,
    environment_sha256="3" * 64,
)
#: The Mac's takes happen before the PC's clock (``NOW``) of the tests.
MAC_START = NOW - timedelta(days=30)


@dataclass
class MacLibrary:
    """A Mac library and its take store, with a clock the tests move forward."""

    layout: AssetLayout
    now: datetime = MAC_START
    serial: int = 0
    store: RecordingTakeStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = RecordingTakeStore(self.layout, clock=lambda: self.now)

    def tick(self, seconds: float = 5.0) -> None:
        self.now += timedelta(seconds=seconds)

    def v2_group(self, catalog: tuple[CatalogReference, CatalogSnapshot]) -> str:
        """A recording-group/v2 against a published version of the Mac's own store."""
        reference, snapshot = catalog
        return self.store.open_group(
            reference=reference,
            suite=snapshot.suite,
            subject_id=COLE_SUBJECT,
            capture_device=MAC_DEVICE,
            capture_host=MAC_HOST,
        ).group_id

    def v1_group(self, suite: PromptSuite, *, subject_id: str = COLE_SUBJECT) -> str:
        """A recording-group/v1 embedding ``suite`` (the pilot catalogs' groups)."""
        return self.store.open_group(suite, subject_id, MAC_DEVICE, MAC_HOST).group_id

    def reserve(self, group_id: str, prompt_id: str) -> CaptureReservation:
        self.serial += 1
        self.tick()
        return self.store.reserve(
            operation_id=f"reserve-{self.serial}",
            group_id=group_id,
            prompt_id=prompt_id,
            process_generation="synthetic-generation",
        )

    def sealed(
        self,
        group_id: str,
        prompt_id: str,
        *,
        seconds: float = 0.25,
        frequency: float | None = None,
        seed: int | None = None,
    ) -> RecordingTake:
        """A sealed draft of ``prompt_id``: its own tone unless ``frequency``/``seed`` say."""
        reservation = self.reserve(group_id, prompt_id)
        number = self.serial
        path = self.layout.root / reservation.original_path
        write_tone(
            path,
            seconds=seconds,
            frequency=300.0 + 7.0 * number if frequency is None else frequency,
            seed=number if seed is None else seed,
        )
        os.chmod(path, 0o600)
        inspection = inspect_canonical_wav(path)
        frames = inspection.audio.frames
        first = reservation.requested_at + timedelta(milliseconds=100)
        last = first + timedelta(seconds=frames / 48_000)
        receipt = CaptureSealReceipt(
            take_id=reservation.take_id,
            reservation_sha256=canonical_sha256(reservation),
            process_generation=reservation.process_generation,
            recorder_build=BUILD,
            original_path=reservation.original_path,
            original_sha256=inspection.sha256,
            original_size_bytes=inspection.size_bytes,
            audio=inspection.audio,
            requested_at=reservation.requested_at,
            first_sample_at=first,
            last_sample_at=last,
            sealed_at=last + timedelta(milliseconds=200),
            continuity=CaptureContinuityEvidence(
                schema_version="cole-voice-lab.capture-continuity/v1",
                backend="coreaudio-audioqueue",
                device_uid=MAC_DEVICE.device_id,
                output_name="original.wav",
                wav_sha256=inspection.sha256,
                wav_size_bytes=inspection.size_bytes,
                frames=frames,
                received_frames=frames,
                callback_count=math.ceil(frames / 4096),
                first_sample_time=1000.0,
                last_sample_end=1000.0 + frames,
                success=True,
                continuity_checked=True,
                stop_reason="requested",
            ),
        )
        self.now = receipt.sealed_at + timedelta(seconds=1)
        return self.store.register_seal(operation_id=f"seal-{number}", receipt=receipt)

    def _selection(self, take: RecordingTake) -> RecordingSelection:
        return RecordingSelection(
            catalog_sha256=take.reservation.group.catalog_sha256,
            prompt_id=take.reservation.prompt.prompt_id,
            take_id=take.reservation.take_id,
            selection_revision=1,
            index_revision=0,
        )

    def save(self, take: RecordingTake, frames: tuple[int, int] | None = None) -> RecordingTake:
        """Save (keep) a draft, trimmed to ``frames`` (start, end) when given."""
        self.tick()
        assert take.frame_range is not None
        frame_range = take.frame_range
        if frames is not None:
            frame_range = RecordingFrameRange(
                source_frames=frame_range.source_frames, start_frame=frames[0], end_frame=frames[1]
            )
        command = SaveTakeCommand(
            operation_id=f"save-{take.reservation.take_id[5:21]}-{self.serial}",
            selection=self._selection(take),
            expected_decision_revision=take.decision_revision,
            expected_edit_revision=take.edit_revision,
            frame_range=frame_range,
            advance=False,
            waveform_ready=True,
        )
        return self.store.save(command).take

    def decide(self, take: RecordingTake, action: str, reason: str | None = None) -> RecordingTake:
        self.tick()
        self.serial += 1
        command = DecisionCommand.model_validate(
            {
                "selection": self._selection(take),
                "operation_id": f"decide-{self.serial}",
                "take_id": take.reservation.take_id,
                "expected_decision_revision": take.decision_revision,
                "action": action,
                "reason": reason,
            }
        )
        return self.store.decide(command).take


def catalog_view(catalog: tuple[CatalogReference, CatalogSnapshot]) -> CatalogView:
    """The PC's ``GET /api/v1/catalog`` answer for ``catalog``."""
    reference, snapshot = catalog
    return CatalogView(
        subject_id=COLE_SUBJECT,
        reference=reference,
        suite=snapshot.suite,
        prompt_aliases=snapshot.prompt_aliases,
        refreshed_at=NOW,
    )


def writable(package: Path) -> None:
    os.chmod(package, 0o700)
    for child in package.iterdir():
        os.chmod(child, 0o600)


def reseal_import(
    package: Path,
    *,
    manifest: dict[str, Any] | None = None,
    files: dict[str, bytes] | None = None,
) -> None:
    """Rewrite an imported take's control files after a test's change, so that only the
    change it makes is wrong: ``files`` replace record files (their digests are bound into the
    manifest), ``manifest`` updates the manifest's fields (raw JSON values), and qc.json and
    READY are re-bound to the new manifest."""
    writable(package)
    document = json.loads((package / "manifest.json").read_bytes())
    keys = {
        "seal.json": "seal",
        "reservation.json": "reservation",
        "group.json": "group",
        "mac-state.json": "mac_state",
    }
    for name, payload in (files or {}).items():
        (package / name).write_bytes(payload)
        if name in keys:
            document["records"][keys[name]] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
    document.update(manifest or {})
    manifest_bytes = canonical_json_bytes(document) + b"\n"
    (package / "manifest.json").write_bytes(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    report = json.loads((package / "qc.json").read_bytes())
    report["manifest_sha256"] = manifest_sha256
    report["session_id"] = document["take_id"]
    report_bytes = canonical_json_bytes(report) + b"\n"
    (package / "qc.json").write_bytes(report_bytes)
    marker = ReadyMarker.model_validate_json((package / READY_NAME).read_bytes())
    (package / READY_NAME).unlink()
    write_canonical_json(
        package / READY_NAME,
        marker.model_copy(
            update={
                "session_id": document["take_id"],
                "manifest_sha256": manifest_sha256,
                "qc_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            }
        ),
    )


def record_json(package: Path, name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((package / name).read_bytes())
    return document


def json_record(document: dict[str, Any]) -> bytes:
    return canonical_json_bytes(document) + b"\n"


def manifest_of(package: Path) -> CorpusTakeImportManifest:
    return CorpusTakeImportManifest.model_validate_json((package / "manifest.json").read_bytes())
