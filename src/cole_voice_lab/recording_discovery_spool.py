"""Owned descriptor transport for prepared metadata; historical paths never reach a publisher.

@canonical discovery_spool -- bounded framed metadata and digest-bound offset references.
"""

from __future__ import annotations

import hashlib
import os
import select
import stat
import struct
import threading
from collections.abc import Iterable, Iterator

from .canonical import canonical_json_bytes
from .errors import ContractError, SecurityError
from .models import ContractModel
from .recording_discovery_contracts import (
    MAX_DISCOVERY_RECORD_BYTES,
    MAX_DISCOVERY_RECORDS,
    MAX_DISCOVERY_SPOOL_BYTES,
    DiscoverySpoolRef,
)


def validate_spool(descriptor: int) -> None:
    """Require the inherited, unlinked private file; never accept a worker-named path."""
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 0
        or metadata.st_size > MAX_DISCOVERY_SPOOL_BYTES
    ):
        raise SecurityError("discovery spool must be an owned private unlinked file")


def write_frame(descriptor: int, value: ContractModel) -> None:
    """Write one bounded control or spool record, handling short writes explicitly."""
    payload = canonical_json_bytes(value)
    if not 0 < len(payload) <= MAX_DISCOVERY_RECORD_BYTES:
        raise ContractError("discovery metadata frame exceeds its bound")
    data = struct.pack(">I", len(payload)) + payload
    while data:
        written = os.write(descriptor, data)
        if written <= 0:
            raise OSError("discovery metadata write made no progress")
        data = data[written:]


def read_frame(descriptor: int, stop: threading.Event | None = None) -> bytes:
    """Read one bounded pipe frame; parent cancellation never waits on a child indefinitely."""

    def exact(length: int) -> bytes:
        result = bytearray()
        while len(result) < length:
            if stop is not None and stop.is_set():
                raise EOFError("discovery owner stopped")
            if not select.select([descriptor], [], [], 0.05)[0]:
                continue
            chunk = os.read(descriptor, length - len(result))
            if not chunk:
                raise EOFError("discovery peer ended")
            result.extend(chunk)
        return bytes(result)

    length = struct.unpack(">I", exact(4))[0]
    if not 0 < length <= MAX_DISCOVERY_RECORD_BYTES:
        raise ContractError("discovery metadata frame exceeds its bound")
    return exact(length)


def spool_records(descriptor: int, values: Iterable[ContractModel]) -> DiscoverySpoolRef:
    """Append bounded prepared records; acknowledgments prevent rewriting published ranges."""
    validate_spool(descriptor)
    start = os.lseek(descriptor, 0, os.SEEK_END)
    count = 0
    for value in values:
        count += 1
        if count > MAX_DISCOVERY_RECORDS:
            raise ContractError("discovery spool record count exceeds its bound")
        write_frame(descriptor, value)
        if os.lseek(descriptor, 0, os.SEEK_CUR) > MAX_DISCOVERY_SPOOL_BYTES:
            raise ContractError("discovery spool bytes exceed their bound")
    end = os.lseek(descriptor, 0, os.SEEK_CUR)
    if count == 0:
        raise ContractError("discovery spool requires an explicit record")
    digest = hashlib.sha256()
    offset = start
    while offset < end:
        data = os.pread(descriptor, min(MAX_DISCOVERY_RECORD_BYTES, end - offset), offset)
        if not data:
            raise SecurityError("discovery spool was truncated")
        digest.update(data)
        offset += len(data)
    return DiscoverySpoolRef(
        offset=start, size_bytes=end - start, sha256=digest.hexdigest(), record_count=count
    )


def verify_spool(descriptor: int, reference: DiscoverySpoolRef) -> None:
    """Verify a complete prepared range outside all projection transactions."""
    validate_spool(descriptor)
    end = reference.offset + reference.size_bytes
    if end > os.fstat(descriptor).st_size or end > MAX_DISCOVERY_SPOOL_BYTES:
        raise SecurityError("discovery spool reference exceeds its owned file")
    digest = hashlib.sha256()
    offset = reference.offset
    while offset < end:
        data = os.pread(descriptor, min(MAX_DISCOVERY_RECORD_BYTES, end - offset), offset)
        if not data:
            raise SecurityError("discovery spool was truncated")
        digest.update(data)
        offset += len(data)
    if digest.hexdigest() != reference.sha256:
        raise SecurityError("discovery spool digest changed")
    # Also validate frame boundaries/counts before a publisher opens its transaction.
    if sum(1 for _ in iter_spool(descriptor, reference)) != reference.record_count:
        raise SecurityError("discovery spool record count changed")


def append_spool(destination: int, source: int, reference: DiscoverySpoolRef) -> DiscoverySpoolRef:
    """Publish a completed legacy section after native alias callbacks have finished."""
    verify_spool(source, reference)
    validate_spool(destination)
    start = os.lseek(destination, 0, os.SEEK_END)
    if start + reference.size_bytes > MAX_DISCOVERY_SPOOL_BYTES:
        raise ContractError("discovery spool bytes exceed their bound")
    offset = reference.offset
    remaining = reference.size_bytes
    while remaining:
        data = os.pread(source, min(MAX_DISCOVERY_RECORD_BYTES, remaining), offset)
        if not data:
            raise SecurityError("prepared discovery spool was truncated")
        offset += len(data)
        remaining -= len(data)
        while data:
            written = os.write(destination, data)
            if written <= 0:
                raise OSError("discovery spool copy made no progress")
            data = data[written:]
    return reference.model_copy(update={"offset": start})


def iter_spool(descriptor: int, reference: DiscoverySpoolRef) -> Iterator[bytes]:
    """Iterate already verified local metadata; never request work from a worker."""
    offset = reference.offset
    end = offset + reference.size_bytes
    count = 0
    while offset < end:
        header = os.pread(descriptor, 4, offset)
        if len(header) != 4:
            raise SecurityError("discovery spool frame header is truncated")
        size = struct.unpack(">I", header)[0]
        if not 0 < size <= MAX_DISCOVERY_RECORD_BYTES or offset + 4 + size > end:
            raise SecurityError("discovery spool frame exceeds its declared bound")
        payload = os.pread(descriptor, size, offset + 4)
        if len(payload) != size:
            raise SecurityError("discovery spool frame is truncated")
        count += 1
        if count > reference.record_count:
            raise SecurityError("discovery spool contains undeclared records")
        yield payload
        offset += 4 + size
    if count != reference.record_count:
        raise SecurityError("discovery spool is missing declared records")
