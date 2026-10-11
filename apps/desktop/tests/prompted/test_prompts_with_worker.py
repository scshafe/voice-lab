"""Record → Prompts over the real capture worker (a process per take) on the simulated UMIK-1:
the screen's recorder is the one the app ships (``capture.process.WorkerRecorder``), so each
take is the worker's PCM and continuity evidence, validated by the parent before it exists
(D-039). A session ends as a capture package labelled ``portaudio`` (D-042); a take whose
continuity breaks is never a take."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from conftest import AppRig, Window, open_window
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from voice_lab_desktop.capture.evidence import PortAudioContinuityEvidence
from voice_lab_desktop.capture.process import WorkerRecorder
from voice_lab_desktop.capture.types import EVIDENCE_NAME, ORIGINAL_NAME

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.intake import validate_capture_package

from .conftest import make_app, wait_phase

SPOKEN = "reference-neutral-01"
#: A real-time multiple: a second of audio every 50 ms.
FAST = {"speed": 20.0}


@contextmanager
def screen(qtbot: Any, data_dir: Path, tmp_path: Path, spec: dict[str, Any]) -> Iterator[Window]:
    model = make_app(data_dir, tmp_path, WorkerRecorder(simulated=spec))  # type: ignore[arg-type]
    try:
        with open_window(AppRig(model, model.player._output, [], None, tmp_path)) as window:
            qtbot.waitExposed(window.root)
            qtbot.waitUntil(lambda: model.prompts.deviceOk, timeout=10000)
            yield window
    finally:
        model.shutdown()


def take(qtbot: Any, prompts: Any, model: Any) -> None:
    """Record until the worker has confirmed some frames, then stop and wait for the check."""
    prompts.toggleRecord()
    qtbot.waitUntil(lambda: prompts.phase == "recording", timeout=10000)
    qtbot.waitUntil(lambda: prompts.elapsedText not in ("", "0:00.0"), timeout=10000)
    prompts.toggleRecord()
    wait_phase(qtbot, model, "ready")


def test_a_session_over_the_worker_becomes_a_portaudio_package(
    qtbot: Any, data_dir: Path, tmp_path: Path
) -> None:
    with screen(qtbot, data_dir, tmp_path, FAST) as window:
        model, prompts = window.model, window.model.prompts
        assert prompts.deviceLabel.startswith("Umik-1")
        prompts.chooseAll(False)
        prompts.choosePrompt(SPOKEN, True)
        prompts.startSession()
        wait_phase(qtbot, model, "ready")
        for _ in prompts.batch:
            take(qtbot, prompts, model)
            assert prompts.lastError == ""
            prompts.saveNext()
            wait_phase(qtbot, model, "ready")
        assert [row["status"] for row in prompts.batch] == ["kept"] * 3
        session_id = prompts.sessionId
        prompts.finishSession()
        qtbot.waitUntil(lambda: prompts.phase == "packaged", timeout=10000)

    session = data_dir / "prompted" / "sessions" / session_id
    takes = sorted(path.parent for path in session.glob(f"takes/*/*/{ORIGINAL_NAME}"))
    assert len(takes) == 3
    for take_dir in takes:
        # Each take is the worker's: its evidence binds this very file (and stays here).
        evidence = PortAudioContinuityEvidence.model_validate_json(
            (take_dir / EVIDENCE_NAME).read_bytes()
        )
        inspection = inspect_canonical_wav(take_dir / ORIGINAL_NAME)
        assert evidence.success and evidence.simulated
        assert evidence.wav_sha256 == inspection.sha256
        assert evidence.frames == inspection.audio.frames
    directory = data_dir / "prompted" / "packages" / session_id
    package = validate_capture_package(directory)
    assert package.manifest.capture_device.backend == "portaudio"
    assert package.manifest.session_id == session_id
    assert {f.kind for f in package.manifest.files} == {"room-tone", "utterance"}
    assert not any(path.name == EVIDENCE_NAME for path in directory.rglob("*"))


def test_a_take_whose_continuity_breaks_is_never_a_take(
    qtbot: Any, data_dir: Path, tmp_path: Path
) -> None:
    # A lost buffer: the ADC time jumps a tenth of a second at the third block.
    spec = {**FAST, "gap_at_block": 3, "gap_frames": 4800}
    with screen(qtbot, data_dir, tmp_path, spec) as window:
        model, prompts = window.model, window.model.prompts
        prompts.chooseAll(False)
        prompts.choosePrompt(SPOKEN, True)
        prompts.startSession()
        wait_phase(qtbot, model, "ready")
        prompts.toggleRecord()
        # The worker stops itself at the gap; the parent refuses what it wrote.
        qtbot.waitUntil(lambda: prompts.lastError != "", timeout=10000)
        wait_phase(qtbot, model, "ready")
        assert "deviation" in prompts.lastError or "continuity" in prompts.lastError.lower()
        assert [row["status"] for row in prompts.takes] == ["failed"]
        assert prompts.reviewNumber == 0
        assert [row["status"] for row in prompts.batch] == ["missing"] * 3
