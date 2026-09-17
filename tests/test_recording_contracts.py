from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pickle
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError
from recording_fixtures import require_external_private_root

from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    write_canonical_json,
)
from cole_voice_lab.capture import CaptureContinuityEvidence
from cole_voice_lab.errors import ContractError
from cole_voice_lab.finetune import FineTuneErrorCode
from cole_voice_lab.models import (
    RESERVED_PROMPT_LABELS,
    AudioQcMetrics,
    AudioSpec,
    CaptureDevice,
    Prompt,
    PromptSuite,
)
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.recording_contracts import (
    CATALOG_EVENT_RESERVE_BYTES,
    CORPUS_SCHEMA_MODELS,
    FRAME_PREFIX_BYTES,
    MAX_CATALOG_SUITE_BYTES,
    MAX_CONTROL_FRAME_BYTES,
    MAX_FINISHED_RUNS,
    MAX_RECORDING_FRAMES,
    MAX_RECORDING_REVISION,
    MAX_TELEMETRY_FRAME_BYTES,
    RECORDING_SCHEMA_MODELS,
    CaptureReservation,
    CaptureSealReceipt,
    CatalogCommand,
    CatalogEvent,
    CatalogReference,
    CatalogSnapshot,
    ContextEvent,
    ContinueLegacyRecordingCommand,
    FineTuneEvent,
    FineTuneProgressEvent,
    FineTuneRunSummary,
    FineTuneStatusEvent,
    NativeRecoveryReceipt,
    NavigationEvent,
    OpenContextCommand,
    PreparedCaptureGate,
    PromptAlias,
    RecorderRequest,
    RecorderStateEvent,
    RecordingAnalysisResult,
    RecordingBuildIdentity,
    RecordingCursor,
    RecordingError,
    RecordingFrameRange,
    RecordingGroup,
    RecordingHistoryEntry,
    RecordingHistoryPage,
    RecordingMessageMeta,
    RecordingNavigationResult,
    RecordingPackageEntry,
    RecordingPackageMap,
    RecordingPromptNotCountedError,
    RecordingRecoveryFinding,
    RecordingRecoveryInventoryPage,
    RecordingRecoveryPage,
    RecordingRecoveryStatus,
    RecordingResponse,
    RecordingSaveOperation,
    RecordingSelection,
    RecordingTake,
    RecordingTelemetry,
    RecordingTransition,
    ReservationEvent,
    SaveEvent,
    ServiceRequest,
    SynthesisEvent,
    SynthesizeCommand,
    WaveformBucket,
    WaveformSnapshot,
    catalog_snapshot_path,
    keep_recordings_identity,
    native_recovery_bytes,
    recording_frame_bytes,
)


def _catalog_reference(suite: PromptSuite, **changes: Any) -> CatalogReference:
    digest = canonical_sha256(suite)
    snapshot = CatalogSnapshot(suite=suite, catalog_sha256=digest, prompt_aliases=())
    payload: dict[str, Any] = {
        "catalog_id": "cole-corpus",
        "catalog_version": 1,
        "catalog_sha256": digest,
        "snapshot_sha256": hashlib.sha256(canonical_json_bytes(snapshot) + b"\n").hexdigest(),
        "suite_id": suite.suite_id,
        "purpose": suite.purpose,
        "snapshot_path": catalog_snapshot_path(digest),
    }
    payload.update(changes)
    return CatalogReference.model_validate(payload)


def synthetic_contracts() -> dict[str, Any]:
    """Generate invented, non-human evidence solely for executable boundary tests."""
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    prompt = Prompt(
        prompt_id="synthetic-room",
        text="Remain silent.",
        category="room-tone",
        style="silence",
        expected_seconds=1.0,
    )
    catalog = PromptSuite(suite_id="synthetic-pilot", purpose="capture-pilot", prompts=(prompt,))
    device = CaptureDevice(backend="coreaudio", device_id="synthetic-uid", display_name="Synthetic")
    group = RecordingGroup(
        group_id="synthetic-group",
        context_revision=0,
        subject_id="synthetic-subject",
        catalog=catalog,
        catalog_sha256=canonical_sha256(catalog),
        capture_device=device,
        capture_host="synthetic-host",
        opened_at=now,
        reason="app-start",
        gate=PreparedCaptureGate(state="not-required"),
        room_tone_take_ids=("synthetic-take",),
    )
    build = RecordingBuildIdentity(
        build_id="synthetic-build",
        source_sha256="1" * 64,
        executable_sha256="2" * 64,
        environment_sha256="3" * 64,
    )
    reservation = CaptureReservation(
        take_id="synthetic-take",
        operation_id="synthetic-reserve",
        process_generation="synthetic-gen",
        group=group,
        prompt=prompt,
        requested_at=now,
        partial_path="tmp/recording-takes/synthetic-take/capture.partial.wav",
        original_path="tmp/recording-takes/synthetic-take/original.wav",
    )
    continuity = CaptureContinuityEvidence(
        schema_version="cole-voice-lab.capture-continuity/v1",
        backend="coreaudio-audioqueue",
        device_uid=device.device_id,
        output_name="original.wav",
        wav_sha256="4" * 64,
        wav_size_bytes=144_044,
        frames=48_000,
        received_frames=48_000,
        callback_count=12,
        first_sample_time=100.0,
        last_sample_end=48_100.0,
        success=True,
        continuity_checked=True,
        stop_reason="requested",
    )
    seal = CaptureSealReceipt(
        take_id=reservation.take_id,
        reservation_sha256=canonical_sha256(reservation),
        process_generation=reservation.process_generation,
        recorder_build=build,
        original_path=reservation.original_path,
        original_sha256=continuity.wav_sha256,
        original_size_bytes=continuity.wav_size_bytes,
        audio=AudioSpec(
            sample_rate_hz=48_000,
            channels=1,
            sample_width_bytes=3,
            encoding="pcm_s24le",
            frames=48_000,
            duration_seconds=1.0,
        ),
        requested_at=now,
        first_sample_at=now,
        last_sample_at=now + timedelta(seconds=1),
        sealed_at=now + timedelta(seconds=2),
        continuity=continuity,
    )
    frame_range = RecordingFrameRange(source_frames=48_000, start_frame=0, end_frame=48_000)
    take = RecordingTake(
        reservation=reservation,
        seal=seal,
        transport="draft",
        decision="draft",
        decision_revision=0,
        analysis="pending",
        export="unprepared",
        edit_revision=0,
        frame_range=frame_range,
        state_revision=2,
    )
    selection = RecordingSelection(
        catalog_sha256=group.catalog_sha256,
        prompt_id=prompt.prompt_id,
        take_id=take.reservation.take_id,
        selection_revision=1,
        index_revision=1,
    )
    save = RecordingSaveOperation(
        operation_id="synthetic-save",
        take_id=reservation.take_id,
        receipt_sha256=canonical_sha256(seal),
        selection=selection,
        decision_revision=1,
        edit_revision=0,
        frame_range=frame_range,
        advance=True,
        navigation=RecordingNavigationResult(
            catalog_sha256=group.catalog_sha256,
            index_revision=2,
            next_prompt=None,
            complete=True,
        ),
        committed_at=now + timedelta(seconds=3),
    )
    error = RecordingError(
        code="RECORDING.CONTINUITY_FAILED",
        message="Synthetic timestamp gap",
        retryable=False,
    )
    recovery = NativeRecoveryReceipt(
        take_id=reservation.take_id,
        reservation_sha256=canonical_sha256(reservation),
        process_generation=reservation.process_generation,
        recorded_at=now,
        partial_path=reservation.partial_path,
        received_frames=48_000,
        written_frames=47_000,
        state="partial",
        error=error,
    )
    analysis = RecordingAnalysisResult(
        take_id=reservation.take_id,
        source_sha256=seal.original_sha256,
        algorithm_version="synthetic-v1",
        analyzer_build=build,
        edit_revision=0,
        context_revision=0,
        state="verified",
        queued_at=now,
        completed_at=now + timedelta(seconds=3),
        metrics=AudioQcMetrics(
            path=reservation.original_path,
            duration_seconds=1.0,
            peak_dbfs=-6.0,
            rms_dbfs=-12.0,
            dc_offset=0.0,
            clipped_samples=0,
            longest_zero_run_ms=0.0,
            passed=True,
        ),
    )
    package = RecordingPackageMap(
        session_id="synthetic-session",
        manifest_sha256="5" * 64,
        package_path="tmp/capture/synthetic-session",
        group=group,
        entries=(
            RecordingPackageEntry(
                take_id=reservation.take_id,
                group_id=group.group_id,
                subject_id=group.subject_id,
                catalog_sha256=group.catalog_sha256,
                prompt=prompt,
                package_relative_path="room-tone-start.wav",
                original_sha256=seal.original_sha256,
                recorded_at=now,
                edit_identity_sha256="6" * 64,
            ),
        ),
        registered_at=now + timedelta(seconds=3),
    )
    meta = RecordingMessageMeta(
        request_id="synthetic-request",
        process_generation=reservation.process_generation,
        state_revision=2,
        event_sequence=1,
        take_id=reservation.take_id,
    )
    request = RecorderRequest.model_validate(
        {
            "data": {
                "command": "start",
                "reservation": reservation,
                "reservation_sha256": canonical_sha256(reservation),
            },
            "meta": meta,
        }
    )
    service = ServiceRequest.model_validate(
        {
            "data": {
                "command": "save",
                "operation_id": "synthetic-save",
                "selection": selection,
                "expected_decision_revision": 0,
                "expected_edit_revision": 0,
                "frame_range": frame_range,
                "advance": True,
                "waveform_ready": True,
            },
            "meta": meta,
        }
    )
    response = RecordingResponse(
        data=RecorderStateEvent(
            transport="draft",
            take_id=reservation.take_id,
            written_frames=48_000,
            seal=seal,
        ),
        error=None,
        meta=meta,
    )
    telemetry = RecordingTelemetry(
        data=WaveformSnapshot(
            sample_clock=None,
            take_id=reservation.take_id,
            process_generation=reservation.process_generation,
            first_frame=0,
            end_frame=480,
            written_frames=480,
            bucket_width_frames=240,
            buckets=(
                WaveformBucket(minimum=-0.5, maximum=0.25),
                WaveformBucket(minimum=0.0, maximum=0.0),
            ),
        ),
        meta=meta,
    )
    return {
        "recording-history-entry": RecordingHistoryEntry(
            take_id=reservation.take_id,
            subject_id=group.subject_id,
            group_id=group.group_id,
            legacy_session_id=None,
            catalog_sha256=group.catalog_sha256,
            prompt=prompt,
            recorded_at=now,
            date_precision="take",
            source_sha256=seal.original_sha256,
            source_frames=seal.audio.frames,
            decision="draft",
            analysis="pending",
            export="unprepared",
            state_revision=2,
        ),
        "recording-recovery-page": RecordingRecoveryPage(
            entries=(
                RecordingRecoveryStatus(
                    take_id=reservation.take_id, status="draft", recovered_seal=True
                ),
            )
        ),
        "recording-recovery-inventory": RecordingRecoveryInventoryPage(
            scan_generation="synthetic-recovery-scan",
            inventory_revision=2,
            scan_state="scanning",
            enumeration_complete=False,
            pending_count=1,
            finding_count=2,
            recovered_seal_count=1,
            entries=(
                RecordingRecoveryFinding(take_id="synthetic-pending", pending=True),
                RecordingRecoveryFinding(
                    take_id=reservation.take_id,
                    observed_take_revision=2,
                    pending=False,
                    result=RecordingRecoveryStatus(
                        take_id=reservation.take_id, status="draft", recovered_seal=True
                    ),
                ),
            ),
        ),
        "recording-group": group,
        "catalog-reference": _catalog_reference(catalog),
        "capture-reservation": reservation,
        "capture-seal": seal,
        "native-recovery": recovery,
        "recording-take": take,
        "recording-save": save,
        "recording-analysis": analysis,
        "recording-package-map": package,
        "recording-recorder-request": request,
        "recording-service-request": service,
        "recording-response": response,
        "recording-telemetry": telemetry,
        "recording-transition": RecordingTransition(
            take_id=reservation.take_id,
            previous="recording",
            current="stopping",
        ),
    }


def invalid_contracts() -> list[tuple[str, str, dict[str, Any]]]:
    """Malformed structural and semantic fixtures; every expected rejection is checked in Python."""
    values = synthetic_contracts()
    cases: list[tuple[str, str, dict[str, Any]]] = []
    mutations: list[tuple[str, str, tuple[str, ...], Any]] = [
        (
            "start-reservation-digest-mismatch",
            "recording-recorder-request",
            ("data", "reservation_sha256"),
            "0" * 64,
        ),
        (
            "start-reservation-digest-malformed",
            "recording-recorder-request",
            ("data", "reservation_sha256"),
            "mutable-reservation-alias",
        ),
        (
            "bad-version",
            "recording-recorder-request",
            ("meta", "protocol_version"),
            "v99",
        ),
        (
            "take-mismatch",
            "recording-recorder-request",
            ("meta", "take_id"),
            "different-take",
        ),
        (
            "generation-mismatch",
            "recording-recorder-request",
            ("meta", "process_generation"),
            "old-gen",
        ),
        (
            "negative-revision",
            "recording-recorder-request",
            ("meta", "state_revision"),
            -1,
        ),
        (
            "floating-revision",
            "recording-service-request",
            ("meta", "state_revision"),
            1.0,
        ),
        (
            "overflow-revision",
            "recording-service-request",
            ("meta", "state_revision"),
            9_223_372_036_854_775_808,
        ),
        (
            "boolean-revision",
            "recording-recorder-request",
            ("meta", "state_revision"),
            True,
        ),
        (
            "unknown-command",
            "recording-service-request",
            ("data", "command"),
            "evaluate",
        ),
        (
            "waveform-not-ready",
            "recording-service-request",
            ("data", "waveform_ready"),
            False,
        ),
        (
            "path-traversal",
            "capture-reservation",
            ("partial_path",),
            "tmp/../outside.wav",
        ),
        (
            "wrong-take-path",
            "capture-reservation",
            ("original_path",),
            "tmp/recording-takes/other/original.wav",
        ),
        (
            "wrong-prompt",
            "capture-reservation",
            ("prompt", "text"),
            "Changed exact text",
        ),
        ("wrong-catalog", "recording-group", ("catalog_sha256",), "0" * 64),
        ("unready-gate", "recording-group", ("gate", "state"), "ready"),
        ("wrong-size", "capture-seal", ("original_size_bytes",), 144_045),
        (
            "wrong-received-frames",
            "capture-seal",
            ("continuity", "received_frames"),
            48_001,
        ),
        (
            "wrong-sample-span",
            "capture-seal",
            ("continuity", "last_sample_end"),
            48_101.0,
        ),
        (
            "wrong-receipt-digest",
            "capture-seal",
            ("continuity", "wav_sha256"),
            "0" * 64,
        ),
        ("naive-time", "capture-seal", ("requested_at",), "2026-09-11T12:00:00"),
        ("out-of-order-time", "capture-seal", ("sealed_at",), "2026-09-11T11:59:59Z"),
        (
            "seal-reservation-mismatch",
            "recording-take",
            ("seal", "reservation_sha256"),
            "0" * 64,
        ),
        ("draft-no-seal", "recording-take", ("seal",), None),
        ("save-no-revision", "recording-take", ("decision",), "kept"),
        ("unchecked-raw", "recording-take", ("export",), "accepted-raw"),
        (
            "save-displayed-take",
            "recording-save",
            ("selection", "take_id"),
            "historical-take",
        ),
        ("empty-frame-range", "recording-save", ("frame_range", "start_frame"), 48_000),
        ("missing-next-result", "recording-save", ("navigation",), None),
        (
            "wrong-next-catalog",
            "recording-save",
            ("navigation", "catalog_sha256"),
            "0" * 64,
        ),
        ("partial-writes-exceed-input", "native-recovery", ("written_frames",), 49_000),
        ("partial-claims-sealed", "native-recovery", ("state",), "sealed"),
        ("partial-silent-error", "native-recovery", ("error",), None),
        ("analysis-missing-metrics", "recording-analysis", ("metrics",), None),
        (
            "analysis-inconsistent-pass",
            "recording-analysis",
            ("metrics", "passed"),
            False,
        ),
        ("analysis-no-completion", "recording-analysis", ("completed_at",), None),
        ("illegal-transition", "recording-transition", ("current",), "ready"),
        ("response-no-result", "recording-response", ("data",), None),
        (
            "waveform-future-frames",
            "recording-telemetry",
            ("data", "written_frames"),
            479,
        ),
        (
            "waveform-wrong-width",
            "recording-telemetry",
            ("data", "bucket_width_frames"),
            480,
        ),
        (
            "waveform-old-generation",
            "recording-telemetry",
            ("data", "process_generation"),
            "old-gen",
        ),
    ]
    for name, model, path, value in mutations:
        payload = copy.deepcopy(values[model].model_dump(mode="json"))
        target = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        cases.append((name, model, payload))
    missing_digest = values["recording-recorder-request"].model_dump(mode="json")
    del missing_digest["data"]["reservation_sha256"]
    cases.append(("start-reservation-digest-missing", "recording-recorder-request", missing_digest))
    for model, value in values.items():
        payload = value.model_dump(mode="json")
        payload["unknown_field"] = True
        cases.append((f"unknown-{model}", model, payload))
    return cases


