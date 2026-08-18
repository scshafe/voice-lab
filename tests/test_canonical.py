from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import BaseModel

import cole_voice_lab.canonical as canonical
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    json_ready,
    open_regular_no_follow,
    read_json_object,
    sha256_file,
    sha256_stream,
    write_canonical_json,
)
from cole_voice_lab.errors import SecurityError


class _JsonModel(BaseModel):
    z: int
    a: str


def test_canonical_json_is_order_independent_and_compact() -> None:
    left = {"z": [2, 1], "a": {"b": True}}
    right = {"a": {"b": True}, "z": [2, 1]}
    assert canonical_json_bytes(left) == b'{"a":{"b":true},"z":[2,1]}'
    assert canonical_sha256(left) == canonical_sha256(right)


def test_canonical_json_supports_models_and_rejects_non_finite_numbers() -> None:
    assert canonical_json_bytes(_JsonModel(z=2, a="café")) == '{"a":"café","z":2}'.encode()
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json_bytes({"unsafe": float("nan")})


def test_stream_hashing_counts_multiple_chunks() -> None:
    payload = b"a" * (canonical._CHUNK_BYTES + 17)
    digest, size = sha256_stream(io.BytesIO(payload))

    assert digest == canonical.hashlib.sha256(payload).hexdigest()
    assert size == len(payload)


def test_atomic_json_write_returns_exact_file_digest(tmp_path: Path) -> None:
    output = tmp_path / "evidence" / "record.json"
    digest = write_canonical_json(output, {"value": 1})

    assert json.loads(output.read_bytes()) == {"value": 1}
    assert sha256_file(output) == (digest, len(output.read_bytes()))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not tuple(output.parent.glob("*.tmp-*"))


def test_hashing_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("private")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(SecurityError, match="safely open"):
        sha256_file(link)


def test_safe_open_rejects_missing_and_non_regular_files(tmp_path: Path) -> None:
    with pytest.raises(SecurityError, match="safely open"):
        open_regular_no_follow(tmp_path / "missing")
    with pytest.raises(SecurityError, match="regular file"):
        open_regular_no_follow(tmp_path)


def test_safe_open_fallback_without_no_follow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "regular"
    path.write_bytes(b"payload")
    monkeypatch.delattr(canonical.os, "O_NOFOLLOW")

    with open_regular_no_follow(path) as stream:
        assert stream.read() == b"payload"


def test_atomic_write_cleans_up_after_publish_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "record.json"
    monkeypatch.setattr(canonical.secrets, "token_hex", lambda _: "fixed")

    def fail_publish(_source: object, _destination: object) -> None:
        raise OSError("publish failed")

    monkeypatch.setattr(canonical.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        write_canonical_json(output, {"value": 1})

    assert not output.exists()
    assert not (tmp_path / ".record.json.tmp-fixed").exists()


def test_atomic_write_preserves_original_error_if_temp_is_already_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "record.json"
    monkeypatch.setattr(canonical.secrets, "token_hex", lambda _: "fixed")

    def remove_then_fail(source: object, _destination: object) -> None:
        Path(source).unlink()
        raise OSError("publish failed")

    monkeypatch.setattr(canonical.os, "replace", remove_then_fail)
    with pytest.raises(OSError, match="publish failed"):
        write_canonical_json(output, {"value": 1})


def test_atomic_write_closes_descriptor_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor: int | None = None
    real_fstat = os.fstat

    def fail_fdopen(open_descriptor: int, *_args: object, **_kwargs: object) -> None:
        nonlocal descriptor
        descriptor = open_descriptor
        raise OSError("fdopen failed")

    monkeypatch.setattr(canonical.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen failed"):
        write_canonical_json(tmp_path / "record.json", {"value": 1})

    assert descriptor is not None
    with pytest.raises(OSError):
        real_fstat(descriptor)
    assert not tuple(tmp_path.glob("*.tmp-*"))


def test_read_json_object_accepts_only_objects(tmp_path: Path) -> None:
    object_path = tmp_path / "object.json"
    object_path.write_text('{"value": 1}', encoding="utf-8")
    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")

    assert read_json_object(object_path) == {"value": 1}
    with pytest.raises(ValueError, match="expected a JSON object"):
        read_json_object(array_path)


def test_json_ready_preserves_dynamic_json_containers() -> None:
    mapping = {"value": [1, 2]}
    sequence = [mapping]
    assert json_ready(mapping) is mapping
    assert json_ready(sequence) is sequence
