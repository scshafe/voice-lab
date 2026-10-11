"""The S-1 harness: does the PortAudio capture worker give D-015-grade continuity evidence
on this machine (D-040)?

    scripts/s1-portaudio --output DIR [--takes 20 --seconds 3] [--cold-takes 20
        --cold-seconds 1 --cold-idle-seconds 5] [--long-takes 2 --long-seconds 60]
        [--max-duration-seconds 2] [--block-frames 1024] [--tolerance-frames 64]
        [--simulated JSON] [--repository PATH]

It records takes from the UMIK-1 with the app's own capture worker (``process.py``, the
same code the Record: prompts screen uses) and writes, into ``DIR`` (new or empty, mode
0700, outside the repository):

- ``takes/NNN-<variant>/``: each take's WAV (``original.wav``, or the kept partial of a
  failed take), its continuity evidence, its per-block trace, and
  ``python-validation.json`` (the evidence re-read from disk and bound again to the WAV,
  independently of the worker and of the parent's first check);
- ``benchmark/report.json``: the measurements in the shape
  ``scripts/recording_acceptance_matrix.py`` reads, the verdicts computed by the existing
  benchmark's own rules and thresholds (``tests/recording_latency_benchmark.py``);
- ``verification/verification.json``: the variants exercised, waveform extrema from the
  written files, and the S-1 results;
- ``matrix/``: the acceptance matrix, run over the two;
- ``s1-report.json`` and ``s1-report.md``: the verdict for this platform and what is left
  for the owner.

The verdict is ``passed`` only for a run on real hardware (the UMIK-1, not the simulated
device) in which every take's evidence is successful at the declared tolerance, no block
had an overflow or no ADC time, and every take re-validates. A ``--simulated`` run checks
the harness itself and always says ``simulated: harness check only``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.desktop_audio import load_pcm_audio
from cole_voice_lab.errors import ContractError, SecurityError

from ..measurement import chain
from ..measurement.devices import DeviceError
from .evidence import read_evidence
from .process import WorkerRecorder
from .timing import DEFAULT_TOLERANCE_FRAMES
from .types import EVIDENCE_NAME, ORIGINAL_NAME, PARTIAL_NAME, SAMPLE_RATE, CaptureFailed
from .worker import DEFAULT_BLOCK_FRAMES

REPORT_VERSION: Final = "voice-lab-desktop.s1-report/v1"
BENCHMARK_VERSION: Final = "voice-lab-desktop.s1-benchmark/v1"
SIMULATED_VERDICT: Final = "simulated: harness check only"
VARIANTS: Final = (
    "portaudio-cold-start",
    "portaudio-warm-take",
    "portaudio-long-take",
    "portaudio-stop-requested",
    "portaudio-maximum-duration",
)
#: Rows of the matrix that a capture worker cannot answer: the Swift app's interface and
#: process parity, and what only a person with the hardware can judge.
OWNER_ROWS_NOTE: Final = (
    "The parity rows describe the Swift app's interface, helper processes and features "
    "(shell geometry, transport, review, fine-tune, Speak, recovery); the Record: prompts "
    "screen is judged by the owner against them. The physical-gate rows are always the "
    "owner's: they need the real microphone, headphones and a person."
)


class HarnessError(Exception):
    """The harness cannot run as asked; the message says why."""


@dataclass
class TakeResult:
    index: int
    variant: str
    seconds: float
    directory: Path
    stop_requested: bool
    success: bool = False
    error: str | None = None
    frames: int = 0
    confirmed_frames: int = 0
    parent_times: dict[str, float | None] = field(default_factory=dict)
    worker: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)

    def evidence_value(self, key: str) -> Any:
        return self.worker.get(key)


def find_repository(start: Path) -> Path | None:
    """The checkout this module runs from (it holds the matrix and the benchmark)."""
    for candidate in start.resolve().parents:
        if (candidate / "scripts" / "recording_acceptance_matrix.py").is_file() and (
            candidate / "tests" / "recording_latency_benchmark.py"
        ).is_file():
            return candidate
    return None


def load_benchmark(repository: Path) -> ModuleType:
    """``tests/recording_latency_benchmark.py`` of ``repository``: its acceptance rows,
    thresholds and verdict rules are the ones the matrix expects."""
    for path in (repository / "tests", repository / "scripts"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return importlib.import_module("recording_latency_benchmark")


def prepare_output(path: Path, repository: Path) -> Path:
    """``path`` as a new or empty private directory outside the repository.

    Raises: ``HarnessError``."""
    absolute = Path(os.path.abspath(path))
    resolved = absolute.resolve()
    if resolved == repository or repository in resolved.parents:
        raise HarnessError(f"{absolute} is inside the repository; recordings never enter Git")
    if os.path.lexists(absolute):
        info = absolute.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise HarnessError(f"{absolute} exists and is not a directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise HarnessError(
                f"{absolute} is not private (mode {stat.S_IMODE(info.st_mode):o}; chmod 700)"
            )
        if any(absolute.iterdir()):
            raise HarnessError(f"{absolute} is not empty; give the harness a new directory")
    else:
        if not absolute.parent.is_dir():
            raise HarnessError(f"{absolute.parent} does not exist")
        absolute.mkdir(mode=0o700)
        os.chmod(absolute, 0o700)
    return absolute


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_json(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")


def revalidate(take_dir: Path) -> dict[str, Any]:
    """The take checked again from disk, independently: the evidence file and contract,
    the WAV's format, and the evidence's binding to the WAV's bytes."""
    checks: dict[str, Any] = {}
    try:
        evidence = read_evidence(take_dir / EVIDENCE_NAME)
        checks["evidence_contract"] = True
        checks["evidence_success"] = evidence.success
        wav = take_dir / ORIGINAL_NAME
        if not wav.exists():
            wav = take_dir / PARTIAL_NAME
        inspection = inspect_canonical_wav(wav)
        checks["wav_canonical"] = True
        checks["sha256_bound"] = inspection.sha256 == evidence.wav_sha256
        checks["size_bound"] = inspection.size_bytes == evidence.wav_size_bytes
        checks["frames_bound"] = inspection.audio.frames == evidence.frames
        ok = all(value is True for value in checks.values())
        return {"ok": ok, "wav": wav.name, "checks": checks, "error": None}
    except (ContractError, SecurityError, OSError) as exc:
        return {"ok": False, "checks": checks, "error": str(exc) or type(exc).__name__}