def synthetic_commands() -> list[tuple[str, type[RecorderRequest | ServiceRequest], Any]]:
    values = synthetic_contracts()
    reservation = values["capture-reservation"]
    meta = values["recording-recorder-request"].meta
    build = values["capture-seal"].recorder_build
    selection = values["recording-save"].selection
    frame_range = values["recording-save"].frame_range
    command_cases = [
        (
            "handshake",
            {"role": "ui", "build": build, "expected_peer_build": build},
            None,
        ),
        ("snapshot", {}, None),
        ("shutdown", {}, None),
        ("catalog", {"operation_id": "op-catalog"}, None),
        (
            "open-context",
            {
                "operation_id": "op-open-context",
                "subject_id": reservation.group.subject_id,
                "catalog_reference": _catalog_reference(reservation.group.catalog),
                "capture_device": reservation.group.capture_device,
                "capture_host": reservation.group.capture_host,
            },
            None,
        ),
        (
            "reserve",
            {
                "operation_id": "op-reserve",
                "recorder_generation": "synthetic-gen",
                "group_id": reservation.group.group_id,
                "selection": selection,
            },
            reservation.take_id,
        ),
        (
            "register-seal",
            {"operation_id": "op-seal", "receipt": values["capture-seal"]},
            reservation.take_id,
        ),
        ("history", {"catalog_sha256": reservation.group.catalog_sha256}, None),
        ("recovery-snapshot", {"page_size": 50}, None),
        (
            "navigate",
            {
                "operation_id": "op-next",
                "selection": selection,
                "mode": "continue-missing",
            },
            reservation.take_id,
        ),
        (
            "navigate",
            {
                "operation_id": "op-select",
                "selection": selection,
                "mode": "selected-prompt",
                "target_prompt_id": reservation.prompt.prompt_id,
            },
            reservation.take_id,
        ),
        (
            "edit",
            {
                "operation_id": "op-edit",
                "selection": selection,
                "take_id": reservation.take_id,
                "source_sha256": "4" * 64,
                "expected_edit_revision": 0,
                "frame_range": frame_range,
            },
            reservation.take_id,
        ),
        (
            "decision",
            {
                "operation_id": "op-remove",
                "selection": selection,
                "take_id": reservation.take_id,
                "expected_decision_revision": 0,
                "action": "remove",
                "reason": "Synthetic",
            },
            reservation.take_id,
        ),
        (
            "decision",
            {
                "operation_id": "op-restore",
                "selection": selection,
                "take_id": reservation.take_id,
                "expected_decision_revision": 1,
                "action": "restore",
            },
            reservation.take_id,
        ),
        (
            "retry-analysis",
            {
                "operation_id": "op-retry",
                "take_id": reservation.take_id,
                "source_sha256": "4" * 64,
            },
            reservation.take_id,
        ),
        (
            "recover",
            {"operation_id": "op-recover", "take_id": reservation.take_id},
            reservation.take_id,
        ),
        (
            "recover",
            {"operation_id": "op-recover-page", "after_take_id": "prior-take", "page_size": 10},
            None,
        ),
        (
            "continue-legacy-recording",
            {
                "operation_id": "op-continue-legacy",
                "session_id": "unfinished-session",
                "session_evidence_sha256": "a" * 64,
                "context": {
                    "command": "open-context",
                    "operation_id": "op-continue-legacy",
                    "subject_id": reservation.group.subject_id,
                    "catalog": reservation.group.catalog,
                    "catalog_sha256": reservation.group.catalog_sha256,
                    "capture_device": reservation.group.capture_device,
                    "capture_host": reservation.group.capture_host,
                    "new_setup": True,
                },
            },
            None,
        ),
        (
            "cancel",
            {"operation_id": "op-cancel", "cancelled_operation_id": "op-reserve"},
            None,
        ),
        ("fine-tune", {"operation_id": "op-fine-tune", "allow_missing": True}, None),
        (
            "fine-tune",
            {"operation_id": "op-fine-tune-short", "epochs": 12, "allow_missing": False},
            None,
        ),
        (
            "cancel-fine-tune",
            {"operation_id": "op-cancel-fine-tune", "cancelled_operation_id": "op-fine-tune"},
            None,
        ),
        ("fine-tune-status", {"operation_id": "op-fine-tune-status"}, None),
    ]
    result = []
    for number, (name, body, take_id) in enumerate(command_cases):
        envelope = {
            "data": {"command": name, **body},
            "meta": meta.model_dump() | {"take_id": take_id},
        }
        result.append((f"service-{name}-{number}", ServiceRequest, envelope))
    for name, body, take_id in [
        (
            "handshake",
            {"role": "ui", "build": build, "expected_peer_build": build},
            None,
        ),
        ("prepare", {"device": reservation.group.capture_device}, None),
        (
            "stop",
            {"take_id": reservation.take_id, "operation_id": "op-stop"},
            reservation.take_id,
        ),
        ("snapshot", {}, None),
        ("shutdown", {}, None),
    ]:
        envelope = {
            "data": {"command": name, **body},
            "meta": meta.model_dump() | {"take_id": take_id},
        }
        result.append((f"recorder-{name}", RecorderRequest, envelope))
    result.append(
        (
            "recorder-maximum-revision",
            RecorderRequest,
            {
                "data": {"command": "snapshot"},
                "meta": meta.model_dump()
                | {"take_id": None, "state_revision": 9_223_372_036_854_775_807},
            },
        )
    )
    return result


@pytest.mark.parametrize("case", synthetic_commands(), ids=lambda case: case[0])
def test_every_command_is_typed_bounded_and_roundtrips(case: Any) -> None:
    _, model, payload = case
    parsed = model.model_validate(payload)
    assert model.model_validate_json(recording_frame_bytes(parsed)[4:]) == parsed


@pytest.mark.parametrize("case", synthetic_commands(), ids=lambda case: case[0])
def test_every_command_rejects_unknown_payload_fields(case: Any) -> None:
    _, model, original = case
    payload = copy.deepcopy(original)
    payload["data"]["arbitrary_argument"] = "not executable"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "model_name,path,new_value",
    [
        ("recording-group", ("capture_device", "backend"), "avfoundation"),
        ("recording-group", ("room_tone_take_ids",), ("same", "same")),
        ("capture-reservation", ("requested_at",), datetime(2026, 9, 10, tzinfo=UTC)),
        ("capture-seal", ("audio", "channels"), 2),
        ("recording-take", ("decision_revision",), 1),
        ("recording-take", ("frame_range", "source_frames"), 48_001),
        ("recording-save", ("navigation", "complete"), False),
        ("recording-analysis", ("completed_at",), datetime(2026, 9, 10, tzinfo=UTC)),
        ("recording-analysis", ("state",), "failed"),
        ("recording-analysis", ("state",), "pending"),
        ("recording-package-map", ("group", "room_tone_take_ids"), ()),
        ("recording-history-entry", ("date_precision",), "unknown"),
        ("recording-history-entry", ("group_id",), None),
        ("recording-history-entry", ("source_frames",), None),
    ],
)
def test_additional_evidence_mismatch_fails_closed(
    model_name: str, path: Any, new_value: Any
) -> None:
    payload = synthetic_contracts()[model_name].model_dump()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = new_value
    with pytest.raises(ValidationError):
        RECORDING_SCHEMA_MODELS[model_name].model_validate(payload)


def test_package_duplicate_and_cross_group_identity_fail_closed() -> None:
    package = synthetic_contracts()["recording-package-map"]
    with pytest.raises(ValidationError):
        RecordingPackageMap.model_validate(package.model_dump() | {"entries": package.entries * 2})
    changed = package.entries[0].model_dump() | {"subject_id": "another-subject"}
    with pytest.raises(ValidationError):
        RecordingPackageMap.model_validate(package.model_dump() | {"entries": (changed,)})


def test_pending_failed_and_nonfatal_analysis_are_separate_states() -> None:
    values = synthetic_contracts()
    payload = values["recording-analysis"].model_dump()
    for state in ("pending", "running"):
        pending = payload | {"state": state, "metrics": None, "completed_at": None}
        assert RecordingAnalysisResult.model_validate(pending).completed_at is None
        with pytest.raises(ValidationError):
            RecordingAnalysisResult.model_validate(pending | {"metrics": payload["metrics"]})
    review = payload | {
        "state": "needs-review",
        "metrics": payload["metrics"] | {"passed": False},
    }
    assert RecordingAnalysisResult.model_validate(review).state == "needs-review"
    for state in ("failed", "invalid"):
        failed = payload | {
            "state": state,
            "error": values["native-recovery"].error,
            "metrics": None,
        }
        assert RecordingAnalysisResult.model_validate(failed).state == state


def test_error_details_are_bounded() -> None:
    payload = synthetic_contracts()["native-recovery"].error.model_dump()
    with pytest.raises(ValidationError):
        RecordingError.model_validate(payload | {"details": (" ",)})


def test_corpus_gate_evidence_is_required_before_reservation() -> None:
    group = synthetic_contracts()["recording-group"]
    payload = group.model_dump()
    catalog = PromptSuite.model_validate(group.catalog.model_dump() | {"purpose": "capture-corpus"})
    payload.update(catalog=catalog, catalog_sha256=canonical_sha256(catalog))
    with pytest.raises(ValidationError):
        RecordingGroup.model_validate(payload)
    payload["gate"] = PreparedCaptureGate(state="unprepared")
    unprepared = RecordingGroup.model_validate(payload)
    reservation = synthetic_contracts()["capture-reservation"].model_dump() | {"group": unprepared}
    with pytest.raises(ValidationError):
        CaptureReservation.model_validate(reservation)
    payload["gate"] = PreparedCaptureGate(
        state="ready",
        approval_path="provenance/pilot-gate.json",
        approval_sha256="a" * 64,
        evidence_sha256="b" * 64,
        verified_at=group.opened_at,
    )
    assert CaptureReservation.model_validate(
        reservation | {"group": RecordingGroup.model_validate(payload)}
    )


@pytest.mark.parametrize("name", tuple(RECORDING_SCHEMA_MODELS))
def test_every_contract_roundtrips_strict_json_and_canonical_bytes(name: str) -> None:
    original = synthetic_contracts()[name]
    parsed = RECORDING_SCHEMA_MODELS[name].model_validate_json(canonical_json_bytes(original))
    assert parsed == original


@pytest.mark.parametrize("case", invalid_contracts(), ids=lambda case: case[0])
def test_malformed_contracts_fail_closed(case: tuple[str, str, dict[str, Any]]) -> None:
    _, name, payload = case
    with pytest.raises(ValidationError):
        RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_waveform_and_metrics_are_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        WaveformBucket(minimum=value, maximum=value)
    payload = synthetic_contracts()["recording-analysis"].model_dump()
    payload["metrics"]["peak_dbfs"] = value
    with pytest.raises(ValidationError):
        RecordingAnalysisResult.model_validate(payload)


@pytest.mark.parametrize("state", ["pending", "running", "verified", "needs-review", "failed"])
def test_saved_take_counts_without_waiting_for_analysis(state: str) -> None:
    payload = synthetic_contracts()["recording-take"].model_dump()
    payload.update(decision="kept", decision_revision=1, analysis=state)
    assert RecordingTake.model_validate(payload).satisfies_coverage


@pytest.mark.parametrize(
    "decision,state",
    [
        ("draft", "pending"),
        ("removed", "verified"),
        ("rejected", "verified"),
        ("kept", "invalid"),
    ],
)
def test_unsaved_removed_rejected_and_invalid_do_not_count(decision: str, state: str) -> None:
    payload = synthetic_contracts()["recording-take"].model_dump()
    payload.update(decision=decision, decision_revision=int(decision != "draft"), analysis=state)
    assert not RecordingTake.model_validate(payload).satisfies_coverage


def test_trim_recipe_can_be_durable_before_derivative_exists() -> None:
    recipe = RecordingFrameRange(source_frames=48_000, start_frame=240, end_frame=47_520)
    assert recipe.end_frame - recipe.start_frame == 47_280


def test_successful_recovery_binds_exact_sealed_take() -> None:
    values = synthetic_contracts()
    payload = values["native-recovery"].model_dump()
    payload.update(state="sealed", seal=values["capture-seal"], error=None, written_frames=48_000)
    receipt = NativeRecoveryReceipt.model_validate(payload)
    encoded = native_recovery_bytes(receipt)
    assert int.from_bytes(encoded[:4], "big") == len(encoded[4:])
    assert NativeRecoveryReceipt.model_validate_json(encoded[4:]) == receipt


def test_recording_framing_is_bounded_and_network_order() -> None:
    message = synthetic_contracts()["recording-telemetry"]
    encoded = recording_frame_bytes(message, telemetry=True)
    assert int.from_bytes(encoded[:4], "big") == len(encoded) - 4
    assert len(encoded) < MAX_TELEMETRY_FRAME_BYTES < MAX_CONTROL_FRAME_BYTES


def test_large_catalog_message_cannot_escape_control_frame_limit() -> None:
    from cole_voice_lab.models import ContractModel

    class Oversized(ContractModel):
        text: str

    with pytest.raises(ValueError, match="frame size"):
        recording_frame_bytes(Oversized(text="x" * MAX_CONTROL_FRAME_BYTES))


def test_waveform_actual_silence_has_zero_extrema() -> None:
    waveform = WaveformSnapshot(
        sample_clock=None,
        take_id="synthetic-take",
        process_generation="synthetic-gen",
        first_frame=0,
        end_frame=0,
        written_frames=0,
        bucket_width_frames=240,
        buckets=(),
    )
    assert waveform.buckets == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"minimum": 0.1, "maximum": -0.1},
        {"minimum": -1.1, "maximum": 0.1},
    ],
)
def test_invalid_extrema_do_not_render_as_audio(changes: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        WaveformBucket.model_validate(changes)


def test_take_maximum_is_enforced_independently_of_ui() -> None:
    payload = synthetic_contracts()["capture-reservation"].model_dump()
    payload["maximum_frames"] = MAX_RECORDING_FRAMES + 1
    with pytest.raises(ValidationError):
        CaptureReservation.model_validate(payload)


def test_history_cursor_and_unknown_completeness_remain_distinct() -> None:
    page = RecordingHistoryPage(
        catalog_sha256="0" * 64,
        index_revision=0,
        completeness="unknown",
        has_more=False,
    )
    assert page.completeness == "unknown"
    with pytest.raises(ValidationError):
        RecordingHistoryPage.model_validate(page.model_dump() | {"has_more": True})


def test_saved_event_exposes_matching_complete_resource() -> None:
    values = synthetic_contracts()
    take = RecordingTake.model_validate(
        values["recording-take"].model_dump()
        | {
            "decision": "kept",
            "decision_revision": 1,
        }
    )
    event = SaveEvent(operation=values["recording-save"], take=take)
    assert event.take.decision == "kept"


def reservation_event_cases() -> list[tuple[str, bool, str, dict[str, Any]]]:
    values = synthetic_contracts()
    take = RecordingTake.model_validate(
        values["recording-take"].model_dump()
        | {
            "seal": None,
            "transport": "ready",
            "frame_range": None,
        }
    )
    response = RecordingResponse(
        data=ReservationEvent(take=take, reservation_sha256=canonical_sha256(take.reservation)),
        error=None,
        meta=values["recording-response"].meta,
    ).model_dump(mode="json")
    cases = [("reservation-event", True, "shared", response)]
    for name, field, value, owner in [
        ("reservation-digest-shape", "reservation_sha256", "malformed", "shared"),
        ("reservation-digest-mismatch", "reservation_sha256", "0" * 64, "python-evidence"),
        (
            "reservation-already-sealed",
            "take",
            values["recording-take"].model_dump(mode="json"),
            "shared",
        ),
    ]:
        payload = copy.deepcopy(response)
        payload["data"][field] = value
        cases.append((name, False, owner, payload))
    missing = copy.deepcopy(response)
    del missing["data"]["reservation_sha256"]
    cases.append(("reservation-digest-missing", False, "shared", missing))
    mismatch = copy.deepcopy(response)
    mismatch["meta"]["take_id"] = "different-take"
    cases.append(("reservation-meta-mismatch", False, "shared", mismatch))
    return cases


@pytest.mark.parametrize("case", reservation_event_cases(), ids=lambda case: case[0])
def test_reservation_event_binds_exact_pre_capture_identity(case: Any) -> None:
    _, valid, _, payload = case
    if valid:
        assert RecordingResponse.model_validate_json(json.dumps(payload)).data.event == "reserved"
    else:
        with pytest.raises(ValidationError):
            RecordingResponse.model_validate_json(json.dumps(payload))


def history_event_cases() -> list[tuple[str, bool, str, dict[str, Any]]]:
    values = synthetic_contracts()
    entry = values["recording-history-entry"].model_dump(mode="json")
    page = {
        "event": "history",
        "catalog_sha256": entry["catalog_sha256"],
        "index_revision": 1,
        "completeness": "complete",
        "takes": [entry],
        "has_more": False,
        "total_take_count": 1,
        "total_group_count": 1,
        "total_prompt_count": 1,
        "covered_prompt_count": 0,
        "missing_prompt_count": 1,
        "prompts": [
            {
                "prompt_id": entry["prompt"]["prompt_id"],
                "prompt_sha256": canonical_sha256(entry["prompt"]),
                "canonical_ordinal": 0,
                "take_count": 1,
                "kept_count": 0,
                "status": "draft",
                "latest_recorded_at": entry["recorded_at"],
                "date_precision": "take",
            }
        ],
        "groups": [
            {
                "group_id": entry["group_id"],
                "legacy_session_id": None,
                "opened_at": entry["recorded_at"],
                "date_precision": "take",
                "take_count": 1,
                "kept_count": 0,
            }
        ],
    }
    base = {
        "data": page,
        "error": None,
        "meta": values["recording-response"].meta.model_dump(mode="json") | {"take_id": None},
    }
    cases = [("history-summaries", True, "shared", base)]
    filtered = copy.deepcopy(base)
    filtered["data"]["take_prompt_id"] = entry["prompt"]["prompt_id"]
    cases.append(("history-prompt-filter", True, "shared", filtered))
    mismatched = copy.deepcopy(filtered)
    mismatched["data"]["take_prompt_id"] = "different-prompt"
    cases.append(("history-prompt-filter-mismatch", False, "shared", mismatched))
    paged = copy.deepcopy(base)
    for more, cursor in (
        ("has_more", "next_cursor"),
        ("has_more_prompts", "next_prompt_cursor"),
        ("has_more_groups", "next_group_cursor"),
    ):
        paged["data"][more] = True
        paged["data"][cursor] = "opaque-page-cursor"
        broken = copy.deepcopy(paged)
        broken["data"][cursor] = None
        cases.append((f"history-{cursor}-mismatch", False, "shared", broken))
    cases.append(("history-independent-cursors", True, "shared", paged))
    mutations: list[tuple[str, tuple[str | int, ...], Any]] = [
        ("incomplete-missing-count", ("completeness",), "unknown"),
        ("covered-exceeds-catalog", ("covered_prompt_count",), 2),
        ("prompt-kept-exceeds-total", ("prompts", 0, "kept_count"), 2),
        ("prompt-date-precision", ("prompts", 0, "date_precision"), "unknown"),
        ("group-kept-exceeds-total", ("groups", 0, "kept_count"), 2),
        ("group-date-precision", ("groups", 0, "date_precision"), "unknown"),
        ("group-no-identity", ("groups", 0, "group_id"), None),
        ("group-ambiguous-identity", ("groups", 0, "legacy_session_id"), "legacy-group"),
    ]
    for label, path, new_value in mutations:
        broken = copy.deepcopy(base)
        target: Any = broken["data"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = new_value
        cases.append((f"history-{label}", False, "shared", broken))
    unknown = copy.deepcopy(base)
    unknown["data"]["completeness"] = "unknown"
    unknown["data"]["missing_prompt_count"] = None
    cases.append(("history-unknown-coverage", True, "shared", unknown))
    return cases


@pytest.mark.parametrize("case", history_event_cases(), ids=lambda case: case[0])
def test_history_summaries_have_shared_semantic_guards(case: Any) -> None:
    _, valid, _, payload = case
    if valid:
        assert RecordingResponse.model_validate_json(json.dumps(payload)).data.event == "history"
    else:
        with pytest.raises(ValidationError):
            RecordingResponse.model_validate_json(json.dumps(payload))


def generate_recording_conformance_fixtures(directory: Path) -> None:
    """Write only synthetic examples to caller-selected temporary/private storage."""
    if not directory.exists():
        require_external_private_root(directory.parent)
        if directory.is_symlink() or directory.absolute() != directory.resolve():
            raise ValueError("synthetic fixture destination must not contain symlinks")
        directory.mkdir(mode=0o700)
    require_external_private_root(directory)
    if any(path.is_symlink() for path in directory.iterdir()):
        raise ValueError("synthetic fixture destination contains a symlink")
    index: list[dict[str, Any]] = []
    for name, model, payload in synthetic_commands():
        instance = model.model_validate(payload)
        filename = f"valid-{name}.json"
        write_canonical_json(directory / filename, instance)
        model_name = (
            "recording-recorder-request"
            if model is RecorderRequest
            else "recording-service-request"
        )
        index.append(
            {
                "file": filename,
                "model": model_name,
                "valid": True,
                "validation_owner": "shared",
            }
        )
    for name, instance in synthetic_contracts().items():
        filename = f"valid-{name}.json"
        write_canonical_json(directory / filename, instance)
        index.append(
            {
                "file": filename,
                "model": name,
                "valid": True,
                "validation_owner": "shared",
            }
        )
    for case, name, payload in invalid_contracts():
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))
        filename = f"invalid-{case}.json"
        write_canonical_json(directory / filename, payload)
        owner = (
            "python-evidence"
            if case
            in {"wrong-catalog", "seal-reservation-mismatch", "start-reservation-digest-mismatch"}
            else "shared"
        )
        index.append({"file": filename, "model": name, "valid": False, "validation_owner": owner})
    for case, valid, owner, payload in (
        reservation_event_cases()
        + history_event_cases()
        + recovery_event_cases()
        + recovery_inventory_event_cases()
    ):
        filename = f"{'valid' if valid else 'invalid'}-{case}.json"
        write_canonical_json(directory / filename, payload)
        index.append(
            {
                "file": filename,
                "model": "recording-response",
                "valid": valid,
                "validation_owner": owner,
            }
        )
    for case, name, valid, owner, payload in (
        navigation_event_cases()
        + review_event_cases()
        + package_map_cases()
        + recovery_command_cases()
        + prompt_suite_v2_cases()
        + recording_group_v2_cases()
        + catalog_event_cases()
        + prompt_equality_cases()
        + finetune_cases()
        + speech_cases()
    ):
        filename = f"{'valid' if valid else 'invalid'}-{case}.json"
        _write_fixture(directory / filename, payload)
        index.append({"file": filename, "model": name, "valid": valid, "validation_owner": owner})
    for name, model in RECORDING_SCHEMA_MODELS.items():
        write_canonical_json(directory / f"{name}.schema.json", model.model_json_schema())
    write_canonical_json(directory / "index.json", index)


