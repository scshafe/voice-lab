"""The resident model process, seen from the job runner (D-046, docs/SPEECH_API.md §7).

@canonical speech.resident -- ``ResidentProcess`` starts ``f5_resident.py`` in the runtime's
interpreter, in a session of its own, sends it the load request and waits for ``ready``;
then sends one render request at a time and waits for its answer, each with a deadline. A
deadline that passes kills the whole process group (the model is then unloaded). Text
travels through the pipe, never a file. The child's stderr, where F5-TTS prints (sometimes
the text it renders), is discarded.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

MAX_LINE_BYTES: Final = 64 * 1024


class ResidentError(Exception):
    """The resident process failed, answered nonsense, or missed its deadline."""

    def __init__(self, message: str, *, timed_out: bool = False, segment_only: bool = False):
        super().__init__(message)
        self.timed_out = timed_out
        self.segment_only = segment_only


class ResidentProcess:
    """One running resident worker."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        load: Mapping[str, Any],
        environment: Mapping[str, str],
        cwd: Path,
        startup_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock
        self._buffer = b""
        self._requests = 0
        # The worker's own report of its peak accelerator memory, when it gives one.
        self.peak_memory_bytes: int | None = None
        try:
            self.process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=cwd,
                env=dict(environment),
            )
        except OSError as error:
            raise ResidentError(f"the resident worker could not start: {error}") from error
        try:
            self._write(dict(load))
            ready = self._read(self.clock() + startup_seconds)
        except ResidentError:
            self.kill()
            raise
        if ready.get("event") != "ready":
            self.kill()
            raise ResidentError(f"the resident worker could not load: {ready.get('error', ready)}")
        self.ready = ready

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def _write(self, message: Mapping[str, Any]) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        try:
            stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            stdin.flush()
        except OSError as error:
            raise ResidentError("the resident worker stopped listening") from error

    def _read(self, deadline: float) -> dict[str, Any]:
        stdout = self.process.stdout
        assert stdout is not None
        descriptor = stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - self.clock()
            if remaining <= 0:
                self.kill()
                raise ResidentError("the resident worker missed its deadline", timed_out=True)
            ready, _, _ = select.select([descriptor], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = os.read(descriptor, 65536)
            if not chunk:
                self.kill()
                raise ResidentError("the resident worker exited")
            self._buffer += chunk
            if len(self._buffer) > MAX_LINE_BYTES:
                self.kill()
                raise ResidentError("the resident worker answered more than one line can hold")
        line, _, self._buffer = self._buffer.partition(b"\n")
        try:
            answer = json.loads(line)
        except ValueError as error:
            self.kill()
            raise ResidentError("the resident worker answered something that is not JSON") from (
                error
            )
        if not isinstance(answer, dict):
            self.kill()
            raise ResidentError("the resident worker answered something that is not an object")
        return answer

    def render(
        self, text: str, *, seed: int, nfe_steps: int, output: Path, deadline_seconds: float
    ) -> int:
        """Render one segment into ``output``; return its frames.

        Raises: ``ResidentError`` (``timed_out`` when the deadline passed; ``segment_only``
        when only this segment failed and the worker is still loaded).
        """
        self._requests += 1
        identifier = self._requests
        self._write(
            {
                "op": "render",
                "id": identifier,
                "text": text,
                "seed": seed,
                "nfe_steps": nfe_steps,
                "output_path": str(output),
            }
        )
        answer = self._read(self.clock() + deadline_seconds)
        if answer.get("id") != identifier:
            self.kill()
            raise ResidentError("the resident worker answered another request")
        if answer.get("event") == "rendered" and isinstance(answer.get("frames"), int):
            peak = answer.get("peak_memory_bytes")
            if isinstance(peak, int) and not isinstance(peak, bool) and peak >= 0:
                self.peak_memory_bytes = max(peak, self.peak_memory_bytes or 0)
            return int(answer["frames"])
        raise ResidentError(
            f"the segment failed in the resident worker: {str(answer.get('error'))[:200]}",
            segment_only=True,
        )

    def close(self) -> None:
        """Ask the worker to quit, then make sure its whole group is gone."""
        if self.alive:
            try:
                self._write({"op": "quit"})
                self.process.wait(timeout=5)
            except (ResidentError, subprocess.TimeoutExpired):
                pass
        self.kill()

    def kill(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:  # pragma: no cover - a broken pipe on close
                    pass
