"""The S-1 harness on the simulated UMIK-1 (D-040), and the PortAudio stream's translation
of PortAudio's callback (over a fake ``sounddevice``: no device is ever opened)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from voice_lab_desktop.capture import s1
from voice_lab_desktop.capture.backends import (
    PortAudioTimedBackend,
    SimulatedSpec,
    SimulatedTimedBackend,
)
from voice_lab_desktop.capture.timing import BlockTiming
from voice_lab_desktop.measurement.devices import DeviceError, InputDevice

REPOSITORY = Path(__file__).resolve().parents[4]
SMALL = [
    "--takes",
    "3",
    "--seconds",
    "0.1",
    "--cold-takes",
    "2",
    "--cold-seconds",
    "0.05",
    "--cold-idle-seconds",
    "0",
    "--long-takes",
    "1",
    "--long-seconds",
    "0.3",
    "--max-duration-seconds",
    "0.05",
]


def harness(tmp_path: Path, *extra: str, spec: dict | None = None) -> tuple[int, Path]:
    output = tmp_path / "s1"
    simulated = json.dumps({"speed": 100, **(spec or {})})
    code = s1.main(["--output", str(output), *SMALL, "--simulated", simulated, *extra])
    return code, output


def test_a_simulated_run_checks_the_harness_and_never_passes_s1(tmp_path, capsys):
    code, output = harness(tmp_path)
    assert code == 0
    report = json.loads((output / "s1-report.json").read_text())
    assert report["verdict"] == s1.SIMULATED_VERDICT
    assert report["continuity_verdict"] == "clean" and report["simulated"] is True
    assert report["hardware"] is False
    continuity = report["continuity"]
    assert continuity["takes"] == continuity["successful"] == continuity["revalidated"] == 7
    assert continuity["input_overflows"] == 0 and continuity["adc_time_valid"] is True
    assert [row["variant"] for row in report["takes"]] == [
        "portaudio-cold-start",
        "portaudio-cold-start",
        "portaudio-warm-take",
        "portaudio-warm-take",
        "portaudio-warm-take",
        "portaudio-long-take",
        "portaudio-maximum-duration",
    ]
    assert report["takes"][-1]["stop_reason"] == "maximum-duration"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    # Each take: its WAV, its evidence, its trace and the independent re-validation.
    for take in sorted((output / "takes").iterdir()):
        names = sorted(path.name for path in take.iterdir())
        assert names == ["continuity.json", "original.wav", "python-validation.json", "trace.jsonl"]
        assert json.loads((take / "python-validation.json").read_text())["ok"] is True

    benchmark = json.loads((output / "benchmark" / "report.json").read_text())
    assert benchmark["backend"] == "portaudio-simulated" and benchmark["profile"] == "s1"
    assert benchmark["host"]["swift_configuration"] is None
    assert benchmark["release_acceptance_passed"] is False
    rows = benchmark["acceptance_rows"]
    # The benchmark's own rules: too few samples for a p95 is underpowered, never a pass.
    assert rows["record_to_first_confirmed_frames"]["verdict"] in {"underpowered", "fail"}
    assert rows["timer_frame_agreement"]["verdict"] == "pass"
    assert rows["visible_command_acknowledgment"]["verdict"] == "not-measured"
    operations = {sample["operation"] for sample in benchmark["samples"]}
    assert operations == {
        "cold_device_start",
        "record_to_first_confirmed_frames",
        "stop_reaches_controller",
        "stop_to_sealed_review",
    }

    identity = json.loads((output / "verification" / "verification.json").read_text())
    assert identity["capture_variants"] == sorted(s1.VARIANTS)
    assert len(identity["waveform"]["takes"]) == 7
    envelopes = identity["waveform"]["takes"]
    assert all(-1 <= entry["min"] < 0 < entry["max"] <= 1 for entry in envelopes)
    assert identity["s1"]["hardware"] is False

    matrix = json.loads((output / "matrix" / "acceptance-matrix.json").read_text())
    assert matrix["acceptance_closed"] is False
    assert matrix["benchmark_report"]["backend"] == "portaudio-simulated"
    answered = report["matrix"]["answered"]
    assert {"capture_variants", "native_capture_and_waveform", "timer_frame_agreement"} <= set(
        answered
    )
    assert not set(answered) & set(report["matrix"]["left_for_owner"])
    markdown = (output / "s1-report.md").read_text()
    assert "Verdict: **simulated: harness check only**" in markdown
    assert "Left for the owner" in markdown
    out = capsys.readouterr().out
    assert "S-1 verdict: simulated: harness check only (continuity clean)" in out


def test_a_simulated_fault_shows_as_failed_continuity(tmp_path):
    code, output = harness(tmp_path, spec={"gap_at_block": 2})
    assert code == 1
    report = json.loads((output / "s1-report.json").read_text())
    assert report["verdict"] == s1.SIMULATED_VERDICT
    assert report["continuity_verdict"] == "failed"
    problems = report["continuity"]["problems"]
    assert problems and all("block 2: discontinuity" in problem for problem in problems)
    take = sorted((output / "takes").iterdir())[0]
    assert (take / "original.partial.wav").exists() and not (take / "original.wav").exists()
    assert "## Problems" in (output / "s1-report.md").read_text()


def test_a_hardware_run_passes_only_when_every_take_is_clean(tmp_path, monkeypatch):
    """The verdict rule for real hardware, over takes recorded by the simulated worker (the
    harness is told the run was on hardware; nothing opens a device)."""
    output = s1.prepare_output(tmp_path / "out", REPOSITORY)
    plan = s1.Plan(takes=1, seconds=0.05, cold_takes=0, long_takes=0, max_duration_seconds=0)
    run = s1.Harness(output, plan=plan, simulated={"speed": 100})
    run.run()
    bench = s1.load_benchmark(REPOSITORY)
    run.simulated = None  # judge it as a hardware run
    facts = {"system": "Linux", "release": "x", "machine": "y", "swift_configuration": None}
    monkeypatch.setattr(s1, "host_facts", lambda *_a, **_k: facts)
    report = s1.build_report(
        run,
        repository=REPOSITORY,
        bench=bench,
        device="Umik-1  Gain: 18dB: USB Audio (hw:2,0) [ALSA]",
        created_at=s1.datetime.now(s1.UTC),
    )
    assert report["verdict"] == "passed"
    run.takes[0].success = False
    run.takes[0].error = "block 9: discontinuity"
    assert s1.continuity_summary(run.takes, 64.0)["clean"] is False


@pytest.mark.parametrize("case", ["inside", "not-empty", "open-mode", "file", "no-parent"])
def test_the_output_directory_must_be_new_private_and_outside_the_repository(tmp_path, case):
    if case == "inside":
        target = REPOSITORY / "s1-output"
        match = "inside the repository"
    elif case == "not-empty":
        target = tmp_path / "used"
        target.mkdir(mode=0o700)
        (target / "x").write_text("x")
        match = "not empty"
    elif case == "open-mode":
        target = tmp_path / "open"
        target.mkdir()
        os.chmod(target, 0o755)
        match = "not private"
    elif case == "file":
        target = tmp_path / "file"
        target.write_text("x")
        match = "not a directory"
    else:
        target = tmp_path / "missing" / "deeper"
        match = "does not exist"
    with pytest.raises(s1.HarnessError, match=match):
        s1.prepare_output(target, REPOSITORY)
    assert not (REPOSITORY / "s1-output").exists()


def test_an_empty_private_directory_is_accepted(tmp_path):
    target = tmp_path / "ready"
    target.mkdir(mode=0o700)
    assert s1.prepare_output(target, REPOSITORY) == target


def test_refusals_exit_2_and_bad_arguments_exit_through_argparse(tmp_path, capsys):
    used = tmp_path / "used"
    used.mkdir(mode=0o700)
    (used / "x").write_text("x")
    assert s1.main(["--output", str(used), "--simulated", "{}"]) == 2
    assert "not empty" in capsys.readouterr().err
    no_umik = json.dumps({"devices": ["built-in"]})
    assert s1.main(["--output", str(tmp_path / "n"), *SMALL, "--simulated", no_umik]) == 2
    assert "No UMIK-1 found" in capsys.readouterr().err
    for bad in (
        ["--takes", "0", "--cold-takes", "0", "--long-takes", "0"],
        ["--seconds", "0"],
        ["--max-duration-seconds", "601"],
        ["--cold-idle-seconds", "-1"],
        ["--simulated", "not json"],
        ["--simulated", "[]"],
    ):
        with pytest.raises(SystemExit):
            s1.main(["--output", str(tmp_path / "z"), *bad])


def test_the_repository_is_found_from_the_module():
    assert s1.find_repository(Path(s1.__file__)) == REPOSITORY
    assert s1.find_repository(Path("/")) is None


def test_the_wrapper_script_is_a_checked_posix_shell_script():
    script = REPOSITORY / "scripts" / "s1-portaudio"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    text = script.read_text()
    assert text.startswith("#!/bin/sh\nset -eu\n") or text.startswith("#!/bin/sh\n#")
    assert "set -eu" in text and "PYTHONDONTWRITEBYTECODE=1" in text and "-I -B" in text
    assert "voice_lab_desktop.capture.s1" in text
    subprocess.run(["/bin/sh", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(script)], check=True)


def test_the_wrapper_runs_the_harness_from_the_checkout(tmp_path):
    """``scripts/s1-portaudio`` with the repository's ``.venv`` (where the app is not
    installed) finds this checkout's app and runs a simulated harness check."""
    if not (REPOSITORY / ".venv" / "bin" / "python").exists():
        pytest.skip("this checkout has no .venv (a git worktree)")
    output = tmp_path / "wrapped"
    completed = subprocess.run(
        [
            str(REPOSITORY / "scripts" / "s1-portaudio"),
            "--output",
            str(output),
            "--takes",
            "1",
            "--seconds",
            "0.05",
            "--cold-takes",
            "0",
            "--long-takes",
            "0",
            "--max-duration-seconds",
            "0",
            "--simulated",
            '{"speed": 100}',
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads((output / "s1-report.json").read_text())
    assert report["verdict"] == s1.SIMULATED_VERDICT and report["continuity"]["takes"] == 1


# --- the PortAudio stream, over a fake sounddevice ----------------------------------------


class FakeStream:
    def __init__(self, **options):
        self.options = options
        self.active = True


class FakePortAudioError(Exception):
    pass


def fake_base(*, fail: bool = False):
    devices = [InputDevice(2, "Umik-1  Gain: 18dB: USB Audio (hw:2,0)", "ALSA", 1, 48000.0)]
    streams: list[FakeStream] = []

    def raw_input_stream(**options):
        if fail:
            raise FakePortAudioError("Invalid sample rate")
        stream = FakeStream(**options)
        streams.append(stream)
        return stream

    sd = SimpleNamespace(
        RawInputStream=raw_input_stream,
        PortAudioError=FakePortAudioError,
        CoreAudioSettings=lambda **options: ("coreaudio", options),
    )
    return SimpleNamespace(sd=sd, list_inputs=lambda: devices), streams


def test_the_portaudio_stream_asks_for_packed_24_bit_blocks_and_passes_the_times(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    base, streams = fake_base()
    backend = PortAudioTimedBackend(base)  # type: ignore[arg-type]
    assert backend.simulated is False
    device = backend.list_inputs()[0]
    seen: list[tuple[bytes, BlockTiming]] = []
    stream = backend.open_timed_input(
        device, block_frames=512, callback=lambda data, timing: seen.append((data, timing))
    )
    options = streams[0].options
    assert stream is streams[0]
    assert options["dtype"] == "int24" and options["channels"] == 1
    assert options["samplerate"] == 48_000 and options["blocksize"] == 512
    assert options["device"] == 2 and options["extra_settings"] is None
    stamp = SimpleNamespace(inputBufferAdcTime=12.5, currentTime=12.51)
    status = SimpleNamespace(input_overflow=True, input_underflow=False)
    options["callback"](memoryview(b"\x01\x02\x03" * 512), 512, stamp, status)
    data, timing = seen[0]
    assert data == b"\x01\x02\x03" * 512
    assert timing == BlockTiming(512, 12.5, 12.51, True, False)


def test_on_macos_the_stream_refuses_a_converter(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    base, streams = fake_base()
    PortAudioTimedBackend(base).open_timed_input(  # type: ignore[arg-type]
        base.list_inputs()[0], block_frames=1024, callback=lambda *_: None
    )
    assert streams[0].options["extra_settings"] == (
        "coreaudio",
        {"change_device_parameters": True, "fail_if_conversion_required": True},
    )


def test_a_stream_portaudio_refuses_is_a_device_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    base, _ = fake_base(fail=True)
    jack = InputDevice(0, "Umik-1 Mono", "JACK Audio Connection Kit", 1, 48000.0)
    with pytest.raises(DeviceError, match="Invalid sample rate.*clock.force-rate 48000"):
        PortAudioTimedBackend(base).open_timed_input(  # type: ignore[arg-type]
            jack, block_frames=1024, callback=lambda *_: None
        )


def test_the_simulated_spec_round_trips_and_its_stream_ends_when_told():
    spec = SimulatedSpec.from_json({"speed": 0, "stop_after_blocks": 2, "devices": ["umik-1"]})
    assert SimulatedSpec.from_json(spec.to_json()) == spec
    backend = SimulatedTimedBackend(spec)
    seen: list[BlockTiming] = []
    stream = backend.open_timed_input(
        backend.list_inputs()[0], block_frames=200, callback=lambda _d, t: seen.append(t)
    )
    stream.start()
    deadline = time.monotonic() + 5
    while stream.active and time.monotonic() < deadline:
        time.sleep(0.005)
    stream.close()
    assert not stream.active and len(seen) == 2
    assert seen[1].adc_time - seen[0].adc_time == pytest.approx(200 / 48_000)