def test_conformance_fixtures_are_created_private_outside_git(tmp_path: Path) -> None:
    directory = tmp_path.resolve() / "contracts"
    generate_recording_conformance_fixtures(directory)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    assert json.loads((directory / "index.json").read_bytes())
    generate_recording_conformance_fixtures(directory)


def test_conformance_fixtures_reject_repository_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("recording_fixtures.REPOSITORY_ROOT", tmp_path.resolve())
    with pytest.raises(ValueError, match="outside the repository"):
        generate_recording_conformance_fixtures(tmp_path.resolve() / "fixtures")
    assert not (tmp_path / "fixtures").exists()


def test_conformance_fixtures_reject_public_and_symlink_targets(tmp_path: Path) -> None:
    private = tmp_path.resolve()
    public = private / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        generate_recording_conformance_fixtures(public)
    actual = private / "actual"
    actual.mkdir(mode=0o700)
    alias = private / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        generate_recording_conformance_fixtures(alias)
    outside = private / "outside"
    outside.write_text("Unchanged")
    (actual / "valid-recording-group.json").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        generate_recording_conformance_fixtures(actual)
    assert outside.read_text() == "Unchanged"


def navigation_event_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    values = synthetic_contracts()
    group = values["recording-group"]
    cursor = RecordingCursor(
        subject_id=group.subject_id,
        group_id=group.group_id,
        selection=values["recording-save"].selection.model_copy(update={"take_id": None}),
        mode="selected-prompt",
        complete=False,
        operation_id="context-open",
    )
    request = ServiceRequest(
        data=OpenContextCommand(
            operation_id="context-open",
            subject_id=group.subject_id,
            catalog=group.catalog,
            catalog_sha256=group.catalog_sha256,
            capture_device=group.capture_device,
            capture_host=group.capture_host,
        ),
        meta=values["recording-response"].meta.model_copy(update={"take_id": None}),
    )
    response = RecordingResponse(
        data=ContextEvent(group=group, cursor=cursor), error=None, meta=request.meta
    )
    target = RecordingNavigationResult(
        catalog_sha256=group.catalog_sha256,
        index_revision=cursor.selection.index_revision,
        next_prompt=group.catalog.prompts[0],
        complete=False,
    )
    navigation = RecordingResponse(
        data=NavigationEvent(result=target, cursor=cursor),
        error=None,
        meta=request.meta,
    )
    # Explicit prompt selection atomically carries its latest retained take.
    kept_take_id = "take-" + "b" * 64
    selected = RecordingResponse(
        data=NavigationEvent(
            result=target.model_copy(update={"selected_take_id": kept_take_id}),
            cursor=cursor.model_copy(
                update={"selection": cursor.selection.model_copy(update={"take_id": kept_take_id})}
            ),
        ),
        error=None,
        meta=request.meta.model_copy(update={"take_id": kept_take_id}),
    )
    cases = [
        (
            "context-command",
            "recording-service-request",
            True,
            "shared",
            request.model_dump(mode="json"),
        ),
        ("context-event", "recording-response", True, "shared", response.model_dump(mode="json")),
        (
            "cursor-navigation",
            "recording-response",
            True,
            "shared",
            navigation.model_dump(mode="json"),
        ),
    ]
    for name, original, path, value, owner in [
        (
            "context-digest-mismatch",
            request,
            ("data", "catalog_sha256"),
            "0" * 64,
            "python-evidence",
        ),
        (
            "context-device",
            request,
            ("data", "capture_device", "backend"),
            "avfoundation",
            "shared",
        ),
        (
            "context-approval-unpaired",
            request,
            ("data", "pilot_approval_path"),
            "provenance/pilot.json",
            "shared",
        ),
        ("cursor-subject", response, ("data", "cursor", "subject_id"), "different", "shared"),
        ("cursor-group", response, ("data", "cursor", "group_id"), "different", "shared"),
        (
            "cursor-prompt",
            response,
            ("data", "cursor", "selection", "prompt_id"),
            "different",
            "shared",
        ),
        (
            "cursor-float-revision",
            response,
            ("data", "cursor", "selection", "selection_revision"),
            1.0,
            "shared",
        ),
        ("cursor-event-meta", response, ("meta", "take_id"), "different", "shared"),
        ("cursor-navigation-complete", navigation, ("data", "cursor", "complete"), True, "shared"),
        (
            "cursor-navigation-take",
            navigation,
            ("data", "cursor", "selection", "take_id"),
            "unexpected",
            "shared",
        ),
        (
            "cursor-navigation-selected-unbound",
            navigation,
            ("data", "result", "selected_take_id"),
            kept_take_id,
            "shared",
        ),
        (
            "cursor-navigation-selected-other-take",
            selected,
            ("data", "result", "selected_take_id"),
            "take-" + "c" * 64,
            "shared",
        ),
        (
            "cursor-navigation-selected-continue-missing",
            selected,
            ("data", "cursor", "mode"),
            "continue-missing",
            "shared",
        ),
    ]:
        payload = original.model_dump(mode="json")
        node = payload
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        cases.append(
            (
                name,
                "recording-service-request" if original is request else "recording-response",
                False,
                owner,
                payload,
            )
        )
    cases.append(
        (
            "cursor-navigation-selected-take",
            "recording-response",
            True,
            "shared",
            selected.model_dump(mode="json"),
        )
    )
    # Each remaining case needs coordinated edits so only the selected-take rule is violated.
    completed = selected.model_dump(mode="json")
    completed["data"]["result"] |= {"next_prompt": None, "complete": True}
    completed["data"]["cursor"]["complete"] = True
    uncommitted = selected.model_dump(mode="json")
    uncommitted["data"]["cursor"] = None
    uncommitted["meta"]["take_id"] = None
    advancing = values["recording-save"].model_dump(mode="json")
    advancing["navigation"] |= {
        "next_prompt": group.catalog.prompts[0].model_dump(mode="json"),
        "complete": False,
    }
    assert RECORDING_SCHEMA_MODELS["recording-save"].model_validate_json(json.dumps(advancing))
    advancing["navigation"]["selected_take_id"] = kept_take_id
    for name, model, payload in [
        ("cursor-navigation-selected-complete", "recording-response", completed),
        ("cursor-navigation-selected-without-cursor", "recording-response", uncommitted),
        ("save-navigation-selected-take", "recording-save", advancing),
    ]:
        cases.append((name, model, False, "shared", payload))
    return cases


@pytest.mark.parametrize("case", navigation_event_cases(), ids=lambda case: case[0])
def test_context_and_navigation_cursor_contracts(case: Any) -> None:
    _, name, valid, _, payload = case
    if valid:
        assert RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


def review_event_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    from cole_voice_lab import recording_contracts as c

    values = synthetic_contracts()
    take = values["recording-take"]
    seal = take.seal
    assert seal is not None and take.frame_range is not None
    cursor = c.RecordingCursor(
        subject_id=take.reservation.group.subject_id,
        group_id=take.reservation.group.group_id,
        selection=values["recording-save"].selection,
        mode="selected-prompt",
        complete=False,
        operation_id="review-context",
    )
    source = c.RecordingReviewSource(
        take_id=take.reservation.take_id,
        subject_id=take.reservation.group.subject_id,
        catalog_sha256=take.reservation.group.catalog_sha256,
        prompt=take.reservation.prompt,
        source_path=seal.original_path,
        source_sha256=seal.original_sha256,
        source_frames=seal.audio.frames,
        source_identity=c.RecordingFileIdentity(
            relative_path=seal.original_path,
            device=1,
            inode=2,
            size_bytes=seal.original_size_bytes,
            mode=0o100600,
            mtime_ns=1,
            ctime_ns=2,
        ),
        frame_range=take.frame_range,
        decision=take.decision,
        decision_revision=take.decision_revision,
        edit_revision=take.edit_revision,
        analysis=take.analysis,
        export=take.export,
        recorded_at=seal.first_sample_at,
        date_precision="take",
        original_kind="native-original",
    )
    legacy = c.RecordingReviewSource.model_validate(
        source.model_dump()
        | {
            "original_kind": "legacy-staged",
            "source_path": "tmp/capture-synthetic-session/recording.wav",
            "source_identity": source.source_identity.model_dump()
            | {"relative_path": "tmp/capture-synthetic-session/recording.wav"},
        }
    )
    trimmed = c.RecordingTake.model_validate(
        take.model_dump()
        | {
            "edit_revision": 1,
            "frame_range": c.RecordingFrameRange(
                source_frames=seal.audio.frames, start_frame=1, end_frame=seal.audio.frames - 1
            ),
        }
    )
    evidence = c.RecordingDecisionEvidence(
        operation_id="review-remove",
        revision=take.decision_revision + 1,
        action="remove",
        previous=take.decision,
        current="removed",
        restore_decision="kept",
        reason="Synthetic exclusion",
        committed_at=values["recording-save"].committed_at,
    )
    removed = c.RecordingTake.model_validate(
        take.model_dump()
        | {
            "decision": "removed",
            "decision_revision": evidence.revision,
            "decision_evidence": evidence,
        }
    )
    events = {
        "review-native": c.ReviewEvent(source=source, cursor=cursor),
        "review-legacy": c.ReviewEvent(source=legacy, cursor=cursor),
        "edited-pending": c.EditEvent(
            operation_id="review-edit",
            take=trimmed,
            recipe_sha256="a" * 64,
            derivative_state="pending",
        ),
        "edited-original": c.EditEvent(
            operation_id="review-reset",
            take=trimmed.model_copy(update={"frame_range": take.frame_range}),
            recipe_sha256="b" * 64,
            derivative_state="not-required",
        ),
        "decision-removed": c.DecisionEvent(take=removed, decision=evidence),
        "legacy-action-pending": c.ReviewActionEvent(
            operation_id="legacy-edit", action="edit", source=legacy, cursor=cursor, state="pending"
        ),
        "legacy-action-completed": c.ReviewActionEvent(
            operation_id="legacy-edit",
            action="edit",
            source=legacy,
            cursor=cursor,
            state="completed",
        ),
        "legacy-action-failed": c.ReviewActionEvent(
            operation_id="legacy-edit",
            action="edit",
            source=legacy,
            cursor=cursor,
            state="failed",
            error=c.RecordingError(
                code="RECORDING.EDIT_FAILED", message="Synthetic render failure", retryable=True
            ),
        ),
        "derivative-ready": c.DerivativeEvent(
            take_id=take.reservation.take_id,
            source_sha256=seal.original_sha256,
            edit_revision=1,
            recipe_sha256="a" * 64,
            state="ready",
            derived_sha256="b" * 64,
            derived_path="derived/recording-edits/native/" + ("a" * 64) + "/" + ("b" * 64) + ".wav",
        ),
        "derivative-failed": c.DerivativeEvent(
            take_id=take.reservation.take_id,
            source_sha256=seal.original_sha256,
            edit_revision=1,
            recipe_sha256="a" * 64,
            state="failed",
            error=c.RecordingError(
                code="RECORDING.EDIT_FAILED", message="Synthetic failure", retryable=True
            ),
        ),
    }
    cases = []
    originals = {}
    for name, event in events.items():
        response = c.RecordingResponse(
            data=event, error=None, meta=values["recording-response"].meta
        )
        originals[name] = response
        cases.append((name, "recording-response", True, "shared", response.model_dump(mode="json")))
    for name, command in {
        "review-command": c.ReviewTakeCommand(
            operation_id="selected-review",
            take_id=take.reservation.take_id,
            selection=cursor.selection,
        ),
        "retake-command": c.RetakeCommand(
            operation_id="selected-retake", selection=cursor.selection
        ),
    }.items():
        request = c.ServiceRequest(data=command, meta=values["recording-response"].meta)
        cases.append(
            (name, "recording-service-request", True, "shared", request.model_dump(mode="json"))
        )
    for name, original, path, value in [
        (
            "review-source-path",
            "review-native",
            ["data", "source", "source_identity", "relative_path"],
            "tmp/another.wav",
        ),
        (
            "review-source-frames",
            "review-native",
            ["data", "source", "source_frames"],
            seal.audio.frames + 1,
        ),
        ("review-date-precision", "review-native", ["data", "source", "date_precision"], "unknown"),
        (
            "review-cursor-take",
            "review-native",
            ["data", "cursor", "selection", "take_id"],
            "other-take",
        ),
        (
            "review-cursor-prompt",
            "review-native",
            ["data", "cursor", "selection", "prompt_id"],
            "other-prompt",
        ),
        (
            "review-cursor-subject",
            "review-native",
            ["data", "cursor", "subject_id"],
            "other-subject",
        ),
        (
            "review-cursor-catalog",
            "review-native",
            ["data", "cursor", "selection", "catalog_sha256"],
            "f" * 64,
        ),
        ("review-meta-take", "review-native", ["meta", "take_id"], "other-take"),
        (
            "review-identity-overflow",
            "review-native",
            ["data", "source", "source_identity", "inode"],
            2**63,
        ),
        (
            "review-fractional-identity",
            "review-native",
            ["data", "source", "source_identity", "inode"],
            1.0,
        ),
        ("edit-pending-full-range", "edited-original", ["data", "derivative_state"], "pending"),
        ("edit-missing-revision", "edited-pending", ["data", "take", "edit_revision"], 0),
        ("edit-meta-take", "edited-pending", ["meta", "take_id"], "other-take"),
        (
            "decision-mismatched-evidence",
            "decision-removed",
            ["data", "decision", "operation_id"],
            "other-operation",
        ),
        (
            "decision-bad-restore",
            "decision-removed",
            ["data", "decision", "restore_decision"],
            None,
        ),
        (
            "legacy-action-native",
            "legacy-action-pending",
            ["data", "source", "original_kind"],
            "native-original",
        ),
        (
            "legacy-action-pending-error",
            "legacy-action-pending",
            ["data", "error"],
            {
                "code": "RECORDING.EDIT_FAILED",
                "message": "Synthetic error",
                "retryable": False,
                "details": [],
            },
        ),
        ("legacy-action-failed-no-error", "legacy-action-failed", ["data", "error"], None),
        ("legacy-action-meta-take", "legacy-action-pending", ["meta", "take_id"], "other-take"),
        (
            "derivative-wrong-recipe-path",
            "derivative-ready",
            ["data", "derived_path"],
            "derived/recording-edits/native/" + ("c" * 64) + "/" + ("b" * 64) + ".wav",
        ),
        ("derivative-missing-digest", "derivative-ready", ["data", "derived_sha256"], None),
        ("derivative-failed-output", "derivative-failed", ["data", "derived_sha256"], "a" * 64),
        ("derivative-missing-revision", "derivative-ready", ["data", "edit_revision"], 0),
        ("derivative-meta-take", "derivative-ready", ["meta", "take_id"], "other-take"),
    ]:
        payload = originals[original].model_dump(mode="json")
        target = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        cases.append((name, "recording-response", False, "shared", payload))
    return cases


