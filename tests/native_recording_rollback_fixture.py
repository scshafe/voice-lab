"""Synthetic-only full rollback replay; never invokes the retained real-root launcher."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.paths import AssetLayout


def _uninstrumented_environment() -> dict[str, str]:
    """Every frozen-source child runs outside this repository's coverage measurement.

    The frozen pre-native release is a separate program under test, not code this gate
    measures. Any child that inherits the coverage subprocess hooks enrols those files in
    the parent's run and silently adds an unrelated denominator to the repository total.
    """

    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_"))
    }


def replay_frozen_rollback(rollback: Path, work: Path) -> dict[str, object]:
    rollback = rollback.resolve(strict=True)
    work = work.resolve()
    work.mkdir(mode=0o700, parents=True)
    frozen_python = rollback / "environment/bin/python"
    source_manifest = json.loads((rollback / "source-identity.json").read_text())
    environment_manifest = json.loads((rollback / "environment-identity.json").read_text())

    def verify_preservation() -> None:
        for relative, digest in source_manifest.items():
            assert sha256_file(rollback / "source" / relative)[0] == digest
        for relative, identity in environment_manifest.items():
            path = rollback / "environment" / relative
            if "symlink" in identity:
                assert path.is_symlink() and os.readlink(path) == identity["symlink"]
            else:
                assert sha256_file(path) == (identity["sha256"], identity["size_bytes"])

    verify_preservation()
    primary = work / "disposable primary"
    subprocess.run(
        (str(frozen_python), "-I", "-B", "-m", "venv", "--without-pip", str(primary)),
        check=True,
        capture_output=True,
        timeout=30,
        env=_uninstrumented_environment(),
    )
    site = primary / "lib/python3.12/site-packages"
    for name in ("PySide6", "cole_voice_lab"):
        package = site / name
        package.mkdir()
        (package / "__init__.py").write_text("# Disposable pre-cutover placeholder.\n")
        (package / "desktop.py").write_text("# Disposable Qt entrypoint placeholder.\n")
        shutil.rmtree(package)
    absent = subprocess.run(
        (
            str(primary / "bin/python"),
            "-I",
            "-B",
            "-c",
            "import importlib.util,json; "
            "print(json.dumps({n:importlib.util.find_spec(n) is None "
            "for n in ('PySide6','cole_voice_lab')}))",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=_uninstrumented_environment(),
    )
    assert json.loads(absent.stdout) == {"PySide6": True, "cole_voice_lab": True}
    assets = AssetLayout.initialize(work / "synthetic assets")
    probe = work / "rollback_probe.py"
    report = work / "rollback-report.json"
    probe.write_text(
        """from pathlib import Path
import json,os,runpy,sys
assert sys.argv[1:5] == ["-I", "-B", "-m", "cole_voice_lab.cli"]
arguments=sys.argv[5:]
assert arguments[0]=="desktop" and arguments[1]=="--asset-root"
asset_root=Path(arguments[2]).resolve()
expected_root=Path("""
        + repr(str(assets.root))
        + """)
assert asset_root==expected_root
import cole_voice_lab,PySide6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import cole_voice_lab.desktop_window as module
Original=module.DesktopWindow
state={"microphone_opened":False,"audio_played":False,"visible_before_close":False,"clean_close":False}
class InertPlayer:
    position_seconds=0.0
    is_playing=False
    def load(self,*args): raise AssertionError("playback is forbidden")
    def play(self,*args): raise AssertionError("playback is forbidden")
    def seek(self,*args): raise AssertionError("playback is forbidden")
    def pause(self): pass
    def stop(self): pass
    def close(self): pass
class FixtureWindow(Original):
    def __init__(self,*args,**kwargs):
        kwargs.update(device_loader=lambda:(),player_factory=InertPlayer)
        super().__init__(*args,**kwargs)
        QTimer.singleShot(500,self.fixture_close)
    def fixture_close(self):
        state["visible_before_close"]=self.isVisible()
        self.grab().save(str(Path("""
        + repr(str(work))
        + """)/"rollback-offscreen.png"))
        self.close()
        state["clean_close"]=not self.isVisible()
module.DesktopWindow=FixtureWindow
sys.argv=["voice-lab",*arguments]
try:
    runpy.run_module("cole_voice_lab.cli",run_name="__main__")
except SystemExit as error:
    code=error.code
else:
    code=0
state.update(event_loop_exit_code=code,source=cole_voice_lab.__file__,qt=PySide6.__file__,asset_root=str(asset_root))
report=Path("""
        + repr(str(report))
        + """)
fd=os.open(report,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w") as stream:json.dump(state,stream,sort_keys=True)
raise SystemExit(code)
"""
    )
    probe.chmod(0o600)
    wrapper = work / "inert frozen Python"
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.join((str(frozen_python), "-I", "-B", str(probe))) + ' "$@"\n'
    )
    wrapper.chmod(0o700)
    app = work / "Frozen Rollback Fixture.app"
    command = (
        "from pathlib import Path; "
        "from cole_voice_lab.desktop_application import create_desktop_application; "
        "create_desktop_application(Path("
        + repr(str(app))
        + "),python=Path("
        + repr(str(wrapper))
        + "),asset_root=Path("
        + repr(str(assets.root))
        + "))"
    )
    subprocess.run(
        (str(frozen_python), "-I", "-B", "-c", command),
        check=True,
        capture_output=True,
        timeout=15,
        env=_uninstrumented_environment(),
    )
    environment = _uninstrumented_environment()
    environment.update(
        QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1"
    )
    result = subprocess.run(
        (str(app / "Contents/MacOS/ColeVoiceLab"),),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(report.read_text())
    assert evidence["visible_before_close"] and evidence["clean_close"]
    assert Path(evidence["source"]).is_relative_to(rollback / "source")
    assert Path(evidence["qt"]).is_relative_to(rollback / "environment")
    verify_preservation()
    evidence.update(
        disposable_primary_qt_absent=True,
        disposable_primary_lab_absent=True,
        source_files_verified=len(source_manifest),
        environment_entries_verified=len(environment_manifest),
        retained_real_root_launcher_executed=False,
        retained_release_unchanged=True,
    )
    report.write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence
