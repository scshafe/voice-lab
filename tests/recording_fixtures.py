"""Deterministic private PCM fixtures and an explicitly inert capture boundary.

@canonical synthetic_recording_fixtures -- never opens a device or audio output.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.capture import expected_recording_paths
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.recording_library import (
    DesktopRecordingSession,
    DesktopTakeEvidence,
    RecordingLibrary,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE_HZ = 48_000
SAMPLE_WIDTH_BYTES = 3
FIXTURE_TIME = datetime(2026, 9, 11, tzinfo=UTC)
SYNTHETIC_DEVICE = CaptureDevice(device_id="synthetic", display_name="Synthetic UMIK-1")
Signal = Literal["sine", "silence"]


def require_external_private_root(path: Path) -> Path:
    """Require a real private directory outside Git; reject aliases and unsafe modes."""
    if path.is_symlink() or path.absolute() != path.resolve():
        raise ValueError("synthetic fixture root must be resolved without symbolic links")
    if path == REPOSITORY_ROOT or REPOSITORY_ROOT in path.parents:
        raise ValueError("synthetic recordings and reports must remain outside the repository")
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError("synthetic fixture root must be a private directory")
    return path


def write_synthetic_wav(path: Path, seconds: float, signal: Signal = "sine") -> int:
    """Write exact mono 48 kHz s24 PCM with bounded generation memory and private mode."""
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 600:
        raise ValueError("synthetic duration must be finite and within (0, 600] seconds")
    require_external_private_root(path.parent)
    frame_count = round(seconds * SAMPLE_RATE_HZ)
    if frame_count < 1:
        raise ValueError("synthetic duration must contain at least one frame")
    if signal not in {"sine", "silence"}:
        raise ValueError("unknown synthetic signal")
    period = b"".join(
        (
            0 if signal == "silence" else round(2_000_000 * math.sin(index * math.tau / 480))
        ).to_bytes(3, "little", signed=True)
        for index in range(480)
    )
    block = period * 100
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        with wave.open(stream, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(SAMPLE_WIDTH_BYTES)
            writer.setframerate(SAMPLE_RATE_HZ)
            remaining = frame_count
            while remaining:
                frames = min(remaining, SAMPLE_RATE_HZ)
                writer.writeframesraw(block[: frames * SAMPLE_WIDTH_BYTES])
                remaining -= frames
        stream.flush()
        os.fsync(stream.fileno())
    return frame_count


def synthetic_catalog(prompt_count: int) -> PromptSuite:
    """Generate exact prompt identities; content never appears in benchmark traces."""
    if prompt_count < 2:
        raise ValueError("benchmark requires a room tone and an utterance")
    return PromptSuite(
        suite_id="synthetic-recording-benchmark-v1",
        purpose="capture-pilot",
        prompts=tuple(
            Prompt(
                prompt_id=f"synthetic-{index:04d}",
                text="Synthetic benchmark instructions.",
                category="room-tone" if index == 0 else "neutral",
                style="Synthetic",
                expected_seconds=10.0,
            )
            for index in range(prompt_count)
        ),
    )


def synthetic_physical_test_catalog() -> PromptSuite:
    """A synthetic prompt-suite/v2 physical-test catalog; no built-in resource is read."""
    return PromptSuite(
        schema_version="cole-voice-lab.prompt-suite/v2",
        suite_id="cole-physical-test-synthetic",
        purpose="physical-test",
        prompts=(
            Prompt(
                prompt_id="p04-room-tone",
                instructions="Remain still and silent.",
                category="room-tone",
                style="Silence",
                labels=("test",),
                expected_seconds=10.0,
            ),
            Prompt(
                prompt_id="p10-unplug-while-capturing",
                instructions="Unplug the synthetic device while capturing.",
                category="procedure",
                style="Follow the steps; speak only if a step says so.",
                labels=("test", "device-loss"),
                expected_seconds=10.0,
            ),
        ),
    )


def synthetic_pilot_v2_catalog() -> PromptSuite:
    """pilot-v2 built in memory by the docs/PROMPT_CATALOG_V2.md §4 contract.

    Spoken prompts are pinned pilot-v1 objects verbatim; each room tone's text moves to
    instructions. No pilot-v2 resource is read, so the coverage consequence is provable
    before the resource ships.
    """
    pilot = load_builtin_prompt_suite("pilot-v1.json")
    return PromptSuite(
        schema_version="cole-voice-lab.prompt-suite/v2",
        suite_id="cole-pilot-v2",
        purpose=pilot.purpose,
        prompts=tuple(
            prompt
            if prompt.category != "room-tone"
            else Prompt.model_validate(
                prompt.model_dump(exclude={"text"}) | {"instructions": prompt.text}
            )
            for prompt in pilot.prompts
        ),
    )


class InertTimer:
    """No threads or elapsed-time synchronization; capture is driven by explicit commands."""

    def __init__(self, seconds: float, callback: Callable[[], None]) -> None:
        self.callback = callback

    def start(self) -> None:
        """Leave the synthetic maximum-duration timer inert."""

    def cancel(self) -> None:
        """Cancel is intentionally inert; no timer was scheduled."""


class InertCaptureProcess:
    """Implements the actual TakeEngine process protocol using pre-generated bytes only."""

    def __init__(self, command: Sequence[str], mark: Callable[[str], None]) -> None:
        self.path = Path(command[-1])
        self.path.touch(mode=0o600, exist_ok=False)
        self.mark = mark
        self.return_code: int | None = None
        self.captured_frames: int | None = None
        self.mark("inert_controller_created")

    def feed(self, source: Path, frames: int) -> None:
        """Explicitly inject a generated WAV; synthetic frame timing is not device timing."""
        if self.captured_frames is not None:
            raise ValueError("synthetic capture may be fed only once")
        shutil.copyfile(source, self.path)
        self.path.chmod(0o600)
        self.captured_frames = frames
        self.mark("synthetic_frames_confirmed")

    def signal_quit(self) -> None:
        """Record Stop delivery and sync the already-injected file."""
        self.mark("stop_delivered_to_controller")
        if self.captured_frames is None:
            raise ValueError("synthetic capture was not fed")
        with self.path.open("rb") as stream:
            os.fsync(stream.fileno())

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


class LegacyRecordingFixture:
    """Actual legacy session/library with only device and timer boundaries replaced."""

    def __init__(
        self,
        root: Path,
        *,
        history_count: int,
        history_wav: Path,
        mark: Callable[[str], None],
        future_takes: int = 1,
    ) -> None:
        require_external_private_root(root)
        self.processes: list[InertCaptureProcess] = []

        def process_factory(command: Sequence[str]) -> InertCaptureProcess:
            process = InertCaptureProcess(command, mark)
            self.processes.append(process)
            return process

        self.library = RecordingLibrary(
            AssetLayout.initialize(root / "assets"),
            synthetic_catalog(history_count + future_takes + 2),
            subject_id="synthetic-benchmark",
            process_factory=process_factory,
            timer_factory=InertTimer,
            clock=lambda: FIXTURE_TIME,
            device_resolver=lambda device: device,
        )
        self.session: DesktopRecordingSession = self.library.begin_session(
            [prompt.prompt_id for prompt in self.library.prompt_suite.prompts], SYNTHETIC_DEVICE
        )
        # Setup is outside measurements. Serialize valid historical metadata once;
        # every measured read still performs all real production validations.
        paths = expected_recording_paths(self.session.evidence.selected_suite)
        with history_wav.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        takes = []
        self.historical_paths: set[Path] = set()
        for index in range(history_count):
            destination = self.session.engine.session_directory / paths[index]
            shutil.copyfile(history_wav, destination)
            destination.chmod(0o600)
            self.historical_paths.add(destination)
            takes.append(
                DesktopTakeEvidence(
                    prompt_id=self.session.evidence.selected_suite.prompts[index].prompt_id,
                    take_started_at=FIXTURE_TIME,
                    capture_device=SYNTHETIC_DEVICE,
                    audio_sha256=digest,
                    accepted=True,
                )
            )
        self.session.evidence = self.session.evidence.model_copy(update={"takes": tuple(takes)})
        write_canonical_json(
            self.library.layout.root
            / "provenance/capture-desktop"
            / f"{self.session.evidence.session_id}.json",
            self.session.evidence,
        )

    def save_and_select_next(self) -> object:
        """Run the legacy Keep, rows/sessions refresh and canonical-order selection calls.

        This measures model response readiness, not Qt render/display latency.
        Related: desktop_window._keep_take / _advance_after_keep.
        """
        prompt_id = self.session.evidence.takes[-1].prompt_id
        result = self.session.accept_take()
        rows = self.library.rows()
        self.library.sessions()
        index = next(index for index, row in enumerate(rows) if row.prompt.prompt_id == prompt_id)
        ordered = (*rows[index + 1 :], *rows[: index + 1])
        target = next((row for row in ordered if row.status == "Missing"), None)
        if target is not None:
            result = self.session.select_prompt(target.prompt.prompt_id)
        return result

    def close(self) -> None:
        """Release only this private synthetic session; never remove fixture evidence."""
        self.session.abort()
