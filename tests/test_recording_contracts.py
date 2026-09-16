from __future__ import annotations

import argparse
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from recording_fixtures import require_external_private_root

from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    write_canonical_json,
)
from cole_voice_lab.capture import CaptureContinuityEvidence
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
    MAX_CONTROL_FRAME_BYTES,
    MAX_RECORDING_FRAMES,
    MAX_TELEMETRY_FRAME_BYTES,
    RECORDING_SCHEMA_MODELS,
    CaptureReservation,
    CaptureSealReceipt,
    ContextEvent,
    NativeRecoveryReceipt,
    NavigationEvent,
    OpenContextCommand,
    PackagePreparationEvent,
    PreparedCaptureGate,
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
    WaveformBucket,
    WaveformSnapshot,
    native_recovery_bytes,
    recording_frame_bytes,
)


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
        "recording-package-preparation-event": PackagePreparationEvent(
            operation_id="prepare-synthetic", group_id=group.group_id, state="queued"
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
            "prepare-package",
            {
                "operation_id": "op-package",
                "group_id": reservation.group.group_id,
                "take_ids": (reservation.take_id,),
                "room_tone_take_ids": (reservation.take_id,),
            },
            None,
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
            {"operation_id": "op-cancel", "cancelled_operation_id": "op-package"},
            None,
        ),
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
        + package_event_cases()
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
        + recovery_command_cases()
        + prompt_suite_v2_cases()
    ):
        filename = f"{'valid' if valid else 'invalid'}-{case}.json"
        write_canonical_json(directory / filename, payload)
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


def package_event_cases() -> list[tuple[str, bool, str, dict[str, Any]]]:
    values = synthetic_contracts()
    event = values["recording-package-preparation-event"].model_dump(mode="json")
    base = {
        "data": event,
        "error": None,
        "meta": values["recording-response"].meta.model_dump(mode="json") | {"take_id": None},
    }
    error = values["native-recovery"].error.model_dump(mode="json")
    cases = []
    for state in ("queued", "running", "blocked", "failed"):
        payload = copy.deepcopy(base)
        payload["data"]["state"] = state
        payload["data"]["error"] = error if state in {"blocked", "failed"} else None
        cases.append((f"package-preparation-{state}", True, "shared", payload))
        broken = copy.deepcopy(payload)
        broken["data"]["error"] = None if payload["data"]["error"] is not None else error
        cases.append((f"package-preparation-{state}-error", False, "shared", broken))
    package = values["recording-package-map"].model_dump(mode="json")
    package["selected_suite"] = {
        "suite_id": "synthetic-export-subset",
        "purpose": package["group"]["catalog"]["purpose"],
        "prompts": [entry["prompt"] for entry in package["entries"]],
    }
    selected = copy.deepcopy(base)
    selected["data"] = {"event": "package", "package": package}
    cases.append(("package-selected-suite", True, "shared", selected))
    broken = copy.deepcopy(selected)
    broken["data"]["package"]["selected_suite"]["purpose"] = "benchmark"
    cases.append(("package-selected-purpose", False, "shared", broken))
    broken = copy.deepcopy(selected)
    broken["data"]["package"]["selected_suite"]["prompts"][0]["text"] = "Changed exact prompt"
    cases.append(("package-selected-content", False, "shared", broken))
    return cases


@pytest.mark.parametrize("case", package_event_cases(), ids=lambda case: case[0])
def test_package_events_bind_prerequisites_and_selected_content(case: Any) -> None:
    _, valid, _, payload = case
    if valid:
        assert RecordingResponse.model_validate_json(json.dumps(payload)).data.event in {
            "package",
            "package-preparation",
        }
    else:
        with pytest.raises(ValidationError):
            RecordingResponse.model_validate_json(json.dumps(payload))


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate temporary synthetic native contract fixtures"
    )
    parser.add_argument("--fixtures", type=Path, required=True)
    generate_recording_conformance_fixtures(parser.parse_args().fixtures)
