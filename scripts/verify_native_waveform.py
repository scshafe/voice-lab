"""Exercise actual helper waveform telemetry using a 9-byte repeating PCM fixture.

Writes only private synthetic evidence outside the repository. No audio device,
visible app, playback, or transfer is used. The helper is built by the caller.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from test_recording_contracts import synthetic_contracts

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    RECORDING_SCHEMA_MODELS,
    CaptureReservation,
    CaptureSealReceipt,
    RecorderRequest,
    RecordingBuildIdentity,
    RecordingResponse,
    RecordingTelemetry,
    recording_frame_bytes,
)
from cole_voice_lab.recording_service import RecordingFrameDecoder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--binary-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--archive-failure",
        action="store_true",
        help="Inject private derived-cache directory write denial",
    )
    options = parser.parse_args()
    os.umask(0o077)
    root = options.output.resolve()
    if root.is_relative_to(options.package.resolve()) or root.is_relative_to(Path.cwd().resolve()):
        raise ValueError("Waveform process fixtures must remain outside source checkout")
    root.mkdir(mode=0o700, parents=True)
    assets = AssetLayout.initialize(root / "assets")
    denied_cache = assets.root / "derived/recording-waveforms"
    if options.archive_failure:
        denied_cache.mkdir(mode=0o700)
        denied_cache.chmod(0o500)
    schemas = root / "schemas"
    schemas.mkdir(mode=0o700)
    for name, model in RECORDING_SCHEMA_MODELS.items():
        write_canonical_json(schemas / f"{name}.schema.json", model.model_json_schema())
    reservation_data = synthetic_contracts()["capture-reservation"].model_dump(mode="json")
    # A legal maximum-length ID whose truncated prefix is not a legal StableId.
    # Background event request IDs must be independently valid and bounded.
    take_id = "a" * 95 + "-" + "a" * 32
    reservation_data.update(
        take_id=take_id,
        partial_path=f"tmp/recording-takes/{take_id}/capture.partial.wav",
        original_path=f"tmp/recording-takes/{take_id}/original.wav",
    )
    reservation_data["requested_at"] = "2026-09-11T12:00:00.123456Z"
    reservation = CaptureReservation.model_validate_json(json.dumps(reservation_data))
    (assets.root / "tmp/recording-takes" / reservation.take_id).mkdir(mode=0o700, parents=True)
    helper = (options.binary_directory / "ColeVoiceRecorderHelper").resolve()
    source = [
        (str(path.relative_to(options.package)), sha256_file(path)[0])
        for path in sorted((options.package / "Sources").rglob("*"))
        if path.is_file()
    ]
    build = RecordingBuildIdentity(
        build_id="synthetic-waveform-helper",
        source_sha256=canonical_sha256(source),
        executable_sha256=sha256_file(helper)[0],
        environment_sha256=canonical_sha256({"synthetic": True}),
    )
    client_build = RecordingBuildIdentity(
        build_id="synthetic-waveform-driver",
        source_sha256=sha256_file(Path(__file__))[0],
        executable_sha256=sha256_file(Path(sys.executable).resolve())[0],
        environment_sha256=canonical_sha256({"python": sys.version}),
    )
    write_canonical_json(root / "client-build.json", client_build)
    write_canonical_json(root / "build.json", build)
    pattern = bytes([0, 0, 0, 255, 255, 127, 0, 0, 128])
    (root / "pattern.pcm").write_bytes(pattern)
    total_frames = 4801
    process = subprocess.Popen(
        [
            str(helper),
            "--asset-root",
            str(assets.root),
            "--process-generation",
            reservation.process_generation,
            "--build-identity",
            str(root / "build.json"),
            "--expected-client-build",
            str(root / "client-build.json"),
            "--schema-directory",
            str(schemas),
            "--synthetic-pcm",
            str(root / "pattern.pcm"),
            "--synthetic-chunk-frames",
            "480",
            "--synthetic-total-frames",
            str(total_frames),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    decoder = {
        process.stdout: RecordingFrameDecoder(),
        process.stderr: RecordingFrameDecoder(telemetry=True),
    }
    events: list[RecordingResponse | RecordingTelemetry] = []
    request_count = 0

    def send(command: dict[str, Any], take_id: str | None = None) -> str:
        nonlocal request_count
        request_count += 1
        request_id = f"waveform-request-{request_count}"
        request = RecorderRequest.model_validate_json(
            json.dumps(
                {
                    "data": command,
                    "error": None,
                    "meta": {
                        "protocol_version": "cole-voice-lab.recording/v1",
                        "request_id": request_id,
                        "process_generation": reservation.process_generation,
                        "state_revision": 0,
                        "event_sequence": 0,
                        "take_id": take_id,
                    },
                }
            )
        )
        process.stdin.write(recording_frame_bytes(request))
        process.stdin.flush()
        return request_id

    def wait(
        predicate: Callable[[RecordingResponse | RecordingTelemetry], bool],
    ) -> RecordingResponse | RecordingTelemetry:
        deadline = time.monotonic() + 5
        while True:
            for event in events:
                if predicate(event):
                    events.remove(event)
                    return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Actual waveform process did not reach its exact frame barrier")
            readable, _, _ = select.select(list(decoder), [], [], remaining)
            for stream in readable:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    raise EOFError("Waveform helper exited before its expected event")
                for payload in decoder[stream].feed(chunk):
                    event = (
                        RecordingResponse.model_validate_json(payload)
                        if stream is process.stdout
                        else RecordingTelemetry.model_validate_json(payload)
                    )
                    if event.error is not None:
                        raise ValueError(f"Waveform helper error: {event.error.code}")
                    events.append(event)
            if len(events) > 128:
                raise ValueError("Synthetic waveform consumer exceeded its bounded event queue")

    try:
        handshake = send(
            {
                "command": "handshake",
                "role": "ui",
                "build": client_build.model_dump(mode="json"),
                "expected_peer_build": build.model_dump(mode="json"),
            }
        )
        wait(lambda event: event.meta.request_id == handshake)
        send(
            {
                "command": "start",
                "reservation": reservation.model_dump(mode="json"),
                "reservation_sha256": canonical_sha256(reservation),
            },
            reservation.take_id,
        )
        live = wait(
            lambda event: (
                isinstance(event, RecordingTelemetry) and event.data.written_frames == total_frames
            )
        )
        assert isinstance(live, RecordingTelemetry) and live.data.sample_clock is not None
        send(
            {"command": "stop", "operation_id": "synthetic-stop", "take_id": reservation.take_id},
            reservation.take_id,
        )
        stopped = wait(
            lambda event: (
                isinstance(event, RecordingResponse)
                and getattr(event.data, "transport", None) == "draft"
            )
        )
        seal = CaptureSealReceipt.model_validate(stopped.data.seal)
        final = wait(
            lambda event: (
                isinstance(event, RecordingTelemetry)
                and event.data.first_frame == 0
                and event.data.end_frame == total_frames
            )
        )
        assert isinstance(final, RecordingTelemetry)
        assert final.data.written_frames == seal.audio.frames == total_frames
        assert (
            final.data.buckets[0].minimum == -1.0
            and final.data.buckets[0].maximum == 8_388_607 / 8_388_608
        )
        assert final.data.buckets[-1].minimum == final.data.buckets[-1].maximum == 0
        cached = wait(
            lambda event: (
                isinstance(event, RecordingResponse)
                and getattr(event.data, "event", None) == "waveform-cache"
            )
        )
        assert cached.data.source_sha256 == seal.original_sha256
        archive = assets.root / "derived/recording-waveforms" / f"{seal.original_sha256}-v2.cvlwave"
        if options.archive_failure:
            assert cached.data.status == "needs-rebuild" and cached.data.error is not None
            assert (
                cached.data.error.code == "RECORDING.WAVEFORM_CACHE_UNAVAILABLE"
                and cached.data.error.retryable
            )
            assert not archive.exists()
        else:
            assert cached.data.status == "stored"
            stored = archive.read_bytes()
            # Version 2 binds the complete header and extrema payload to a trailing digest.
            assert stored[:8] == b"CVLWAV02" and len(stored) <= 3_600_084
            assert stored[-32:] == hashlib.sha256(stored[:-32]).digest()
        original = assets.root / seal.original_path
        expected = (pattern * ((total_frames + 2) // 3))[: total_frames * 3]
        assert original.read_bytes()[44:] == expected
        assert sha256_file(original) == (seal.original_sha256, seal.original_size_bytes)
        write_canonical_json(root / "final-telemetry.json", final)
        write_canonical_json(root / "seal.json", seal)
        conformance = root / "conformance"
        conformance.mkdir(mode=0o700)
        write_canonical_json(
            conformance / "recording-telemetry.schema.json", RecordingTelemetry.model_json_schema()
        )
        valid = final.model_dump(mode="json")
        index = []
        cases = [("current", valid, True)]
        historical = copy.deepcopy(valid)
        historical["data"]["sample_clock"] = None
        cases.append(("historical", historical, True))
        for name, value in [
            ("anchor_frame", total_frames),
            ("host_time_ticks", -1),
            ("host_time_ticks", 2**63),
            ("host_time_ticks", 1.0),
            ("seconds_per_tick", 0),
            ("seconds_per_tick", -0.1),
            ("unknown", True),
        ]:
            invalid = copy.deepcopy(valid)
            invalid["data"]["sample_clock"][name] = value
            cases.append((f"invalid-{name}-{len(cases)}", invalid, False))
        for label, payload, expected in cases:
            filename = label + ".json"
            write_canonical_json(conformance / filename, payload)
            index.append(
                {
                    "file": filename,
                    "model": "recording-telemetry",
                    "valid": expected,
                    "validation_owner": "shared",
                }
            )
        write_canonical_json(
            conformance / "recording-response.schema.json", RecordingResponse.model_json_schema()
        )
        cache_payload = cached.model_dump(mode="json")
        cache_payload["data"]["status"] = "stored"
        cache_payload["data"]["error"] = None
        cache_cases = [("stored", cache_payload, True)]
        retryable = copy.deepcopy(cache_payload)
        retryable["data"]["status"] = "needs-rebuild"
        retryable["data"]["error"] = {
            "code": "RECORDING.WAVEFORM_CACHE_UNAVAILABLE",
            "message": "Synthetic rebuild",
            "details": [],
            "retryable": True,
        }
        cache_cases.append(("needs-rebuild", retryable, True))
        for label in [
            "bad-source",
            "wrong-take",
            "missing-error",
            "stored-error",
            "wrong-error-code",
            "nonretryable",
        ]:
            invalid = copy.deepcopy(cache_payload)
            if label == "bad-source":
                invalid["data"]["source_sha256"] = "bad"
            elif label == "wrong-take":
                invalid["meta"]["take_id"] = "different"
            elif label == "missing-error":
                invalid["data"]["status"] = "needs-rebuild"
            elif label == "stored-error":
                invalid["data"]["error"] = retryable["data"]["error"]
            else:
                invalid = copy.deepcopy(retryable)
                if label == "wrong-error-code":
                    invalid["data"]["error"]["code"] = "RECORDING.WRITE_FAILED"
                else:
                    invalid["data"]["error"]["retryable"] = False
            cache_cases.append((label, invalid, False))
        for label, payload, expected in cache_cases:
            filename = "cache-" + label + ".json"
            write_canonical_json(conformance / filename, payload)
            index.append(
                {
                    "file": filename,
                    "model": "recording-response",
                    "valid": expected,
                    "validation_owner": "shared",
                }
            )
        write_canonical_json(conformance / "index.json", index)
        subprocess.run(
            [str(options.binary_directory / "RecordingHarness"), "contracts", str(conformance)],
            check=True,
            timeout=20,
        )
        outgoing = conformance / "swift"
        for case in json.loads((outgoing / "index.json").read_text()):
            try:
                RECORDING_SCHEMA_MODELS[case["model"]].model_validate_json(
                    (outgoing / case["file"]).read_bytes()
                )
                accepted = True
            except ValidationError:
                accepted = False
            if accepted != case["valid"]:
                raise ValueError("Python disagrees with Swift sample-clock conformance")
        result = {
            "real_helper_process": True,
            "synthetic_fixture_bytes": len(pattern),
            "exact_repeated_frames": total_frames,
            "final_written_count_agreement": True,
            "partial_bucket_preserved": True,
            "actual_sample_clock_present": True,
            "background_archive_stored": not options.archive_failure,
            "background_archive_rebuild_reported": options.archive_failure,
            "microphone_opened": False,
        }
        write_canonical_json(root / "waveform-process-result.json", result)
        print(json.dumps(result))
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)
        process.stdout.close()
        process.stderr.close()
        if options.archive_failure:
            denied_cache.chmod(0o700)


if __name__ == "__main__":
    main()
