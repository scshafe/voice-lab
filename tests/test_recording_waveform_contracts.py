"""Strict same-boot waveform clock contract conformance; synthetic instances only."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError
from test_recording_contracts import synthetic_contracts

from cole_voice_lab.recording_contracts import RecordingResponse, RecordingTelemetry


def fixture() -> dict[str, object]:
    value = synthetic_contracts()["recording-telemetry"].model_dump(mode="json")
    value["data"]["sample_clock"] = {
        "anchor_frame": 0,
        "host_time_ticks": 1000000,
        "seconds_per_tick": 0.000001,
    }
    return value


def test_valid_sample_clock_and_explicit_historical_null() -> None:
    value = fixture()
    assert RecordingTelemetry.model_validate_json(json.dumps(value)).data.sample_clock is not None
    value["data"]["sample_clock"] = None
    assert RecordingTelemetry.model_validate_json(json.dumps(value)).data.sample_clock is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("anchor_frame", 480),
        ("anchor_frame", -1),
        ("host_time_ticks", -1),
        ("host_time_ticks", 2**63),
        ("host_time_ticks", 1.0),
        ("seconds_per_tick", 0),
        ("seconds_per_tick", -0.1),
        ("seconds_per_tick", float("inf")),
        ("seconds_per_tick", float("nan")),
        ("unknown", True),
    ],
)
def test_invalid_clock_shape_bounds_and_cross_field_binding(key: str, value: object) -> None:
    payload = copy.deepcopy(fixture())
    payload["data"]["sample_clock"][key] = value
    with pytest.raises(ValidationError):
        RecordingTelemetry.model_validate_json(json.dumps(payload))


def test_sample_clock_required_even_when_unknown() -> None:
    payload = fixture()
    del payload["data"]["sample_clock"]
    with pytest.raises(ValidationError):
        RecordingTelemetry.model_validate_json(json.dumps(payload))


def cache_event() -> dict[str, object]:
    value = synthetic_contracts()["recording-response"].model_dump(mode="json")
    value["data"] = {
        "event": "waveform-cache",
        "take_id": value["meta"]["take_id"],
        "source_sha256": "a" * 64,
        "status": "stored",
        "error": None,
    }
    return value


def test_waveform_cache_stored_and_retryable_rebuild_are_separate_from_capture() -> None:
    payload = cache_event()
    assert RecordingResponse.model_validate_json(json.dumps(payload)).data.status == "stored"
    payload["data"]["status"] = "needs-rebuild"
    payload["data"]["error"] = {
        "code": "RECORDING.WAVEFORM_CACHE_UNAVAILABLE",
        "message": "Cache needs rebuild",
        "details": [],
        "retryable": True,
    }
    assert RecordingResponse.model_validate_json(json.dumps(payload)).data.status == "needs-rebuild"


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "wrong-take",
        "bad-source",
        "missing-error",
        "stored-error",
        "nonretryable",
        "wrong-error-code",
    ],
)
def test_waveform_cache_status_rejects_invalid_shape_and_identity(mutation: str) -> None:
    payload = cache_event()
    error = {
        "code": "RECORDING.WAVEFORM_CACHE_UNAVAILABLE",
        "message": "Cache needs rebuild",
        "details": [],
        "retryable": True,
    }
    if mutation == "unknown":
        payload["data"]["unknown"] = True
    elif mutation == "wrong-take":
        payload["meta"]["take_id"] = "another-take"
    elif mutation == "bad-source":
        payload["data"]["source_sha256"] = "bad"
    elif mutation == "missing-error":
        payload["data"]["status"] = "needs-rebuild"
    elif mutation == "stored-error":
        payload["data"]["error"] = error
    else:
        payload["data"]["status"] = "needs-rebuild"
        payload["data"]["error"] = error
        if mutation == "nonretryable":
            error["retryable"] = False
        else:
            error["code"] = "RECORDING.WRITE_FAILED"
    with pytest.raises(ValidationError):
        RecordingResponse.model_validate_json(json.dumps(payload))
