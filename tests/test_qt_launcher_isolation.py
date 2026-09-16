"""TQ1: the live Qt launcher path opens no prompt-suite resource other than pilot-v1.json.

Audit hooks cannot be removed in-process, so the launcher runs in a child interpreter
started exactly like the installed launcher (-I -B, working directory /).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from test_pilot_v2_coverage import analyzer_build as analyzer_build
from test_pilot_v2_coverage import prepare_pilot_v2_package
from test_recording_library import DEVICE, START, Process, Timer, complete

from cole_voice_lab.capture import TakeEngine
from cole_voice_lab.intake import accept_capture_session
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.resources import resource_path

PILOT_V1_SHA256 = "eac118a995a1e184d5ed20b9ee23c3e0924c272b28d87308df2abeee1f8772d0"
PILOT_V2_SHA256 = "0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028"

_DRIVER = r"""
import sys

RESOURCES = sys.argv[2]
OPENS = []


def _resource_guard(event, arguments):
    # Installed before cole_voice_lab is imported; only resource JSON is policed.
    if event != "open" or not arguments or not isinstance(arguments[0], (str, bytes)):
        return
    path = arguments[0].decode() if isinstance(arguments[0], bytes) else arguments[0]
    directory, _, name = path.rpartition("/")
    if directory == RESOURCES and name.endswith(".json"):
        if name != "pilot-v1.json":
            raise RuntimeError("Qt launcher opened a non-pilot-v1 resource: " + name)
        OPENS.append(name)


sys.addaudithook(_resource_guard)

import json
import types

RESULT = {}


class _Inert:
    def __init__(self, *arguments, **keywords):
        pass

    def __getattr__(self, name):
        return lambda *arguments, **keywords: None


class QImage(_Inert):
    def isNull(self):
        return False


class QPixmap(_Inert):
    @staticmethod
    def fromImage(image):
        return QPixmap()


class QApplication(_Inert):
    def exec(self):
        return 0


class DesktopWindow(_Inert):
    def __init__(self, library, requested_device=None):
        from cole_voice_lab.canonical import canonical_sha256

        history, sessions, sidecars = library.verified_history_snapshot()
        RESULT.update(
            suite_sha256=canonical_sha256(library.prompt_suite),
            history_sessions=sorted({entry.session_id for entry in history}),
            history=sorted({(entry.session_id, entry.prompt_id) for entry in history}),
            sessions=[[info.session_id, info.catalog_known] for info in sessions],
            sidecars=len(sidecars),
        )


package = types.ModuleType("PySide6")
package.__path__ = []
gui = types.ModuleType("PySide6.QtGui")
gui.QIcon, gui.QImage, gui.QPixmap = _Inert, QImage, QPixmap
widgets = types.ModuleType("PySide6.QtWidgets")
widgets.QApplication = QApplication
window = types.ModuleType("cole_voice_lab.desktop_window")
window.DesktopWindow = DesktopWindow
sys.modules.update(
    {
        "PySide6": package,
        "PySide6.QtGui": gui,
        "PySide6.QtWidgets": widgets,
        "cole_voice_lab.desktop_window": window,
    }
)

from cole_voice_lab.cli import main

status = main(["desktop", "--asset-root", sys.argv[1], "--suite", "pilot"])
print(json.dumps({"status": status, "opens": OPENS, **RESULT}))
"""


def _legacy_root(tmp_path: Path, analyzer_build: Any) -> tuple[AssetLayout, str, str]:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    # The pinned resource, never the `pilot` alias: only the child launcher may resolve the alias,
    # so moving it fails that launcher's audit hook and hash assertion rather than this fixture.
    suite = load_builtin_prompt_suite("pilot-v1.json")
    spoken = next(prompt.prompt_id for prompt in suite.prompts if prompt.category != "room-tone")
    library = RecordingLibrary(
        layout,
        suite,
        process_factory=Process,
        timer_factory=Timer,
        clock=lambda: START,
        device_resolver=lambda device: device,
    )
    session = library.begin_session([spoken], DEVICE)
    complete(session)
    session.finalize()
    engine = TakeEngine(
        layout=layout,
        prompt_suite=suite,
        subject_id="cole-self",
        device=DEVICE,
        session_id="legacy-pilot",
        process_factory=Process,
        timer_factory=Timer,
        clock=lambda: START,
    )
    for _ in suite.prompts:
        engine.start_take()
        engine.stop_take()
        engine.accept_take()
    engine.finalize()
    assert (layout.root / "provenance/capture-desktop").is_dir()
    # Post-switch evidence: a local package prepared from a group on the released pilot-v2
    # resource (map, native takes, raw/ alias) must stay readable by the pilot-v1 Qt library.
    _, package = prepare_pilot_v2_package(layout.root, analyzer_build, ("room-tone-start", spoken))
    assert package.group.catalog_sha256 == PILOT_V2_SHA256
    incoming = layout.directory("incoming") / package.session_id
    shutil.copytree(layout.root / package.package_path, incoming)
    accept_capture_session(layout, incoming, min_free_bytes=0)
    return layout, package.session_id, spoken


def test_qt_launcher_opens_only_pilot_v1_resource(tmp_path: Path, analyzer_build: Any) -> None:
    layout, package_session, spoken = _legacy_root(tmp_path, analyzer_build)
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    completed = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            str(driver),
            str(layout.root),
            str(resource_path("pilot-v1.json").parent),
        ),
        cwd="/",
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["status"] == 0
    assert result["suite_sha256"] == PILOT_V1_SHA256
    # run_desktop reads pilot-v1 once; the sidecar-less rule recognizes it by its pin.
    assert result["opens"] == ["pilot-v1.json"]
    assert "legacy-pilot" in result["history_sessions"] and len(result["history_sessions"]) == 3
    assert [package_session, spoken] in result["history"]
    assert [package_session, "room-tone-start"] not in result["history"]
    assert result["sidecars"] == 1
    assert package_session in {session for session, _ in result["sessions"]}
    assert all(known for _, known in result["sessions"])
