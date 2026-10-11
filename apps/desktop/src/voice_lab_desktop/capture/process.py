"""The parent side of the capture worker: one process per take (D-039).

``WorkerRecorder`` is the app's ``TakeRecorder``. ``start`` spawns the worker
(``worker.py``) on a new, private take directory and returns a ``WorkerTake``, whose
reader threads follow the worker's progress lines (``confirmed_frames``, D-014) and its
stderr (the summary line). ``wait`` validates the evidence and the WAV
(``evidence.validate_take``) and only then publishes ``original.wav``; a worker that
dies, writes anything but progress lines, or exits without a valid, successful take is a
``CaptureFailed`` that keeps what was written.

The worker runs as ``python -I -B -c <bootstrap> <package root> ...`` so that it imports
this very package whether it is installed (uv tool) or run from the source tree (tests,
the S-1 harness); in a frozen app bundle it is the bundle's own executable with
``--capture-worker``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Final

from cole_voice_lab.models import utc_now

from ..measurement.devices import choose_umik
from .backends import PortAudioTimedBackend, SimulatedSpec, SimulatedTimedBackend, TimedBackend
from .evidence import validate_take
from .timing import DEFAULT_TOLERANCE_FRAMES
from .types import EVIDENCE_NAME, FRAME_BYTES, PARTIAL_NAME, CapturedTake, CaptureFailed
from .worker import DEFAULT_BLOCK_FRAMES, MAX_CAPTURE_SECONDS, SUMMARY_PREFIX

TRACE_NAME: Final = "trace.jsonl"
_PROGRESS: Final = re.compile(rb"[1-9][0-9]{0,9}\n\Z")
_BOOTSTRAP: Final = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from voice_lab_desktop.capture.worker import main; raise SystemExit(main(sys.argv[1:]))"
)
#: Lines of the worker's stderr kept for a failure message.
_STDERR_LINES: Final = 40


def worker_command(arguments: list[str]) -> list[str]:
    """The command that runs the worker with ``arguments``."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--capture-worker", *arguments]
    package_root = Path(__file__).resolve().parents[2]
    return [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, str(package_root), *arguments]