@pytest.mark.parametrize("case", review_event_cases(), ids=lambda case: case[0])
def test_review_edit_decision_and_derivative_shared_contracts(case: Any) -> None:
    _, name, valid, _, payload = case
    if valid:
        assert RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


def package_map_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    """Selected-suite rules of a v1 (embedded-catalog) package map, checked on the map itself."""
    package = synthetic_contracts()["recording-package-map"].model_dump(mode="json")
    package["selected_suite"] = {
        "suite_id": "synthetic-export-subset",
        "purpose": package["group"]["catalog"]["purpose"],
        "prompts": [entry["prompt"] for entry in package["entries"]],
    }
    cases = [("package-selected-suite", "recording-package-map", True, "shared", package)]
    broken = copy.deepcopy(package)
    broken["selected_suite"]["purpose"] = "benchmark"
    cases.append(("package-selected-purpose", "recording-package-map", False, "shared", broken))
    broken = copy.deepcopy(package)
    broken["selected_suite"]["prompts"][0]["text"] = "Changed exact prompt"
    cases.append(("package-selected-content", "recording-package-map", False, "shared", broken))
    # The two cases above also fail other rules (the entry shares the edited prompt object;
    # "benchmark" is no purpose at all). These fail only the v1 selected-suite rule.
    broken = copy.deepcopy(package)
    suite = broken["selected_suite"]
    suite["prompts"] = copy.deepcopy(suite["prompts"])
    suite["prompts"][0]["text"] = "Changed exact prompt"
    assert broken["entries"][0]["prompt"]["text"] != "Changed exact prompt"
    cases.append(
        ("package-selected-content-only", "recording-package-map", False, "shared", broken)
    )
    broken = copy.deepcopy(package)
    broken["selected_suite"]["purpose"] = "capture-corpus"
    cases.append(
        ("package-selected-other-purpose", "recording-package-map", False, "shared", broken)
    )
    return cases


@pytest.mark.parametrize("case", package_map_cases(), ids=lambda case: case[0])
def test_package_maps_bind_their_selected_content(case: Any) -> None:
    _, name, valid, _, payload = case
    if valid:
        assert RecordingPackageMap.model_validate_json(json.dumps(payload)).selected_suite
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


class NonCanonicalFixture(dict[str, Any]):
    """A fixture canonical JSON cannot write: it holds a non-finite number.

    Written as Python's json module writes it (``Infinity``), a token neither strict parser
    accepts, so both sides must refuse the file.
    """


def _write_fixture(path: Path, payload: Any) -> None:
    if not isinstance(payload, NonCanonicalFixture):
        write_canonical_json(path, payload)
        return
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


FINETUNE_WIRE_CODES: tuple[str, ...] = get_args(FineTuneErrorCode.__value__)
FINETUNE_RUN_ID = "finetune-20260916t080000z"
FINETUNE_RUN_ROOT = f"derived/finetune/{FINETUNE_RUN_ID}"


