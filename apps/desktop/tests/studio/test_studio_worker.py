"""The prompt studio over the real capture worker (a process per take, D-039) on the simulated
UMIK-1 (``capture/backends.py``): the take is the worker's PCM and continuity evidence,
validated by the parent, then saved by the corpus client. The simulated device's takes stay
on this machine (local-only); a take whose continuity breaks never becomes a take."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corpus_rig import PC
from voice_lab_desktop.capture.evidence import read_evidence
from voice_lab_desktop.capture.process import WorkerRecorder

from .conftest import open_studio, ready, studio_app, wait_phase

#: A real-time multiple: a second of audio every 50 ms.
FAST = {"speed": 20.0}


def test_a_take_over_the_worker_is_saved_here(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC
) -> None:
    model = studio_app(data_dir, tmp_path, pc, WorkerRecorder(simulated=FAST))
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            ready(qtbot, model)
            studio = model.studio
            first = studio.promptId
            assert studio.deviceLabel.startswith("Umik-1")
            studio.toggleRecord()
            wait_phase(qtbot, studio, "recording")
            qtbot.waitUntil(lambda: studio.elapsedText not in ("00:00.00",), timeout=10000)
            studio.toggleRecord()
            wait_phase(qtbot, studio, "review")
            studio.saveNext()
            qtbot.waitUntil(lambda: studio.promptId != first, timeout=10000)
            assert studio.phase == "ready" and studio.coverageText == "1 / 34"
            (take,) = studio.client.takes()
            assert take.prompt_id == first and take.sync.decision.decision == "kept"
            evidence = read_evidence(take.path / "continuity.json")
            assert evidence.success and evidence.simulated
            assert evidence.wav_sha256 == take.record.audio_sha256
            assert take.record.simulated and take.sync.upload == "local-only"
            assert take.sync.outbox == ()

            # A lost buffer: the worker stops itself, the parent refuses what it wrote.
            studio._recorder = WorkerRecorder(
                simulated={**FAST, "gap_at_block": 3, "gap_frames": 4800}
            )
            studio.toggleRecord()
            wait_phase(qtbot, studio, "failed")
            assert "deviation" in studio.statusText or "continuity" in studio.statusText.lower()
            studio.discard()
            wait_phase(qtbot, studio, "ready")
            assert len(studio.client.takes()) == 1
            assert list(studio.client.spool.recording_root.iterdir()) == []
    finally:
        model.shutdown()
