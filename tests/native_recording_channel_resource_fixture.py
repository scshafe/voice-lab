"""Synthetic framed peer for native pipe resource tests; no assets or audio devices."""

import json
import struct
import sys
from typing import Any, BinaryIO


def read_request() -> dict[str, Any] | None:
    """Read one parent command, or return None when its control pipe closes."""
    prefix = sys.stdin.buffer.read(4)
    if not prefix:
        return None
    size = struct.unpack(">I", prefix)[0]
    value: dict[str, Any] = json.loads(sys.stdin.buffer.read(size))
    return value


def write_message(stream: BinaryIO, value: dict[str, Any]) -> None:
    """Write a bounded framed synthetic event."""
    payload = json.dumps(value, separators=(",", ":")).encode()
    stream.write(struct.pack(">I", len(payload)) + payload)
    stream.flush()


def main() -> None:
    """Remain alive between bursts so reader work items cannot drain at EOF."""
    mode = sys.argv[1]
    sequence = 0
    telemetry_sequence = 0
    while (request := read_request()) is not None:
        sequence += 1
        metadata = {**request["meta"], "event_sequence": sequence}
        command = request["data"]
        if command["command"] == "handshake":
            data = {
                "event": "handshake",
                "role": "recorder",
                "build": command["expected_peer_build"],
            }
        else:
            if mode == "telemetry":
                count = 128 if metadata["request_id"] == "warmup" else 4096
                for _ in range(count):
                    telemetry_sequence += 1
                    write_message(
                        sys.stderr.buffer,
                        {
                            "data": {
                                "event": "waveform",
                                "take_id": "synthetic",
                                "process_generation": metadata["process_generation"],
                                "first_frame": 0,
                                "end_frame": 64,
                                "written_frames": 64,
                                "bucket_width_frames": 64,
                                "buckets": [{"minimum": 0.0, "maximum": 0.0}],
                                "sample_clock": None,
                            },
                            "error": None,
                            "meta": {
                                **metadata,
                                "event_sequence": telemetry_sequence,
                                "take_id": "synthetic",
                            },
                        },
                    )
            data = {
                "event": "acknowledged",
                "action": "recovered",
                "operation_id": metadata["request_id"],
            }
        write_message(sys.stdout.buffer, {"data": data, "error": None, "meta": metadata})


if __name__ == "__main__":
    main()