@dataclass
class Plan:
    takes: int = 20
    seconds: float = 3.0
    cold_takes: int = 20
    cold_seconds: float = 1.0
    cold_idle_seconds: float = 5.0
    long_takes: int = 2
    long_seconds: float = 60.0
    max_duration_seconds: float = 2.0


class Harness:
    def __init__(
        self,
        output: Path,
        *,
        plan: Plan,
        block_frames: int = DEFAULT_BLOCK_FRAMES,
        tolerance_frames: float = DEFAULT_TOLERANCE_FRAMES,
        simulated: dict[str, Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        say: Callable[[str], None] = print,
    ) -> None:
        self.output = output
        self.plan = plan
        self.block_frames = block_frames
        self.tolerance_frames = tolerance_frames
        self.simulated = simulated
        self.sleep = sleep
        self.say = say
        self.takes: list[TakeResult] = []
        self.takes_root = _private_dir(output / "takes")

    def recorder(self, max_seconds: float = 600.0) -> WorkerRecorder:
        return WorkerRecorder(
            simulated=self.simulated,
            max_seconds=max_seconds,
            block_frames=self.block_frames,
            tolerance_frames=self.tolerance_frames,
            trace=True,
        )

    def run(self) -> list[TakeResult]:
        plan = self.plan
        for _ in range(plan.cold_takes):
            self.sleep(plan.cold_idle_seconds)
            self.recorder().device_label()  # a fresh device listing before a cold start
            self.take("portaudio-cold-start", plan.cold_seconds)
        for _ in range(plan.takes):
            self.take("portaudio-warm-take", plan.seconds)
        for _ in range(plan.long_takes):
            self.take("portaudio-long-take", plan.long_seconds)
        if plan.max_duration_seconds > 0:
            self.take(
                "portaudio-maximum-duration",
                plan.max_duration_seconds,
                stop=False,
                recorder=self.recorder(max_seconds=plan.max_duration_seconds),
            )
        return self.takes

    def take(
        self,
        variant: str,
        seconds: float,
        *,
        stop: bool = True,
        recorder: WorkerRecorder | None = None,
    ) -> TakeResult:
        index = len(self.takes) + 1
        directory = _private_dir(self.takes_root / f"{index:03d}-{variant}")
        result = TakeResult(index, variant, seconds, directory, stop_requested=stop)
        self.takes.append(result)
        try:
            running = (recorder or self.recorder()).start(directory)
        except CaptureFailed as exc:
            result.error = str(exc)
            result.validation = revalidate(directory)
            return result
        target = round(seconds * SAMPLE_RATE)
        deadline = time.monotonic() + seconds * 3 + 30
        while running.confirmed_frames < target and not running.done:
            if time.monotonic() > deadline:
                break
            time.sleep(0.005)
        if stop:
            running.stop()
        try:
            captured = running.wait(timeout=seconds + 60)
            result.success = True
            result.frames = captured.frames
        except CaptureFailed as exc:
            result.error = str(exc)
        except TimeoutError as exc:
            running.kill()
            try:
                running.wait(timeout=10)
            except (CaptureFailed, TimeoutError):
                pass
            result.error = str(exc)
        result.confirmed_frames = running.confirmed_frames
        result.parent_times = dict(running.times)
        result.worker = dict(running.summary or {})
        result.validation = revalidate(directory)
        _write_json(directory / "python-validation.json", result.validation)
        state = "ok" if result.success else f"FAILED: {result.error}"
        self.say(f"take {index:3d} {variant:28s} {seconds:6.1f} s  {state}")
        return result


def _ms(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start) * 1000.0


def samples(takes: Sequence[TakeResult], *, signal: str) -> list[dict[str, Any]]:
    """The benchmark's samples (one per measured boundary per take)."""
    out: list[dict[str, Any]] = []

    def add(operation: str, take: TakeResult, value_ms: float | None) -> None:
        if value_ms is None:
            return
        out.append(
            {
                "operation": operation,
                "configured_history_level": 0,
                "durable_take_count_at_start": 0,
                "take_seconds": take.seconds,
                "signal": signal,
                "workload": "idle",
                "duration_ns": round(value_ms * 1_000_000),
                "status": "ok" if take.success else "failed",
                "wav_open_count": 0,
                "wav_inspection_count": 0,
                "historical_wav_open_count": 0,
                "take": take.index,
                "variant": take.variant,
            }
        )

    for take in takes:
        parent = take.parent_times
        worker = take.worker.get("monotonic") or {}
        first = _ms(parent.get("requested"), parent.get("first_frames"))
        if take.variant == "portaudio-cold-start":
            add("cold_device_start", take, first)
        else:
            add("record_to_first_confirmed_frames", take, first)
        if take.stop_requested:
            add(
                "stop_reaches_controller",
                take,
                _ms(parent.get("stop_sent"), worker.get("stop_read")),
            )
            add(
                "stop_to_sealed_review", take, _ms(parent.get("stop_sent"), parent.get("validated"))
            )
    return out


def observations(takes: Sequence[TakeResult]) -> dict[str, dict[str, Any]]:
    """The scalar rows' observations, each naming the boundary it measured."""
    succeeded = [take for take in takes if take.success]
    readings: dict[str, dict[str, Any]] = {}
    if succeeded:
        readings["timer_frame_agreement"] = {
            "measured_boundary": (
                "the parent's confirmed frames (the worker's progress lines) against the "
                "frames of the published WAV and of its continuity evidence, every take"
            ),
            "sample_count": len(succeeded),
            "exact": all(
                take.confirmed_frames == take.frames == take.evidence_value("frames")
                for take in succeeded
            ),
        }
    growth = [
        take.worker["max_rss_bytes"] - take.worker["rss_before_bytes"]
        for take in takes
        if isinstance(take.worker.get("max_rss_bytes"), int)
        and isinstance(take.worker.get("rss_before_bytes"), int)
    ]
    if growth:
        readings["recorder_memory_growth"] = {
            "measured_boundary": (
                "the capture worker's peak resident memory over its resident memory before "
                "the stream opened (the recorder only: the app draws the waveform)"
            ),
            "sample_count": len(growth),
            "value_bytes": max(growth),
        }
    return readings


def host_facts(repository: Path, *, simulated: bool) -> dict[str, Any]:
    def run(arguments: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                arguments, cwd=repository, capture_output=True, text=True, check=False, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    facts: dict[str, Any] = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "git_revision": run(["git", "rev-parse", "HEAD"]),
        "macos_build": run(["sw_vers", "-buildVersion"]) if sys.platform == "darwin" else None,
        # The Swift app's fields: this is not the Swift app.
        "swift_configuration": None,
        "sanitizers": None,
        "capture": "voice-lab-desktop capture worker (PortAudio)",
    }
    if not simulated:
        try:
            import sounddevice

            facts["portaudio"] = sounddevice.get_portaudio_version()[1]
        except (ImportError, OSError):
            facts["portaudio"] = None
    return facts


def waveform_identity(takes: Sequence[TakeResult]) -> dict[str, Any] | None:
    entries = []
    for take in takes:
        if not take.success:
            continue
        clip = load_pcm_audio(
            take.directory / ORIGINAL_NAME, expected_sha256=take.evidence_value("wav_sha256")
        )
        entries.append(
            {
                "take": take.index,
                "bins": len(clip.waveform_bins),
                "min": min(low for low, _ in clip.waveform_bins),
                "max": max(high for _, high in clip.waveform_bins),
                "sha256": clip.source_sha256,
            }
        )
    if not entries:
        return None
    return {
        "source": "min/max envelopes computed from the written takes (cole_voice_lab."
        "desktop_audio.load_pcm_audio, bound to each take's SHA-256)",
        "takes": entries,
    }


def continuity_summary(takes: Sequence[TakeResult], tolerance: float) -> dict[str, Any]:
    deviations = [
        take.evidence_value("max_abs_deviation_frames")
        for take in takes
        if isinstance(take.evidence_value("max_abs_deviation_frames"), float | int)
    ]
    problems = []
    for take in takes:
        if not take.success:
            problems.append(f"take {take.index} ({take.variant}): {take.error}")
        elif not take.validation.get("ok"):
            problems.append(f"take {take.index}: re-validation failed")
    return {
        "takes": len(takes),
        "successful": sum(take.success for take in takes),
        "revalidated": sum(bool(take.validation.get("ok")) for take in takes),
        "tolerance_frames": tolerance,
        "max_abs_deviation_frames": max(deviations) if deviations else None,
        "input_overflows": sum(int(take.evidence_value("input_overflows") or 0) for take in takes),
        "input_underflows": sum(
            int(take.evidence_value("input_underflows") or 0) for take in takes
        ),
        "adc_time_valid": all(take.evidence_value("adc_time_valid") is not False for take in takes),
        "clean": bool(takes) and not problems,
        "problems": problems,
    }


def take_rows(takes: Sequence[TakeResult]) -> list[dict[str, Any]]:
    return [
        {
            "take": take.index,
            "variant": take.variant,
            "seconds": take.seconds,
            "directory": take.directory.name,
            "success": take.success,
            "error": take.error,
            "frames": take.frames,
            "callback_count": take.evidence_value("callback_count"),
            "max_abs_deviation_frames": take.evidence_value("max_abs_deviation_frames"),
            "input_overflows": take.evidence_value("input_overflows"),
            "input_underflows": take.evidence_value("input_underflows"),
            "adc_time_valid": take.evidence_value("adc_time_valid"),
            "stop_reason": take.evidence_value("stop_reason"),
            "revalidated": bool(take.validation.get("ok")),
        }
        for take in takes
    ]


def run_matrix(repository: Path, benchmark: Path, verification: Path, output: Path) -> Path:
    """``scripts/recording_acceptance_matrix.py`` over the two. Raises: ``HarnessError``."""
    script = repository / "scripts" / "recording_acceptance_matrix.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(script),
            "--benchmark-report",
            str(benchmark),
            "--verification-root",
            str(verification),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    matrix = output / "acceptance-matrix.json"
    if completed.returncode != 0 or not matrix.is_file():
        raise HarnessError(f"the acceptance matrix did not run: {completed.stderr.strip()[-500:]}")
    return matrix


def _number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return "-" if value is None else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    continuity = report["continuity"]
    lines = [
        "# S-1: PortAudio continuity evidence",
        "",
        f"Verdict: **{report['verdict']}**",
        "",
        f"- Created: {report['created_at']}",
        f"- Host: {report['host']['system']} {report['host']['release']} "
        f"({report['host']['machine']}), PortAudio host API {report['host_api']}",
        f"- Device: {report['device']} (simulated: {report['simulated']})",
        f"- Blocks of {report['block_frames']} frames; tolerance ±{report['tolerance_frames']:g} "
        "frames",
        f"- Takes: {continuity['takes']}, successful {continuity['successful']}, re-validated "
        f"{continuity['revalidated']}",
        "- Largest ADC deviation between blocks: "
        f"{_number(continuity['max_abs_deviation_frames'])} frames",
        f"- Overflows {continuity['input_overflows']}, underflows "
        f"{continuity['input_underflows']}, every block had an ADC time: "
        f"{continuity['adc_time_valid']}",
        "",
    ]
    if continuity["problems"]:
        lines += ["## Problems", "", *[f"- {problem}" for problem in continuity["problems"]], ""]
    lines += [
        "## Takes",
        "",
        "| Take | Variant | Seconds | Result | Max deviation (frames) | Overflows | Stop |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["takes"]:
        result = "ok" if row["success"] else f"failed: {row['error']}"
        lines.append(
            f"| {row['take']} | {row['variant']} | {row['seconds']:g} | {result} "
            f"| {_number(row['max_abs_deviation_frames'])} | {row['input_overflows']} "
            f"| {row['stop_reason']} |"
        )
    performance = report["performance"]
    lines += [
        "",
        "## Performance rows the harness measures",
        "",
        "| Row | Verdict | Observed | Target |",
        "| --- | --- | --- | --- |",
    ]
    for row_id, row in performance.items():
        lines.append(
            f"| {row_id} | {row['verdict']} | {row.get('observed') or '-'} "
            f"| {row.get('thresholds') or '-'} |"
        )
    matrix = report["matrix"]
    lines += [
        "",
        "## The acceptance matrix",
        "",
        f"`matrix/acceptance-matrix.md`: {matrix['outstanding']} rows outstanding.",
        "",
        "Answered by this harness: " + (", ".join(matrix["answered"]) or "none") + ".",
        "",
        "Left for the owner: " + OWNER_ROWS_NOTE,
        "",
    ]
    return "\n".join(lines)


def build_report(
    harness: Harness,
    *,
    repository: Path,
    bench: ModuleType,
    device: str,
    created_at: datetime,
) -> dict[str, Any]:
    takes = harness.takes
    simulated = harness.simulated is not None
    host_api = next(
        (str(take.evidence_value("host_api")) for take in takes if take.evidence_value("host_api")),
        device.rsplit("[", 1)[-1].rstrip("]"),
    )
    slug = "simulated" if simulated else host_api.lower().replace(" ", "-")
    host = host_facts(repository, simulated=simulated)
    measured = samples(takes, signal="simulated" if simulated else "device")
    readings = observations(takes)
    summaries = bench.summarize(measured)
    verdicts = bench.acceptance_verdicts(
        summaries, backend=f"portaudio-{slug}", observations=readings
    )
    benchmark = {
        "schema_version": BENCHMARK_VERSION,
        "backend": f"portaudio-{slug}",
        "profile": "s1",
        "created_at": created_at.isoformat(),
        "host": host,
        "samples": measured,
        "summaries": summaries,
        "observations": readings,
        "acceptance_rows": verdicts,
    }
    benchmark["release_acceptance_passed"] = bench.acceptance_passed(benchmark)
    benchmark_dir = _private_dir(harness.output / "benchmark")
    _write_json(benchmark_dir / "report.json", benchmark)

    continuity = continuity_summary(takes, harness.tolerance_frames)
    exercised = sorted(
        {take.variant for take in takes}
        | ({"portaudio-stop-requested"} if any(t.stop_requested for t in takes) else set())
    )
    identity: dict[str, Any] = {
        "capture_variants": exercised,
        "s1": {
            "hardware": not simulated,
            "simulated": simulated,
            "host_api": host_api,
            "device": device,
            "block_frames": harness.block_frames,
            "continuity": continuity,
            "takes": take_rows(takes),
        },
    }
    waveform = waveform_identity(takes)
    if waveform is not None:
        identity["waveform"] = waveform
    verification_dir = _private_dir(harness.output / "verification")
    _write_json(verification_dir / "verification.json", identity)

    matrix_path = run_matrix(
        repository, benchmark_dir / "report.json", verification_dir, harness.output / "matrix"
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    answered = [
        row["id"]
        for row in (*matrix["parity"], *matrix["performance"])
        if row["verdict"] in {"verified", "pass"}
    ]
    device_name = device.rsplit(" [", 1)[0]
    if simulated:
        verdict = SIMULATED_VERDICT
    elif continuity["clean"] and chain.is_umik1(device_name):
        verdict = "passed"
    else:
        verdict = "failed"
    return {
        "schema_version": REPORT_VERSION,
        "created_at": created_at.isoformat(),
        "verdict": verdict,
        "continuity_verdict": "clean" if continuity["clean"] else "failed",
        "simulated": simulated,
        "hardware": not simulated,
        "device": device,
        "host_api": host_api,
        "host": host,
        "block_frames": harness.block_frames,
        "tolerance_frames": harness.tolerance_frames,
        "plan": harness.plan.__dict__,
        "continuity": continuity,
        "takes": take_rows(takes),
        "performance": {
            row_id: {
                "verdict": entry["verdict"],
                "observed": entry.get("observed_ms") or entry.get("observed"),
                "thresholds": entry.get("thresholds_ms") or entry.get("thresholds"),
            }
            for row_id, entry in verdicts.items()
            if entry["verdict"] != "not-measured"
        },
        "matrix": {
            "path": "matrix/acceptance-matrix.json",
            "outstanding": len(matrix["outstanding"]),
            "acceptance_closed": matrix["acceptance_closed"],
            "answered": answered,
            "left_for_owner": [row for row in matrix["outstanding"] if row not in answered],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="s1-portaudio",
        description="Spike S-1: PortAudio continuity evidence from the UMIK-1 (D-040).",
    )
    ap.add_argument("--output", type=Path, required=True, help="new or empty 0700 directory")
    ap.add_argument("--takes", type=int, default=20, help="warm takes (default 20)")
    ap.add_argument("--seconds", type=float, default=3.0, help="warm take length")
    ap.add_argument("--cold-takes", type=int, default=20)
    ap.add_argument("--cold-seconds", type=float, default=1.0)
    ap.add_argument("--cold-idle-seconds", type=float, default=5.0)
    ap.add_argument("--long-takes", type=int, default=2)
    ap.add_argument("--long-seconds", type=float, default=60.0)
    ap.add_argument(
        "--max-duration-seconds",
        type=float,
        default=2.0,
        help="one take the worker stops by itself at this length (0: none)",
    )
    ap.add_argument("--block-frames", type=int, default=DEFAULT_BLOCK_FRAMES)
    ap.add_argument("--tolerance-frames", type=float, default=DEFAULT_TOLERANCE_FRAMES)
    ap.add_argument("--simulated", metavar="JSON", help="the simulated UMIK-1 (a dry run)")
    ap.add_argument("--repository", type=Path, help="the voice-lab checkout (default: found)")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    ap = build_parser()
    options = ap.parse_args(argv)
    counts = (options.takes, options.cold_takes, options.long_takes)
    if any(count < 0 for count in counts) or not any(counts):
        ap.error("the take counts must be >= 0, and at least one take is needed")
    lengths = (options.seconds, options.cold_seconds, options.long_seconds)
    if any(not 0 < value <= 600 for value in lengths):
        ap.error("take lengths must be in (0, 600] seconds")
    if not 0 <= options.max_duration_seconds <= 600:
        ap.error("--max-duration-seconds must be in [0, 600]")
    if options.cold_idle_seconds < 0:
        ap.error("--cold-idle-seconds must be >= 0")
    simulated = None
    if options.simulated is not None:
        try:
            simulated = json.loads(options.simulated)
        except ValueError as exc:
            ap.error(f"--simulated: {exc}")
        if not isinstance(simulated, dict):
            ap.error("--simulated must be a JSON object")
    repository = options.repository or find_repository(Path(__file__))
    if repository is None:
        ap.error("the voice-lab checkout was not found; pass --repository")
    repository = repository.resolve()
    try:
        bench = load_benchmark(repository)
        output = prepare_output(options.output, repository)
        harness = Harness(
            output,
            plan=Plan(
                takes=options.takes,
                seconds=options.seconds,
                cold_takes=options.cold_takes,
                cold_seconds=options.cold_seconds,
                cold_idle_seconds=options.cold_idle_seconds,
                long_takes=options.long_takes,
                long_seconds=options.long_seconds,
                max_duration_seconds=options.max_duration_seconds,
            ),
            block_frames=options.block_frames,
            tolerance_frames=options.tolerance_frames,
            simulated=simulated,
        )
        device = harness.recorder().device_label()
        print(f"S-1 on {device}{' (simulated)' if simulated is not None else ''}; output {output}")
        created_at = datetime.now(UTC)
        harness.run()
        report = build_report(
            harness, repository=repository, bench=bench, device=device, created_at=created_at
        )
    except (HarnessError, DeviceError, ValueError) as exc:
        print(f"s1-portaudio: {exc}", file=sys.stderr)
        return 2
    _write_json(output / "s1-report.json", report)
    descriptor = os.open(
        output / "s1-report.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(render_markdown(report))
    print(f"S-1 verdict: {report['verdict']} (continuity {report['continuity_verdict']})")
    print(f"report: {output / 's1-report.md'}")
    return 0 if report["continuity_verdict"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
