"""Takes recorded here first, then sent to the PC in order (D-052, RECORDING-V1.md step 5).

``<data dir>/corpus/`` (directories 0700, records 0600, canonical JSON written atomically):

- ``recording/<rec-…>/``: a take being recorded. ``new_recording_dir`` makes it new, empty
  and private; the capture worker writes ``original.wav`` and ``continuity.json`` into it
  (D-039).
- ``takes/<take_id>/``: a saved take, named by its audio's SHA-256 (``take-<sha256>``).
  ``save_take`` checks the worker's evidence against the WAV, builds the take package once
  into ``package/`` (``cole_voice_lab.corpus_takes.build_take_package``: it is kept and sent
  as built, since a rebuilt manifest is another package under the same take id), writes
  ``take.json`` (``LocalTake``) and ``state.json`` (``TakeSync``, with the upload queued), and
  only then renames the directory into ``takes/``: Save is durable on this machine at once.
  A take of the simulated device is never packaged or sent (the PC refuses its audio): it is
  local-only, and so is a take that could not be packaged (with a notice saying why).
- ``notices.json`` (``Notices``) and ``sync.json`` (``SyncRecord``).

The operator's changes (``decide``, ``restore``, ``set_trim``) change the state here and, for
a take that is sent, append an operation to its outbox with a request id made once and the
revision it expects, chained on the change queued before it. The sync engine (``sync.py``)
takes each take's operations off the front, one at a time, and records what the PC did
(``upload_accepted``, ``change_applied``, ``adopt``, ...). A take's ``state.json`` is
rewritten whole under one lock for every change, so the GUI thread and the sync worker never
lose each other's writes.
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.canonical import (
    canonical_sha256,
    fsync_directory,
    read_canonical_model,
    rename_no_replace,
    write_canonical_json,
)
from cole_voice_lab.corpus_read_model import LibraryTake
from cole_voice_lab.corpus_requests import check_decision
from cole_voice_lab.corpus_take_store import TakeDecisionName
from cole_voice_lab.corpus_takes import (
    TAKE_ID_PATTERN,
    TakeClient,
    build_take_package,
    take_id_for,
    validate_take_package,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import CaptureDevice, Prompt, utc_now
from cole_voice_lab.recording_contracts import CatalogReference

from .. import __version__
from ..capture.evidence import read_evidence
from ..capture.types import EVIDENCE_NAME, ORIGINAL_NAME, CapturedTake
from .files import private_dir
from .records import (
    JOURNAL_LIMIT,
    NOTICE_LIMIT,
    ChangeOp,
    DecisionEvent,
    DecisionOp,
    Finished,
    LocalDecision,
    LocalTake,
    LocalTrim,
    Notice,
    NoticeKind,
    Notices,
    Operation,
    SyncRecord,
    TakeSync,
    TrimOp,
    UploadOp,
    UploadReceipt,
    new_notice_id,
    new_request_id,
)

TAKES: Final = "takes"
RECORDING: Final = "recording"
PACKAGE: Final = "package"
TAKE_FILE: Final = "take.json"
STATE_FILE: Final = "state.json"
NOTICES_FILE: Final = "notices.json"
SYNC_FILE: Final = "sync.json"
SIMULATED: Final = "simulated: the simulated capture device's audio never leaves this machine"
CLIENT_NAME: Final = "voice-lab-desktop"
MAX_TAKE_BYTES: Final = 64 * 1024
MAX_STATE_BYTES: Final = 1024 * 1024
MAX_NOTICES_BYTES: Final = 1024 * 1024
#: A take's decision history here (Restore reads it).
HISTORY_LIMIT: Final = 50
#: How often a change re-checks its take's upload after the PC said it has no such take
#: (the PC refused the take, or never saw it): once, never in a loop. The upload's own
#: answer then says which (a refusal stops the take).
MAX_RECHECKS: Final = 1
_TAKE_ID: Final = re.compile(TAKE_ID_PATTERN.replace("$", r"\Z"))
_RESTORABLE: Final = ("rejected", "removed")


class SpoolError(Exception):
    """A take that cannot be saved or changed as asked; the message says why."""


@dataclass(frozen=True)
class SpooledTake:
    """A saved take: what it is, and its state and outbox here."""

    path: Path
    record: LocalTake
    sync: TakeSync

    @property
    def take_id(self) -> str:
        return self.record.take_id

    @property
    def prompt_id(self) -> str:
        return self.record.prompt.prompt_id

    @property
    def audio(self) -> Path:
        """The take's ``original.wav`` here (played from disk: it is this machine's own)."""
        return self.path / ORIGINAL_NAME

    @property
    def package(self) -> Path:
        return self.path / PACKAGE


def default_client() -> TakeClient:
    """The recording client a take names: this app, its version and platform."""
    system = f"{platform.system() or 'unknown'} {platform.machine() or 'unknown'}"
    return TakeClient(name=CLIENT_NAME, version=__version__, platform=system[:500])


def default_capture_host() -> str:
    return (platform.node() or "unknown-host")[:500]


def capture_device(device: str) -> CaptureDevice:
    """The take's device: the PortAudio worker's UMIK-1 (D-039), named as the worker opened it."""
    label = device[:400] or "unknown"
    return CaptureDevice(
        backend="portaudio",
        device_id=f"umik-1:{label}",
        display_name=f"miniDSP UMIK-1 via PortAudio ({label})",
    )


def restore_target(sync: TakeSync) -> TakeDecisionName:
    """What Restore sets a rejected or removed take back to: its last ``kept`` or ``draft``
    decision before that (``draft`` when it never had one), as the PC expects (D-052)."""
    for event in reversed(sync.history):
        if event.decision in ("kept", "draft"):
            return event.decision
    return "draft"


def _next_expected(sync: TakeSync, kind: Literal["decision", "trim"]) -> int:
    """The revision a new change of ``kind`` expects: the last queued one's + 1, else the
    revision the PC last confirmed."""
    queued = [op for op in sync.changes if op.kind == kind]
    if queued:
        return queued[-1].expected_revision + 1
    return sync.decision.revision if kind == "decision" else sync.trim.revision


def _finished(
    sync: TakeSync,
    op: Operation,
    result: Literal["accepted", "applied", "refused", "dropped"],
    at: datetime,
    *,
    code: str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    """The journal entry and counter of ``op`` leaving the outbox, as changes to ``sync``."""
    entry = Finished(
        kind=op.kind,
        request_id=None if isinstance(op, UploadOp) else op.request_id,
        result=result,
        code=code,
        revision=revision,
        at=at,
    )
    counter = {"accepted": "sent", "applied": "sent", "refused": "refused", "dropped": "dropped"}
    name = counter[result]
    return {
        "journal": (*sync.journal, entry)[-JOURNAL_LIMIT:],
        name: getattr(sync, name) + 1,
    }


def _replace(sync: TakeSync, **changes: Any) -> TakeSync:
    """``sync`` with ``changes``, validated again (every rule of ``TakeSync`` holds)."""
    return TakeSync.model_validate({**dict(sync), **changes})


class CorpusSpool:
    """The takes under ``root`` (``<data dir>/corpus``)."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.root = Path(root)
        self.clock = clock
        self._lock = threading.RLock()
        self._takes: dict[str, SpooledTake] = {}
        self._listed = False
        #: Take directories that could not be read (shown, never sent).
        self.damaged: list[str] = []

    @property
    def takes_root(self) -> Path:
        return self.root / TAKES

    @property
    def recording_root(self) -> Path:
        return self.root / RECORDING

    def take_dir(self, take_id: str) -> Path:
        if not _TAKE_ID.match(take_id):
            raise SpoolError(f"not a take id: {take_id!r}")
        return self.takes_root / take_id

    # -- reading --------------------------------------------------------------------------------

    def _read(self, path: Path) -> SpooledTake:
        try:
            record = read_canonical_model(path / TAKE_FILE, LocalTake, max_bytes=MAX_TAKE_BYTES)
            sync = read_canonical_model(path / STATE_FILE, TakeSync, max_bytes=MAX_STATE_BYTES)
        except (OSError, ContractError, SecurityError) as exc:
            raise SpoolError(f"{path.name} cannot be read: {exc}") from exc
        if record.take_id != path.name or sync.take_id != path.name:
            raise SpoolError(f"{path.name} holds another take's records")
        return SpooledTake(path, record, sync)

    def _recover(self) -> None:
        """Finish a save a crash interrupted after its records were written: the recording
        directory moves into ``takes/``. Recordings without both records are left alone."""
        if not self.recording_root.is_dir():
            return
        for path in sorted(self.recording_root.iterdir()):
            if not (path / TAKE_FILE).is_file() or not (path / STATE_FILE).is_file():
                continue
            try:
                record = read_canonical_model(path / TAKE_FILE, LocalTake, max_bytes=MAX_TAKE_BYTES)
                rename_no_replace(path, private_dir(self.takes_root) / record.take_id)
            except (OSError, ContractError, SecurityError):
                continue
            fsync_directory(self.takes_root)

    def takes(self) -> list[SpooledTake]:
        """Every saved take, in the order they were saved."""
        with self._lock:
            if not self._listed:
                self._recover()
                self.damaged = []
                if self.takes_root.is_dir():
                    for path in sorted(self.takes_root.iterdir()):
                        if not _TAKE_ID.match(path.name) or not path.is_dir():
                            continue
                        try:
                            self._takes[path.name] = self._read(path)
                        except SpoolError:
                            self.damaged.append(path.name)
                self._listed = True
            return sorted(
                self._takes.values(), key=lambda take: (take.record.saved_at, take.take_id)
            )

    def takes_for_prompt(self, prompt_id: str) -> list[SpooledTake]:
        """The takes of one prompt, newest first."""
        found = [take for take in self.takes() if take.prompt_id == prompt_id]
        return sorted(found, key=lambda take: take.record.requested_at, reverse=True)

    def take(self, take_id: str) -> SpooledTake:
        """Raises: ``SpoolError`` for an unknown or damaged take."""
        with self._lock:
            if take_id in self._takes:
                return self._takes[take_id]
            path = self.take_dir(take_id)
            if not path.is_dir():
                raise SpoolError(f"no take {take_id} here")
            take = self._read(path)
            self._takes[take_id] = take
            return take

    def _store(self, take: SpooledTake, sync: TakeSync) -> SpooledTake:
        write_canonical_json(take.path / STATE_FILE, sync)
        stored = SpooledTake(take.path, take.record, sync)
        self._takes[take.take_id] = stored
        return stored

    # -- saving a take -------------------------------------------------------------------------

    def new_recording_dir(self) -> Path:
        """A new, empty, private directory for the capture worker's next take."""
        stamp = self.clock().astimezone(UTC).strftime("%Y%m%dt%H%M%S")
        path = private_dir(self.recording_root) / f"rec-{stamp}-{secrets.token_hex(4)}"
        path.mkdir(mode=0o700)
        return path

    def save_take(
        self,
        recording_dir: Path,
        captured: CapturedTake,
        *,
        prompt: Prompt,
        catalog: CatalogReference,
        client: TakeClient | None = None,
        capture_host: str | None = None,
    ) -> SpooledTake:
        """Save the worker's validated take of ``prompt``, recorded against ``catalog``.

        Raises: ``SpoolError`` when the take is not the worker's checked output in
        ``recording_dir``, is already saved, or cannot be written (the recording stays)."""
        recording_dir = Path(recording_dir)
        if recording_dir.parent != self.recording_root or (
            captured.path != recording_dir / ORIGINAL_NAME
            or captured.evidence_path != recording_dir / EVIDENCE_NAME
        ):
            raise SpoolError("the take was not recorded into this spool's recording directory")
        try:
            evidence = read_evidence(captured.evidence_path)
            inspection = inspect_canonical_wav(captured.path)
        except (OSError, ContractError, SecurityError) as exc:
            raise SpoolError(f"the take cannot be saved: {exc}") from exc
        if (
            not evidence.success
            or evidence.wav_sha256 != captured.sha256
            or inspection.sha256 != captured.sha256
            or inspection.size_bytes != captured.size_bytes
            or inspection.audio.frames != captured.frames
            or evidence.frames != captured.frames
        ):
            raise SpoolError("the take's audio is not what its continuity evidence describes")
        take_id = take_id_for(captured.sha256)
        target = self.take_dir(take_id)
        if os.path.lexists(target):
            raise SpoolError(f"this take is already saved ({take_id})")
        # The recording directory is this save's alone: the package is built, and the
        # records written, without the lock; only the rename into takes/ takes it.
        local_only: str | None = SIMULATED if evidence.simulated else None
        manifest_sha256: str | None = None
        if local_only is None:
            try:
                manifest_sha256 = self._package(
                    recording_dir,
                    captured,
                    evidence=evidence,
                    prompt=prompt,
                    catalog=catalog,
                    client=client or default_client(),
                    capture_host=capture_host or default_capture_host(),
                )
            except (ContractError, SecurityError) as exc:
                local_only = f"the take could not be packaged: {exc}"[:2000]
            except OSError as exc:
                raise SpoolError(f"the take could not be packaged: {exc}") from exc
        now = self.clock()
        record = LocalTake(
            take_id=take_id,
            prompt=prompt,
            prompt_sha256=canonical_sha256(prompt),
            catalog=catalog,
            requested_at=captured.requested_at,
            saved_at=now,
            device=(captured.device or evidence.device_name)[:500],
            audio_sha256=captured.sha256,
            size_bytes=captured.size_bytes,
            frames=captured.frames,
            simulated=evidence.simulated,
            local_only=local_only,
            package_manifest_sha256=manifest_sha256,
        )
        sync = TakeSync(
            take_id=take_id,
            decision=LocalDecision(decision="draft"),
            trim=LocalTrim(),
            history=(DecisionEvent(decision="draft", at=now),),
            upload="local-only" if local_only else "queued",
            outbox=() if local_only else (UploadOp(queued_at=now),),
        )
        try:
            write_canonical_json(recording_dir / TAKE_FILE, record)
            write_canonical_json(recording_dir / STATE_FILE, sync)
        except OSError as exc:
            raise SpoolError(f"the take could not be saved: {exc}") from exc
        with self._lock:
            try:
                rename_no_replace(recording_dir, private_dir(self.takes_root) / take_id)
                fsync_directory(self.takes_root)
            except FileExistsError as exc:
                raise SpoolError(f"this take is already saved ({take_id})") from exc
            except OSError as exc:
                raise SpoolError(f"the take could not be saved: {exc}") from exc
            take = SpooledTake(target, record, sync)
            self._takes[take_id] = take
            if local_only is not None and not evidence.simulated:
                self._notice(take, "local-only", f"Kept on this machine only: {local_only}")
            return take

    def _package(
        self,
        recording_dir: Path,
        captured: CapturedTake,
        *,
        evidence: Any,
        prompt: Prompt,
        catalog: CatalogReference,
        client: TakeClient,
        capture_host: str,
    ) -> str:
        """The take package's manifest digest: built once into ``package/``; a package an
        interrupted save already built is validated and kept."""
        package = recording_dir / PACKAGE
        if os.path.lexists(package):
            built = validate_take_package(package)
            if built.manifest.audio.sha256 != captured.sha256:
                raise SecurityError("the package beside the take holds other audio")
            return built.manifest_sha256
        built = build_take_package(
            package,
            wav=captured.path,
            evidence=evidence,
            prompt=prompt,
            catalog=catalog,
            requested_at=captured.requested_at,
            capture_host=capture_host,
            client=client,
            device=capture_device(captured.device or evidence.device_name),
            clock=self.clock,
        )
        return built.manifest_sha256

    def discard_recording(self, recording_dir: Path) -> None:
        """Remove a recording directory that never became a take (a failed or abandoned
        take); a saved take is never removed."""
        recording_dir = Path(recording_dir)
        if recording_dir.parent != self.recording_root or not recording_dir.name.startswith("rec-"):
            raise SpoolError("not a recording directory of this spool")
        shutil.rmtree(recording_dir, ignore_errors=True)

    # -- the operator's changes ------------------------------------------------------------------

    @staticmethod
    def _sends(sync: TakeSync) -> bool:
        return sync.upload in ("queued", "accepted")

    def decide(
        self, take_id: str, decision: TakeDecisionName, reason: str | None = None
    ) -> SpooledTake:
        """Keep, reject (with a reason), remove or set back a take; queued for the PC.

        Raises: ``SpoolError`` (a rejection without a reason, a reason for anything else)."""
        try:
            check_decision(decision, reason)
        except ValueError as exc:
            raise SpoolError(str(exc)) from exc
        with self._lock:
            take = self.take(take_id)
            sync = take.sync
            if (sync.decision.decision, sync.decision.reason) == (decision, reason):
                return take
            now = self.clock()
            changes: dict[str, Any] = {
                "decision": LocalDecision(
                    decision=decision,
                    reason=reason,
                    changed_at=now,
                    revision=sync.decision.revision,
                ),
                "history": (*sync.history, DecisionEvent(decision=decision, at=now))[
                    -HISTORY_LIMIT:
                ],
            }
            if self._sends(sync):
                op = DecisionOp(
                    queued_at=now,
                    request_id=new_request_id(),
                    decision=decision,
                    reason=reason,
                    expected_revision=_next_expected(sync, "decision"),
                )
                changes["outbox"] = (*sync.outbox, op)
            return self._store(take, _replace(sync, **changes))

    def restore(self, take_id: str) -> SpooledTake:
        """A rejected or removed take back to its decision before (``kept`` or ``draft``)."""
        with self._lock:
            take = self.take(take_id)
            if take.sync.decision.decision not in _RESTORABLE:
                return take
            return self.decide(take_id, restore_target(take.sync))

    def set_trim(self, take_id: str, start_frame: int | None, end_frame: int | None) -> SpooledTake:
        """Keep the frames ``[start_frame, end_frame)`` (both ``None``, or the whole take: no
        trim); the original is never rewritten. Queued for the PC.

        Raises: ``SpoolError`` for a range outside the take or shorter than one frame."""
        with self._lock:
            take = self.take(take_id)
            frames = take.record.frames
            if (start_frame, end_frame) == (0, frames):
                start_frame = end_frame = None
            if (start_frame is None) != (end_frame is None):
                raise SpoolError("a trim has both ends or neither")
            if start_frame is not None and end_frame is not None:
                if not 0 <= start_frame < end_frame <= frames:
                    raise SpoolError(f"a trim must lie within the take's {frames} frames")
            sync = take.sync
            if (sync.trim.start_frame, sync.trim.end_frame) == (start_frame, end_frame):
                return take
            now = self.clock()
            changes: dict[str, Any] = {
                "trim": LocalTrim(
                    start_frame=start_frame, end_frame=end_frame, revision=sync.trim.revision
                )
            }
            if self._sends(sync):
                op = TrimOp(
                    queued_at=now,
                    request_id=new_request_id(),
                    start_frame=start_frame,
                    end_frame=end_frame,
                    expected_revision=_next_expected(sync, "trim"),
                )
                changes["outbox"] = (*sync.outbox, op)
            return self._store(take, _replace(sync, **changes))

    # -- what the sync engine records -------------------------------------------------------------

    def _head(self, take_id: str, request_id: str | None) -> tuple[SpooledTake, Operation]:
        """The take and the operation at the front of its outbox, which must be the one the
        engine worked on (``request_id``; ``None`` for the upload)."""
        take = self.take(take_id)
        outbox = take.sync.outbox
        if not outbox:
            raise SpoolError(f"{take_id} has nothing queued")
        op = outbox[0]
        if (None if isinstance(op, UploadOp) else op.request_id) != request_id:
            raise SpoolError(f"{take_id}'s first queued operation changed")
        return take, op

    def tried(
        self, take_id: str, request_id: str | None, *, error: str, retry_at: datetime | None
    ) -> None:
        """A try of the first operation that did not finish; it stays first."""
        with self._lock:
            take, op = self._head(take_id, request_id)
            tried = op.model_copy(
                update={
                    "attempts": op.attempts + 1,
                    "last_attempt_at": self.clock(),
                    "last_error": error[:2000] or "interrupted",
                    "not_before": retry_at,
                }
            )
            self._store(take, _replace(take.sync, outbox=(tried, *take.sync.outbox[1:])))

    def upload_waiting(self, take_id: str, *, error: str, retry_at: datetime | None) -> None:
        """The PC holds the whole package but has not published it (deferred)."""
        with self._lock:
            take, op = self._head(take_id, None)
            waiting = op.model_copy(
                update={
                    "attempts": op.attempts + 1,
                    "last_attempt_at": self.clock(),
                    "last_error": error[:2000] or "waiting for the PC",
                    "not_before": retry_at,
                    "awaiting_receipt": True,
                }
            )
            self._store(take, _replace(take.sync, outbox=(waiting, *take.sync.outbox[1:])))

    def upload_accepted(self, take_id: str, receipt: UploadReceipt) -> None:
        with self._lock:
            take, op = self._head(take_id, None)
            sync = take.sync
            changes = _finished(sync, op, "accepted", receipt.accepted_at)
            self._store(
                take,
                _replace(
                    sync, upload="accepted", receipt=receipt, outbox=sync.outbox[1:], **changes
                ),
            )

    def upload_refused(self, take_id: str, *, code: str, message: str) -> None:
        """The PC refused the take: nothing more of it is sent; noticed."""
        with self._lock:
            take, _op = self._head(take_id, None)
            dropped = self._drop_all(take.sync, code=code)
            stored = self._store(take, _replace(take.sync, upload="refused", **dropped))
            self._notice(stored, "take-refused", f"The PC refused this take: {message}"[:2000])

    def change_submitted(self, take_id: str, request_id: str) -> None:
        """The PC answered the change's request: from now on it is only polled."""
        with self._lock:
            take, op = self._head(take_id, request_id)
            if isinstance(op, UploadOp) or op.submitted_at is not None:
                return
            submitted = op.model_copy(update={"submitted_at": self.clock()})
            self._store(take, _replace(take.sync, outbox=(submitted, *take.sync.outbox[1:])))

    def resubmit(self, take_id: str, request_id: str, *, error: str) -> None:
        """The PC has no such request although it answered it once: it is submitted again,
        under the same id (a try is counted)."""
        with self._lock:
            take, op = self._head(take_id, request_id)
            again = op.model_copy(
                update={
                    "attempts": op.attempts + 1,
                    "last_attempt_at": self.clock(),
                    "last_error": error[:2000] or "the PC lost the request",
                    "submitted_at": None,
                }
            )
            self._store(take, _replace(take.sync, outbox=(again, *take.sync.outbox[1:])))

    def change_applied(self, take_id: str, request_id: str, revision: int) -> None:
        """The PC applied the change at ``revision``: the revision here follows."""
        with self._lock:
            take, op = self._head(take_id, request_id)
            sync = take.sync
            changes = _finished(sync, op, "applied", self.clock(), revision=revision)
            if isinstance(op, DecisionOp):
                changes["decision"] = sync.decision.model_copy(update={"revision": revision})
            elif isinstance(op, TrimOp):
                changes["trim"] = sync.trim.model_copy(update={"revision": revision})
            self._store(take, _replace(sync, outbox=sync.outbox[1:], **changes))

    def recheck_upload(self, take_id: str, request_id: str, *, renew: bool) -> bool:
        """The PC has no record of the take: queue its upload again before the change (a new
        request id when the PC already refused this one). ``False`` once it was re-checked
        ``MAX_RECHECKS`` times (the caller then treats the change as refused)."""
        with self._lock:
            take, op = self._head(take_id, request_id)
            if isinstance(op, UploadOp) or op.rechecks >= MAX_RECHECKS:
                return False
            now = self.clock()
            again: ChangeOp = op.model_copy(
                update={
                    "rechecks": op.rechecks + 1,
                    "submitted_at": None,
                    "request_id": new_request_id() if renew else op.request_id,
                    "not_before": None,
                }
            )
            outbox = (UploadOp(queued_at=now), again, *take.sync.outbox[1:])
            self._store(take, _replace(take.sync, upload="queued", receipt=None, outbox=outbox))
            return True

    def adopt(
        self,
        take_id: str,
        request_id: str,
        *,
        pc: LibraryTake | None,
        code: str,
        revision: int | None,
        detail: str = "",
    ) -> None:
        """The PC refused a change (``revision_conflict``: another client changed the take):
        the PC is canonical. Its state is adopted (``pc``, from the prompt's history; when it
        could not be read, only the refused kind's revision), every change still queued for
        the take is dropped, and a notice says so."""
        with self._lock:
            take, op = self._head(take_id, request_id)
            sync = take.sync
            now = self.clock()
            changes = self._drop_all(sync, code=code, revision=revision)
            if pc is not None:
                same = pc.decision == sync.decision.decision
                changes["decision"] = LocalDecision(
                    decision=pc.decision,
                    reason=sync.decision.reason if same else None,
                    changed_at=sync.decision.changed_at if same else now,
                    revision=pc.decision_revision,
                )
                if not same:
                    # What happened here since the PC last confirmed was never so there:
                    # the history restarts from the PC's decision (Restore then knows only it).
                    changes["history"] = (DecisionEvent(decision=pc.decision, at=now, source="pc"),)
                changes["trim"] = LocalTrim(
                    start_frame=pc.trim.start_frame,
                    end_frame=pc.trim.end_frame,
                    revision=pc.trim.revision,
                )
            elif revision is not None and isinstance(op, DecisionOp):
                changes["decision"] = sync.decision.model_copy(update={"revision": revision})
            elif revision is not None and isinstance(op, TrimOp):
                changes["trim"] = sync.trim.model_copy(update={"revision": revision})
            stored = self._store(take, _replace(sync, **changes))
            dropped = len(take.sync.outbox) - 1
            if code == "revision_conflict":
                kind: NoticeKind = "changed-elsewhere"
                text = "Changed elsewhere: this take now shows the PC's state"
            else:
                kind = "change-refused"
                text = f"The PC refused a change of this take ({code})"
            if pc is None:
                text += "; the PC's state could not be read"
            if dropped:
                text += f"; {dropped} queued change(s) were dropped"
            if detail:
                text += f" ({detail})"
            self._notice(stored, kind, (text + ".")[:2000])

    def _drop_all(
        self, sync: TakeSync, *, code: str, revision: int | None = None
    ) -> dict[str, Any]:
        """Every queued operation out, as changes to ``sync``: the first refused (``code``),
        the others dropped."""
        now = self.clock()
        current = sync
        for index, op in enumerate(sync.outbox):
            result: Literal["refused", "dropped"] = "refused" if index == 0 else "dropped"
            finished = _finished(
                current,
                op,
                result,
                now,
                code=code if index == 0 else None,
                revision=revision if index == 0 else None,
            )
            current = current.model_copy(update=finished)
        return {
            "outbox": (),
            "journal": current.journal,
            "sent": current.sent,
            "refused": current.refused,
            "dropped": current.dropped,
        }

    # -- notices and the last contact -------------------------------------------------------------

    def _notice(self, take: SpooledTake, kind: NoticeKind, text: str) -> None:
        notice = Notice(
            notice_id=new_notice_id(),
            take_id=take.take_id,
            prompt_id=take.prompt_id,
            kind=kind,
            text=text,
            at=self.clock(),
        )
        current = self.notices()
        write_canonical_json(
            private_dir(self.root) / NOTICES_FILE,
            Notices(notices=(*current, notice)[-NOTICE_LIMIT:]),
        )

    def notices(self) -> tuple[Notice, ...]:
        """The notices not dismissed yet, oldest first (a damaged file reads as none)."""
        with self._lock:
            path = self.root / NOTICES_FILE
            if not os.path.lexists(path):
                return ()
            try:
                return read_canonical_model(path, Notices, max_bytes=MAX_NOTICES_BYTES).notices
            except (OSError, ContractError, SecurityError):
                return ()

    def dismiss(self, notice_id: str) -> None:
        with self._lock:
            kept = tuple(n for n in self.notices() if n.notice_id != notice_id)
            write_canonical_json(private_dir(self.root) / NOTICES_FILE, Notices(notices=kept))

    def sync_record(self) -> SyncRecord:
        with self._lock:
            path = self.root / SYNC_FILE
            if not os.path.lexists(path):
                return SyncRecord()
            try:
                return read_canonical_model(path, SyncRecord, max_bytes=MAX_TAKE_BYTES)
            except (OSError, ContractError, SecurityError):
                return SyncRecord()

    def record_pass(self, *, reached: bool, error: str | None) -> SyncRecord:
        """A sync pass: when it ran, whether it reached the PC, what went wrong."""
        with self._lock:
            now = self.clock()
            last = self.sync_record()
            record = SyncRecord(
                last_sync_at=now if reached else last.last_sync_at,
                last_attempt_at=now,
                last_error=(error or None) and error[:2000],
            )
            write_canonical_json(private_dir(self.root) / SYNC_FILE, record)
            return record


def doubling(count: int, *, base: float, cap: float) -> float:
    """``base`` seconds doubled for every count after the first, at most ``cap``."""
    return min(cap, base * 2.0 ** min(32, max(0, count - 1)))


def backoff(attempts: int, *, base: float, cap: float) -> timedelta:
    """The wait after ``attempts`` failed tries: ``doubling``."""
    return timedelta(seconds=doubling(attempts, base=base, cap=cap))
