"""Canonical JSON and descriptor-based hashing helpers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

from pydantic import BaseModel

from .errors import SecurityError

_CHUNK_BYTES = 1024 * 1024


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


def open_regular_no_follow(path: Path) -> BinaryIO:
    """Open one regular file without following its final symlink."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SecurityError(f"cannot safely open regular file {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecurityError(f"expected a regular file: {path}")
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
