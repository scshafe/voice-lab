"""launcher.test.mjs, translated (design §5.3), plus the spawned child's contract.

The C launcher is compiled with ``cc`` into ``tmp_path``; without a compiler the tests
skip. ``browser`` mode and the no-argument default are gone: the Mac no longer serves.
"""

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from voice_lab_mirror import mirror

SOURCE = Path(__file__).resolve().parent.parent / "launcher.c"
CC = shutil.which("cc")
needs_cc = pytest.mark.skipif(CC is None, reason="no C compiler (cc)")
PYTHON = "/opt/voice-lab-mirror/venv/bin/python3"


def compile_launcher(output: Path, source: Path, python: str = PYTHON) -> None:
    assert CC is not None
    subprocess.run(
        [CC, "-Wall", "-Werror", f'-DVL_MIRROR_PYTHON="{python}"', "-o", str(output), str(source)],
        check=True,
    )


@needs_cc
def test_launcher_mode_allowlist(tmp_path: Path) -> None:
    harness = tmp_path / "harness.c"
    harness.write_text(
        f"""#define VL_LAUNCHER_NO_MAIN 1
#include "{SOURCE}"
int main(int argc, char *argv[]) {{
  const struct mode *m = resolve_mode(argc, argv);
  if (m == NULL) {{ puts("REFUSED"); return 0; }}
  for (const char *const *a = m->argv; *a != NULL; a++) puts(*a);
  return 0;
}}
"""
    )
    exe = tmp_path / "harness"
    compile_launcher(exe, harness)

    def resolve(*args: str) -> list[str]:
        result = subprocess.run([str(exe), *args], check=True, capture_output=True, text=True)
        return result.stdout.splitlines()

    mirror = resolve("mirror")
    assert mirror[:5] == [PYTHON, "-I", "-m", "voice_lab_mirror", "--approval"]
    assert len(mirror) == 6 and mirror[5].startswith("owner standing approval")
    bad: list[list[str]] = [
        [],
        ["browser"],
        ["/bin/sh"],
        ["-c", "id"],
        ["Mirror"],
        [""],
        ["mirror "],
        ["mirror", "--approval", "x"],
        ["browser", "mirror"],
        ["mirror\n"],
    ]
    for args in bad:
        assert resolve(*args) == ["REFUSED"], args


@needs_cc
def test_launcher_binary_refuses_an_unknown_mode_with_exit_64(tmp_path: Path) -> None:
    exe = tmp_path / "launcher"
    marker = tmp_path / "spawned"
    child = tmp_path / "python3"
    child.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    child.chmod(0o755)
    compile_launcher(exe, SOURCE, python=str(child))
    for args in (["/bin/true"], [], ["browser"]):
        result = subprocess.run([str(exe), *args], capture_output=True, text=True)
        assert result.returncode == 64
        assert "usage" in result.stderr
    assert not marker.exists()


@needs_cc
def test_launcher_spawns_the_fixed_interpreter_and_mirrors_its_exit(tmp_path: Path) -> None:
    exe = tmp_path / "launcher"
    record = tmp_path / "argv"
    child = tmp_path / "python3"
    child.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{record}'\n"
        f'printf "%s\\n" "$VOICE_LAB_MIRROR_DEST" >> \'{record}\'\nexit 3\n'
    )
    child.chmod(0o755)
    compile_launcher(exe, SOURCE, python=str(child))
    result = subprocess.run(
        [str(exe), "mirror"], env={"VOICE_LAB_MIRROR_DEST": "vj-mirror@the-pc:"}, check=False
    )
    assert result.returncode == 3  # the drop was locked: launchd retries next period
    lines = record.read_text().splitlines()
    assert lines[:4] == ["-I", "-m", "voice_lab_mirror", "--approval"]
    assert lines[-1] == "vj-mirror@the-pc:"
    child.unlink()
    missing = subprocess.run([str(exe), "mirror"], capture_output=True, text=True)
    assert missing.returncode == 78
    assert "posix_spawn" in missing.stderr


def test_the_plist_template_runs_the_launcher_and_configures_the_target() -> None:
    template = (SOURCE.parent / "launchd/com.scshafe.voice-lab.mirror.plist.in").read_text()
    values = {
        "@LAUNCHER@": "/opt/vlm/bin/voice-lab-mirror-launcher",
        "@DEST@": "vj-mirror@cole-laptop:",
        "@SSH_KEY@": "/Users/someone/.ssh/vj-mirror",
        "@RSYNC@": "/opt/homebrew/bin/rsync",
        "@STATE_DIR@": "/Users/someone/.local/state/voice-lab-mirror",
        "@LOG@": "/Users/someone/Library/Logs/voice-lab-mirror.log",
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    assert re.search(r"<string>[^<]*@[A-Z_]+@", template) is None
    job = plistlib.loads(template.encode())
    assert job["ProgramArguments"] == [values["@LAUNCHER@"], "mirror"]
    assert job["StartInterval"] == 900
    environment = job["EnvironmentVariables"]
    assert environment[mirror.DEST_ENV] == "vj-mirror@cole-laptop:"
    assert environment[mirror.SSH_KEY_ENV] == values["@SSH_KEY@"]
    assert environment[mirror.RSYNC_ENV] == values["@RSYNC@"]
    assert environment[mirror.STATE_ENV] == values["@STATE_DIR@"]
