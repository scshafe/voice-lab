"""Canonical JSON, descriptor-based hashing and write-once publishing helpers."""

from __future__ import annotations

import ctypes
import errno
import functools
import hashlib
import json
import os
import secrets
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Final, cast

from pydantic import BaseModel, ValidationError

from .errors import ContractError, SecurityError

_CHUNK_BYTES = 1024 * 1024
# darwin <sys/stdio.h>: the renameatx_np flag that fails with EEXIST instead of replacing.
_RENAME_EXCL: Final = 0x00000004
# darwin <sys/fcntl.h>: the current directory for *at calls.
_DARWIN_AT_FDCWD: Final = -2
# Linux <fcntl.h> / <linux/fs.h>: the current directory for *at calls, and the renameat2 flag
# that fails with EEXIST instead of replacing.
_AT_FDCWD: Final = -100
_RENAME_NOREPLACE: Final = 1
# Errors with which the kernel or filesystem refuses the no-replace flag itself. EINVAL is
# ambiguous: it also reports a directory renamed into itself.
_NO_REPLACE_REFUSED_ERRNOS: Final = frozenset(
    {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
)
# call(source_directory, source, target_directory, target) -> the C call's result
type _RenameAtCall = Callable[[int, bytes, int, bytes], int]


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON deterministically without platform-dependent whitespace."""

    return json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def require_private_metadata(
    metadata: os.stat_result, label: str, *, directory: bool = False
) -> None:
    """Fail closed unless ``metadata`` describes an owned private file or directory.

    Params: ``metadata`` is an ``os.stat``/``os.fstat`` result; ``label`` names the path in
    the error; ``directory`` selects the directory rule (S_ISDIR, no link-count check)
    instead of the regular-file rule (S_ISREG and exactly one hard link).
    Returns: nothing.
    Raises: ``SecurityError`` when the type is wrong, the owner is not this process's
    user, any group/other permission bit is set, or a regular file has more than one link.
    """
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        not valid_type
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or (not directory and metadata.st_nlink != 1)
    ):
        kind = "directory" if directory else "regular file"
        raise SecurityError(f"requires an owned private {kind}: {label}")


def file_identity(metadata: os.stat_result) -> tuple[int, int]:
    """The ``(st_dev, st_ino)`` pair that detects a replaced file or directory."""
    return metadata.st_dev, metadata.st_ino


def open_regular_no_follow(path: Path, *, nonblocking: bool = False) -> BinaryIO:
    """Open one regular file without following its final symlink.

    Params: ``path``; ``nonblocking`` opens with ``O_NONBLOCK`` so an operator-named FIFO or
    device cannot block the open itself (the flag is cleared again once ``fstat`` shows a
    regular file, so reads behave exactly as without it).
    Returns: the open binary stream.
    Raises: ``SecurityError`` when the path cannot be opened this way (a final symlink
    included) or is not a regular file.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if nonblocking:
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SecurityError(f"cannot safely open regular file {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecurityError(f"expected a regular file: {path}")
        if nonblocking:
            os.set_blocking(descriptor, True)
        return cast(BinaryIO, os.fdopen(descriptor, "rb", closefd=True))
    except Exception:
        os.close(descriptor)
        raise


def sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(_CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def sha256_file(path: Path) -> tuple[str, int]:
    with open_regular_no_follow(path) as stream:
        return sha256_stream(stream)


def write_canonical_json(path: Path, value: object, *, mode: int = 0o600) -> str:
    """Atomically write canonical JSON and return the exact file digest."""

    payload = canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, mode)
    descriptor_owned = True
    try:
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor_owned = False
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if descriptor_owned:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return hashlib.sha256(payload).hexdigest()


class NoReplaceRenameUnavailableError(OSError):
    """No atomic no-replace rename is available for this call; nothing was renamed.

    ``errno`` is the call's own error when the kernel or filesystem refused the no-replace
    flag (``EINVAL``, which also reports a directory renamed into itself, ``ENOSYS``,
    ``ENOTSUP`` or ``EOPNOTSUPP``). Callers map it to their own refusal; it is never worked
    around with an existence check or a ``link``/``unlink`` pair.
    """


class NoReplaceRenameUnsupportedPlatformError(NoReplaceRenameUnavailableError):
    """This platform or C library has no no-replace rename at all (``errno`` is ``ENOTSUP``)."""


def _system_platform() -> str:
    return sys.platform


@functools.cache
def _c_library() -> Any:
    return ctypes.CDLL(None, use_errno=True)


def _no_replace_rename_call(platform: str) -> tuple[_RenameAtCall, int]:
    """The platform's descriptor-relative no-replace rename and its ``AT_FDCWD`` value.

    The call is ``call(source_directory, source, target_directory, target) -> C result``:
    darwin's ``renameatx_np(..., RENAME_EXCL)`` or glibc's ``renameat2(..., RENAME_NOREPLACE)``.
    Raises: ``NoReplaceRenameUnsupportedPlatformError`` on a platform other than darwin and
    Linux, or when the C library lacks the call (glibc before 2.28).
    """
    if platform == "darwin":
        function_name, flag, current_directory = "renameatx_np", _RENAME_EXCL, _DARWIN_AT_FDCWD
    elif platform.startswith("linux"):
        function_name, flag, current_directory = "renameat2", _RENAME_NOREPLACE, _AT_FDCWD
    else:
        raise NoReplaceRenameUnsupportedPlatformError(
            errno.ENOTSUP, f"no atomic no-replace rename exists on {platform}"
        )
    try:
        function = getattr(_c_library(), function_name)
    except AttributeError as error:
        raise NoReplaceRenameUnsupportedPlatformError(
            errno.ENOTSUP, f"the C library on {platform} has no no-replace rename"
        ) from error
    # int renameatx_np(int, const char *, int, const char *, unsigned int), and renameat2 alike
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int

    def call(source_directory: int, source: bytes, target_directory: int, target: bytes) -> int:
        return int(function(source_directory, source, target_directory, target, flag))

    return call, current_directory


def _c_path(value: str | Path) -> bytes:
    """``value`` encoded for C; an embedded NUL is refused, as ``os`` refuses it.

    Raises: ``ValueError`` for a NUL byte (C would silently cut the name there).
    """
    encoded = os.fsencode(value)
    if b"\x00" in encoded:
        raise ValueError(f"embedded null byte in a path: {value!r}")
    return encoded


def _require_renamed(result: int, target: str) -> None:
    """Raise the error of a no-replace rename call that returned ``result``; see its callers."""
    if result == 0:
        return
    number = ctypes.get_errno()
    message = os.strerror(number)
    if number == errno.EEXIST:
        raise FileExistsError(number, message, target)
    if number in _NO_REPLACE_REFUSED_ERRNOS:
        raise NoReplaceRenameUnavailableError(
            number, f"{message} (no-replace rename refused)", target
        )
    raise OSError(number, message, target)


def rename_no_replace(source: Path, target: Path) -> None:
    """Rename ``source`` to ``target`` in one atomic step that never replaces ``target``.

    darwin uses ``renameatx_np(AT_FDCWD, ..., RENAME_EXCL)``; Linux (glibc) uses
    ``renameat2(AT_FDCWD, ..., RENAME_NOREPLACE)``. Files and directories both work, within
    one directory or across directories of one filesystem. A final symlink is renamed, never
    followed; parent components are resolved by the kernel, so the caller confines both
    paths (``rename_no_replace_at`` renames between open directories instead). The rename is
    durable only once the caller has fsynced the directories involved (``fsync_directory``).
    Params: ``source``, an existing entry; ``target``, a name that must not exist.
    Returns: nothing.
    Raises: ``ValueError`` for a path with an embedded NUL (before any I/O);
    ``FileExistsError`` when ``target`` exists (both entries are left unchanged);
    ``NoReplaceRenameUnavailableError`` when the kernel or filesystem refused the no-replace
    flag, and its subclass ``NoReplaceRenameUnsupportedPlatformError`` on a platform without
    such a call; any other failure as ``OSError(errno, strerror, target)`` (for example
    ``FileNotFoundError`` for a missing ``source``).
    """
    encoded_source, encoded_target = _c_path(source), _c_path(target)
    call, current_directory = _no_replace_rename_call(_system_platform())
    result = call(current_directory, encoded_source, current_directory, encoded_target)
    _require_renamed(result, str(target))


def rename_no_replace_at(
    source_directory: int, source_name: str, target_directory: int, target_name: str
) -> None:
    """Rename one entry of an open directory into another without ever replacing the target.

    As ``rename_no_replace``, but each name is one entry of an open directory, so no path
    component is resolved again: a directory swapped for a symlink after the caller opened it
    cannot redirect the rename.
    Params: ``source_directory`` and ``target_directory``, open directory descriptors (they
    may be the same); ``source_name``, an existing entry of the first; ``target_name``, a
    name that must not exist in the second. Each name is one path component.
    Returns: nothing.
    Raises: ``ValueError`` for a negative descriptor, a name that is empty, ``.``, ``..``,
    holds a ``/`` or an embedded NUL (before any I/O); otherwise as ``rename_no_replace``,
    with ``target_name`` as the error's file name.
    """
    for descriptor in (source_directory, target_directory):
        if descriptor < 0:
            raise ValueError(f"a directory descriptor is not negative: {descriptor}")
    for name in (source_name, target_name):
        if name in {"", ".", ".."} or "/" in name:
            raise ValueError(f"a name below a directory descriptor is one component: {name!r}")
    encoded_source, encoded_target = _c_path(source_name), _c_path(target_name)
    call, _ = _no_replace_rename_call(_system_platform())
    result = call(source_directory, encoded_source, target_directory, encoded_target)
    _require_renamed(result, target_name)


def fsync_directory(path: Path) -> None:
    """Flush a directory's entries (a create, rename or unlink in it) to stable storage.

    Params: ``path``, a directory; a final symlink is not followed.
    Returns: nothing.
    Raises: ``OSError`` when ``path`` cannot be opened as a directory this way (missing, not a
    directory, or a symlink) or the fsync fails.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_private_file(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Publish ``data`` as a new private file at ``path``; an existing entry is never replaced.

    A temporary ``.<name>.tmp-<16 hex>`` is created beside ``path``
    (``O_CREAT|O_EXCL|O_NOFOLLOW``, 0600), written, given ``mode`` and fsynced, then renamed
    into place with ``rename_no_replace``, and the directory is fsynced. A reader sees either
    no file or the complete file with its final mode and one link. On any failure before the
    rename the temporary is removed; nothing is ever left at ``path`` half-written.
    Params: ``path``, whose parent directory exists (the caller confines it); ``data``;
    ``mode``, owner permission bits only (for example 0o600 or 0o400).
    Returns: nothing.
    Raises: ``ValueError`` for a ``mode`` with bits outside 0o700 (before any I/O);
    ``FileExistsError`` when ``path`` exists; ``NoReplaceRenameUnavailableError`` (see
    ``rename_no_replace``); ``OSError`` from creating, writing or syncing (for example
    ``FileNotFoundError`` for a missing parent), including a failed directory fsync after
    the file was published.
    """
    if mode & ~0o700:
        raise ValueError(f"a private file mode has owner bits only, not {mode:#o}")
    temporary = path.parent / f".{path.name}.tmp-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(temporary, flags, 0o600)
    descriptor_owned = True
    try:
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor_owned = False
        with stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        rename_no_replace(temporary, path)
    except BaseException:
        if descriptor_owned:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    fsync_directory(path.parent)


def require_evidence_file(path: Path, *, max_bytes: int | None = None) -> os.stat_result:
    """Return ``path``'s metadata when it is one plain, unshared evidence file.

    The rule every reviewed record on this Mac is read under — approvals, gates, bindings and
    the vault's own records: one regular file, not a symbolic link, with exactly one link, and
    no larger than the caller's bound.
    Params: ``path``; ``max_bytes``, the largest size accepted (no bound when ``None``).
    Returns: the ``lstat`` metadata.
    Raises: ``FileNotFoundError`` when nothing is there; ``SecurityError`` for a link, a
    non-regular file or a hard-linked one; ``ContractError`` when it is too large.
    """
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SecurityError(f"evidence must be a regular non-link file: {path}")
    if metadata.st_nlink != 1:
        raise SecurityError(f"hard-linked evidence is forbidden: {path}")
    if max_bytes is not None and metadata.st_size > max_bytes:
        raise ContractError(f"evidence exceeds {max_bytes} bytes: {path}")
    return metadata


def read_canonical_model[ModelT: BaseModel](
    path: Path, model_type: type[ModelT], *, max_bytes: int
) -> ModelT:
    """Read one record whose file must be exactly its canonical JSON plus a newline.

    Params: ``path``; ``model_type``, the strict contract it must be; ``max_bytes``, the
    largest file accepted.
    Returns: the validated record.
    Raises: as ``require_evidence_file``; ``ContractError`` when the bytes are not valid for
    the contract or are not the record's canonical form.
    """
    require_evidence_file(path, max_bytes=max_bytes)
    try:
        with open_regular_no_follow(path) as stream:
            payload = stream.read(max_bytes + 1)
        record = model_type.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"invalid canonical evidence {path}: {error}") from error
    if payload != canonical_json_bytes(record) + b"\n":
        raise ContractError(f"JSON evidence is not canonical: {path}")
    return record


def read_json_object(path: Path) -> Mapping[str, Any]:
    """Read a bounded-by-caller JSON object through the no-follow boundary."""

    with open_regular_no_follow(path) as stream:
        value = json.load(stream)
    if not isinstance(value, Mapping):
        raise ValueError(f"expected a JSON object in {path}")
    return cast(Mapping[str, Any], value)


def json_ready(value: Mapping[str, Any] | Sequence[Any]) -> object:
    """Narrow helper retained for callers that assemble JSON dynamically."""

    return value
