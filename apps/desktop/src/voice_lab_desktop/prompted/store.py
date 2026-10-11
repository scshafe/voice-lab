"""Prompted sessions and their takes on this machine (phase 4c; D-010, D-012, D-042).

``<data dir>/prompted/``:

- ``sessions/<session id>/session.json``: the session (its catalog embedded with its
  digest, the batch, when it started, the device, its state);
- ``sessions/<session id>/takes/<prompt id>/<n>/``: one directory per take, made empty and
  private before the recorder starts. The recorder writes ``original.wav`` and its
  continuity evidence ``continuity.json`` there; the take exists once ``take.json`` is
  written beside them (its number, when it was asked for, the PCM's digest and frame count,
  the decision with its history, the trim). A take that failed keeps what the recorder left
  and a ``failure.json`` saying why; a directory with neither record is damaged. Neither is
  ever a take.
- ``packages/<session id>/``: the capture package, once the session is finished
  (``package.py``), and ``packages/<session id>.upload.json`` beside it (Uploads).

Directories are 0700 and records 0600, written atomically (``write_canonical_json``). A
take's original is never rewritten: a trim is a recipe in ``take.json``, bound to the
original's digest (D-012), and the package carries the original bytes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.models import PromptSuite, utc_now

from ..capture.types import EVIDENCE_NAME, ORIGINAL_NAME, CapturedTake

SESSION_SCHEMA: Final = "voice-lab-desktop.prompted-session/v1"
TAKE_SCHEMA: Final = "voice-lab-desktop.prompted-take/v1"
SESSION_FILE: Final = "session.json"
TAKE_FILE: Final = "take.json"
FAILURE_FILE: Final = "failure.json"
UPLOAD_SUFFIX: Final = ".upload.json"
#: ``native-``: prompt-suite/v2 packages are the recording application's (capture.py);
#: ``desktop``: this app's, told apart from the Swift app's.
SESSION_PREFIX: Final = "native-desktop-"
_SESSION_PATTERN: Final = r"^native-desktop-[0-9]{8}t[0-9]{6}z-[0-9a-f]{6}$"
_SESSION_ID: Final = re.compile(_SESSION_PATTERN.replace("$", r"\Z"))
_TAKE_NUMBER: Final = re.compile(r"[1-9][0-9]{0,5}\Z")

type Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type Decision = Literal["draft", "kept", "removed"]
type SessionState = Literal["open", "finished", "packaged", "abandoned"]


class StoreError(Exception):
    """A record is missing, damaged or cannot change as asked; the message says which."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SessionRecord(_Record):
    schema_version: Literal["voice-lab-desktop.prompted-session/v1"] = SESSION_SCHEMA
    session_id: Annotated[str, StringConstraints(pattern=_SESSION_PATTERN)]
    catalog_source: Text
    catalog_label: Text
    catalog: PromptSuite
    catalog_sha256: Sha256
    #: The batch: the chosen prompts plus every room tone, in catalog order (D-010).
    prompt_ids: tuple[Text, ...] = Field(min_length=1)
    started_at: datetime
    device: Text
    state: SessionState = "open"
    finished_at: datetime | None = None
    package_manifest_sha256: Sha256 | None = None


class DecisionEvent(_Record):
    decision: Decision
    at: datetime


class Trim(_Record):
    """Frames ``[start_frame, end_frame)`` of the original whose digest is ``source_sha256``."""

    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    source_sha256: Sha256


class TakeRecord(_Record):
    schema_version: Literal["voice-lab-desktop.prompted-take/v1"] = TAKE_SCHEMA
    prompt_id: Text
    take_number: int = Field(ge=1)
    requested_at: datetime
    sha256: Sha256
    size_bytes: int = Field(gt=44)
    frames: int = Field(gt=0)
    device: Text
    decision: Decision = "draft"
    history: tuple[DecisionEvent, ...] = ()
    trim: Trim | None = None

    @property
    def kept_at(self) -> datetime | None:
        """When it was last kept (its latest ``kept`` event), if it is kept now."""
        if self.decision != "kept":
            return None
        kept = [event.at for event in self.history if event.decision == "kept"]
        return kept[-1] if kept else None


class FailureRecord(_Record):
    prompt_id: Text
    take_number: int = Field(ge=1)
    requested_at: datetime
    message: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


@dataclass(frozen=True)
class TakeEntry:
    """A take directory as the history shows it: a take, a failed take, or damaged."""

    prompt_id: str
    number: int
    path: Path
    record: TakeRecord | None
    failure: FailureRecord | None = None

    @property
    def status(self) -> str:
        """``draft`` | ``kept`` | ``removed`` | ``failed`` | ``damaged``."""
        if self.record is not None:
            return self.record.decision
        return "failed" if self.failure is not None else "damaged"

    @property
    def original(self) -> Path:
        return self.path / ORIGINAL_NAME


def new_session_id(now: datetime) -> str:
    stamp = now.astimezone(UTC).strftime("%Y%m%dt%H%M%Sz")
    return f"{SESSION_PREFIX}{stamp}-{secrets.token_hex(3)}"


