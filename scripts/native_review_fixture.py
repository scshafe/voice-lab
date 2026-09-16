"""Bounded synthetic legacy capture adapter for the real native review process gate."""

from __future__ import annotations

import shutil
import wave
from collections.abc import Callable, Sequence
from pathlib import Path

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.intake import accept_capture_session
from cole_voice_lab.models import CaptureDevice, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_library import RecordingLibrary


class _SyntheticTimer:
    def __init__(self, seconds: float, callback: Callable[[], None]) -> None:
        pass

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        pass


class _SyntheticCapture:
    def __init__(self, command: Sequence[str]) -> None:
        self.path = Path(command[-1])
        self.return_code: int | None = None
        self.captured_frames: int | None = None
        self.path.touch(mode=0o600)

    def signal_quit(self) -> None:
        sample = 100 if "room-tone" in self.path.name else 2_000_000
        with wave.open(str(self.path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(3)
            writer.setframerate(48_000)
            writer.writeframes(
                b"".join(n.to_bytes(3, "little", signed=True) for n in [sample, -sample] * 480)
            )
        self.captured_frames = 960

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        self.return_code = 0
        return 0

    def send_signal(self, signal_number: int) -> None:
        self.return_code = 0

    def terminate(self) -> None:
        self.return_code = 0

    def kill(self) -> None:
        self.return_code = 0


def create_legacy(layout: AssetLayout, catalog: PromptSuite, *, accepted: bool) -> None:
    library = RecordingLibrary(
        layout,
        catalog,
        subject_id="synthetic-subject",
        process_factory=_SyntheticCapture,
        timer_factory=_SyntheticTimer,
        device_resolver=lambda device: device,
    )
    session = library.begin_session(
        ["room-tone", "reading-one"],
        CaptureDevice(device_id="synthetic-legacy", display_name="Synthetic legacy UMIK-1"),
    )
    for _ in range(2):
        session.start_take()
        session.stop_take()
        session.accept_take()
    result = session.finalize()
    if accepted:
        incoming = layout.directory("incoming") / session.evidence.session_id
        shutil.copytree(result.session_directory, incoming)
        accept_capture_session(layout, incoming)
    write_canonical_json(
        layout.root.parent / "legacy-fixture.json",
        {
            "original_kind": "accepted-raw" if accepted else "legacy-staged",
            "source_frames": 960,
        },
    )
