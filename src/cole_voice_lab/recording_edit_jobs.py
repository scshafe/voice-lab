"""Owned asynchronous render jobs for canonical native frame recipes.

@canonical recording_edit_jobs -- recipes are durable before scheduling; one
bounded child renders one selected original and parent loss preserves source
and incomplete private artifacts. Late results never replace a newer recipe.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, model_validator

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    write_canonical_json,
)
from .errors import ContractError, SecurityError
from .models import ContractModel, Sha256, utc_now
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    MAX_RECORDING_FRAMES,
    DerivativeEvent,
    RecordingError,
    RecordingFileIdentity,
    ReviewActionEvent,
)
from .recording_edits import (
    RecordingEditDerivative,
    RecordingEditRecipe,
    RecordingLegacyEditIntent,
    RecordingLegacyEditResult,
    _legacy_key,
    current_native_edit,
    legacy_action_event,
    legacy_edit_intent,
    legacy_edit_result,
    legacy_intent_directory,
    materialize_legacy_edit,
    native_edit_derivative,
    native_edit_directory,
    render_native_edit,
)
from .recording_takes import RecordingTakeStore, read_recording_evidence

MAX_RENDER_QUEUE = 16
MAX_RENDER_FRAME = 65_536
# Derivative rendering must never consume the space an active capture needs, so this
# reserve subsumes the native writer's own requirement rather than sitting beside it.
DERIVATIVE_RENDER_RESERVE_BYTES = MAX_RECORDING_FRAMES * 3 + 256 * 1024 * 1024
LOW_SPACE_RETRY_SECONDS = 30.0
_WAV_HEADER_BYTES = 44


class RecordingEditJob(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-edit-job/v1"] = (
        "cole-voice-lab.recording-edit-job/v1"
    )
    recipe: RecordingEditRecipe
    recipe_sha256: Sha256
    source_identity: RecordingFileIdentity

    @model_validator(mode="after")
    def _exact_recipe(self) -> RecordingEditJob:
        if self.recipe_sha256 != canonical_sha256(self.recipe):
            raise ValueError("render job must bind its canonical recipe")
        return self


class RecordingLegacyEditJob(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-legacy-edit-job/v1"] = (
        "cole-voice-lab.recording-legacy-edit-job/v1"
    )
    intent: RecordingLegacyEditIntent
    recipe_sha256: Sha256

    @model_validator(mode="after")
    def _intent_binding(self) -> RecordingLegacyEditJob:
        if self.recipe_sha256 != canonical_sha256(self.intent):
            raise ValueError("legacy render job must bind its durable intent")
        return self


class RecordingEditJobReply(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-edit-job-reply/v1"] = (
        "cole-voice-lab.recording-edit-job-reply/v1"
    )
    recipe_sha256: Sha256
    result: RecordingEditDerivative | None = None
    error: RecordingError | None = None
    legacy_result: RecordingLegacyEditResult | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> RecordingEditJobReply:
        if sum(value is not None for value in (self.result, self.error, self.legacy_result)) != 1:
            raise ValueError("render job reply requires one result or error")
        if self.result is not None and self.result.recipe_sha256 != self.recipe_sha256:
            raise ValueError("rendered result must identify its requested recipe")
        if (
            self.legacy_result is not None
            and self.legacy_result.intent_sha256 != self.recipe_sha256
        ):
            raise ValueError("legacy result must identify its requested intent")
        return self


def _frame(value: ContractModel) -> bytes:
    payload = canonical_json_bytes(value)
    if not 0 < len(payload) <= MAX_RENDER_FRAME:
        raise ContractError("render frame exceeds its bound")
    return len(payload).to_bytes(4, "big") + payload


def _read_frame(fd: int) -> bytes:
    def exact(count: int) -> bytes:
        result = b""
        while len(result) < count:
            chunk = os.read(fd, count - len(result))
            if not chunk:
                raise ContractError("render frame ended before its declared boundary")
            result += chunk
        return result

    size = int.from_bytes(exact(4), "big")
    if not 0 < size <= MAX_RENDER_FRAME:
        raise ContractError("render frame exceeds its bound")
    return exact(size)


class RecordingEditCoordinator:
    """Bounded metadata supervision; no PCM decoding or rendering occurs in this process."""

    def __init__(
        self,
        store: RecordingTakeStore,
        *,
        published: Callable[[DerivativeEvent | ReviewActionEvent], None],
        failed: Callable[[RecordingError], None] | None = None,
        available_bytes: Callable[[], int] | None = None,
    ) -> None:
        self.store, self.published = store, published
        self.failed = failed
        self._available_bytes = available_bytes or (
            lambda: shutil.disk_usage(store.layout.directory("derived")).free
        )
        self._low_space = False
        self._retry_after: float | None = None
        self._queue: queue.Queue[str] = queue.Queue(MAX_RENDER_QUEUE)
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._rerun: set[str] = set()
        self._discovery: Iterator[Path] | None = None
        self._rescan = True
        self._stop = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        directory = store._directory("provenance/recording-edits/native")
        self._owner_fd = os.open(
            directory / ".render-owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(self._owner_fd)
            if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise SecurityError("render ownership must remain one private regular file")
            fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self._owner_fd)
            raise
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="recording-edit-supervisor"
        )
        self._worker.start()

    def submit(self, take_id: str) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if take_id in self._pending:
                self._rerun.add(take_id)
                return
            if self._queue.full():
                self._rescan = True
                return  # The durable current recipe is found by the next bounded metadata pass.
            self._pending.add(take_id)
            self._queue.put_nowait(take_id)

    def _discover(self) -> None:
        if self._discovery is None:
            with self._lock:
                if not self._rescan:
                    return
                self._rescan = False
            directory = confined_path(self.store.layout.root, "provenance/recording-edits/native")
            legacy = confined_path(self.store.layout.root, "provenance/recording-edits/legacy")
            from itertools import chain

            self._discovery = iter(
                chain(directory.iterdir(), legacy.iterdir() if legacy.exists() else ())
            )
        while not self._stop.is_set() and not self._queue.full():
            try:
                child = next(self._discovery)
            except StopIteration:
                self._discovery = None
                return
            if child.name.startswith("take-") and child.is_dir() and not child.is_symlink():
                self.submit(child.name)
            elif (
                child.parent.name == "legacy"
                and not child.name.startswith(".")
                and child.is_dir()
                and not child.is_symlink()
            ):
                self.submit("legacy:" + child.name)

    def _derivative_bytes(self, take_id: str) -> int:
        """Exact output size of one queued render, by metadata only.

        Never routes through the take store: that takes the metadata flock and would
        contend with the interactive command path. A take that will not render — no
        recipe, an untrimmed range, an existing derivative, or already-burned evidence —
        costs nothing, and any unreadable identity contributes nothing rather than
        charging this job for another take's problem.
        """

        try:
            if take_id.startswith("legacy:"):
                intent = read_recording_evidence(
                    legacy_intent_directory(self.store.layout, take_id[len("legacy:") :])
                    / "head.json",
                    RecordingLegacyEditIntent,
                )
                if legacy_edit_result(self.store.layout, intent) is not None:
                    return 0
                span = intent.source.frame_range
                # A full-range legacy edit publishes no derivative at all; budgeting it
                # would refuse pure metadata remove/restore/reject on a full disk.
                if (span.start_frame, span.end_frame) == (0, span.source_frames):
                    return 0
                return _WAV_HEADER_BYTES + 3 * (span.end_frame - span.start_frame)
            recipe = current_native_edit(self.store.layout, take_id)
            if recipe is None or not recipe.trimmed:
                return 0
            if native_edit_derivative(self.store.layout, take_id) is not None:
                return 0
            failure = (
                native_edit_directory(self.store.layout, take_id)
                / f"{canonical_sha256(recipe)}.failure.json"
            )
            if failure.exists():
                return 0
            span = recipe.frame_range
            return _WAV_HEADER_BYTES + 3 * (span.end_frame - span.start_frame)
        except (ContractError, SecurityError, OSError, ValueError):
            return 0

    def _space_for(self) -> tuple[bool, int, int, int]:
        """Decide whether every queued render still fits above the reserve.

        The take under test is already in the queued set, so it is summed exactly once.
        A probe that cannot read the filesystem refuses rather than rendering blind.
        """

        with self._lock:
            queued = sorted(self._pending)
        required = sum(self._derivative_bytes(item) for item in queued)
        required += DERIVATIVE_RENDER_RESERVE_BYTES
        try:
            free = int(self._available_bytes())
        except OSError:
            return False, 0, required, len(queued)
        return free >= required, free, required, len(queued)

    def _refuse_for_space(self, free: int, required: int, queued: int) -> None:
        """Defer without burning the recipe, and say so once per low-space episode."""

        with self._lock:
            self._retry_after = time.monotonic() + LOW_SPACE_RETRY_SECONDS
            announce = not self._low_space
            self._low_space = True
        if announce and self.failed is not None:
            self.failed(
                RecordingError(
                    code="RECORDING.DISK_SPACE",
                    message=(
                        f"Insufficient private storage: {free // (1024 * 1024)} MiB available, "
                        f"{required // (1024 * 1024)} MiB required for {queued} queued trim "
                        "render(s) and their reserve; originals and trim decisions are retained"
                    ),
                    retryable=True,
                    details=(
                        f"free_bytes={free}",
                        f"required_bytes={required}",
                        f"queued_renders={queued}",
                    ),
                )
            )

    def _job(self, take_id: str) -> RecordingEditJob | RecordingLegacyEditJob | None:
        if take_id.startswith("legacy:"):
            key = take_id.removeprefix("legacy:")
            intent = read_recording_evidence(
                legacy_intent_directory(self.store.layout, key) / "head.json",
                RecordingLegacyEditIntent,
            )
            if legacy_edit_result(self.store.layout, intent) is not None:
                return None
            return RecordingLegacyEditJob(intent=intent, recipe_sha256=canonical_sha256(intent))
        take = self.store.get_take(take_id)
        recipe = current_native_edit(self.store.layout, take_id)
        if (
            recipe is None
            or not recipe.trimmed
            or native_edit_derivative(self.store.layout, take_id) is not None
        ):
            return None
        error_path = (
            native_edit_directory(self.store.layout, take_id)
            / f"{canonical_sha256(recipe)}.failure.json"
        )
        if error_path.exists():
            return (
                None  # Explicit re-edit creates a fresh revision; failed evidence remains visible.
            )
        if (
            take.seal is None
            or take.edit_revision != recipe.edit_revision
            or take.frame_range != recipe.frame_range
        ):
            raise SecurityError("render source is not the current canonical take revision")
        return RecordingEditJob(
            recipe=recipe,
            recipe_sha256=canonical_sha256(recipe),
            source_identity=self.store._source_identity(take.seal.original_path),
        )

    def _run_job(self, job: RecordingEditJob | RecordingLegacyEditJob) -> RecordingEditJobReply:
        with self._lock:
            if self._stop.is_set():
                raise ContractError("render supervisor is closing")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "cole_voice_lab.recording_edit_jobs",
                    "--worker",
                    "--asset-root",
                    str(self.store.layout.root),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._process = process
        assert process.stdin is not None and process.stdout is not None
        payload = _frame(job)
        offset = 0
        output = bytearray()
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        try:
            while True:
                if self._stop.is_set():
                    raise ContractError("render job interrupted by owning service shutdown")
                if offset < len(payload):
                    try:
                        offset += os.write(process.stdin.fileno(), payload[offset:])
                    except BlockingIOError:
                        pass
                try:
                    chunk = os.read(process.stdout.fileno(), 4096)
                except BlockingIOError:
                    chunk = None
                if chunk:
                    output.extend(chunk)
                    if len(output) > MAX_RENDER_FRAME + 4:
                        raise ContractError("render reply exceeds its frame bound")
                    if len(output) >= 4:
                        declared = int.from_bytes(output[:4], "big")
                        if not 0 < declared <= MAX_RENDER_FRAME or len(output) > declared + 4:
                            raise ContractError("render reply has an invalid or extra frame")
                if chunk == b"" and process.poll() is not None:
                    break
                self._stop.wait(0.01)
            if process.returncode != 0 or offset != len(payload):
                raise ContractError("render worker stopped before a complete reply")
            if len(output) < 4 or len(output) != int.from_bytes(output[:4], "big") + 4:
                raise ContractError("render worker returned a truncated frame")
            reply = RecordingEditJobReply.model_validate_json(bytes(output[4:]))
            if reply.recipe_sha256 != job.recipe_sha256:
                raise SecurityError("render worker returned a mismatched result")
            return reply
        finally:
            process.stdin.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.5)
            process.stdout.close()
            with self._lock:
                self._process = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                take_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    # A refused take is only rediscovered if discovery is re-armed, and
                    # only after a delay: re-arming every idle tick would rescan both
                    # provenance trees at 10 Hz while the shortage lasts.
                    if self._retry_after is not None and time.monotonic() >= self._retry_after:
                        self._retry_after = None
                        self._rescan = True
                try:
                    self._discover()
                except (ContractError, SecurityError, OSError, ValueError):
                    if self.failed is not None:
                        self.failed(
                            RecordingError(
                                code="RECORDING.EDIT_FAILED",
                                message="Trim recovery metadata is unavailable",
                                retryable=True,
                            )
                        )
                continue
            job = None
            try:
                job = self._job(take_id)
                if job is not None:
                    fits, free, required, queued = self._space_for()
                    if not fits:
                        self._refuse_for_space(free, required, queued)
                        continue
                    reply = self._run_job(job)
                    if isinstance(job, RecordingLegacyEditJob):
                        intent = job.intent
                        if (
                            legacy_edit_intent(
                                self.store.layout,
                                subject_id=intent.source.subject_id,
                                session_id=intent.session_id,
                                prompt_id=intent.source.prompt.prompt_id,
                                source_sha256=intent.source.source_sha256,
                            )
                            != intent
                        ):
                            continue
                        if reply.legacy_result is not None:
                            self.published(legacy_action_event(intent, reply.legacy_result))
                        elif reply.error is not None:
                            self._failure(job, reply.error)
                        else:
                            raise SecurityError("legacy child returned a native derivative")
                        continue
                    if current_native_edit(self.store.layout, take_id) != job.recipe:
                        continue
                    if reply.result is not None:
                        result = reply.result
                        with self._lock:
                            self._low_space = False
                        self.published(
                            DerivativeEvent(
                                take_id=take_id,
                                source_sha256=result.source_sha256,
                                edit_revision=result.edit_revision,
                                recipe_sha256=result.recipe_sha256,
                                state="ready",
                                derived_path=result.derived_path,
                                derived_sha256=result.derived_sha256,
                            )
                        )
                    else:
                        assert reply.error is not None
                        self._failure(job, reply.error)
            except (ContractError, SecurityError, OSError, ValueError) as error:
                if not self._stop.is_set():
                    failure = RecordingError(
                        code="RECORDING.EDIT_FAILED",
                        message="Trim preparation failed; original recording is preserved",
                        retryable=True,
                        details=(type(error).__name__,),
                    )
                    if job is not None:
                        self._failure(job, failure)
                    elif self.failed is not None:
                        self.failed(failure)
            finally:
                with self._lock:
                    self._pending.discard(take_id)
                    rerun = take_id in self._rerun
                    self._rerun.discard(take_id)
                self._queue.task_done()
                if rerun:
                    self.submit(take_id)

    def _failure(
        self, job: RecordingEditJob | RecordingLegacyEditJob, error: RecordingError
    ) -> None:
        if error.code == "RECORDING.DISK_SPACE":
            # A full disk is transient. Durable failure evidence would refuse this exact
            # recipe forever, and the operator cannot resend an unchanged trim.
            _, free, required, queued = self._space_for()
            self._refuse_for_space(free, required, queued)
            return
        if isinstance(job, RecordingLegacyEditJob):
            intent = job.intent
            if (
                legacy_edit_intent(
                    self.store.layout,
                    subject_id=intent.source.subject_id,
                    session_id=intent.session_id,
                    prompt_id=intent.source.prompt.prompt_id,
                    source_sha256=intent.source.source_sha256,
                )
                != intent
            ):
                return
            result = RecordingLegacyEditResult(intent_sha256=job.recipe_sha256, error=error)
            write_canonical_json(
                legacy_intent_directory(self.store.layout, _legacy_key(intent))
                / f"{intent.request.operation_id}.result.json",
                result,
            )
            self.published(legacy_action_event(intent, result))
            return
        if current_native_edit(self.store.layout, job.recipe.take_id) != job.recipe:
            return
        event = DerivativeEvent(
            take_id=job.recipe.take_id,
            source_sha256=job.recipe.source_sha256,
            edit_revision=job.recipe.edit_revision,
            recipe_sha256=job.recipe_sha256,
            state="failed",
            error=error,
        )
        write_canonical_json(
            native_edit_directory(self.store.layout, job.recipe.take_id)
            / f"{job.recipe_sha256}.failure.json",
            event,
        )
        self.published(event)

    def close(self) -> None:
        self._stop.set()
        self._worker.join(1.5)
        if self._worker.is_alive():
            raise ContractError("render supervisor did not stop within its deadline")
        if self._owner_fd >= 0:
            os.close(self._owner_fd)
            self._owner_fd = -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    args = parser.parse_args()
    if not stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode):
        raise ContractError("render worker requires its parent's inherited local pipe")
    job: RecordingEditJob | RecordingLegacyEditJob = TypeAdapter(
        RecordingEditJob | RecordingLegacyEditJob
    ).validate_json(_read_frame(sys.stdin.fileno()))

    def parent() -> None:
        while os.read(sys.stdin.fileno(), 1):
            pass
        os._exit(0)

    threading.Thread(target=parent, daemon=True, name="recording-edit-parent").start()
    try:
        layout = AssetLayout.validate(args.asset_root)
        if isinstance(job, RecordingLegacyEditJob):
            legacy_result = materialize_legacy_edit(layout, job.intent)
            reply = RecordingEditJobReply(
                recipe_sha256=job.recipe_sha256, legacy_result=legacy_result
            )
        else:
            result = render_native_edit(
                layout,
                confined_path(layout.root, job.source_identity.relative_path, must_exist=True),
                job.recipe,
                source_identity=job.source_identity,
                completed_at=utc_now(),
            )
            reply = RecordingEditJobReply(recipe_sha256=job.recipe_sha256, result=result)
    except (ContractError, SecurityError, OSError, ValueError, wave.Error) as error:
        # A storage shortage is transient and must stay distinguishable from a render
        # that genuinely cannot succeed; only the latter earns durable failure evidence.
        out_of_space = isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}
        reply = RecordingEditJobReply(
            recipe_sha256=job.recipe_sha256,
            error=RecordingError(
                code="RECORDING.DISK_SPACE" if out_of_space else "RECORDING.EDIT_FAILED",
                message=(
                    "Trim render ran out of private storage; original recording is preserved"
                    if out_of_space
                    else "Trim render failed; original recording is preserved"
                ),
                retryable=True,
            ),
        )
    sys.stdout.buffer.write(_frame(reply))
    sys.stdout.buffer.flush()
    return 0


RECORDING_EDIT_JOB_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "recording-edit-job": RecordingEditJob,
    "recording-legacy-edit-job": RecordingLegacyEditJob,
    "recording-edit-job-reply": RecordingEditJobReply,
}


if __name__ == "__main__":
    raise SystemExit(main())
