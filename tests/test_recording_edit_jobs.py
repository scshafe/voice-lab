from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_recording_native_edits import edit
from test_recording_navigation import NavigationRig
from test_recording_navigation import nav as nav

from cole_voice_lab.recording_edit_jobs import RecordingEditCoordinator
from cole_voice_lab.recording_edits import current_native_edit, native_edit_derivative


def wait_for(path: Path, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


@pytest.mark.serial
def test_actual_render_child_can_block_without_blocking_save_and_parent_loss_is_bounded(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    take = nav.capture()
    changed = edit(nav, take)
    source = nav.rig.layout.root / take.seal.original_path
    original = source.read_bytes()
    marker = tmp_path / "child-waiting"
    release = tmp_path / "child-release"
    code = f"""import time
from pathlib import Path
from cole_voice_lab import recording_edit_jobs as jobs
original=jobs.render_native_edit
def delayed(*args,**kwargs):
    Path({str(marker)!r}).touch(mode=0o600)
    while not Path({str(release)!r}).exists():time.sleep(0.01)
    return original(*args,**kwargs)
jobs.render_native_edit=delayed
raise SystemExit(jobs.main())
"""
    real = subprocess.Popen
    children = []

    def launch(command: Any, **kwargs: Any) -> Any:
        child = real([sys.executable, "-c", code, *command[3:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("cole_voice_lab.recording_edit_jobs.subprocess.Popen", launch)
    events = []
    coordinator = RecordingEditCoordinator(
        nav.rig.store, published=events.append, available_bytes=lambda: 1 << 60
    )
    coordinator.submit(take.reservation.take_id)
    try:
        wait_for(marker)
        assert nav.save(changed.take).take.decision == "kept"
        started = time.monotonic()
        coordinator.close()
        assert time.monotonic() - started < 1.5
        assert children[0].poll() is not None and not events
        assert (
            source.read_bytes() == original
            and current_native_edit(nav.rig.layout, take.reservation.take_id) is not None
        )
    finally:
        coordinator.close()
    monkeypatch.setattr("cole_voice_lab.recording_edit_jobs.subprocess.Popen", real)
    finished = threading.Event()
    recovered = RecordingEditCoordinator(
        nav.rig.store, published=lambda event: finished.set(), available_bytes=lambda: 1 << 60
    )
    try:
        assert finished.wait(5)
    finally:
        recovered.close()
    assert native_edit_derivative(nav.rig.layout, take.reservation.take_id) is not None


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00\x00\x00\x05{}",
        b"\xff\xff\xff\xff",
        b"\x00\x00\x00\x02{}x",
        (20000).to_bytes(4, "big") + b"x" * 20000,
    ],
)
def test_malformed_real_child_replies_are_bounded_named_failures(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    take = nav.capture()
    edit(nav, take)
    real = subprocess.Popen
    code = f"import os; os.read(0,65536); os.write(1,{payload!r})"

    def launch(command: Any, **kwargs: Any) -> Any:
        return real([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr("cole_voice_lab.recording_edit_jobs.subprocess.Popen", launch)
    done = threading.Event()
    events = []

    def publish(event: Any) -> None:
        events.append(event)
        done.set()

    coordinator = RecordingEditCoordinator(
        nav.rig.store, published=publish, available_bytes=lambda: 1 << 60
    )
    try:
        coordinator.submit(take.reservation.take_id)
        assert done.wait(5)
        assert coordinator._worker.is_alive()
    finally:
        coordinator.close()
    assert events[-1].state == "failed" and events[-1].error.code == "RECORDING.EDIT_FAILED"
    assert native_edit_derivative(nav.rig.layout, take.reservation.take_id) is None


def test_blocked_child_input_cannot_hold_service_shutdown(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    take = nav.capture()
    edit(nav, take)
    real = subprocess.Popen
    children = []

    def launch(command: Any, **kwargs: Any) -> Any:
        child = real([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("cole_voice_lab.recording_edit_jobs.subprocess.Popen", launch)
    monkeypatch.setattr(
        "cole_voice_lab.recording_edit_jobs._frame", lambda _: b"\x00\x00\xff\xfc" + b" " * 65532
    )
    coordinator = RecordingEditCoordinator(
        nav.rig.store, published=lambda _: None, available_bytes=lambda: 1 << 60
    )
    coordinator.submit(take.reservation.take_id)
    deadline = time.monotonic() + 3
    while not children and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    coordinator.close()
    assert time.monotonic() - started < 1.5 and children[0].poll() is not None
