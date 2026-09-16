"""Exercise real synthetic recorder process loss, blocked telemetry and ownership.

Requires a private fixture manifest created by verify_native_bridge.py. Never
uses an audio device; every spawned helper retains the explicit synthetic flag.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import select
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.errors import ContractError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    CaptureReservation,
    NativeRecoveryReceipt,
    RecorderRequest,
    RecordingResponse,
    recording_frame_bytes,
)
from cole_voice_lab.recording_ownership import (
    acquire_capture_ownership,
    capture_ownership,
)

PARENT_LOSS_DEADLINE_SECONDS = 2.0


class HelperProbe:
    """One actual helper process with intentionally unread telemetry."""

    def __init__(self, manifest: dict[str, Any], label: str, *, frames: int = 4800) -> None:
        self.manifest = manifest
        self.generation = f"synthetic-{label}"
        self.request_count = 0
        self.buffer = bytearray()
        self.asset_root = Path(manifest["asset_root"])
        original = manifest["reservation"]
        take_id = f"synthetic-{label}"
        self.reservation = CaptureReservation.model_validate_json(
            json.dumps(
                original
                | {
                    "take_id": take_id,
                    "operation_id": f"reserve-{label}",
                    "process_generation": self.generation,
                    "partial_path": f"tmp/recording-takes/{take_id}/capture.partial.wav",
                    "original_path": f"tmp/recording-takes/{take_id}/original.wav",
                }
            )
        )
        self.directory = self.asset_root / "tmp" / "recording-takes" / take_id
        self.directory.mkdir(mode=0o700)
        self.pcm = bytes([0, 0, 0, 255, 255, 127, 0, 0, 128]) * (frames // 3)
        pcm_path = Path(manifest["synthetic_pcm"]).parent / f"{label}.pcm"
        pcm_path.write_bytes(self.pcm)
        arguments = list(manifest["helper_arguments"])
        arguments[arguments.index("--process-generation") + 1] = self.generation
        arguments[arguments.index("--synthetic-pcm") + 1] = str(pcm_path)
        self.process = subprocess.Popen(
            [manifest["helper_executable"], *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        """Terminate only this probe's synthetic child if its watchdog failed."""
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=4)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def send(self, data: dict[str, Any], take_id: str | None = None) -> RecordingResponse:
        self.request_count += 1
        request_id = f"probe-{self.request_count}"
        message = RecorderRequest.model_validate_json(
            json.dumps(
                {
                    "data": data,
                    "error": None,
                    "meta": {
                        "protocol_version": "cole-voice-lab.recording/v1",
                        "request_id": request_id,
                        "process_generation": self.generation,
                        "state_revision": 0,
                        "event_sequence": 0,
                        "take_id": take_id,
                    },
                }
            )
        )
        assert self.process.stdin is not None
        framed = recording_frame_bytes(message)
        if self.generation == "synthetic-split-frame":
            for chunk in (framed[:1], framed[1:3], framed[3:]):
                self.process.stdin.write(chunk)
                self.process.stdin.flush()
        else:
            self.process.stdin.write(framed)
        self.process.stdin.flush()
        deadline = time.monotonic() + 5
        while True:
            response = self.receive(deadline)
            if response.meta.request_id == request_id:
                return response

    def receive(self, deadline: float) -> RecordingResponse:
        assert self.process.stdout is not None
        while True:
            if len(self.buffer) >= 4:
                count = int.from_bytes(self.buffer[:4], "big")
                if not 1 <= count <= 1_048_576:
                    raise ValueError("Native response frame exceeded its bound")
                if len(self.buffer) >= count + 4:
                    payload = bytes(self.buffer[4 : count + 4])
                    del self.buffer[: count + 4]
                    return RecordingResponse.model_validate_json(payload)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("Native helper did not answer before its deadline")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise EOFError(f"Native helper exited before response: {self.process.poll()}")
            self.buffer.extend(chunk)

    def handshake(self) -> RecordingResponse:
        return self.send(
            {
                "command": "handshake",
                "role": "ui",
                "build": self.manifest["client_build"],
                "expected_peer_build": self.manifest["helper_build"],
            }
        )

    def start(self) -> RecordingResponse:
        return self.send(
            {
                "command": "start",
                "reservation": self.reservation.model_dump(mode="json"),
                "reservation_sha256": canonical_sha256(self.reservation),
            },
            self.reservation.take_id,
        )

    def wait_for_frames(self, frame_count: int) -> RecordingResponse:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.send({"command": "snapshot"})
            data = response.data
            if data is not None and getattr(data, "written_frames", 0) >= frame_count:
                return response
        raise TimeoutError("Synthetic writer did not reach its exact frame barrier")

    def close(self) -> float:
        assert self.process.stdin is not None
        started = time.monotonic()
        self.process.stdin.close()
        self.process.wait(timeout=4)
        return time.monotonic() - started


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    options = parser.parse_args()
    os.umask(0o077)
    manifest = json.loads(options.manifest.read_text())
    parent = HelperProbe(manifest, "parent-loss")
    assert parent.handshake().error is None
    assert parent.start().error is None
    parent.wait_for_frames(480)
    parent_loss_seconds = parent.close()
    recovery = NativeRecoveryReceipt.model_validate_json(
        (parent.directory / "native-recovery.json").read_bytes()
    )
    assert recovery.state == "partial" and recovery.error is not None
    assert recovery.error.code == "RECORDING.PARENT_LOST" and recovery.written_frames > 0
    assert (parent.directory / "capture.partial.wav").exists()

    blocked = HelperProbe(manifest, "blocked-telemetry", frames=144_000)
    assert blocked.handshake().error is None
    assert blocked.start().error is None
    blocked.wait_for_frames(480)
    layout = AssetLayout.validate(blocked.asset_root)
    try:
        descriptor = acquire_capture_ownership(layout)
    except ContractError:
        pass
    else:
        os.close(descriptor)
        raise AssertionError("Python capture owner bypassed the native helper lock")
    competing = HelperProbe(manifest, "competing-owner")
    assert competing.handshake().error is None
    conflict = competing.start()
    assert conflict.error is not None and conflict.error.code == "RECORDING.OWNERSHIP_CONFLICT"
    competing.close()
    blocked.wait_for_frames(144_000)
    stop = blocked.send(
        {
            "command": "stop",
            "operation_id": "stop-blocked",
            "take_id": blocked.reservation.take_id,
        },
        blocked.reservation.take_id,
    )
    assert stop.error is None
    deadline = time.monotonic() + 5
    while True:
        state = blocked.send({"command": "snapshot"})
        if state.data is not None and getattr(state.data, "transport", None) == "draft":
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Blocked telemetry delayed native sealing")
    assert (blocked.directory / "original.wav").read_bytes()[44:] == blocked.pcm
    blocked.close()

    python_locked = HelperProbe(manifest, "python-owner")
    assert python_locked.handshake().error is None
    with capture_ownership(layout):
        conflict = python_locked.start()
        assert conflict.error is not None and conflict.error.code == "RECORDING.OWNERSHIP_CONFLICT"
        assert not (python_locked.directory / "capture.partial.wav").exists()
    assert python_locked.start().error is None
    python_locked.wait_for_frames(480)
    python_locked.close()

    wrong_build = HelperProbe(manifest, "wrong-build")
    changed = manifest["client_build"] | {"source_sha256": "f" * 64}
    mismatch = wrong_build.send(
        {
            "command": "handshake",
            "role": "ui",
            "build": changed,
            "expected_peer_build": manifest["helper_build"],
        }
    )
    assert mismatch.error is not None and mismatch.error.code == "RECORDING.BUILD_MISMATCH"
    assert wrong_build.start().error is not None
    wrong_build.close()
    assert not (wrong_build.directory / "capture.partial.wav").exists()

    stale = HelperProbe(manifest, "stale-generation")
    assert stale.handshake().error is None
    stale.generation = "old-generation"
    mismatch = stale.send({"command": "snapshot"})
    assert mismatch.error is not None and mismatch.error.code == "RECORDING.STALE_GENERATION"
    stale.close()

    split = HelperProbe(manifest, "split-frame")
    assert split.handshake().error is None
    split.close()

    version = HelperProbe(manifest, "wrong-version")
    assert version.process.stdin is not None
    invalid = {
        "data": {"command": "snapshot"},
        "error": None,
        "meta": {
            "protocol_version": "cole-voice-lab.recording/v0",
            "request_id": "version",
            "process_generation": version.generation,
            "state_revision": 0,
            "event_sequence": 0,
            "take_id": None,
        },
    }
    payload = json.dumps(invalid).encode()
    version.process.stdin.write(struct.pack("!I", len(payload)) + payload)
    version.process.stdin.flush()
    assert version.process.wait(timeout=4) == 65
    assert not (version.directory / "capture.partial.wav").exists()

    oversized = HelperProbe(manifest, "oversized-frame")
    assert oversized.process.stdin is not None
    oversized.process.stdin.write(struct.pack("!I", 1_048_577))
    oversized.process.stdin.flush()
    assert oversized.process.wait(timeout=4) == 65
    oversized.process.stdin.close()
    assert not (oversized.directory / "capture.partial.wav").exists()

    truncated = HelperProbe(manifest, "truncated-frame")
    assert truncated.process.stdin is not None
    truncated.process.stdin.write(b"\x00\x00\x01")
    truncated.process.stdin.flush()
    truncated.close()
    assert truncated.process.returncode == 65
    assert not (truncated.directory / "capture.partial.wav").exists()
    # The design requires the helper to exit within its watchdog deadline, so bound the
    # measurement rather than only recording it.
    assert parent_loss_seconds <= PARENT_LOSS_DEADLINE_SECONDS, (
        f"helper took {parent_loss_seconds:.3f}s to exit after losing its owner; "
        f"the watchdog deadline is {PARENT_LOSS_DEADLINE_SECONDS}s"
    )
    result = {
        "parent_loss_exit_seconds": parent_loss_seconds,
        "parent_loss_deadline_seconds": PARENT_LOSS_DEADLINE_SECONDS,
        "parent_loss_partial_preserved": True,
        "unread_telemetry_exact_pcm_frames": 144_000,
        "competing_native_ownership_rejected": True,
        "python_native_shared_lock_both_directions": True,
        "wrong_build_rejected_before_capture": True,
        "stale_generation_rejected": True,
        "split_frames_accepted": True,
        "version_mismatch_rejected": True,
        "oversized_frame_rejected": True,
        "truncated_frame_rejected": True,
        "microphone_opened": False,
    }
    write_canonical_json(options.manifest.parent / "bridge-failures.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