def synthetic_finetune_run(**changes: Any) -> FineTuneRunSummary:
    """A finished synthetic run with one fine-tuned and one base sample."""
    payload: dict[str, Any] = {
        "run_id": FINETUNE_RUN_ID,
        "run_root": FINETUNE_RUN_ROOT,
        "status": "finished",
        "started_at": datetime(2026, 9, 16, 8, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
        "epochs": 120,
        "prompt_count": 34,
        "retained_seconds": 181.25,
        "finetuned_sample_paths": (f"{FINETUNE_RUN_ROOT}/samples/finetuned/sample-01.wav",),
        "base_sample_paths": (f"{FINETUNE_RUN_ROOT}/samples/base/sample-01.wav",),
    }
    return FineTuneRunSummary.model_validate(payload | changes)


def synthetic_finished_runs(count: int) -> tuple[FineTuneRunSummary, ...]:
    """``count`` finished runs, newest first: an hour apart, except that the last two finish
    together (the greater run id first)."""
    newest = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    runs = [synthetic_finetune_run()]
    for index in range(1, count):
        tie = count > 2 and index == count - 1
        finished = newest - timedelta(hours=index - 1 if tie else index)
        run_id = f"finetune-run-{99 - index:02d}"
        root = f"derived/finetune/{run_id}"
        runs.append(
            synthetic_finetune_run(
                run_id=run_id,
                run_root=root,
                started_at=finished - timedelta(minutes=30),
                finished_at=finished,
                finetuned_sample_paths=(f"{root}/samples/finetuned/sample-01.wav",),
                base_sample_paths=(f"{root}/samples/base/sample-01.wav",),
            )
        )
    return tuple(runs)


def synthetic_finetune_progress(**changes: Any) -> FineTuneProgressEvent:
    """A synthetic job in its training stage, pass 30 of 120."""
    payload: dict[str, Any] = {
        "operation_id": "op-fine-tune",
        "state": "running",
        "stage": "training",
        "run_id": FINETUNE_RUN_ID,
        "epoch": 30,
        "epochs": 120,
        "update": 270,
        "loss": 0.52,
        "eta_seconds": 2520,
    }
    return FineTuneProgressEvent.model_validate(payload | changes)


def finetune_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    """Fine-tune commands and events: take-less, with named states and relative run paths."""
    values = synthetic_contracts()
    response_meta = values["recording-response"].meta.model_copy(update={"take_id": None})
    request_meta = values["recording-service-request"].meta.model_dump(mode="json")
    failure = RecordingError(
        code="FINETUNE.TRAINING_FAILED", message="Synthetic training failure", retryable=False
    )

    def response(data: Any = None, error: RecordingError | None = None) -> dict[str, Any]:
        return RecordingResponse(data=data, error=error, meta=response_meta).model_dump(mode="json")

    def request(**data: Any) -> dict[str, Any]:
        return {"data": data, "error": None, "meta": request_meta | {"take_id": None}}

    progress = synthetic_finetune_progress()
    finished_run = synthetic_finetune_run()
    valid: list[tuple[str, str, dict[str, Any]]] = [
        (
            "progress-queued",
            "recording-response",
            response(FineTuneProgressEvent(operation_id="op-fine-tune", state="queued")),
        ),
        (
            "progress-selecting",
            "recording-response",
            response(
                FineTuneProgressEvent(
                    operation_id="op-fine-tune",
                    state="running",
                    stage="selecting",
                    message="Preparing the recordings",
                )
            ),
        ),
        (
            "progress-started",
            "recording-response",
            response(FineTuneProgressEvent(operation_id="op-fine-tune", state="running")),
        ),
        ("progress-training", "recording-response", response(progress)),
        (
            "progress-finished",
            "recording-response",
            response(
                progress.model_copy(update={"state": "finished", "stage": None, "epoch": 120})
            ),
        ),
        (
            "progress-failed",
            "recording-response",
            response(synthetic_finetune_progress(state="failed", error=failure)),
        ),
        (
            "progress-cancelled",
            "recording-response",
            response(synthetic_finetune_progress(state="cancelled")),
        ),
        (
            "run-finished",
            "recording-response",
            response(FineTuneEvent(operation_id="op-fine-tune", run=finished_run)),
        ),
        (
            "run-finished-unsampled",
            "recording-response",
            response(
                FineTuneEvent(
                    operation_id="op-fine-tune",
                    run=synthetic_finetune_run(finetuned_sample_paths=(), base_sample_paths=()),
                )
            ),
        ),
        (
            "run-running",
            "recording-response",
            response(
                FineTuneEvent(
                    operation_id="op-fine-tune",
                    run=synthetic_finetune_run(
                        status="running",
                        finished_at=None,
                        finetuned_sample_paths=(),
                        base_sample_paths=(),
                    ),
                )
            ),
        ),
        (
            "run-failed",
            "recording-response",
            response(
                FineTuneEvent(
                    operation_id="op-fine-tune",
                    run=synthetic_finetune_run(
                        status="failed",
                        error="training ended without model_last.pt",
                        finetuned_sample_paths=(),
                        base_sample_paths=(),
                    ),
                )
            ),
        ),
        (
            "status-unconfigured",
            "recording-response",
            response(
                FineTuneStatusEvent(operation_id="op-fine-tune-status", runtime_configured=False)
            ),
        ),
        (
            "status-active",
            "recording-response",
            response(
                FineTuneStatusEvent(
                    operation_id="op-fine-tune-status",
                    runtime_configured=True,
                    active=progress,
                    latest_run=finished_run,
                    finished_runs=(finished_run,),
                )
            ),
        ),
        (
            "status-finished-runs-newest-first",
            "recording-response",
            response(
                FineTuneStatusEvent(
                    operation_id="op-fine-tune-status",
                    runtime_configured=True,
                    latest_run=finished_run,
                    finished_runs=synthetic_finished_runs(3),
                )
            ),
        ),
        (
            "status-most-finished-runs",
            "recording-response",
            response(
                FineTuneStatusEvent(
                    operation_id="op-fine-tune-status",
                    runtime_configured=True,
                    latest_run=finished_run,
                    finished_runs=synthetic_finished_runs(MAX_FINISHED_RUNS),
                )
            ),
        ),
        (
            "run-reference-prompts",
            "recording-response",
            response(
                FineTuneEvent(
                    operation_id="op-fine-tune",
                    run=synthetic_finetune_run(
                        reference_prompt_ids=("reference-neutral-01", "reference-warm-01")
                    ),
                )
            ),
        ),
        (
            "status-queued",
            "recording-response",
            response(
                FineTuneStatusEvent(
                    operation_id="op-fine-tune-status",
                    runtime_configured=True,
                    active=FineTuneProgressEvent(operation_id="op-fine-tune", state="queued"),
                )
            ),
        ),
        (
            "status-latest-run-unreadable",
            "recording-response",
            response(
                FineTuneStatusEvent(
                    operation_id="op-fine-tune-status",
                    runtime_configured=True,
                    active=progress,
                    latest_run_error=RecordingError(
                        code="FINETUNE.SOURCE_CHANGED",
                        message="the latest fine-tuned model cannot be read: synthetic",
                        retryable=False,
                    ),
                )
            ),
        ),
        (
            "command-maximum-epochs",
            "recording-service-request",
            request(
                command="fine-tune", operation_id="op-fine-tune", epochs=10_000, allow_missing=True
            ),
        ),
    ]
    valid += [
        (
            f"error-{code.lower().replace('.', '-').replace('_', '-')}",
            "recording-response",
            response(error=RecordingError(code=code, message="Synthetic", retryable=False)),
        )
        for code in FINETUNE_WIRE_CODES
    ]

    absent = object()  # this runs at import, before the module's own _ABSENT exists

    def changed(payload: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        target = result
        for key in path[:-1]:
            target = target[key]
        if value is absent:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return result

    def without_samples(payload: dict[str, Any], *run_path: str) -> dict[str, Any]:
        cleared = changed(payload, (*run_path, "finetuned_sample_paths"), [])
        return changed(cleared, (*run_path, "base_sample_paths"), [])

    training = response(progress)
    finished = response(FineTuneEvent(operation_id="op-fine-tune", run=finished_run))
    status = next(payload for name, _, payload in valid if name == "status-active")
    listed = next(
        payload for name, _, payload in valid if name == "status-finished-runs-newest-first"
    )
    most = next(payload for name, _, payload in valid if name == "status-most-finished-runs")
    command = request(command="fine-tune", operation_id="op-fine-tune", allow_missing=True)
    run = ("data", "run")
    unsampled = without_samples(finished, *run)
    invalid: list[tuple[str, str, dict[str, Any]]] = [
        (
            "progress-training-without-epoch",
            "recording-response",
            changed(training, ("data", "epoch"), None),
        ),
        (
            "progress-epoch-without-total",
            "recording-response",
            changed(changed(training, ("data", "stage"), "sampling"), ("data", "epochs"), None),
        ),
        (
            "progress-epoch-over-total",
            "recording-response",
            changed(training, ("data", "epoch"), 121),
        ),
        ("progress-epoch-zero", "recording-response", changed(training, ("data", "epoch"), 0)),
        (
            "progress-update-negative",
            "recording-response",
            changed(training, ("data", "update"), -1),
        ),
        (
            "progress-finished-without-run",
            "recording-response",
            changed(changed(training, ("data", "state"), "finished"), ("data", "run_id"), None),
        ),
        (
            "progress-failed-without-error",
            "recording-response",
            changed(training, ("data", "state"), "failed"),
        ),
        (
            "progress-cancelled-with-error",
            "recording-response",
            changed(
                changed(training, ("data", "state"), "cancelled"),
                ("data", "error"),
                failure.model_dump(mode="json"),
            ),
        ),
        (
            "progress-running-with-error",
            "recording-response",
            changed(training, ("data", "error"), failure.model_dump(mode="json")),
        ),
        (
            "progress-queued-with-stage",
            "recording-response",
            changed(training, ("data", "state"), "queued"),
        ),
        (
            "progress-unknown-state",
            "recording-response",
            changed(training, ("data", "state"), "paused"),
        ),
        (
            "progress-unknown-stage",
            "recording-response",
            changed(training, ("data", "stage"), "packing"),
        ),
        ("progress-loss-text", "recording-response", changed(training, ("data", "loss"), "0.52")),
        (
            "progress-blank-message",
            "recording-response",
            changed(training, ("data", "message"), ""),
        ),
        (
            "progress-take-bound",
            "recording-response",
            changed(training, ("meta", "take_id"), "take-a"),
        ),
        (
            "run-absolute-root",
            "recording-response",
            changed(finished, (*run, "run_root"), "/Users/synthetic/" + FINETUNE_RUN_ROOT),
        ),
        (
            "run-parent-root",
            "recording-response",
            changed(finished, (*run, "run_root"), "derived/../" + FINETUNE_RUN_ROOT),
        ),
        (
            "run-absolute-sample",
            "recording-response",
            changed(
                finished,
                (*run, "finetuned_sample_paths", 0),
                "/" + FINETUNE_RUN_ROOT + "/samples/finetuned/sample-01.wav",
            ),
        ),
        (
            "run-sample-outside-run",
            "recording-response",
            changed(
                finished,
                (*run, "base_sample_paths", 0),
                "derived/finetune/another-run/samples/base/sample-01.wav",
            ),
        ),
        (
            "run-sample-parent",
            "recording-response",
            changed(
                finished,
                (*run, "base_sample_paths", 0),
                FINETUNE_RUN_ROOT + "/../another-run/sample-01.wav",
            ),
        ),
        (
            "run-sample-is-root",
            "recording-response",
            changed(finished, (*run, "base_sample_paths", 0), FINETUNE_RUN_ROOT),
        ),
        (
            "run-sample-in-sibling-run",
            "recording-response",
            changed(
                finished, (*run, "base_sample_paths", 0), FINETUNE_RUN_ROOT + "-2/sample-01.wav"
            ),
        ),
        (
            "run-running-with-finish",
            "recording-response",
            changed(unsampled, (*run, "status"), "running"),
        ),
        (
            "run-finished-without-finish",
            "recording-response",
            changed(finished, (*run, "finished_at"), None),
        ),
        (
            "run-finished-before-start",
            "recording-response",
            changed(finished, (*run, "finished_at"), "2026-09-16T07:59:59Z"),
        ),
        (
            "run-finished-with-error",
            "recording-response",
            changed(finished, (*run, "error"), "Synthetic"),
        ),
        (
            "run-failed-without-error",
            "recording-response",
            changed(unsampled, (*run, "status"), "failed"),
        ),
        (
            "run-failed-with-samples",
            "recording-response",
            changed(changed(finished, (*run, "status"), "failed"), (*run, "error"), "Synthetic"),
        ),
        (
            "run-cancelled-status",
            "recording-response",
            changed(finished, (*run, "status"), "cancelled"),
        ),
        ("run-zero-prompts", "recording-response", changed(finished, (*run, "prompt_count"), 0)),
        (
            "run-negative-seconds",
            "recording-response",
            changed(finished, (*run, "retained_seconds"), -1.0),
        ),
        (
            "run-runtime-path",
            "recording-response",
            changed(finished, (*run, "pretrained_path"), "models/pretrained.pt"),
        ),
        (
            "status-active-finished",
            "recording-response",
            changed(status, ("data", "active", "state"), "finished"),
        ),
        (
            "status-latest-failed",
            "recording-response",
            changed(
                changed(
                    without_samples(status, "data", "latest_run"),
                    ("data", "latest_run", "status"),
                    "failed",
                ),
                ("data", "latest_run", "error"),
                "Synthetic",
            ),
        ),
        (
            "status-configuration-absent",
            "recording-response",
            changed(status, ("data", "runtime_configured"), absent),
        ),
        (
            "status-latest-run-and-error",
            "recording-response",
            changed(
                status,
                ("data", "latest_run_error"),
                {"code": "FINETUNE.SOURCE_CHANGED", "message": "Synthetic", "retryable": False},
            ),
        ),
        (
            "status-finished-runs-out-of-order",
            "recording-response",
            changed(
                listed,
                ("data", "finished_runs"),
                [listed["data"]["finished_runs"][index] for index in (0, 2, 1)],
            ),
        ),
        (
            "status-finished-run-repeated",
            "recording-response",
            changed(listed, ("data", "finished_runs", 2), listed["data"]["finished_runs"][1]),
        ),
        (
            "status-latest-not-first",
            "recording-response",
            changed(listed, ("data", "latest_run"), listed["data"]["finished_runs"][1]),
        ),
        (
            "status-finished-runs-without-latest",
            "recording-response",
            changed(listed, ("data", "latest_run"), None),
        ),
        (
            "status-latest-without-finished-runs",
            "recording-response",
            changed(listed, ("data", "finished_runs"), []),
        ),
        (
            "status-finished-run-failed",
            "recording-response",
            changed(
                changed(
                    without_samples(listed, "data", "finished_runs", 2),
                    ("data", "finished_runs", 2, "status"),
                    "failed",
                ),
                ("data", "finished_runs", 2, "error"),
                "Synthetic",
            ),
        ),
        (
            "status-too-many-finished-runs",
            "recording-response",
            changed(
                most,
                ("data", "finished_runs"),
                [
                    *most["data"]["finished_runs"],
                    synthetic_finished_runs(MAX_FINISHED_RUNS + 1)[-1].model_dump(mode="json"),
                ],
            ),
        ),
        (
            "status-latest-run-error-with-finished-runs",
            "recording-response",
            changed(
                changed(listed, ("data", "latest_run"), None),
                ("data", "latest_run_error"),
                {"code": "FINETUNE.SOURCE_CHANGED", "message": "Synthetic", "retryable": False},
            ),
        ),
        (
            "run-reference-prompt-repeated",
            "recording-response",
            changed(finished, (*run, "reference_prompt_ids"), ["reference-a", "reference-a"]),
        ),
        (
            "run-reference-prompt-unstable",
            "recording-response",
            changed(finished, (*run, "reference_prompt_ids"), ["Reference A"]),
        ),
        (
            "run-too-many-reference-prompts",
            "recording-response",
            changed(
                finished,
                (*run, "reference_prompt_ids"),
                [f"reference-{index:02d}" for index in range(65)],
            ),
        ),
        (
            "status-latest-run-error-unknown-code",
            "recording-response",
            changed(
                changed(status, ("data", "latest_run"), None),
                ("data", "latest_run_error"),
                {"code": "FINETUNE.CANCELLED", "message": "Synthetic", "retryable": False},
            ),
        ),
        (
            "error-unknown-finetune-code",
            "recording-response",
            response(error=failure)
            | {"error": failure.model_dump(mode="json") | {"code": "FINETUNE.UNKNOWN"}},
        ),
        (
            "package-preparation-retired",
            "recording-response",
            response(error=failure)
            | {
                "data": {
                    "event": "package-preparation",
                    "operation_id": "op-package",
                    "group_id": values["recording-group"].group_id,
                    "state": "queued",
                    "error": None,
                },
                "error": None,
            },
        ),
        (
            "command-epochs-zero",
            "recording-service-request",
            changed(command, ("data", "epochs"), 0),
        ),
        (
            "command-epochs-over",
            "recording-service-request",
            changed(command, ("data", "epochs"), 10_001),
        ),
        (
            "command-epochs-float",
            "recording-service-request",
            changed(command, ("data", "epochs"), 12.0),
        ),
        (
            "command-allow-missing-absent",
            "recording-service-request",
            changed(command, ("data", "allow_missing"), absent),
        ),
        (
            "command-allow-missing-text",
            "recording-service-request",
            changed(command, ("data", "allow_missing"), "true"),
        ),
        (
            "command-take-bound",
            "recording-service-request",
            changed(command, ("meta", "take_id"), "take-a"),
        ),
        (
            "cancel-without-target",
            "recording-service-request",
            request(command="cancel-fine-tune", operation_id="op-cancel-fine-tune"),
        ),
        (
            "status-with-arguments",
            "recording-service-request",
            request(command="fine-tune-status", operation_id="op-status", allow_missing=True),
        ),
        (
            "prepare-package-retired",
            "recording-service-request",
            request(
                command="prepare-package",
                operation_id="op-package",
                group_id=values["recording-group"].group_id,
                take_ids=["synthetic-take"],
                room_tone_take_ids=["synthetic-take"],
            ),
        ),
    ]
    nonfinite = NonCanonicalFixture(training)
    nonfinite["data"] = training["data"] | {"loss": float("inf")}
    return (
        [(f"finetune-{name}", model, True, "shared", payload) for name, model, payload in valid]
        + [
            (f"finetune-{name}", model, False, "shared", payload)
            for name, model, payload in invalid
        ]
        + [("finetune-progress-nonfinite-loss", "recording-response", False, "shared", nonfinite)]
    )


SPEECH_OPERATION = "op-speak"
SPEECH_CLIP = f"derived/speech/{SPEECH_OPERATION}/speech-01.wav"


def synthetic_speech(**changes: Any) -> SynthesisEvent:
    """A finished Speak request: the latest run spoke two seconds with its reference."""
    payload: dict[str, Any] = {
        "operation_id": SPEECH_OPERATION,
        "state": "finished",
        "model": "latest",
        "text": "The meeting moved to Thursday.",
        "run_id": FINETUNE_RUN_ID,
        "reference_run_id": FINETUNE_RUN_ID,
        "reference_prompt_id": "reference-neutral-01",
        "relative_path": SPEECH_CLIP,
        "sha256": "5" * 64,
        "frames": 48_000,
        "sample_rate_hz": 24_000,
        "seconds": 2.0,
    }
    return SynthesisEvent.model_validate(payload | changes)


def speech_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    """Speak commands and events: take-less, bounded text, clips under their operation."""
    values = synthetic_contracts()
    response_meta = values["recording-response"].meta.model_copy(update={"take_id": None})
    request_meta = values["recording-service-request"].meta.model_dump(mode="json")
    deadline = RecordingError(
        code="FINETUNE.SYNTHESIS_DEADLINE", message="Synthetic deadline", retryable=True
    )
    unfinished = {
        "run_id": None,
        "reference_run_id": None,
        "reference_prompt_id": None,
        "relative_path": None,
        "sha256": None,
        "frames": None,
        "sample_rate_hz": None,
        "seconds": None,
    }
    resolved = unfinished | {
        "run_id": FINETUNE_RUN_ID,
        "reference_run_id": FINETUNE_RUN_ID,
        "reference_prompt_id": "reference-neutral-01",
    }

    def response(event: SynthesisEvent) -> dict[str, Any]:
        return RecordingResponse(data=event, error=None, meta=response_meta).model_dump(mode="json")

    def request(**data: Any) -> dict[str, Any]:
        return {"data": data, "error": None, "meta": request_meta | {"take_id": None}}

    longest = ("é" * 1_999) + "\n"
    speak = request(command="synthesize", operation_id=SPEECH_OPERATION, text="Hi.", model="latest")
    valid: list[tuple[str, str, dict[str, Any]]] = [
        ("synthesize-latest", "recording-service-request", speak),
        (
            "synthesize-base-with-reference",
            "recording-service-request",
            request(
                command="synthesize",
                operation_id=SPEECH_OPERATION,
                text="Line one.\nLine two.",
                model="base",
                reference_prompt_id="reference-neutral-01",
                nfe_steps=4,
            ),
        ),
        (
            "synthesize-run-longest",
            "recording-service-request",
            request(
                command="synthesize",
                operation_id=SPEECH_OPERATION,
                text=longest,
                model=FINETUNE_RUN_ID,
                nfe_steps=64,
            ),
        ),
        (
            "cancel-synthesis",
            "recording-service-request",
            request(
                command="cancel-synthesis",
                operation_id="op-cancel-speak",
                cancelled_operation_id=SPEECH_OPERATION,
            ),
        ),
        ("queued", "recording-response", response(synthetic_speech(state="queued", **unfinished))),
        (
            "running",
            "recording-response",
            response(synthetic_speech(state="running", **unfinished)),
        ),
        (
            "running-resolved",
            "recording-response",
            response(synthetic_speech(state="running", **resolved)),
        ),
        ("finished-latest", "recording-response", response(synthetic_speech())),
        (
            "finished-base",
            "recording-response",
            response(synthetic_speech(model="base", run_id=None)),
        ),
        (
            "finished-run",
            "recording-response",
            response(synthetic_speech(model=FINETUNE_RUN_ID)),
        ),
        (
            "failed",
            "recording-response",
            response(synthetic_speech(state="failed", error=deadline, **unfinished)),
        ),
        (
            "failed-resolved",
            "recording-response",
            response(synthetic_speech(state="failed", error=deadline, **resolved)),
        ),
        (
            "cancelled",
            "recording-response",
            response(synthetic_speech(state="cancelled", **unfinished)),
        ),
    ]

    absent = object()

    def changed(payload: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        target = result
        for key in path[:-1]:
            target = target[key]
        if value is absent:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return result

    def text(value: Any) -> dict[str, Any]:
        return changed(speak, ("data", "text"), value)

    finished = response(synthetic_speech())
    running = response(synthetic_speech(state="running", **unfinished))
    base = response(synthetic_speech(model="base", run_id=None))
    chosen = response(synthetic_speech(model=FINETUNE_RUN_ID))
    cancel = next(payload for name, _, payload in valid if name == "cancel-synthesis")

    def event(field: str, value: Any, payload: dict[str, Any] = finished) -> dict[str, Any]:
        return changed(payload, ("data", field), value)

    invalid: list[tuple[str, str, dict[str, Any]]] = [
        ("synthesize-empty-text", "recording-service-request", text("")),
        ("synthesize-text-over", "recording-service-request", text("é" * 2_001)),
        ("synthesize-whitespace-text", "recording-service-request", text("  \n \u3000 ")),
        ("synthesize-control-character", "recording-service-request", text("Hello\tthere.")),
        ("synthesize-format-character", "recording-service-request", text("Hello\u200bthere.")),
        ("synthesize-text-absent", "recording-service-request", text(absent)),
        (
            "synthesize-nfe-under",
            "recording-service-request",
            changed(speak, ("data", "nfe_steps"), 3),
        ),
        (
            "synthesize-nfe-over",
            "recording-service-request",
            changed(speak, ("data", "nfe_steps"), 65),
        ),
        (
            "synthesize-nfe-float",
            "recording-service-request",
            changed(speak, ("data", "nfe_steps"), 32.5),
        ),
        (
            "synthesize-model-absent",
            "recording-service-request",
            changed(speak, ("data", "model"), absent),
        ),
        (
            "synthesize-model-unstable",
            "recording-service-request",
            changed(speak, ("data", "model"), "Latest"),
        ),
        (
            "synthesize-reference-unstable",
            "recording-service-request",
            changed(speak, ("data", "reference_prompt_id"), "Reference One"),
        ),
        (
            "synthesize-unknown-field",
            "recording-service-request",
            changed(speak, ("data", "voice"), "cole"),
        ),
        (
            "synthesize-take-bound",
            "recording-service-request",
            changed(speak, ("meta", "take_id"), "take-a"),
        ),
        (
            "cancel-synthesis-without-target",
            "recording-service-request",
            changed(cancel, ("data", "cancelled_operation_id"), absent),
        ),
        ("finished-without-path", "recording-response", event("relative_path", None)),
        ("finished-without-sha256", "recording-response", event("sha256", None)),
        ("finished-without-frames", "recording-response", event("frames", None)),
        ("finished-without-rate", "recording-response", event("sample_rate_hz", None)),
        ("finished-without-seconds", "recording-response", event("seconds", None)),
        ("finished-without-reference", "recording-response", event("reference_prompt_id", None)),
        (
            "finished-without-reference-run",
            "recording-response",
            event("reference_run_id", None, base),
        ),
        ("finished-latest-without-run", "recording-response", event("run_id", None)),
        ("finished-base-with-run", "recording-response", event("model", "base")),
        (
            "finished-run-speaking-as-another",
            "recording-response",
            event("model", "finetune-another-run"),
        ),
        (
            "finished-with-foreign-reference",
            "recording-response",
            event("reference_run_id", "finetune-another-run"),
        ),
        (
            "finished-chosen-run-with-foreign-reference",
            "recording-response",
            event("reference_run_id", "finetune-another-run", chosen),
        ),
        (
            "path-outside-operation",
            "recording-response",
            event("relative_path", "derived/speech/op-other/speech-01.wav"),
        ),
        (
            "path-in-sibling-operation",
            "recording-response",
            event("relative_path", f"derived/speech/{SPEECH_OPERATION}-2/speech-01.wav"),
        ),
        (
            "path-is-operation",
            "recording-response",
            event("relative_path", f"derived/speech/{SPEECH_OPERATION}"),
        ),
        (
            "path-escaping-operation",
            "recording-response",
            event("relative_path", f"derived/speech/{SPEECH_OPERATION}/../op-other/speech-01.wav"),
        ),
        ("path-absolute", "recording-response", event("relative_path", "/" + SPEECH_CLIP)),
        (
            "path-in-run",
            "recording-response",
            event("relative_path", f"{FINETUNE_RUN_ROOT}/speech-01.wav"),
        ),
        ("frames-zero", "recording-response", event("frames", 0)),
        (
            "frames-over",
            "recording-response",
            changed(
                event("frames", MAX_RECORDING_FRAMES + 1),
                ("data", "seconds"),
                (MAX_RECORDING_FRAMES + 1) / 24_000,
            ),
        ),
        ("frames-float", "recording-response", event("frames", 48_000.5)),
        ("rate-48k", "recording-response", event("sample_rate_hz", 48_000)),
        ("seconds-disagree", "recording-response", event("seconds", 1.0)),
        ("seconds-zero", "recording-response", event("seconds", 0.0)),
        ("sha256-short", "recording-response", event("sha256", "5" * 63)),
        ("running-with-clip", "recording-response", event("state", "running")),
        (
            "failed-with-clip",
            "recording-response",
            changed(event("state", "failed"), ("data", "error"), deadline.model_dump(mode="json")),
        ),
        ("failed-without-error", "recording-response", event("state", "failed", running)),
        (
            "cancelled-with-error",
            "recording-response",
            changed(
                event("state", "cancelled", running),
                ("data", "error"),
                deadline.model_dump(mode="json"),
            ),
        ),
        (
            "finished-with-error",
            "recording-response",
            event("error", deadline.model_dump(mode="json")),
        ),
        (
            "queued-resolved",
            "recording-response",
            changed(
                changed(
                    changed(event("state", "queued", running), ("data", "run_id"), FINETUNE_RUN_ID),
                    ("data", "reference_run_id"),
                    FINETUNE_RUN_ID,
                ),
                ("data", "reference_prompt_id"),
                "reference-neutral-01",
            ),
        ),
        ("unknown-state", "recording-response", event("state", "paused", running)),
        ("text-absent", "recording-response", event("text", absent)),
        ("text-whitespace", "recording-response", event("text", " \n ")),
        ("text-over", "recording-response", event("text", "x" * 2_001)),
        ("take-bound", "recording-response", changed(finished, ("meta", "take_id"), "take-a")),
        ("model-unstable", "recording-response", event("model", "Base")),
    ]
    nonfinite = NonCanonicalFixture(finished)
    nonfinite["data"] = finished["data"] | {"seconds": float("inf")}
    return (
        [(f"speak-{name}", model, True, "shared", payload) for name, model, payload in valid]
        + [(f"speak-{name}", model, False, "shared", payload) for name, model, payload in invalid]
        + [("speak-nonfinite-seconds", "recording-response", False, "shared", nonfinite)]
    )


@pytest.mark.parametrize("case", speech_cases(), ids=lambda case: case[0])
def test_speech_messages_bound_their_text_state_and_clip(case: Any) -> None:
    _, name, valid, _, payload = case
    encoded = json.dumps(payload)
    if valid:
        message = RECORDING_SCHEMA_MODELS[name].model_validate_json(encoded)
        assert message.meta.take_id is None
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(encoded)


def test_speech_rules_name_what_is_wrong() -> None:
    for changes, message in (
        ({"relative_path": None}, "names its clip"),
        ({"reference_prompt_id": None}, "names its reference"),
        ({"run_id": None}, "names its run unless the base model spoke"),
        ({"state": "running"}, "only a finished synthesis names a clip"),
        ({"state": "failed"}, "names its error"),
        ({"model": "finetune-other"}, "speaks as itself"),
        ({"reference_run_id": "finetune-other"}, "its own reference"),
        ({"relative_path": "derived/speech/op-other/speech-01.wav"}, "derived/speech"),
        ({"seconds": 3.0}, "frames at its sample rate"),
        ({"text": "   "}, "more than whitespace"),
        ({"text": "a\x00b"}, "control characters"),
    ):
        with pytest.raises(ValidationError, match=message):
            synthetic_speech(**changes)
    with pytest.raises(ValidationError, match="the base model speaks without a run"):
        synthetic_speech(
            state="running",
            model="base",
            relative_path=None,
            sha256=None,
            frames=None,
            sample_rate_hz=None,
            seconds=None,
        )
    with pytest.raises(ValidationError, match="has not been resolved"):
        synthetic_speech(
            state="queued",
            relative_path=None,
            sha256=None,
            frames=None,
            sample_rate_hz=None,
            seconds=None,
        )
    command = SynthesizeCommand(operation_id="op", text="Hi.", model="base")
    assert (command.nfe_steps, command.reference_prompt_id) == (32, None)


@pytest.mark.parametrize("case", finetune_cases(), ids=lambda case: case[0])
def test_finetune_messages_bind_their_state_and_relative_run_paths(case: Any) -> None:
    _, name, valid, _, payload = case
    encoded = json.dumps(payload)
    if valid:
        message = RECORDING_SCHEMA_MODELS[name].model_validate_json(encoded)
        assert message.meta.take_id is None
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(encoded)


def test_every_finetune_code_is_a_valid_wire_error() -> None:
    names = {name for name, *_ in finetune_cases()}
    assert len(FINETUNE_WIRE_CODES) == 22
    for code in FINETUNE_WIRE_CODES:
        assert f"finetune-error-{code.lower().replace('.', '-').replace('_', '-')}" in names
    assert {
        "FINETUNE.CORPUS_INCOMPLETE",
        "FINETUNE.JOB_ACTIVE",
        "FINETUNE.INTERRUPTED",
        "FINETUNE.RUNTIME_UNAVAILABLE",
        "FINETUNE.NO_FINISHED_RUN",
        "FINETUNE.SYNTHESIS_DEADLINE",
    } <= set(FINETUNE_WIRE_CODES)
    # A cancelled job carries no error, so no code names a cancellation.
    assert "FINETUNE.CANCELLED" not in FINETUNE_WIRE_CODES


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_finetune_numbers_must_be_finite(value: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        synthetic_finetune_progress(loss=value)
    with pytest.raises(ValidationError, match="finite"):
        synthetic_finetune_run(retained_seconds=value)


def test_finetune_state_rules_name_what_is_missing() -> None:
    failure = RecordingError(code="FINETUNE.INTERRUPTED", message="Synthetic", retryable=False)
    for changes, message in (
        ({"epoch": None}, "pass and total"),
        ({"stage": "sampling", "epochs": None}, "counted against its total"),
        ({"state": "finished", "run_id": None}, "names its run"),
        ({"state": "failed"}, "names its error"),
        ({"state": "cancelled", "error": failure}, "names its error"),
        ({"state": "queued"}, "has not started a stage"),
    ):
        with pytest.raises(ValidationError, match=message):
            synthetic_finetune_progress(**changes)
    no_samples = {"finetuned_sample_paths": (), "base_sample_paths": ()}
    for changes, message in (
        ({"status": "running"}, "finish time"),
        ({"finished_at": datetime(2026, 9, 16, 7, 0, tzinfo=UTC)}, "before it started"),
        ({"status": "failed"} | no_samples, "name their error"),
        ({"status": "failed", "error": "Synthetic"}, "only a finished run has samples"),
        ({"base_sample_paths": ("derived/finetune/other/a.wav",)}, "under the run's directory"),
        ({"run_root": "/abs/" + FINETUNE_RUN_ROOT}, "absolute"),
    ):
        with pytest.raises(ValidationError, match=message):
            synthetic_finetune_run(**changes)
    progress = synthetic_finetune_progress()
    with pytest.raises(ValidationError, match="queued or running"):
        FineTuneStatusEvent(
            operation_id="op-status",
            runtime_configured=True,
            active=progress.model_copy(update={"state": "finished"}),
        )
    with pytest.raises(ValidationError, match="finished run"):
        FineTuneStatusEvent(
            operation_id="op-status",
            runtime_configured=True,
            latest_run=synthetic_finetune_run(status="failed", error="Synthetic", **no_samples),
        )
    with pytest.raises(ValidationError, match="never both"):
        FineTuneStatusEvent(
            operation_id="op-status",
            runtime_configured=True,
            latest_run=synthetic_finetune_run(),
            latest_run_error=failure,
        )


def recovery_event_cases() -> list[tuple[str, bool, str, dict[str, Any]]]:
    values = synthetic_contracts()
    page = values["recording-recovery-page"].model_dump(mode="json")
    take_id = page["entries"][0]["take_id"]
    base = {
        "data": {
            "event": "recovery",
            "operation_id": "recover-one",
            "take_id": take_id,
            "page": page,
        },
        "error": None,
        "meta": values["recording-response"].meta.model_dump(mode="json") | {"take_id": take_id},
    }
    cases = [("recovery-single-draft", True, "shared", base)]
    paged = copy.deepcopy(base)
    paged["data"]["take_id"] = None
    paged["meta"]["take_id"] = None
    paged["data"]["page"]["next_take_id"] = "later-page"
    cases.append(("recovery-independent-page", True, "shared", paged))
    for label, path, value in (
        ("take-identity", ("take_id",), "foreign-take"),
        ("partial-seal", ("page", "entries", 0, "status"), "partial"),
        ("unavailable-error", ("page", "entries", 0, "status"), "unavailable"),
        ("single-cursor", ("page", "next_take_id"), "later-page"),
        ("duplicate-take", ("page", "entries"), page["entries"] * 2),
    ):
        broken = copy.deepcopy(base)
        target: Any = broken["data"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        cases.append((f"recovery-{label}", False, "shared", broken))
    broken = copy.deepcopy(base)
    broken["data"]["page"]["entries"][0]["error"] = values["native-recovery"].error.model_dump(
        mode="json"
    )
    cases.append(("recovery-sealed-error", False, "shared", broken))
    return cases


@pytest.mark.parametrize("case", recovery_event_cases(), ids=lambda case: case[0])
def test_recovery_events_bind_their_exact_scope_and_evidence(case: Any) -> None:
    _, valid, _, payload = case
    if valid:
        assert RecordingResponse.model_validate_json(json.dumps(payload)).data.event == "recovery"
    else:
        with pytest.raises(ValidationError):
            RecordingResponse.model_validate_json(json.dumps(payload))


def recovery_inventory_event_cases() -> list[tuple[str, bool, str, dict[str, Any]]]:
    values = synthetic_contracts()
    page = values["recording-recovery-inventory"].model_dump(mode="json")
    base = {
        "data": {"event": "recovery-inventory", "page": page},
        "error": None,
        "meta": values["recording-response"].meta.model_dump(mode="json") | {"take_id": None},
    }
    cases = [("recovery-inventory-pending", True, "shared", base)]
    complete = copy.deepcopy(base)
    complete["data"]["page"].update(
        scan_state="complete",
        enumeration_complete=True,
        pending_count=0,
        finding_count=1,
        covered_store_revision=2,
        entries=page["entries"][1:],
    )
    cases.append(("recovery-inventory-complete", True, "shared", complete))
    changed = copy.deepcopy(base)
    changed["data"] = {
        "event": "recovery-inventory-changed",
        "scan_generation": page["scan_generation"],
        "inventory_revision": page["inventory_revision"],
    }
    cases.append(("recovery-inventory-changed", True, "shared", changed))
    for label, path, value in (
        ("complete-pending", ("scan_state",), "complete"),
        ("unfinished-coverage", ("covered_store_revision",), 2),
        ("missing-error", ("scan_state",), "error"),
        ("missing-pending", ("pending_count",), 0),
        ("excess-pending", ("pending_count",), 3),
        ("missing-recovered", ("recovered_seal_count",), 0),
        ("pending-recovered", ("recovered_seal_count",), 2),
        ("duplicate-take", ("entries",), page["entries"] * 2),
        ("missing-result", ("entries", 0, "pending"), False),
        ("wrong-result-take", ("entries", 1, "result", "take_id"), "foreign-take"),
    ):
        broken = copy.deepcopy(base)
        target: Any = broken["data"]["page"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        cases.append(("recovery-inventory-" + label, False, "shared", broken))
    return cases


@pytest.mark.parametrize("case", recovery_inventory_event_cases(), ids=lambda case: case[0])
def test_recovery_inventory_events_preserve_pending_counts_and_generation(case: Any) -> None:
    _, valid, _, payload = case
    if valid:
        response = RecordingResponse.model_validate_json(json.dumps(payload))
        assert response.data is not None
        assert response.data.event in {"recovery-inventory", "recovery-inventory-changed"}
    else:
        with pytest.raises(ValidationError):
            RecordingResponse.model_validate_json(json.dumps(payload))


def recovery_command_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    commands = {
        payload["data"]["command"]: model.model_validate(payload).model_dump(mode="json")
        for _, model, payload in synthetic_commands()
        if model is ServiceRequest
    }
    cases = []
    changes: list[tuple[str, str, tuple[str, ...], Any]] = [
        ("page-zero", "recover", ("data", "page_size"), 0),
        ("page-overflow", "recover", ("data", "page_size"), 51),
        ("page-float", "recover", ("data", "page_size"), 1.0),
        ("page-bool", "recover", ("data", "page_size"), True),
        ("snapshot-page-zero", "recovery-snapshot", ("data", "page_size"), 0),
        ("snapshot-page-overflow", "recovery-snapshot", ("data", "page_size"), 51),
        ("snapshot-page-bool", "recovery-snapshot", ("data", "page_size"), True),
        ("snapshot-cursor-overflow", "recovery-snapshot", ("data", "cursor"), "a" * 4097),
        ("mixed-scope", "recover", ("data", "take_id"), "take-other"),
        (
            "legacy-operation",
            "continue-legacy-recording",
            ("data", "context", "operation_id"),
            "different-operation",
        ),
        ("legacy-setup", "continue-legacy-recording", ("data", "context", "new_setup"), False),
        (
            "legacy-digest-shape",
            "continue-legacy-recording",
            ("data", "session_evidence_sha256"),
            "a" * 63,
        ),
        (
            "legacy-unknown-context",
            "continue-legacy-recording",
            ("data", "context", "unknown"),
            True,
        ),
    ]
    for label, command, path, value in changes:
        broken = copy.deepcopy(commands[command])
        target: Any = broken
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        cases.append(
            ("recovery-command-" + label, "recording-service-request", False, "shared", broken)
        )
    return cases


@pytest.mark.parametrize("case", recovery_command_cases(), ids=lambda case: case[0])
def test_recovery_commands_reject_invalid_context_and_page_scopes(case: Any) -> None:
    _, _, _, _, payload = case
    with pytest.raises(ValidationError):
        ServiceRequest.model_validate_json(json.dumps(payload))


def prompt_suite_v2_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    """Prompt-suite v1/v2 rules embedded in groups, so Python and Swift cannot diverge."""
    base = synthetic_contracts()["recording-group"].model_dump(mode="json")
    spoken = {
        "prompt_id": "synthetic-spoken",
        "text": "Read these synthetic words.",
        "category": "neutral",
        "style": "Natural pace",
        "expected_seconds": 4.0,
    }
    room = {
        "prompt_id": "synthetic-room",
        "instructions": "Remain still and silent.",
        "category": "room-tone",
        "style": "silence",
        "expected_seconds": 1.0,
    }
    step = {
        "prompt_id": "synthetic-step",
        "instructions": "Follow the synthetic step.",
        "category": "procedure",
        "style": "Follow the steps",
        "expected_seconds": 10.0,
        "labels": ["test"],
    }
    physical = ("cole-physical-test-synthetic", "physical-test")

    def group(
        prompts: list[dict[str, Any]],
        *,
        version: str = "v2",
        identity: tuple[str, str] = ("synthetic-pilot-v2", "capture-pilot"),
    ) -> dict[str, Any]:
        catalog = {
            "schema_version": f"cole-voice-lab.prompt-suite/{version}",
            "suite_id": identity[0],
            "purpose": identity[1],
            "prompts": prompts,
        }
        try:
            digest = canonical_sha256(PromptSuite.model_validate_json(json.dumps(catalog)))
        except ValidationError:
            digest = canonical_sha256(catalog)
        return copy.deepcopy(base) | {"catalog": catalog, "catalog_sha256": digest}

    def with_(prompt: dict[str, Any], **changes: Any) -> dict[str, Any]:
        return prompt | changes

    valid = [
        ("pilot", group([with_(spoken, labels=["warm-up"]), room])),
        ("null-text", group([spoken, with_(room, text=None, labels=[])])),
        ("physical-test", group([with_(room, labels=["test"]), step], identity=physical)),
        (
            "v1-empty-labels",
            group(
                [with_(room, text="Remain silent.", instructions=None), with_(spoken, labels=[])],
                version="v1",
            ),
        ),
    ]
    # TS2: the released v2 resources validate as embedded catalogs in Python and Swift alike.
    for resource in ("pilot-v2.json", "physical-test-v1.json"):
        suite = load_builtin_prompt_suite(resource)
        released = {
            "catalog": suite.model_dump(mode="json"),
            "catalog_sha256": canonical_sha256(suite),
        }
        valid.append((f"resource-{resource.removesuffix('.json')}", copy.deepcopy(base) | released))
    invalid = [
        (
            "v1-labels",
            group(
                [with_(spoken, labels=["warm-up"]), with_(room, text="Silent.", instructions=None)],
                version="v1",
            ),
        ),
        ("v1-instructions", group([with_(spoken, instructions="Extra")], version="v1")),
        ("v1-absent-text", group([room], version="v1")),
        ("v1-procedure", group([with_(step, labels=[])], version="v1")),
        ("v1-physical-test", group([spoken], version="v1", identity=physical)),
        ("p2-spoken-without-text", group([with_(spoken, text=None)])),
        ("p3-procedure-text", group([with_(step, text="Words")], identity=physical)),
        ("p4-duplicate-label", group([with_(spoken, labels=["warm-up", "warm-up"])])),
        ("s2-room-tone-text", group([with_(room, text="Remain silent.")])),
        ("s2-room-tone-without-instructions", group([with_(room, instructions=None)])),
        (
            "s4-purpose-without-prefix",
            group([step], identity=("synthetic-pilot-v2", "physical-test")),
        ),
        (
            "s4-prefix-without-purpose",
            group([spoken], identity=("cole-physical-test-x", "capture-pilot")),
        ),
        ("s4-missing-test-label", group([with_(step, labels=["recovery"])], identity=physical)),
        (
            "s4-test-reference",
            group([with_(spoken, labels=["test"], reference_candidate=True)], identity=physical),
        ),
        ("s4-test-label-outside", group([with_(spoken, labels=["test"])])),
        ("s4-procedure-outside", group([room, with_(step, labels=[])])),
        ("s7-zero-width", group([with_(spoken, text="Read\u200b this.")])),
        ("s7-bidi-override", group([with_(room, instructions="Stay\u202e silent.")])),
        ("s7-tab", group([with_(spoken, style="Natural\tpace")])),
    ]
    invalid += [
        (f"p4-reserved-{token}", group([with_(spoken, labels=[token])]))
        for token in sorted(RESERVED_PROMPT_LABELS)
    ]
    return [
        (f"prompt-suite-v2-{name}", "recording-group", True, "shared", payload)
        for name, payload in valid
    ] + [
        (f"prompt-suite-v2-{name}", "recording-group", False, "shared", payload)
        for name, payload in invalid
    ]


@pytest.mark.parametrize("case", prompt_suite_v2_cases(), ids=lambda case: case[0])
def test_prompt_suite_v2_conformance_cases(case: Any) -> None:
    _, name, valid, _, payload = case
    if valid:
        RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


RECORDING_GROUP_V2 = "cole-voice-lab.recording-group/v2"
CORPUS_WIRE_CODES = (
    "CORPUS.NO_ACTIVE_CATALOG",
    "CORPUS.SNAPSHOT_MISMATCH",
    "CORPUS.STORE_MISSING",
)
# Store codes the recording service never sends (the CLI still prints them).
CORPUS_STORE_ONLY_CODES = ("CORPUS.CATALOG_UNKNOWN", "CORPUS.PROMPT_DUPLICATE")
_ABSENT = object()


def _rebuild(model: Any, **changes: Any) -> Any:
    """Re-validate a synthetic contract with replaced fields (never an unchecked copy)."""
    return type(model).model_validate(model.model_dump() | changes)


def synthetic_v2_group(
    *, purpose: str = "capture-pilot", gate: PreparedCaptureGate | None = None
) -> RecordingGroup:
    """The synthetic group as recording-group/v2: its catalog referenced, never embedded."""
    group = synthetic_contracts()["recording-group"]
    suite = PromptSuite.model_validate(group.catalog.model_dump() | {"purpose": purpose})
    return _rebuild(
        group,
        schema_version=RECORDING_GROUP_V2,
        catalog=None,
        catalog_reference=_catalog_reference(suite),
        catalog_sha256=canonical_sha256(suite),
        gate=gate or PreparedCaptureGate(state="not-required"),
    )


def recording_group_v2_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    """recording-group/v2 alone and nested in the unchanged v1 containers.

    Python and Swift apply the same version-dependent rules: v1 embeds exactly its catalog,
    v2 carries exactly a content-addressed reference whose purpose drives the gate rule, and
    prompt-membership checks that need embedded prompts run only for v1.
    """
    values = synthetic_contracts()
    v1 = values["recording-group"]
    group = synthetic_v2_group()
    ready = PreparedCaptureGate(
        state="ready",
        approval_path="provenance/pilot-gate.json",
        approval_sha256="a" * 64,
        evidence_sha256="b" * 64,
        verified_at=v1.opened_at,
    )
    corpus = synthetic_v2_group(purpose="capture-corpus", gate=ready)
    reservation = _rebuild(values["capture-reservation"], group=group)
    corpus_reservation = _rebuild(values["capture-reservation"], group=corpus)
    seal = _rebuild(values["capture-seal"], reservation_sha256=canonical_sha256(reservation))
    take = _rebuild(values["recording-take"], reservation=reservation, seal=seal)
    meta = values["recording-response"].meta.model_copy(update={"take_id": None})
    # The service opens the cursor on a snapshot prompt the group itself does not list.
    cursor = RecordingCursor(
        subject_id=group.subject_id,
        group_id=group.group_id,
        selection=RecordingSelection(
            catalog_sha256=group.catalog_sha256,
            prompt_id="snapshot-only-prompt",
            take_id=None,
            selection_revision=1,
            index_revision=1,
        ),
        mode="selected-prompt",
        complete=False,
        operation_id="context-open",
    )
    context = RecordingResponse(
        data=ContextEvent(group=group, cursor=cursor), error=None, meta=meta
    )
    package = values["recording-package-map"]
    selected = PromptSuite(
        suite_id="synthetic-export-subset",
        purpose="capture-pilot",
        prompts=tuple(entry.prompt for entry in package.entries),
    )
    package_map = _rebuild(package, group=group, selected_suite=selected)

    def dump(model: Any) -> dict[str, Any]:
        return copy.deepcopy(model.model_dump(mode="json"))

    def changed(model: Any, path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
        payload = dump(model)
        target: Any = payload
        for key in path[:-1]:
            target = target[key]
        if value is _ABSENT:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return payload

    embedded = v1.catalog.model_dump(mode="json")
    reference = group.catalog_reference.model_dump(mode="json")
    both = dump(group) | {"catalog": embedded}
    unbound_digest = changed(group, ("catalog_reference", "catalog_sha256"), "0" * 64)
    unbound_digest["catalog_reference"]["snapshot_path"] = catalog_snapshot_path("0" * 64)
    valid: list[tuple[str, str, dict[str, Any]]] = [
        ("group", "recording-group", dump(group)),
        ("corpus-group", "recording-group", dump(corpus)),
        ("explicit-null-catalog", "recording-group", dump(group) | {"catalog": None}),
        ("v1-explicit-null-reference", "recording-group", dump(v1) | {"catalog_reference": None}),
        ("reference", "catalog-reference", reference),
        ("reservation", "capture-reservation", dump(reservation)),
        ("corpus-reservation", "capture-reservation", dump(corpus_reservation)),
        ("take", "recording-take", dump(take)),
        ("context-event", "recording-response", dump(context)),
        ("package-map", "recording-package-map", dump(package_map)),
    ]
    valid += [
        (
            f"error-{code.lower().replace('.', '-').replace('_', '-')}",
            "recording-response",
            dump(
                RecordingResponse(
                    data=None,
                    error=RecordingError(
                        code=code, message="Synthetic corpus-store failure", retryable=False
                    ),
                    meta=meta,
                )
            ),
        )
        for code in CORPUS_WIRE_CODES
    ]
    unready = dump(corpus_reservation)
    unready["group"]["gate"] = {"state": "unprepared"}
    invalid: list[tuple[str, str, dict[str, Any]]] = [
        ("v2-embeds-both", "recording-group", both),
        ("v1-carries-both", "recording-group", both | {"schema_version": v1.schema_version}),
        (
            "v2-catalog-instead-of-reference",
            "recording-group",
            {key: value for key, value in both.items() if key != "catalog_reference"},
        ),
        (
            "v1-reference-instead-of-catalog",
            "recording-group",
            changed(v1, ("catalog",), _ABSENT) | {"catalog_reference": reference},
        ),
        ("v2-neither", "recording-group", changed(group, ("catalog_reference",), _ABSENT)),
        ("v1-neither", "recording-group", changed(v1, ("catalog",), _ABSENT)),
        # An omitted version reads as v1, which requires the embedded catalog.
        ("v2-missing-version", "recording-group", changed(group, ("schema_version",), _ABSENT)),
        (
            "unknown-version",
            "recording-group",
            changed(group, ("schema_version",), "cole-voice-lab.recording-group/v3"),
        ),
        ("reference-digest", "recording-group", unbound_digest),
        (
            "reference-purpose-corpus-without-gate",
            "recording-group",
            changed(group, ("catalog_reference", "purpose"), "capture-corpus"),
        ),
        (
            "reference-purpose-pilot-with-gate",
            "recording-group",
            changed(corpus, ("catalog_reference", "purpose"), "capture-pilot"),
        ),
        (
            "reference-snapshot-path",
            "recording-group",
            changed(
                group,
                ("catalog_reference", "snapshot_path"),
                "provenance/corpus/snapshots/other.json",
            ),
        ),
        (
            "reference-unknown-key",
            "recording-group",
            changed(group, ("catalog_reference", "native_extra"), True),
        ),
        (
            "reference-version-zero",
            "recording-group",
            changed(group, ("catalog_reference", "catalog_version"), 0),
        ),
        (
            "standalone-reference-snapshot-path",
            "catalog-reference",
            reference | {"snapshot_path": f"elsewhere/{reference['catalog_sha256']}.json"},
        ),
        ("corpus-reservation-unready", "capture-reservation", unready),
        (
            "package-map-without-selected-suite",
            "recording-package-map",
            changed(package_map, ("selected_suite",), _ABSENT),
        ),
        (
            "package-map-selected-purpose",
            "recording-package-map",
            changed(package_map, ("selected_suite", "purpose"), "capture-corpus"),
        ),
        (
            "package-map-selected-content",
            "recording-package-map",
            changed(package_map, ("selected_suite", "prompts", 0, "text"), "Changed prompt"),
        ),
        (
            "context-event-catalog",
            "recording-response",
            changed(context, ("data", "cursor", "selection", "catalog_sha256"), "0" * 64),
        ),
        *(
            (
                f"error-store-only-code-{code.removeprefix('CORPUS.').lower().replace('_', '-')}",
                "recording-response",
                changed(
                    RecordingResponse(
                        data=None,
                        error=RecordingError(
                            code=CORPUS_WIRE_CODES[0], message="Synthetic", retryable=False
                        ),
                        meta=meta,
                    ),
                    ("error", "code"),
                    code,
                ),
            )
            for code in CORPUS_STORE_ONLY_CODES
        ),
    ]
    return [
        (f"recording-group-v2-{name}", model, True, "shared", payload)
        for name, model, payload in valid
    ] + [
        (f"recording-group-v2-{name}", model, False, "shared", payload)
        for name, model, payload in invalid
    ]


@pytest.mark.parametrize("case", recording_group_v2_cases(), ids=lambda case: case[0])
def test_recording_group_v2_conformance_cases(case: Any) -> None:
    _, name, valid, _, payload = case
    if valid:
        RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


CORPUS_SHAPED_PROMPT_COUNT = 2_000


def corpus_shaped_catalog(count: int = CORPUS_SHAPED_PROMPT_COUNT) -> PromptSuite:
    """A synthetic catalog shaped like the real corpus (1,886 prompts), a little larger.

    Every prompt is longer than the imported Harvard/ARCTIC sentences (at most 91
    characters), so a frame that fits this catalog fits the real one.
    """
    return PromptSuite(
        schema_version="cole-voice-lab.prompt-suite/v2",
        suite_id="synthetic-corpus-shape",
        purpose="capture-corpus",
        prompts=tuple(
            Prompt(
                prompt_id=f"shape-{number:04d}",
                text=f"Synthetic corpus sentence {number:04d} keeps a realistic length so the "
                "whole catalog is measured honestly.",
                category="neutral",
                style="Plain reading, natural pace.",
                expected_seconds=6.3,
                labels=("synthetic-set",),
            )
            for number in range(count)
        ),
    )


def catalog_event_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    """The catalog request/reply and the reference form of open-context, shared with Swift.

    Python alone recomputes canonical digests, so a catalog event whose suite does not hash
    to its reference is python-evidence; every other rule is checked on both sides.
    """
    values = synthetic_contracts()
    group = values["recording-group"]
    suite = group.catalog
    meta = values["recording-response"].meta.model_copy(update={"take_id": None})
    reference = _catalog_reference(suite)
    corpus_suite = PromptSuite.model_validate(suite.model_dump() | {"purpose": "capture-corpus"})
    corpus_reference = _catalog_reference(corpus_suite)
    large = corpus_shaped_catalog()

    def dump(model: Any) -> dict[str, Any]:
        return copy.deepcopy(model.model_dump(mode="json"))

    def command(data: dict[str, Any]) -> dict[str, Any]:
        return {"data": data, "error": None, "meta": meta.model_dump(mode="json")}

    def reply(catalog: PromptSuite, named: CatalogReference) -> dict[str, Any]:
        return dump(
            RecordingResponse(
                data=CatalogEvent(operation_id="op-catalog", catalog=catalog, reference=named),
                error=None,
                meta=meta,
            )
        )

    catalog_command = command({"command": "catalog", "operation_id": "op-catalog"})
    event = reply(suite, reference)
    referenced = dump(
        OpenContextCommand(
            operation_id="context-referenced",
            subject_id=group.subject_id,
            catalog_reference=reference,
            capture_device=group.capture_device,
            capture_host=group.capture_host,
        )
    )
    approved = referenced | {
        "catalog_reference": corpus_reference.model_dump(mode="json"),
        "pilot_approval_path": "provenance/pilot-gate.json",
        "pilot_approval_sha256": "a" * 64,
    }
    embedded = {
        "catalog": suite.model_dump(mode="json"),
        "catalog_sha256": canonical_sha256(suite),
    }
    without_reference = {
        key: value for key, value in referenced.items() if key != "catalog_reference"
    }
    legacy = next(
        dump(ServiceRequest.model_validate(payload))
        for name, _, payload in synthetic_commands()
        if name.startswith("service-continue-legacy-recording")
    )
    legacy_referenced = copy.deepcopy(legacy)
    legacy_context = legacy_referenced["data"]["context"]
    del legacy_context["catalog"], legacy_context["catalog_sha256"]
    legacy_context["catalog_reference"] = reference.model_dump(mode="json")
    other_digest = "0" * 64
    unbound = copy.deepcopy(event)
    unbound["data"]["reference"] |= {
        "catalog_sha256": other_digest,
        "snapshot_path": catalog_snapshot_path(other_digest),
    }
    changed_suite = copy.deepcopy(event)
    changed_suite["data"]["reference"]["suite_id"] = "another-suite"
    changed_purpose = copy.deepcopy(event)
    changed_purpose["data"]["reference"]["purpose"] = "capture-corpus"
    missing_reference = copy.deepcopy(event)
    del missing_reference["data"]["reference"]
    unknown_field = copy.deepcopy(event)
    unknown_field["data"]["native_extra"] = True
    event_with_take = copy.deepcopy(event)
    event_with_take["meta"]["take_id"] = "synthetic-take"
    unbound_path = copy.deepcopy(event)
    unbound_path["data"]["reference"]["snapshot_path"] = "provenance/corpus/snapshots/other.json"
    command_with_take = copy.deepcopy(catalog_command)
    command_with_take["meta"]["take_id"] = "synthetic-take"
    command_without_operation = command({"command": "catalog"})
    valid: list[tuple[str, str, dict[str, Any]]] = [
        ("command", "recording-service-request", catalog_command),
        ("event", "recording-response", event),
        ("event-corpus-shaped", "recording-response", reply(large, _catalog_reference(large))),
        ("open-context-referenced", "recording-service-request", command(referenced)),
        ("open-context-referenced-approval", "recording-service-request", command(approved)),
        (
            "open-context-referenced-explicit-null-embedding",
            "recording-service-request",
            command(referenced | {"catalog": None, "catalog_sha256": None}),
        ),
    ]
    invalid: list[tuple[str, str, str, dict[str, Any]]] = [
        ("event-digest-mismatch", "recording-response", "python-evidence", unbound),
        ("event-suite-id", "recording-response", "shared", changed_suite),
        ("event-purpose", "recording-response", "shared", changed_purpose),
        ("event-missing-reference", "recording-response", "shared", missing_reference),
        ("event-unknown-field", "recording-response", "shared", unknown_field),
        ("event-meta-take", "recording-response", "shared", event_with_take),
        ("event-snapshot-path", "recording-response", "shared", unbound_path),
        ("command-meta-take", "recording-service-request", "shared", command_with_take),
        ("command-operation", "recording-service-request", "shared", command_without_operation),
        (
            "open-context-both-forms",
            "recording-service-request",
            "shared",
            command(referenced | embedded),
        ),
        (
            "open-context-reference-and-digest",
            "recording-service-request",
            "shared",
            command(referenced | {"catalog_sha256": embedded["catalog_sha256"]}),
        ),
        (
            "open-context-reference-and-catalog",
            "recording-service-request",
            "shared",
            command(referenced | {"catalog": embedded["catalog"]}),
        ),
        ("open-context-neither", "recording-service-request", "shared", command(without_reference)),
        (
            "open-context-catalog-without-digest",
            "recording-service-request",
            "shared",
            command(without_reference | {"catalog": embedded["catalog"]}),
        ),
        (
            "open-context-referenced-approval-pilot",
            "recording-service-request",
            "shared",
            command(approved | {"catalog_reference": reference.model_dump(mode="json")}),
        ),
        (
            "open-context-reference-unknown-key",
            "recording-service-request",
            "shared",
            command(
                referenced
                | {"catalog_reference": reference.model_dump(mode="json") | {"native_extra": 1}}
            ),
        ),
        ("continue-legacy-referenced", "recording-service-request", "shared", legacy_referenced),
    ]
    return [
        (f"catalog-{name}", model, True, "shared", payload) for name, model, payload in valid
    ] + [
        (f"catalog-{name}", model, False, owner, payload) for name, model, owner, payload in invalid
    ]


@pytest.mark.parametrize("case", catalog_event_cases(), ids=lambda case: case[0])
def test_catalog_event_and_referenced_context_conformance_cases(case: Any) -> None:
    _, name, valid, _, payload = case
    if valid:
        RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))
    else:
        with pytest.raises(ValidationError):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


def test_catalog_event_binds_its_exact_suite_and_reference() -> None:
    suite = synthetic_contracts()["recording-group"].catalog
    reference = _catalog_reference(suite)
    event = CatalogEvent(operation_id="op-catalog", catalog=suite, reference=reference)
    assert CatalogEvent.model_validate_json(canonical_json_bytes(event)) == event
    other = PromptSuite.model_validate(suite.model_dump() | {"suite_id": "another-suite"})
    for catalog, named, message in (
        (other, reference, "exact suite its reference names"),
        (other, _catalog_reference(other, suite_id=suite.suite_id), "suite id and purpose"),
        (
            suite,
            reference.model_copy(update={"purpose": "capture-corpus"}),
            "suite id and purpose",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            CatalogEvent.model_validate(
                {"operation_id": "op-catalog", "catalog": catalog, "reference": named}
            )
    command = CatalogCommand(operation_id="op-catalog")
    assert canonical_json_bytes(command) == b'{"command":"catalog","operation_id":"op-catalog"}'


def test_a_corpus_sized_catalog_event_fits_one_control_frame() -> None:
    suite = corpus_shaped_catalog()
    assert len(suite.prompts) >= 1_886
    meta = synthetic_contracts()["recording-response"].meta.model_copy(update={"take_id": None})
    response = RecordingResponse(
        data=CatalogEvent(
            operation_id="op-catalog", catalog=suite, reference=_catalog_reference(suite)
        ),
        error=None,
        meta=meta,
    )
    frame = recording_frame_bytes(response)
    # A quarter of the cap stays free for longer real prompts (pilot prompts run to about
    # 350 bytes each), so the reply never becomes the frame that closes the session.
    assert len(frame) <= MAX_CONTROL_FRAME_BYTES * 3 // 4
    assert RecordingResponse.model_validate_json(frame[4:]) == response


def catalog_of_exactly(size: int, *, suite_id: str = "synthetic-corpus-shape") -> PromptSuite:
    """A corpus-shaped catalog whose canonical suite bytes are exactly ``size``.

    Its last prompt, ``shape-pad``, carries filler text sized to land on ``size``; the
    other prompts are ``corpus_shaped_catalog``'s.
    """

    def build(count: int, pad: int) -> PromptSuite:
        filler = Prompt(
            prompt_id="shape-pad",
            text="p" * pad,
            category="neutral",
            style="Plain reading, natural pace.",
            expected_seconds=6.3,
        )
        return PromptSuite(
            schema_version="cole-voice-lab.prompt-suite/v2",
            suite_id=suite_id,
            purpose="capture-corpus",
            prompts=(*corpus_shaped_catalog(count).prompts, filler),
        )

    one = len(canonical_json_bytes(build(1, 1)))
    per_prompt = len(canonical_json_bytes(build(2, 1))) - one
    shaped = 1 + (size - one) // per_prompt
    suite = build(shaped, size - one - (shaped - 1) * per_prompt + 1)
    assert len(canonical_json_bytes(suite)) == size
    return suite


def test_a_catalog_at_the_suite_budget_fits_one_frame_with_the_largest_envelope() -> None:
    longest = "a" * 128
    suite = catalog_of_exactly(MAX_CATALOG_SUITE_BYTES, suite_id="s" * 128)
    response = RecordingResponse(
        data=CatalogEvent(
            operation_id=longest,
            catalog=suite,
            reference=_catalog_reference(
                suite, catalog_id="c" * 128, catalog_version=MAX_RECORDING_REVISION
            ),
        ),
        error=None,
        meta=RecordingMessageMeta(
            request_id=longest,
            process_generation=longest,
            state_revision=MAX_RECORDING_REVISION,
            event_sequence=MAX_RECORDING_REVISION,
        ),
    )
    frame = recording_frame_bytes(response)
    envelope = len(frame) - FRAME_PREFIX_BYTES - MAX_CATALOG_SUITE_BYTES
    # The reserve covers the largest envelope with room to spare.
    assert envelope * 3 <= CATALOG_EVENT_RESERVE_BYTES
    assert MAX_CATALOG_SUITE_BYTES + CATALOG_EVENT_RESERVE_BYTES == MAX_CONTROL_FRAME_BYTES


def test_open_context_names_exactly_one_catalog_form() -> None:
    values = synthetic_contracts()
    group = values["recording-group"]
    suite = group.catalog
    reference = _catalog_reference(suite)
    common: dict[str, Any] = {
        "operation_id": "context-open",
        "subject_id": group.subject_id,
        "capture_device": group.capture_device,
        "capture_host": group.capture_host,
    }
    referenced = OpenContextCommand(**common, catalog_reference=reference)
    assert referenced.catalog_purpose == "capture-pilot"
    assert referenced.catalog is None and referenced.catalog_sha256 is None
    encoded = canonical_json_bytes(referenced)
    assert {"catalog", "catalog_sha256"}.isdisjoint(json.loads(encoded))
    assert OpenContextCommand.model_validate_json(encoded) == referenced
    embedded = OpenContextCommand(**common, catalog=suite, catalog_sha256=canonical_sha256(suite))
    assert embedded.catalog_purpose == "capture-pilot"
    # The embedded form serializes exactly as the contract before the corpus store did, so
    # durable legacy-continuation intents and replay digests are unchanged.
    assert canonical_json_bytes(embedded) == canonical_json_bytes(
        {
            "command": "open-context",
            **common,
            "capture_device": group.capture_device.model_dump(mode="json"),
            "catalog": suite.model_dump(mode="json"),
            "catalog_sha256": canonical_sha256(suite),
            "expected_selection_revision": None,
            "new_setup": False,
            "setup_description": "",
            "pilot_approval_path": None,
            "pilot_approval_sha256": None,
        }
    )
    corpus = _catalog_reference(
        PromptSuite.model_validate(suite.model_dump() | {"purpose": "capture-corpus"})
    )
    approval = {
        "pilot_approval_path": "provenance/pilot-gate.json",
        "pilot_approval_sha256": "a" * 64,
    }
    assert OpenContextCommand(**common, catalog_reference=corpus, **approval).catalog_purpose == (
        "capture-corpus"
    )
    for changes, message in (
        ({"catalog_reference": reference, "catalog": suite}, "not both"),
        ({"catalog_reference": reference, "catalog_sha256": "0" * 64}, "not both"),
        ({}, "requires a catalog reference"),
        ({"catalog": suite}, "requires a catalog reference"),
        ({"catalog_sha256": canonical_sha256(suite)}, "requires a catalog reference"),
        ({"catalog": suite, "catalog_sha256": "0" * 64}, "exact canonical catalog"),
        ({"catalog_reference": reference, **approval}, "only to the corpus catalog"),
    ):
        with pytest.raises(ValidationError, match=message):
            OpenContextCommand.model_validate(common | changes)
    with pytest.raises(ContractError, match="names no catalog"):
        _ = OpenContextCommand.model_construct(**common).catalog_purpose


def test_legacy_continuation_requires_the_embedded_context() -> None:
    legacy = next(
        ServiceRequest.model_validate(payload).data
        for name, _, payload in synthetic_commands()
        if name.startswith("service-continue-legacy-recording")
    )
    assert isinstance(legacy, ContinueLegacyRecordingCommand)
    assert legacy.legacy_catalog == legacy.context.catalog
    referenced = legacy.context.model_copy(
        update={
            "catalog": None,
            "catalog_sha256": None,
            "catalog_reference": _catalog_reference(legacy.legacy_catalog),
        }
    )
    with pytest.raises(ValidationError, match="embedded catalog, not a catalog reference"):
        ContinueLegacyRecordingCommand.model_validate(
            legacy.model_dump() | {"context": referenced.model_dump()}
        )
    unchecked = ContinueLegacyRecordingCommand.model_construct(
        **(legacy.model_dump() | {"context": referenced})
    )
    with pytest.raises(ContractError, match="embedded catalog"):
        _ = unchecked.legacy_catalog


def prompt_equality_cases() -> list[tuple[str, str, bool, str, dict[str, Any]]]:
    """Prompt comparisons follow Python value equality on both sides.

    An integer token in a float field is the same prompt (Python strict floats accept it and
    ``1 == 1.0``); canonically equivalent but differently encoded text is a different prompt
    (Python compares code points, Swift ``String ==`` would not).
    """
    values = synthetic_contracts()
    composed, decomposed = "Remain sil\u00e9nt.", "Remain sile\u0301nt."

    def dump(model: Any) -> dict[str, Any]:
        return copy.deepcopy(model.model_dump(mode="json"))

    def with_text(payload: dict[str, Any], text: str) -> dict[str, Any]:
        return payload | {"text": text}

    def embedded_group(prompt: dict[str, Any]) -> dict[str, Any]:
        group = dump(values["recording-group"])
        group["catalog"]["prompts"] = [prompt]
        group["catalog_sha256"] = canonical_sha256(group["catalog"])
        return group

    def package(
        group: dict[str, Any], entry: dict[str, Any], selected: dict[str, Any] | None
    ) -> dict[str, Any]:
        payload = dump(values["recording-package-map"]) | {"group": group}
        payload["entries"][0] |= {"prompt": entry, "catalog_sha256": group["catalog_sha256"]}
        if selected is not None:
            payload["selected_suite"] = {
                "suite_id": "synthetic-export-subset",
                "purpose": "capture-pilot",
                "prompts": [selected],
            }
        return payload

    def reservation(group: dict[str, Any], prompt: dict[str, Any]) -> dict[str, Any]:
        return dump(values["capture-reservation"]) | {"group": group, "prompt": prompt}

    prompt = dump(values["recording-group"])["catalog"]["prompts"][0]
    integer = prompt | {"expected_seconds": 1}
    assert isinstance(prompt["expected_seconds"], float)
    nfc, nfd = with_text(prompt, composed), with_text(prompt, decomposed)
    plain = dump(values["recording-group"])
    composed_group = embedded_group(nfc)
    v2 = synthetic_v2_group().model_dump(mode="json")
    valid = [
        ("package-selected-integer-seconds", package(plain, prompt, integer)),
        ("package-entry-integer-seconds", package(plain, integer, None)),
        ("v2-package-integer-seconds", package(v2, integer, prompt)),
        ("reservation-integer-seconds", reservation(plain, integer)),
        ("package-composed-text", package(composed_group, nfc, nfc)),
        ("reservation-composed-text", reservation(composed_group, nfc)),
    ]
    invalid = [
        ("package-selected-decomposed-text", package(composed_group, nfc, nfd)),
        ("package-entry-decomposed-text", package(composed_group, nfd, None)),
        ("v2-package-decomposed-text", package(v2, nfc, nfd)),
        ("reservation-decomposed-text", reservation(composed_group, nfd)),
    ]
    model = {"package": "recording-package-map", "reservation": "capture-reservation"}

    def model_of(name: str) -> str:
        return model["reservation" if name.startswith("reservation") else "package"]

    return [
        (f"prompt-equality-{name}", model_of(name), True, "shared", payload)
        for name, payload in valid
    ] + [
        (f"prompt-equality-{name}", model_of(name), False, "shared", payload)
        for name, payload in invalid
    ]


@pytest.mark.parametrize("case", prompt_equality_cases(), ids=lambda case: case[0])
def test_prompt_comparisons_follow_python_value_equality(case: Any) -> None:
    _, name, valid, _, payload = case
    if valid:
        RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))
    else:
        with pytest.raises(ValidationError, match="exact"):
            RECORDING_SCHEMA_MODELS[name].model_validate_json(json.dumps(payload))


def test_recording_group_v1_bytes_are_unchanged_and_v2_embeds_no_catalog() -> None:
    v1 = synthetic_contracts()["recording-group"]
    v1_bytes = canonical_json_bytes(v1)
    assert b'"catalog":' in v1_bytes and b"catalog_reference" not in v1_bytes
    assert v1.schema_version == "cole-voice-lab.recording-group/v1"
    assert RecordingGroup.model_validate_json(v1_bytes) == v1
    assert v1.catalog_purpose == "capture-pilot"
    assert not hasattr(v1, "require_embedded_catalog")
    group = synthetic_v2_group()
    encoded = canonical_json_bytes(group)
    assert b'"catalog":' not in encoded and b'"catalog_reference":' in encoded
    assert RecordingGroup.model_validate_json(encoded) == group
    assert group.catalog_purpose == "capture-pilot"
    assert (
        synthetic_v2_group(
            purpose="capture-corpus", gate=PreparedCaptureGate(state="unprepared")
        ).catalog_purpose
        == "capture-corpus"
    )
    for payload, message in (
        (group.model_dump() | {"catalog": v1.catalog}, "recording-group/v2 carries"),
        (v1.model_dump() | {"catalog_reference": group.catalog_reference}, "recording-group/v1"),
        (
            group.model_dump()
            | {"catalog_reference": _catalog_reference(v1.catalog, catalog_version=2)}
            | {"catalog_sha256": "0" * 64},
            "digest must match its catalog reference",
        ),
        (
            group.model_dump()
            | {"catalog_reference": _catalog_reference(v1.catalog, purpose="capture-corpus")},
            "corpus catalog requires a prepared capture gate",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            RecordingGroup.model_validate(payload)


def test_catalog_reference_group_package_map_requires_its_selected_suite() -> None:
    package = next(
        payload
        for name, model, valid, _, payload in recording_group_v2_cases()
        if name == "recording-group-v2-package-map"
    )
    assert RecordingPackageMap.model_validate_json(json.dumps(package)).selected_suite is not None
    with pytest.raises(ValidationError, match="requires its selected suite"):
        RecordingPackageMap.model_validate_json(json.dumps(package | {"selected_suite": None}))


def test_catalog_reference_binds_the_content_addressed_snapshot_path() -> None:
    suite = synthetic_contracts()["recording-group"].catalog
    reference = _catalog_reference(suite)
    assert reference.snapshot_path == f"provenance/corpus/snapshots/{reference.catalog_sha256}.json"
    assert reference.snapshot_sha256 != reference.catalog_sha256
    assert CatalogReference.model_validate_json(canonical_json_bytes(reference)) == reference
    for changes, message in (
        ({"snapshot_path": "provenance/corpus/snapshots/other.json"}, "content-addressed"),
        ({"snapshot_path": f"elsewhere/{reference.catalog_sha256}.json"}, "content-addressed"),
        ({"catalog_version": 0}, "greater than or equal to 1"),
        ({"purpose": "karaoke"}, "purpose"),
        ({"catalog_id": "Bad Id"}, "pattern"),
        ({"snapshot_sha256": "not-a-digest"}, "snapshot_sha256"),
    ):
        with pytest.raises(ValidationError, match=message):
            _catalog_reference(suite, **changes)
    with pytest.raises(ValidationError, match="snapshot_sha256"):
        CatalogReference.model_validate(
            {
                key: value
                for key, value in reference.model_dump().items()
                if key != "snapshot_sha256"
            }
        )


def test_prompt_alias_never_accepts_its_own_digest() -> None:
    alias = PromptAlias(prompt_sha256="a" * 64, accepted_prompt_sha256="b" * 64)
    assert alias.accepted_prompt_sha256 == "b" * 64
    with pytest.raises(ValidationError, match="cannot accept the current digest"):
        PromptAlias(prompt_sha256="a" * 64, accepted_prompt_sha256="a" * 64)


def test_catalog_snapshot_validates_its_digest_and_aliases_and_maps_recorded_digests() -> None:
    suite = load_builtin_prompt_suite("evaluation-v1.json")
    digest = canonical_sha256(suite)
    current = [canonical_sha256(prompt) for prompt in suite.prompts]
    aliases = tuple(
        sorted(
            (
                PromptAlias(prompt_sha256=current[1], accepted_prompt_sha256="1" * 64),
                PromptAlias(prompt_sha256=current[1], accepted_prompt_sha256="2" * 64),
                PromptAlias(prompt_sha256=current[2], accepted_prompt_sha256="3" * 64),
            ),
            key=lambda alias: (alias.prompt_sha256, alias.accepted_prompt_sha256),
        )
    )
    snapshot = CatalogSnapshot(suite=suite, catalog_sha256=digest, prompt_aliases=aliases)
    assert snapshot.schema_version == "cole-voice-lab.catalog-snapshot/v1"
    assert snapshot.accepted_hashes(current[0]) == {current[0]}
    assert snapshot.accepted_hashes(current[1]) == {current[1], "1" * 64, "2" * 64}
    assert snapshot.current_for(current[0]) == current[0]
    assert snapshot.current_for("2" * 64) == current[1]
    assert snapshot.current_for("3" * 64) == current[2]
    assert snapshot.current_for("9" * 64) is None
    with pytest.raises(KeyError):
        snapshot.accepted_hashes("9" * 64)
    restored = CatalogSnapshot.model_validate_json(canonical_json_bytes(snapshot))
    assert restored == snapshot
    assert restored.current_for("1" * 64) == current[1]
    assert set(CORPUS_SCHEMA_MODELS) == {"catalog-snapshot"}
    assert CORPUS_SCHEMA_MODELS["catalog-snapshot"] is CatalogSnapshot
    assert RECORDING_SCHEMA_MODELS["catalog-reference"] is CatalogReference
    embedded = CatalogSnapshot.embedded(suite)
    assert embedded == CatalogSnapshot(suite=suite, catalog_sha256=digest, prompt_aliases=())
    assert embedded.current_for(current[1]) == current[1]
    assert embedded.current_for("1" * 64) is None
    for prompt_aliases, message in (
        (tuple(reversed(aliases)), "sorted and unique"),
        ((aliases[0], aliases[0]), "sorted and unique"),
        ((PromptAlias(prompt_sha256="f" * 64, accepted_prompt_sha256="1" * 64),), "belong to"),
        (
            (PromptAlias(prompt_sha256=current[1], accepted_prompt_sha256=current[0]),),
            "cannot also be a current prompt digest",
        ),
        (
            tuple(
                sorted(
                    (
                        PromptAlias(prompt_sha256=current[1], accepted_prompt_sha256="e" * 64),
                        PromptAlias(prompt_sha256=current[2], accepted_prompt_sha256="e" * 64),
                    ),
                    key=lambda alias: alias.prompt_sha256,
                )
            ),
            "cannot belong to more than one prompt",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            CatalogSnapshot(suite=suite, catalog_sha256=digest, prompt_aliases=prompt_aliases)
    with pytest.raises(ValidationError, match="digest must match the exact suite"):
        CatalogSnapshot(suite=suite, catalog_sha256="0" * 64, prompt_aliases=())


def _aliased(current: PromptSuite, recorded: Prompt) -> CatalogSnapshot:
    """A snapshot of ``current`` whose first prompt accepts ``recorded`` as an alias."""
    return CatalogSnapshot(
        suite=current,
        catalog_sha256=canonical_sha256(current),
        prompt_aliases=(
            PromptAlias(
                prompt_sha256=canonical_sha256(current.prompts[0]),
                accepted_prompt_sha256=canonical_sha256(recorded),
            ),
        ),
    )


def test_an_alias_counts_only_a_recording_that_keeps_the_prompts_identity() -> None:
    spoken = Prompt(
        prompt_id="prompt-01",
        text="A corrected sentence.",
        category="neutral",
        style="plain",
        expected_seconds=2.0,
    )
    suite = PromptSuite(suite_id="training", purpose="capture-pilot", prompts=(spoken,))
    earlier = spoken.model_copy(update={"text": "An earlier sentence."})
    restyled = earlier.model_copy(update={"category": "technical", "style": "careful"})
    evaluation = earlier.model_copy(update={"holdout": True})
    silent = earlier.model_copy(update={"category": "room-tone"})
    renamed = earlier.model_copy(update={"prompt_id": "prompt-02"})
    assert keep_recordings_identity(earlier) == keep_recordings_identity(spoken)
    assert keep_recordings_identity(restyled) == keep_recordings_identity(spoken)
    for other in (evaluation, silent, renamed):
        assert keep_recordings_identity(other) != keep_recordings_identity(spoken)
    assert _aliased(suite, earlier).current_prompt_for(earlier) == spoken
    assert _aliased(suite, restyled).current_prompt_for(restyled) == spoken
    for other in (evaluation, silent, renamed):
        snapshot = _aliased(suite, other)
        assert snapshot.current_for(canonical_sha256(other)) == canonical_sha256(spoken)
        assert snapshot.current_prompt_for(other) is None
        with pytest.raises(RecordingPromptNotCountedError, match="no longer counts") as caught:
            snapshot.require_current_prompt_for(other, take_id="take-synthetic")
        assert isinstance(caught.value, ContractError)
        assert (caught.value.take_id, caught.value.prompt_id) == ("take-synthetic", other.prompt_id)
        assert "exact catalog" in str(caught.value)
    assert _aliased(suite, earlier).require_current_prompt_for(spoken, take_id="t") == spoken
    # Two silent categories never cover each other either.
    tone = Prompt(
        prompt_id="tone", category="room-tone", style="silence", expected_seconds=1.0, text="x"
    )
    procedure = tone.model_copy(update={"category": "procedure"})
    assert keep_recordings_identity(tone) != keep_recordings_identity(procedure)
    restored = pickle.loads(pickle.dumps(RecordingPromptNotCountedError("take-a", "prompt-a")))
    assert (restored.take_id, restored.prompt_id, str(restored)) == (
        "take-a",
        "prompt-a",
        str(RecordingPromptNotCountedError("take-a", "prompt-a")),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate temporary synthetic native contract fixtures"
    )
    parser.add_argument("--fixtures", type=Path, required=True)
    generate_recording_conformance_fixtures(parser.parse_args().fixtures)
