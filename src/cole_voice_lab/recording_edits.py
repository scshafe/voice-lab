"""Non-destructive recording trims and recoverable eligibility decisions.

@canonical recording_edits -- original capture bytes are immutable inputs;
frame-exact derivatives and operator decisions live outside capture packages.
The shared exclusion gate also protects future dataset/reference selections.
This does not defend against an owner deliberately rewriting private evidence.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
import wave
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, ValidationError, field_validator, model_validator

from .audio import inspect_canonical_wav
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    write_canonical_json,
)
from .errors import ContractError, SecurityError
from .models import ContractModel, Sha256, StableId
from .paths import AssetLayout, confined_path, validate_relative_path
from .recording_contracts import (
    DecisionCommand,
    EditTakeCommand,
    PositiveRevision,
    RecordingCursor,
    RecordingError,
    RecordingFileIdentity,
    RecordingFrameRange,
    RecordingHistoryEntry,
    RecordingReviewSource,
    RecordingTake,
    ReviewActionEvent,
    UtcTimestamp,
)

MAX_EDIT_BYTES = 64 * 1024
COPY_FRAMES = 65_536


class RecordingEdit(ContractModel):
    """One exact source's effective frame selection and recoverable removal state."""

    schema_version: Literal["cole-voice-lab.recording-edit/v1"] = "cole-voice-lab.recording-edit/v1"
    subject_id: StableId
    session_id: StableId
    prompt_id: StableId
    source_sha256: Sha256
    source_frames: int = Field(gt=0)
    sample_rate: Literal[48_000] = 48_000
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    removed: bool = False
    removal_reason: str | None = Field(default=None, min_length=1, max_length=160)
    derived_path: str | None = None
    derived_sha256: Sha256 | None = None
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("recording edits require a timezone-aware timestamp")
        return value.astimezone(UTC)

    @field_validator("derived_path")
    @classmethod
    def _derived_location(cls, value: str | None) -> str | None:
        if value is not None:
            validate_relative_path(value)
            if not value.startswith("derived/recording-edits/"):
                raise ValueError("trim output must live beneath derived/recording-edits")
        return value

    @field_validator("removal_reason")
    @classmethod
    def _meaningful_reason(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("removal reason must be readable nonblank text")
        return value

    @model_validator(mode="after")
    def _valid_edit(self) -> RecordingEdit:
        if not self.start_frame < self.end_frame <= self.source_frames:
            raise ValueError("trim must retain a nonempty range within the original recording")
        if self.removed != (self.removal_reason is not None):
            raise ValueError("only removed recordings require a removal reason")
        if (self.derived_path is None) != (self.derived_sha256 is None):
            raise ValueError("derived recording path and digest must be supplied together")
        if self.trimmed != (self.derived_path is not None):
            raise ValueError("trimmed recordings require a checksum-bound derivative")
        return self

    @property
    def trimmed(self) -> bool:
        return self.start_frame != 0 or self.end_frame != self.source_frames


def recording_edit_key(
    *, subject_id: str, session_id: str, prompt_id: str, source_sha256: str
) -> str:
    """Return location-independent identity shared by staged and accepted raw copies."""
    return canonical_sha256([subject_id, session_id, prompt_id, source_sha256])


def _key(edit: RecordingEdit) -> str:
    return recording_edit_key(
        subject_id=edit.subject_id,
        session_id=edit.session_id,
        prompt_id=edit.prompt_id,
        source_sha256=edit.source_sha256,
    )


def _directory(layout: AssetLayout, relative: str, *, create: bool = False) -> Path:
    path = confined_path(layout.root, relative)
    cursor = layout.root
    for component in Path(relative).parts:
        cursor /= component
        if not cursor.exists():
            if not create:
                break
            cursor.mkdir(mode=0o700)
            parent_fd = os.open(cursor.parent, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        metadata = cursor.stat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError(f"recording edit directory must be private: {cursor}")
    return path


def _read_private(path: Path) -> bytes:
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError(f"recording edit must be one private regular file: {path}")
        if metadata.st_size > MAX_EDIT_BYTES:
            raise SecurityError("recording edit evidence is oversized")
        payload = stream.read(MAX_EDIT_BYTES + 1)
        if len(payload) > MAX_EDIT_BYTES:
            raise SecurityError("recording edit evidence grew beyond its size limit")
    return payload


def read_recording_edit(
    layout: AssetLayout, *, subject_id: str, session_id: str, prompt_id: str, source_sha256: str
) -> RecordingEdit | None:
    """Read validated private edit evidence; absence is unedited, damage raises."""
    key = recording_edit_key(
        subject_id=subject_id,
        session_id=session_id,
        prompt_id=prompt_id,
        source_sha256=source_sha256,
    )
    directory = _directory(layout, "provenance/recording-edits")
    if not directory.exists():
        return None
    path = confined_path(directory, f"{key}.json")
    if not path.exists():
        return None
    payload = _read_private(path)
    try:
        edit = RecordingEdit.model_validate_json(payload)
    except ValidationError as error:
        raise ContractError(f"invalid recording edit evidence: {error}") from error
    if payload != canonical_json_bytes(edit) + b"\n" or _key(edit) != key:
        raise SecurityError("recording edit is noncanonical or bound to another source")
    return edit


def assert_recording_not_removed(
    layout: AssetLayout,
    *,
    subject_id: str,
    session_id: str,
    prompt_id: str,
    source_sha256: str,
    manifest_sha256: str | None = None,
) -> None:
    """Refuse operator-excluded raw sources at dataset and reference boundaries."""
    intent = legacy_edit_intent(
        layout,
        subject_id=subject_id,
        session_id=session_id,
        prompt_id=prompt_id,
        source_sha256=source_sha256,
    )
    if intent is not None:
        require_current_legacy_edit(layout, intent)
        if intent.source.decision in {"removed", "rejected"}:
            raise ContractError("recording was excluded from use; restore it before selecting it")
        result = legacy_edit_result(layout, intent)
        if result is None or result.edit is None:
            raise ContractError(
                "recording edit is pending; wait for compatible evidence before selecting it"
            )
    edit = read_recording_edit(
        layout,
        subject_id=subject_id,
        session_id=session_id,
        prompt_id=prompt_id,
        source_sha256=source_sha256,
    )
    if edit is not None and edit.removed:
        raise ContractError("recording was removed from use; restore it before selecting it")

    from .recording_packages import resolve_native_package_alias

    native = resolve_native_package_alias(
        layout,
        subject_id=subject_id,
        session_id=session_id,
        prompt_id=prompt_id,
        source_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
    )
    if native is not None and native.decision != "kept":
        raise ContractError(
            "native recording is not kept for use; restore it before selecting an alias"
        )


def source_edit_state(
    layout: AssetLayout,
    source: Path,
    *,
    subject_id: str,
    session_id: str,
    prompt_id: str,
    source_sha256: str,
    updated_at: datetime,
) -> RecordingEdit:
    """Bind edit state to a revalidated original WAV; no edit file is created on read."""
    inspection = inspect_canonical_wav(source)
    if inspection.sha256 != source_sha256:
        raise SecurityError("original recording changed before editing")
    edit = read_recording_edit(
        layout,
        subject_id=subject_id,
        session_id=session_id,
        prompt_id=prompt_id,
        source_sha256=source_sha256,
    )
    if edit is None:
        return RecordingEdit(
            subject_id=subject_id,
            session_id=session_id,
            prompt_id=prompt_id,
            source_sha256=source_sha256,
            source_frames=inspection.audio.frames,
            start_frame=0,
            end_frame=inspection.audio.frames,
            updated_at=updated_at,
        )
    if edit.source_frames != inspection.audio.frames:
        raise SecurityError("recording edit frame count disagrees with original recording")
    return edit


def effective_recording_path(layout: AssetLayout, source: Path, edit: RecordingEdit) -> Path:
    """Resolve original or checksum-validated derived audio without altering either."""
    if edit.derived_path is None:
        return source
    path = confined_path(layout.root, edit.derived_path, must_exist=True)
    _directory(layout, path.parent.relative_to(layout.root).as_posix())
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError("derived recording must be one private regular file")
    inspection = inspect_canonical_wav(path)
    if (
        inspection.sha256 != edit.derived_sha256
        or inspection.audio.frames != edit.end_frame - edit.start_frame
    ):
        raise SecurityError("trimmed recording no longer matches its edit evidence")
    return path


@contextmanager
def _edit_lock(layout: AssetLayout, key: str, *, native: bool = False) -> Iterator[None]:
    directory = _directory(
        layout,
        "provenance/recording-edits/native" if native else "provenance/recording-edits",
        create=True,
    )
    lock = confined_path(directory, f".{key}.lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise SecurityError("recording edit lock must be one private regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ContractError("another recording editor is updating this take") from error
        yield
    finally:
        os.close(descriptor)


def _publish_trim(
    layout: AssetLayout, source: Path, edit: RecordingEdit, start_frame: int, end_frame: int
) -> tuple[str, str]:
    directory = _directory(layout, f"derived/recording-edits/{_key(edit)}", create=True)
    return _publish_frame_range(
        layout, source, directory, edit.source_sha256, start_frame, end_frame
    )


def _publish_frame_range(
    layout: AssetLayout,
    source: Path,
    directory: Path,
    source_sha256: str,
    start_frame: int,
    end_frame: int,
) -> tuple[str, str]:
    temporary = confined_path(directory, f".trim-{secrets.token_hex(8)}.wav")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output, open_regular_no_follow(source) as original:
        with wave.open(original, "rb") as reader, wave.open(output, "wb") as writer:
            writer.setparams(reader.getparams())
            reader.setpos(start_frame)
            remaining = end_frame - start_frame
            while remaining:
                payload = reader.readframes(min(remaining, COPY_FRAMES))
                if not payload or len(payload) % 3:
                    raise SecurityError("original recording ended during trim publication")
                writer.writeframesraw(payload)
                remaining -= len(payload) // 3
        output.flush()
        os.fsync(output.fileno())
    if inspect_canonical_wav(source).sha256 != source_sha256:
        raise SecurityError("original recording changed during trim publication")
    inspection = inspect_canonical_wav(temporary)
    destination = confined_path(directory, f"{inspection.sha256}.wav")
    if destination.exists():
        if inspect_canonical_wav(destination) != inspection:
            raise SecurityError("existing trim derivative does not match its digest")
        temporary.unlink()
    else:
        os.rename(temporary, destination)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return destination.relative_to(layout.root).as_posix(), inspection.sha256


def update_recording_edit(
    layout: AssetLayout,
    source: Path,
    state: RecordingEdit,
    *,
    start_frame: int | None = None,
    end_frame: int | None = None,
    removed: bool | None = None,
    reason: str | None = None,
    updated_at: datetime,
    before_publish: Callable[[], None] | None = None,
    publication_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> RecordingEdit:
    """Atomically update one source's edit pointer, retaining all original/derived audio."""
    with _edit_lock(layout, _key(state)):
        current = source_edit_state(
            layout,
            source,
            subject_id=state.subject_id,
            session_id=state.session_id,
            prompt_id=state.prompt_id,
            source_sha256=state.source_sha256,
            updated_at=state.updated_at,
        )
        if current != state:
            raise SecurityError("recording edit changed; refresh before editing")
        updates: dict[str, object] = {"updated_at": updated_at}
        if removed is not None:
            updates.update(removed=removed, removal_reason=reason if removed else None)
        if start_frame is not None or end_frame is not None:
            if (
                type(start_frame) is not int
                or type(end_frame) is not int
                or not 0 <= start_frame < end_frame <= state.source_frames
            ):
                raise ContractError("trim must retain a nonempty frame range within the original")
            path, digest = (
                (None, None)
                if (start_frame, end_frame) == (0, state.source_frames)
                else _publish_trim(layout, source, state, start_frame, end_frame)
            )
            updates.update(
                start_frame=start_frame,
                end_frame=end_frame,
                derived_path=path,
                derived_sha256=digest,
            )
        payload = state.model_dump()
        payload.update(updates)
        try:
            edit = RecordingEdit.model_validate(payload)
        except ValidationError as error:
            raise ContractError(f"invalid recording edit: {error}") from error
        destination = confined_path(
            _directory(layout, "provenance/recording-edits"), f"{_key(edit)}.json"
        )
        with nullcontext() if publication_guard is None else publication_guard():
            if before_publish is not None:
                before_publish()
            write_canonical_json(destination, edit)
        return edit


class RecordingEditRecipe(ContractModel):
    """Version 2 keeps a durable frame decision independent of derivative rendering."""

    schema_version: Literal["cole-voice-lab.recording-edit/v2"] = "cole-voice-lab.recording-edit/v2"
    subject_id: StableId
    take_id: StableId
    prompt_id: StableId
    source_sha256: Sha256
    frame_range: RecordingFrameRange
    edit_revision: PositiveRevision
    operation_id: StableId
    updated_at: UtcTimestamp

    @property
    def trimmed(self) -> bool:
        return (
            self.frame_range.start_frame != 0
            or self.frame_range.end_frame != self.frame_range.source_frames
        )


class RecordingEditDerivative(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-edit-derivative/v1"] = (
        "cole-voice-lab.recording-edit-derivative/v1"
    )
    recipe_sha256: Sha256
    take_id: StableId
    source_sha256: Sha256
    edit_revision: PositiveRevision
    frame_range: RecordingFrameRange
    source_identity: RecordingFileIdentity
    derived_path: str
    derived_sha256: Sha256
    derived_size_bytes: int = Field(gt=44)
    completed_at: UtcTimestamp

    @model_validator(mode="after")
    def _exact_derivative(self) -> RecordingEditDerivative:
        validate_relative_path(self.derived_path)
        if (
            self.derived_path
            != f"derived/recording-edits/native/{self.recipe_sha256}/{self.derived_sha256}.wav"
        ):
            raise ValueError("native derivative path must bind the recipe and rendered digest")
        if self.derived_size_bytes != 44 + 3 * (
            self.frame_range.end_frame - self.frame_range.start_frame
        ):
            raise ValueError("native derivative byte count must match its exact selected frames")
        return self


def native_edit_directory(layout: AssetLayout, take_id: str, *, create: bool = False) -> Path:
    """Native edits are isolated from the permanent v1 evidence namespace."""
    from pydantic import TypeAdapter

    TypeAdapter(StableId).validate_python(take_id)
    return _directory(layout, f"provenance/recording-edits/native/{take_id}", create=create)


def current_native_edit(layout: AssetLayout, take_id: str) -> RecordingEditRecipe | None:
    directory = native_edit_directory(layout, take_id)
    if not directory.exists():
        return None
    path = confined_path(directory, "head.json")
    if not path.exists():
        return None
    payload = _read_private(path)
    recipe = RecordingEditRecipe.model_validate_json(payload)
    if recipe.take_id != take_id or payload != canonical_json_bytes(recipe) + b"\n":
        raise SecurityError("native edit head is noncanonical or binds a different take")
    frozen = confined_path(path.parent, f"{canonical_sha256(recipe)}.json", must_exist=True)
    if _read_private(frozen) != payload:
        raise SecurityError("native edit head differs from its immutable recipe")
    return recipe


def publish_native_edit_recipe(layout: AssetLayout, recipe: RecordingEditRecipe) -> None:
    """Called only while the canonical TakeStore operation lock is held."""
    directory = native_edit_directory(layout, recipe.take_id, create=True)
    payload = canonical_json_bytes(recipe) + b"\n"
    frozen = confined_path(directory, f"{canonical_sha256(recipe)}.json")
    if frozen.exists():
        if _read_private(frozen) != payload:
            raise SecurityError("immutable native edit recipe changed")
    else:
        write_canonical_json(frozen, recipe)
    prior = current_native_edit(layout, recipe.take_id)
    if prior is None or prior.edit_revision < recipe.edit_revision:
        write_canonical_json(directory / "head.json", recipe)
    elif prior.edit_revision == recipe.edit_revision and prior != recipe:
        raise SecurityError("native edit revision binds conflicting recipes")


def native_edit_identity(
    layout: AssetLayout, take_id: str, *, take: RecordingTake | None = None
) -> str:
    """Bind originals and edited ranges uniformly; no derivative or source read is required."""
    from .recording_takes import read_recording_evidence

    if take is None:
        pending = confined_path(layout.root, "provenance/recording-takes/.pending.json")
        if pending.exists():
            raise ContractError("native edit identity requires completed metadata recovery")
        path = confined_path(
            layout.root, f"provenance/recording-takes/{take_id}/head.json", must_exist=True
        )
        take = read_recording_evidence(path, RecordingTake)
        if pending.exists():
            raise ContractError("native edit identity changed during metadata recovery")
    if take.reservation.take_id != take_id:
        raise SecurityError("native edit identity differs from its selected take")
    if take.seal is None or take.frame_range is None:
        raise ContractError("native edit identity requires a sealed original")
    recipe = current_native_edit(layout, take_id)
    if recipe is None:
        if take.edit_revision != 0 or (
            take.frame_range.start_frame,
            take.frame_range.end_frame,
        ) != (0, take.seal.audio.frames):
            raise SecurityError("native take is missing its current edit recipe")
        return canonical_sha256(
            {
                "kind": "native-original",
                "take_id": take_id,
                "source_sha256": take.seal.original_sha256,
                "frame_range": take.frame_range.model_dump(mode="json"),
                "edit_revision": 0,
            }
        )
    if (
        recipe.edit_revision != take.edit_revision
        or recipe.frame_range != take.frame_range
        or recipe.source_sha256 != take.seal.original_sha256
    ):
        raise SecurityError("native edit recipe differs from the current authoritative take")
    return canonical_sha256(recipe)


def _native_source_identity(layout: AssetLayout, path: Path) -> RecordingFileIdentity:
    relative = path.relative_to(layout.root).as_posix()
    confined_path(layout.root, relative, must_exist=True)
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecurityError("native edit source must be one private regular file")
        return RecordingFileIdentity(
            relative_path=relative,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size_bytes=metadata.st_size,
            mode=metadata.st_mode,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )


def render_native_edit(
    layout: AssetLayout,
    source: Path,
    recipe: RecordingEditRecipe,
    *,
    source_identity: RecordingFileIdentity,
    completed_at: datetime,
) -> RecordingEditDerivative:
    """Render only one exact source/range off the interactive path; original bytes never change."""
    if current_native_edit(layout, recipe.take_id) != recipe:
        raise ContractError("native edit job is stale before rendering")
    if _native_source_identity(layout, source) != source_identity:
        raise SecurityError("native edit source identity changed before rendering")
    inspection = inspect_canonical_wav(source)
    if (
        inspection.sha256 != recipe.source_sha256
        or inspection.audio.frames != recipe.frame_range.source_frames
    ):
        raise SecurityError("native edit source differs from its bound original digest/frames")
    directory = _directory(
        layout, f"derived/recording-edits/native/{canonical_sha256(recipe)}", create=True
    )
    relative, digest = _publish_frame_range(
        layout,
        source,
        directory,
        recipe.source_sha256,
        recipe.frame_range.start_frame,
        recipe.frame_range.end_frame,
    )
    if _native_source_identity(layout, source) != source_identity:
        raise SecurityError("native edit source identity changed during rendering")
    result = RecordingEditDerivative(
        recipe_sha256=canonical_sha256(recipe),
        take_id=recipe.take_id,
        source_sha256=recipe.source_sha256,
        edit_revision=recipe.edit_revision,
        frame_range=recipe.frame_range,
        source_identity=source_identity,
        derived_path=relative,
        derived_sha256=digest,
        derived_size_bytes=44 + 3 * (recipe.frame_range.end_frame - recipe.frame_range.start_frame),
        completed_at=completed_at,
    )
    # The immutable artifact remains useful evidence; a stale job never installs
    # an effective pointer for a newer range. The parent publishes matching events.
    with _edit_lock(layout, recipe.take_id, native=True):
        if current_native_edit(layout, recipe.take_id) != recipe:
            raise ContractError("native edit job became stale during rendering")
        destination = (
            native_edit_directory(layout, recipe.take_id)
            / f"{canonical_sha256(recipe)}.derivative.json"
        )
        if destination.exists():
            previous = RecordingEditDerivative.model_validate_json(_read_private(destination))
            if previous.model_dump(exclude={"completed_at"}) != result.model_dump(
                exclude={"completed_at"}
            ):
                raise SecurityError("native derivative identity changed for one recipe")
            return previous
        write_canonical_json(destination, result)
    return result


def native_edit_derivative(layout: AssetLayout, take_id: str) -> RecordingEditDerivative | None:
    recipe = current_native_edit(layout, take_id)
    if recipe is None:
        return None
    path = native_edit_directory(layout, take_id) / f"{canonical_sha256(recipe)}.derivative.json"
    if not path.exists():
        return None
    payload = _read_private(path)
    result = RecordingEditDerivative.model_validate_json(payload)
    if (
        payload != canonical_json_bytes(result) + b"\n"
        or result.recipe_sha256 != canonical_sha256(recipe)
        or result.source_sha256 != recipe.source_sha256
        or result.edit_revision != recipe.edit_revision
        or result.frame_range != recipe.frame_range
        or result.take_id != take_id
    ):
        raise SecurityError("native derivative result does not bind the current recipe")
    return result


RECORDING_EDIT_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "recording-edit": RecordingEdit,
    "recording-edit-recipe": RecordingEditRecipe,
    "recording-edit-derivative": RecordingEditDerivative,
}


class RecordingLegacyEditIntent(ContractModel):
    """Durable legacy intent precedes its compatible v1 derivative and edit pointer."""

    schema_version: Literal["cole-voice-lab.recording-legacy-edit-intent/v1"] = (
        "cole-voice-lab.recording-legacy-edit-intent/v1"
    )
    request: EditTakeCommand | DecisionCommand = Field(discriminator="command")
    request_sha256: Sha256
    session_id: StableId
    before: RecordingReviewSource
    source: RecordingReviewSource
    cursor: RecordingCursor
    original_edit: RecordingEdit
    projection: RecordingHistoryEntry
    restore_decision: Literal["draft", "kept"]
    committed_at: UtcTimestamp

    @model_validator(mode="after")
    def _legacy_binding(self) -> RecordingLegacyEditIntent:
        if self.request_sha256 != canonical_sha256(self.request):
            raise ValueError("legacy intent must bind its exact command")
        if (
            self.before.original_kind == "native-original"
            or self.source.original_kind == "native-original"
            or self.before.take_id != self.source.take_id
            or self.request.take_id != self.source.take_id
            or self.request.selection != self.cursor.selection
            or self.cursor.selection.take_id != self.source.take_id
            or self.original_edit.subject_id != self.source.subject_id
            or self.original_edit.session_id != self.session_id
            or self.original_edit.prompt_id != self.source.prompt.prompt_id
            or self.original_edit.source_sha256 != self.source.source_sha256
            or self.projection.take_id != self.source.take_id
            or self.projection.legacy_session_id != self.session_id
            or self.projection.decision != self.source.decision
        ):
            raise ValueError("legacy intent must bind one exact source and selection")
        ReviewActionEvent(
            operation_id=self.request.operation_id,
            action=self.action,
            source=self.source,
            cursor=self.cursor,
            state="pending",
        )
        if (
            self.projection.prompt != self.source.prompt
            or self.projection.source_sha256 != self.source.source_sha256
            or self.projection.source_frames != self.source.source_frames
            or self.original_edit.source_frames != self.source.source_frames
        ):
            raise ValueError(
                "legacy intent projection and original must preserve exact source frames"
            )
        for field in (
            "catalog_sha256",
            "original_kind",
            "subject_id",
            "prompt",
            "source_path",
            "source_sha256",
            "source_frames",
            "source_identity",
            "recorded_at",
            "date_precision",
        ):
            if getattr(self.before, field) != getattr(self.source, field):
                raise ValueError("legacy edits cannot rewrite source or recording provenance")
        if isinstance(self.request, EditTakeCommand):
            if (
                self.source.edit_revision != self.before.edit_revision + 1
                or self.request.source_sha256 != self.source.source_sha256
                or self.source.decision != self.before.decision
                or self.request.expected_edit_revision != self.before.edit_revision
                or self.source.frame_range != self.request.frame_range
                or self.source.decision_revision != self.before.decision_revision
            ):
                raise ValueError("legacy trim revisions must match their requested frame recipe")
        elif (
            self.source.decision_revision != self.before.decision_revision + 1
            or self.request.expected_decision_revision != self.before.decision_revision
            or self.source.edit_revision != self.before.edit_revision
        ):
            raise ValueError("legacy decision revisions must match their explicit intent")
        if isinstance(self.request, DecisionCommand):
            expected = (
                self.restore_decision
                if self.request.action == "restore"
                else "removed"
                if self.request.action == "remove"
                else "rejected"
            )
            if (
                self.source.decision != expected
                or self.source.frame_range != self.before.frame_range
            ):
                raise ValueError("legacy decision must preserve frames and apply its exact action")
        return self

    @property
    def action(self) -> Literal["edit", "reject", "remove", "restore"]:
        return "edit" if isinstance(self.request, EditTakeCommand) else self.request.action


class RecordingLegacyEditResult(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-legacy-edit-result/v1"] = (
        "cole-voice-lab.recording-legacy-edit-result/v1"
    )
    intent_sha256: Sha256
    edit: RecordingEdit | None = None
    error: RecordingError | None = None

    @model_validator(mode="after")
    def _outcome(self) -> RecordingLegacyEditResult:
        if (self.edit is None) == (self.error is None):
            raise ValueError("legacy materialization requires one compatible edit or error")
        return self


def legacy_intent_directory(layout: AssetLayout, key: str, *, create: bool = False) -> Path:
    from pydantic import TypeAdapter

    TypeAdapter(Sha256).validate_python(key)
    return _directory(layout, f"provenance/recording-edits/legacy/{key}", create=create)


def _legacy_key(intent: RecordingLegacyEditIntent) -> str:
    return recording_edit_key(
        subject_id=intent.source.subject_id,
        session_id=intent.session_id,
        prompt_id=intent.source.prompt.prompt_id,
        source_sha256=intent.source.source_sha256,
    )


def legacy_edit_intent(
    layout: AssetLayout, *, subject_id: str, session_id: str, prompt_id: str, source_sha256: str
) -> RecordingLegacyEditIntent | None:
    from .recording_takes import read_recording_evidence

    key = recording_edit_key(
        subject_id=subject_id,
        session_id=session_id,
        prompt_id=prompt_id,
        source_sha256=source_sha256,
    )
    directory = legacy_intent_directory(layout, key)
    if not directory.exists():
        return None
    path = confined_path(directory, "head.json")
    if not path.exists():
        return None
    intent = read_recording_evidence(path, RecordingLegacyEditIntent)
    if _legacy_key(intent) != key:
        raise SecurityError("legacy edit head binds another original source")
    frozen = read_recording_evidence(
        confined_path(
            layout.root,
            f"provenance/recording-edits/legacy/.operations/{intent.request.operation_id}.json",
        ),
        RecordingLegacyEditIntent,
    )
    if frozen != intent:
        raise SecurityError("legacy edit head differs from its immutable request")
    return intent


def legacy_edit_result(
    layout: AssetLayout, intent: RecordingLegacyEditIntent
) -> RecordingLegacyEditResult | None:
    from .recording_takes import read_recording_evidence

    path = (
        legacy_intent_directory(layout, _legacy_key(intent))
        / f"{intent.request.operation_id}.result.json"
    )
    if not path.exists():
        return None
    result = read_recording_evidence(path, RecordingLegacyEditResult)
    if result.intent_sha256 != canonical_sha256(intent):
        raise SecurityError("legacy edit result binds another intent")
    if result.edit is not None and (
        _key(result.edit) != _legacy_key(intent)
        or result.edit.start_frame != intent.source.frame_range.start_frame
        or result.edit.end_frame != intent.source.frame_range.end_frame
        or result.edit.removed != (intent.source.decision in {"removed", "rejected"})
    ):
        raise SecurityError("legacy edit result differs from its requested frame/decision")
    return result


def require_current_legacy_edit(layout: AssetLayout, intent: RecordingLegacyEditIntent) -> None:
    """Do not let an older native intent hide a later edit by a permanent v1 reader."""
    result = legacy_edit_result(layout, intent)
    if result is None or result.edit is None:
        return
    current = read_recording_edit(
        layout,
        subject_id=intent.source.subject_id,
        session_id=intent.session_id,
        prompt_id=intent.source.prompt.prompt_id,
        source_sha256=intent.source.source_sha256,
    )
    if current != result.edit:
        raise SecurityError("compatible legacy edit changed after its bound native intent")


def legacy_action_event(
    intent: RecordingLegacyEditIntent, result: RecordingLegacyEditResult | None = None
) -> ReviewActionEvent:
    return ReviewActionEvent(
        operation_id=intent.request.operation_id,
        action=intent.action,
        source=intent.source,
        cursor=intent.cursor,
        state="pending" if result is None else "completed" if result.edit is not None else "failed",
        error=None if result is None else result.error,
    )


def commit_legacy_edit(
    layout: AssetLayout,
    *,
    request: EditTakeCommand | DecisionCommand,
    source: RecordingReviewSource,
    cursor: RecordingCursor,
    entry: RecordingHistoryEntry,
    committed_at: datetime,
    commit_barrier: Callable[[], None] | None = None,
) -> RecordingLegacyEditIntent:
    """Commit only private metadata; owned rendering materializes the permanent v1 contract."""
    from .recording_takes import read_recording_evidence

    if (
        entry.legacy_session_id is None
        or request.take_id != source.take_id
        or request.selection != cursor.selection
    ):
        raise ContractError("legacy edit requires its exact displayed source")
    key = recording_edit_key(
        subject_id=source.subject_id,
        session_id=entry.legacy_session_id,
        prompt_id=source.prompt.prompt_id,
        source_sha256=source.source_sha256,
    )
    with (
        _edit_lock(layout, "legacy-operation-" + request.operation_id, native=True),
        _edit_lock(layout, "legacy-intent-" + key, native=True),
    ):
        directory = legacy_intent_directory(layout, key, create=True)
        operations = _directory(
            layout, "provenance/recording-edits/legacy/.operations", create=True
        )
        operation = confined_path(operations, f"{request.operation_id}.json")
        if operation.exists():
            replay = read_recording_evidence(operation, RecordingLegacyEditIntent)
            if replay.request != request:
                raise SecurityError("legacy operation ID already binds a different request")
            # An immutable request is retained if the process stopped before its
            # head commit; an exact retry completes that same metadata decision.
            head = directory / "head.json"
            current = (
                None
                if not head.exists()
                else read_recording_evidence(head, RecordingLegacyEditIntent)
            )
            if (
                current is None
                or current.projection.state_revision <= replay.projection.state_revision
            ):
                if commit_barrier is not None:
                    commit_barrier()
                write_canonical_json(head, replay)
            return replay
        current = legacy_edit_intent(
            layout,
            subject_id=source.subject_id,
            session_id=entry.legacy_session_id,
            prompt_id=source.prompt.prompt_id,
            source_sha256=source.source_sha256,
        )
        if current is not None and (
            source.edit_revision != current.source.edit_revision
            or source.decision_revision != current.source.decision_revision
        ):
            raise ContractError("legacy source changed; refresh its selected revision")
        state = read_recording_edit(
            layout,
            subject_id=source.subject_id,
            session_id=entry.legacy_session_id,
            prompt_id=source.prompt.prompt_id,
            source_sha256=source.source_sha256,
        )
        if state is None:
            state = RecordingEdit(
                subject_id=source.subject_id,
                session_id=entry.legacy_session_id,
                prompt_id=source.prompt.prompt_id,
                source_sha256=source.source_sha256,
                source_frames=source.source_frames,
                start_frame=0,
                end_frame=source.source_frames,
                updated_at=committed_at,
            )
        restore = (
            current.restore_decision
            if current is not None
            else source.decision
            if source.decision in {"draft", "kept"}
            else "kept"
            if source.original_kind == "accepted-raw"
            else "draft"
        )
        updates: dict[str, object] = {}
        if isinstance(request, EditTakeCommand):
            if (
                request.expected_edit_revision != source.edit_revision
                or request.source_sha256 != source.source_sha256
                or request.frame_range.source_frames != source.source_frames
            ):
                raise ContractError("legacy frame recipe is stale or names a different original")
            updates.update(frame_range=request.frame_range, edit_revision=source.edit_revision + 1)
        else:
            if request.expected_decision_revision != source.decision_revision:
                raise ContractError("legacy decision revision changed")
            if request.action == "restore" and source.decision not in {"removed", "rejected"}:
                raise ContractError("legacy source is not excluded")
            updates.update(
                decision=restore
                if request.action == "restore"
                else "rejected"
                if request.action == "reject"
                else "removed",
                decision_revision=source.decision_revision + 1,
            )
        after = RecordingReviewSource.model_validate(source.model_dump() | updates)
        projected = RecordingHistoryEntry.model_validate(
            entry.model_dump()
            | {
                "decision": after.decision,
                "state_revision": max(
                    entry.state_revision,
                    0 if current is None else current.projection.state_revision,
                )
                + 1,
            }
        )
        intent = RecordingLegacyEditIntent(
            request=request,
            request_sha256=canonical_sha256(request),
            session_id=entry.legacy_session_id,
            before=source,
            source=after,
            cursor=cursor,
            original_edit=state,
            projection=projected,
            restore_decision=cast(Literal["draft", "kept"], restore),
            committed_at=committed_at,
        )
        if commit_barrier is not None:
            commit_barrier()
        write_canonical_json(operation, intent)
        write_canonical_json(directory / "head.json", intent)
        return intent


def materialize_legacy_edit(
    layout: AssetLayout, intent: RecordingLegacyEditIntent
) -> RecordingLegacyEditResult:
    """Owned child work; a superseded recipe cannot replace compatible v1 evidence."""

    def current() -> None:
        if (
            legacy_edit_intent(
                layout,
                subject_id=intent.source.subject_id,
                session_id=intent.session_id,
                prompt_id=intent.source.prompt.prompt_id,
                source_sha256=intent.source.source_sha256,
            )
            != intent
        ):
            raise SecurityError("legacy edit was superseded during rendering")

    @contextmanager
    def publication_guard() -> Iterator[None]:
        with _edit_lock(layout, "legacy-intent-" + _legacy_key(intent), native=True):
            current()
            yield

    current()
    previous = legacy_edit_result(layout, intent)
    if previous is not None and previous.edit is not None:
        return previous
    source = confined_path(layout.root, intent.source.source_path, must_exist=True)
    if _native_source_identity(layout, source) != intent.source.source_identity:
        raise SecurityError("legacy original identity changed before rendering")
    # A prior worker may have completed the compatible pointer before losing its
    # reply. Verify that pointer and recover the same result without re-rendering.
    state = read_recording_edit(
        layout,
        subject_id=intent.source.subject_id,
        session_id=intent.session_id,
        prompt_id=intent.source.prompt.prompt_id,
        source_sha256=intent.source.source_sha256,
    )
    expected_removed = intent.source.decision in {"removed", "rejected"}
    if (
        state is not None
        and state.updated_at == intent.committed_at
        and state.start_frame == intent.source.frame_range.start_frame
        and state.end_frame == intent.source.frame_range.end_frame
        and state.removed == expected_removed
    ):
        edit = state
    else:
        edit = update_recording_edit(
            layout,
            source,
            intent.original_edit,
            start_frame=intent.source.frame_range.start_frame,
            end_frame=intent.source.frame_range.end_frame,
            removed=expected_removed,
            reason=(
                intent.request.reason
                if isinstance(intent.request, DecisionCommand) and intent.request.reason is not None
                else "Excluded by the operator"
            )
            if expected_removed
            else None,
            updated_at=intent.committed_at,
            publication_guard=publication_guard,
        )
    current()
    if _native_source_identity(layout, source) != intent.source.source_identity:
        raise SecurityError("legacy original identity changed during rendering")
    result = RecordingLegacyEditResult(intent_sha256=canonical_sha256(intent), edit=edit)
    write_canonical_json(
        legacy_intent_directory(layout, _legacy_key(intent))
        / f"{intent.request.operation_id}.result.json",
        result,
    )
    return result


def project_legacy_edit(layout: AssetLayout, entry: RecordingHistoryEntry) -> RecordingHistoryEntry:
    if entry.legacy_session_id is None or entry.source_sha256 is None:
        return entry
    intent = legacy_edit_intent(
        layout,
        subject_id=entry.subject_id,
        session_id=entry.legacy_session_id,
        prompt_id=entry.prompt.prompt_id,
        source_sha256=entry.source_sha256,
    )
    if intent is None:
        return entry
    require_current_legacy_edit(layout, intent)
    if intent.projection.take_id != entry.take_id or intent.projection.prompt != entry.prompt:
        raise SecurityError("legacy action projection differs from its original event")
    return intent.projection


RECORDING_EDIT_SCHEMA_MODELS.update(
    {
        "recording-legacy-edit-intent": RecordingLegacyEditIntent,
        "recording-legacy-edit-result": RecordingLegacyEditResult,
    }
)