class WorkerTake:
    """A take being recorded by a worker process (``RunningTake``)."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        take_dir: Path,
        requested_at: datetime,
        *,
        requested_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.process = process
        self.take_dir = take_dir
        self.requested_at = requested_at
        self.monotonic = monotonic
        #: Parent-side monotonic times: the start request, the first confirmed frames,
        #: the stop request, the validated take (the S-1 harness's measurements).
        self.times: dict[str, float | None] = {
            "requested": monotonic() if requested_monotonic is None else requested_monotonic,
            "first_frames": None,
            "stop_sent": None,
            "validated": None,
        }
        self.summary: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._bytes = 0
        self._protocol_error: str | None = None
        self._stderr: list[str] = []
        self._result: CapturedTake | None = None
        self._failure: CaptureFailed | None = None
        self._stopped = False
        assert process.stdout is not None and process.stderr is not None
        self._readers = [
            threading.Thread(
                target=self._read_progress, args=(process.stdout,), name="capture-progress"
            ),
            threading.Thread(
                target=self._read_stderr, args=(process.stderr,), name="capture-stderr"
            ),
        ]
        for reader in self._readers:
            reader.daemon = True
            reader.start()

    @property
    def confirmed_frames(self) -> int:
        with self._lock:
            return self._bytes // FRAME_BYTES

    @property
    def done(self) -> bool:
        return self.process.poll() is not None

    def _read_progress(self, stream: IO[bytes]) -> None:
        for line in stream:
            if not _PROGRESS.match(line) or int(line) % FRAME_BYTES:
                with self._lock:
                    if self._protocol_error is None:
                        self._protocol_error = f"the worker wrote {line[:40]!r} as progress"
                # A worker that breaks the protocol is not asked to stop; it is ended.
                self.kill()
                continue
            with self._lock:
                if self._bytes == 0:
                    self.times["first_frames"] = self.monotonic()
                self._bytes += int(line)

    def _read_stderr(self, stream: IO[bytes]) -> None:
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith(SUMMARY_PREFIX):
                try:
                    parsed = json.loads(line[len(SUMMARY_PREFIX) :])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    self.summary = parsed
                    continue
            self._stderr = [*self._stderr, line][-_STDERR_LINES:]

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self.times["stop_sent"] = self.monotonic()
        try:
            assert self.process.stdin is not None
            self.process.stdin.write(b"q")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # the worker has exited already

    def kill(self) -> None:
        """End the worker at once (the app is closing); the take fails, its files stay."""
        if self.process.poll() is None:
            self.process.kill()

    def wait(self, timeout: float | None = None) -> CapturedTake:
        if self._result is not None:
            return self._result
        if self._failure is not None:
            raise self._failure
        try:
            self.process.wait(timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("the capture worker is still running") from exc
        for reader in self._readers:
            reader.join(timeout=5)
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self._result = self._finish()
        except CaptureFailed as failure:
            self._failure = failure
            raise
        self.times["validated"] = self.monotonic()
        return self._result

    def _finish(self) -> CapturedTake:
        partial = self.take_dir / PARTIAL_NAME
        evidence = self.take_dir / EVIDENCE_NAME
        kept = {
            "partial": partial if os.path.lexists(partial) else None,
            "evidence": evidence if os.path.lexists(evidence) else None,
        }
        if self._protocol_error is not None:
            raise CaptureFailed(f"the take was not kept: {self._protocol_error}", **kept)
        if self.process.returncode != 0:
            reason = (self.summary or {}).get("error") or (
                self._stderr[-1] if self._stderr else f"exit status {self.process.returncode}"
            )
            raise CaptureFailed(f"the take was not kept: {reason}", **kept)
        if self.summary is None or self.summary.get("success") is not True:
            raise CaptureFailed("the take was not kept: the worker reported no result", **kept)
        return validate_take(
            self.take_dir,
            progress_frames=self.confirmed_frames,
            requested_at=self.requested_at,
        )


class WorkerRecorder:
    """Starts takes in the capture worker (``TakeRecorder``)."""

    def __init__(
        self,
        *,
        simulated: Mapping[str, Any] | None = None,
        max_seconds: float = MAX_CAPTURE_SECONDS,
        block_frames: int = DEFAULT_BLOCK_FRAMES,
        tolerance_frames: float = DEFAULT_TOLERANCE_FRAMES,
        trace: bool = False,
        queue_blocks: int | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.simulated = dict(simulated) if simulated is not None else None
        self.max_seconds = max_seconds
        self.block_frames = block_frames
        self.tolerance_frames = tolerance_frames
        self.trace = trace
        self.queue_blocks = queue_blocks
        self.clock = clock

    def backend(self) -> TimedBackend:
        if self.simulated is not None:
            return SimulatedTimedBackend(SimulatedSpec.from_json(self.simulated))
        return PortAudioTimedBackend()

    def device_label(self) -> str:
        return choose_umik(self.backend().list_inputs()).describe()

    def arguments(self, take_dir: Path) -> list[str]:
        arguments = [
            "--output",
            str(take_dir / PARTIAL_NAME),
            "--evidence",
            str(take_dir / EVIDENCE_NAME),
            "--max-seconds",
            repr(self.max_seconds),
            "--block-frames",
            str(self.block_frames),
            "--tolerance-frames",
            repr(self.tolerance_frames),
        ]
        if self.queue_blocks is not None:
            arguments += ["--queue-blocks", str(self.queue_blocks)]
        if self.trace:
            arguments += ["--trace", str(take_dir / TRACE_NAME)]
        if self.simulated is not None:
            arguments += ["--simulated", json.dumps(self.simulated, sort_keys=True)]
        return arguments

    def start(self, take_dir: Path) -> WorkerTake:
        requested_at = self.clock()
        requested_monotonic = time.monotonic()
        try:
            process = subprocess.Popen(
                worker_command(self.arguments(take_dir)),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except OSError as exc:
            raise CaptureFailed(f"the capture worker did not start: {exc}") from exc
        return WorkerTake(process, take_dir, requested_at, requested_monotonic=requested_monotonic)
