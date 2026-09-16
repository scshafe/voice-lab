"""Verify finished-helper idleness, final visual delivery and fresh-take telemetry.

Uses only the private synthetic bridge fixture and its exact built helper. The
first capture deliberately leaves stderr unread through sealing, so a final
snapshot must survive a partial earlier frame and drain without another offer.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import select
import subprocess
import time
from pathlib import Path

from verify_native_bridge_failures import HelperProbe

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.recording_contracts import CaptureReservation, RecordingTelemetry


def cpu_seconds(pid: int) -> float:
    """Read cumulative user/system CPU of the owned live helper, in seconds."""
    value = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "time="], text=True)
    fields = value.strip().split(":")
    return sum(float(part) * 60**index for index, part in enumerate(reversed(fields)))


class TelemetryReader:
    """Bounded wire reader that preserves incomplete frames across drain windows."""

    def __init__(self, probe: HelperProbe) -> None:
        assert probe.process.stderr is not None
        self.stream = probe.process.stderr
        self.buffer = bytearray()

    def drain(self, seconds: float) -> list[RecordingTelemetry]:
        deadline = time.monotonic() + seconds
        records = []
        while True:
            while len(self.buffer) >= 4:
                size = int.from_bytes(self.buffer[:4], "big")
                assert 1 <= size <= 262_144, "Telemetry frame exceeded its wire bound"
                if len(self.buffer) < size + 4:
                    break
                records.append(
                    RecordingTelemetry.model_validate_json(bytes(self.buffer[4 : size + 4]))
                )
                del self.buffer[: size + 4]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.stream], [], [], remaining)[0]:
                return records
            chunk = os.read(self.stream.fileno(), 65_536)
            assert chunk, "Owned helper closed telemetry before the observation finished"
            self.buffer.extend(chunk)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    options = parser.parse_args()
    os.umask(0o077)
    manifest = json.loads(options.manifest.read_text())
    probe = HelperProbe(manifest, "finished-telemetry", frames=216_000)
    reader = TelemetryReader(probe)
    results = []
    try:
        assert probe.handshake().error is None
        for cycle in range(2):
            if cycle:
                take_id = "synthetic-finished-telemetry-second"
                probe.reservation = CaptureReservation.model_validate_json(
                    json.dumps(
                        probe.reservation.model_dump(mode="json")
                        | {
                            "take_id": take_id,
                            "operation_id": f"reserve-{take_id}",
                            "partial_path": f"tmp/recording-takes/{take_id}/capture.partial.wav",
                            "original_path": f"tmp/recording-takes/{take_id}/original.wav",
                        }
                    )
                )
                probe.directory = probe.asset_root / "tmp" / "recording-takes" / take_id
                probe.directory.mkdir(mode=0o700)
            assert probe.start().error is None
            live = []
            deadline = time.monotonic() + 12
            while True:
                state = probe.send({"command": "snapshot"})
                assert state.error is None
                if state.data is not None and getattr(state.data, "written_frames", 0) >= 216_000:
                    break
                assert time.monotonic() < deadline, (
                    "Synthetic capture did not reach its frame barrier"
                )
                if cycle:
                    live.extend(reader.drain(0.02))
                else:
                    time.sleep(0.02)
            stopped = probe.send(
                {
                    "command": "stop",
                    "operation_id": f"telemetry-stop-{cycle}",
                    "take_id": probe.reservation.take_id,
                },
                probe.reservation.take_id,
            )
            assert stopped.error is None
            deadline = time.monotonic() + 5
            while True:
                state = probe.send({"command": "snapshot"})
                assert state.error is None
                if state.data is not None and getattr(state.data, "transport", None) == "draft":
                    break
                assert time.monotonic() < deadline, "Unread telemetry delayed reliable sealing"
                time.sleep(0.02)
            seal = getattr(state.data, "seal", None)
            assert seal is not None
            inspected = inspect_canonical_wav(probe.directory / "original.wav")
            assert inspected.audio == seal.audio and inspected.sha256 == seal.original_sha256
            assert (probe.directory / "original.wav").read_bytes()[44:] == probe.pcm
            # Let the completed visual offer encounter the still-blocked pipe first.
            time.sleep(0.3)
            drained = reader.drain(1)
            before_cpu = cpu_seconds(probe.process.pid)
            before_wall = time.monotonic()
            idle = reader.drain(1)
            elapsed = time.monotonic() - before_wall
            consumed = cpu_seconds(probe.process.pid) - before_cpu
            finals = [
                event
                for event in drained
                if (
                    event.data.take_id == probe.reservation.take_id
                    and event.meta.state_revision >= state.meta.state_revision
                    and event.data.first_frame == 0
                    and event.data.end_frame == 216_000
                    and event.data.written_frames == 216_000
                )
            ]
            fresh_live = [
                event
                for event in live
                if (
                    event.data.take_id == probe.reservation.take_id
                    and 0 < event.data.written_frames < 216_000
                )
            ]
            results.append(
                {
                    "take_id": probe.reservation.take_id,
                    "frames": seal.audio.frames,
                    "original_sha256": seal.original_sha256,
                    "seal_revision": state.meta.state_revision,
                    "telemetry_was_blocked_through_seal": cycle == 0,
                    "complete_final_snapshots_after_drain": len(finals),
                    "idle_telemetry_frames": len(idle),
                    "idle_partial_wire_bytes": len(reader.buffer),
                    "idle_wall_seconds": elapsed,
                    "idle_helper_cpu_seconds": consumed,
                    "idle_helper_cpu_percent": 100 * consumed / elapsed,
                    "fresh_live_snapshots": len(fresh_live),
                    "final_sequence": finals[-1].meta.event_sequence if finals else None,
                }
            )
        report = {
            "synthetic_only": True,
            "microphone_opened": False,
            "helper_pid": probe.process.pid,
            "helper_executable": manifest["helper_executable"],
            "helper_build": manifest["helper_build"],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "outcomes": results,
        }
        write_canonical_json(options.manifest.parent / "helper-telemetry.json", report)
        print(json.dumps(report), flush=True)
        assert all(row["complete_final_snapshots_after_drain"] == 1 for row in results), (
            "Every capture must publish exactly one complete final waveform after sealing"
        )
        assert all(
            row["idle_telemetry_frames"] == row["idle_partial_wire_bytes"] == 0 for row in results
        ), "Finished helper must stop producing duplicate waveform traffic"
        assert results[1]["fresh_live_snapshots"] > 0, "Next capture must resume live telemetry"
        assert probe.send({"command": "snapshot"}).error is None
    finally:
        probe.cleanup()


if __name__ == "__main__":
    main()