def _private_dir(path: Path, *, exist_ok: bool = True) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=exist_ok)
    os.chmod(path, 0o700)
    return path


def _read_model[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StoreError(f"{path.name} is missing") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4 * 1024 * 1024:
        raise StoreError(f"{path} is not a record")
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise StoreError(f"{path} is damaged") from exc


class PromptedStore:
    """The prompted sessions under ``<data dir>/prompted``."""

    def __init__(self, data_dir: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.root = Path(data_dir) / "prompted"
        self.clock = clock

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    @property
    def packages_root(self) -> Path:
        return self.root / "packages"

    def session_dir(self, session_id: str) -> Path:
        if not _SESSION_ID.match(session_id):
            raise StoreError(f"not a prompted session id: {session_id!r}")
        return self.sessions_root / session_id

    # -- sessions ---------------------------------------------------------------------------

    def create(
        self,
        *,
        catalog_source: str,
        catalog_label: str,
        catalog: PromptSuite,
        catalog_sha256: str,
        prompt_ids: tuple[str, ...],
        device: str,
    ) -> SessionRecord:
        now = self.clock()
        record = SessionRecord(
            session_id=new_session_id(now),
            catalog_source=catalog_source,
            catalog_label=catalog_label[:500],
            catalog=catalog,
            catalog_sha256=catalog_sha256,
            prompt_ids=prompt_ids,
            started_at=now,
            device=device[:500] or "unknown device",
        )
        _private_dir(self.sessions_root)
        directory = self.session_dir(record.session_id)
        _private_dir(directory, exist_ok=False)
        _private_dir(directory / "takes")
        write_canonical_json(directory / SESSION_FILE, record)
        return record

    def load(self, session_id: str) -> SessionRecord:
        record = _read_model(self.session_dir(session_id) / SESSION_FILE, SessionRecord)
        if record.session_id != session_id:
            raise StoreError(f"{session_id}/session.json names another session")
        return record

    def save(self, record: SessionRecord) -> SessionRecord:
        write_canonical_json(self.session_dir(record.session_id) / SESSION_FILE, record)
        return record

    def update(self, record: SessionRecord, **changes: Any) -> SessionRecord:
        return self.save(record.model_copy(update=changes))

    def sessions(self) -> list[SessionRecord]:
        """Every readable session, oldest first (a damaged one is left alone)."""
        if not self.sessions_root.is_dir():
            return []
        out = []
        for path in sorted(self.sessions_root.iterdir()):
            if not _SESSION_ID.match(path.name):
                continue
            try:
                out.append(self.load(path.name))
            except StoreError:
                continue
        return out

    def open_session(self) -> SessionRecord | None:
        """The session still being recorded (the latest open one), if any."""
        open_ = [record for record in self.sessions() if record.state == "open"]
        return open_[-1] if open_ else None

    # -- takes ------------------------------------------------------------------------------

    def _prompt_dir(self, session: SessionRecord, prompt_id: str) -> Path:
        if prompt_id not in session.prompt_ids:
            raise StoreError(f"{prompt_id} is not in session {session.session_id}'s batch")
        return self.session_dir(session.session_id) / "takes" / prompt_id

    def new_take_dir(self, session: SessionRecord, prompt_id: str) -> tuple[int, Path]:
        """The next take's number and its new, empty, private directory (numbers are never
        reused, failed and damaged takes included)."""
        prompt_dir = _private_dir(self._prompt_dir(session, prompt_id))
        numbers = [int(p.name) for p in prompt_dir.iterdir() if _TAKE_NUMBER.match(p.name)]
        number = max(numbers, default=0) + 1
        path = prompt_dir / str(number)
        _private_dir(path, exist_ok=False)
        return number, path

    def record_take(
        self, session: SessionRecord, prompt_id: str, number: int, captured: CapturedTake
    ) -> TakeRecord:
        """The recorder's validated take becomes a draft take."""
        path = self._prompt_dir(session, prompt_id) / str(number)
        if captured.path != path / ORIGINAL_NAME:
            raise StoreError("the recorder wrote the take somewhere else")
        record = TakeRecord(
            prompt_id=prompt_id,
            take_number=number,
            requested_at=captured.requested_at,
            sha256=captured.sha256,
            size_bytes=captured.size_bytes,
            frames=captured.frames,
            device=captured.device[:500] or "unknown device",
            history=(DecisionEvent(decision="draft", at=self.clock()),),
        )
        write_canonical_json(path / TAKE_FILE, record)
        return record

    def record_failure(
        self,
        session: SessionRecord,
        prompt_id: str,
        number: int,
        *,
        requested_at: datetime,
        message: str,
    ) -> FailureRecord:
        path = self._prompt_dir(session, prompt_id) / str(number)
        record = FailureRecord(
            prompt_id=prompt_id,
            take_number=number,
            requested_at=requested_at,
            message=message[:2000] or "the take failed",
        )
        write_canonical_json(path / FAILURE_FILE, record)
        return record

    def takes(self, session: SessionRecord, prompt_id: str) -> list[TakeEntry]:
        """Every take directory of ``prompt_id``, by number."""
        prompt_dir = self._prompt_dir(session, prompt_id)
        if not prompt_dir.is_dir():
            return []
        entries = []
        for path in prompt_dir.iterdir():
            if not _TAKE_NUMBER.match(path.name) or not path.is_dir():
                continue
            number = int(path.name)
            entries.append(self._entry(prompt_id, number, path))
        return sorted(entries, key=lambda entry: entry.number)

    def _entry(self, prompt_id: str, number: int, path: Path) -> TakeEntry:
        failure: FailureRecord | None = None
        try:
            record = _read_model(path / TAKE_FILE, TakeRecord)
        except StoreError:
            record = None
            try:
                failure = _read_model(path / FAILURE_FILE, FailureRecord)
            except StoreError:
                failure = None
        if record is not None:
            original = path / ORIGINAL_NAME
            consistent = (
                record.prompt_id == prompt_id
                and record.take_number == number
                and _regular_size(original) == record.size_bytes
                and _regular_size(path / EVIDENCE_NAME) is not None
            )
            if not consistent:
                record = None
        return TakeEntry(prompt_id, number, path, record, failure)

    def take(self, session: SessionRecord, prompt_id: str, number: int) -> TakeRecord:
        entry = self._entry(prompt_id, number, self._prompt_dir(session, prompt_id) / str(number))
        if entry.record is None:
            raise StoreError(f"take {number} of {prompt_id} is {entry.status}")
        return entry.record

    def _write_take(self, session: SessionRecord, record: TakeRecord) -> TakeRecord:
        path = self._prompt_dir(session, record.prompt_id) / str(record.take_number)
        write_canonical_json(path / TAKE_FILE, record)
        return record

    def decide(
        self, session: SessionRecord, prompt_id: str, number: int, decision: Decision
    ) -> TakeRecord:
        """Keep, remove, or set a take back to draft; the history keeps every decision."""
        record = self.take(session, prompt_id, number)
        history = (*record.history, DecisionEvent(decision=decision, at=self.clock()))
        return self._write_take(
            session, record.model_copy(update={"decision": decision, "history": history})
        )

    def restore(self, session: SessionRecord, prompt_id: str, number: int) -> TakeRecord:
        """A removed take back to what it was before it was removed (D-012: removal is
        reversible)."""
        record = self.take(session, prompt_id, number)
        if record.decision != "removed":
            return record
        before = [event.decision for event in record.history if event.decision != "removed"]
        return self.decide(session, prompt_id, number, before[-1] if before else "draft")

    def set_trim(
        self, session: SessionRecord, prompt_id: str, number: int, trim: tuple[int, int] | None
    ) -> TakeRecord:
        """The take's trim recipe (``None``: the whole take); never touches the original.

        Raises: ``StoreError`` for a range outside the take or shorter than one frame."""
        record = self.take(session, prompt_id, number)
        value: Trim | None = None
        if trim is not None:
            start, end = trim
            if not 0 <= start < end <= record.frames:
                raise StoreError(f"a trim must lie within the take's {record.frames} frames")
            if (start, end) != (0, record.frames):
                value = Trim(start_frame=start, end_frame=end, source_sha256=record.sha256)
        return self._write_take(session, record.model_copy(update={"trim": value}))

    def kept(self, session: SessionRecord, prompt_id: str) -> TakeRecord | None:
        """The prompt's take for the package: the one kept most recently (D-016)."""
        kept = [
            entry.record
            for entry in self.takes(session, prompt_id)
            if entry.record is not None and entry.record.kept_at is not None
        ]
        return max(kept, key=lambda record: (record.kept_at, record.take_number), default=None)

    def statuses(self, session: SessionRecord) -> dict[str, str]:
        """Per batch prompt: ``kept`` (a take is kept), ``draft`` (takes, none kept) or
        ``missing``."""
        out = {}
        for prompt_id in session.prompt_ids:
            entries = [e for e in self.takes(session, prompt_id) if e.record is not None]
            if any(e.record is not None and e.record.decision == "kept" for e in entries):
                out[prompt_id] = "kept"
            elif any(e.record is not None and e.record.decision == "draft" for e in entries):
                out[prompt_id] = "draft"
            else:
                out[prompt_id] = "missing"
        return out

    # -- packages and their uploads -------------------------------------------------------

    def package_dir(self, session_id: str) -> Path:
        self.session_dir(session_id)  # checks the id
        return self.packages_root / session_id

    def upload_file(self, session_id: str) -> Path:
        return self.packages_root / f"{session_id}{UPLOAD_SUFFIX}"

    def upload_state(self, session_id: str) -> dict[str, Any]:
        try:
            data = json.loads(self.upload_file(session_id).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_upload_state(self, session_id: str, data: dict[str, Any]) -> None:
        _private_dir(self.packages_root)
        write_canonical_json(self.upload_file(session_id), data)


def _regular_size(path: Path) -> int | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    return metadata.st_size if stat.S_ISREG(metadata.st_mode) else None
