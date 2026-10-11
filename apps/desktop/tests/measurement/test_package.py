"""The package and the intake's manifest rules, client side."""

from __future__ import annotations

import copy

import pytest
from helpers import record_session
from voice_lab_desktop.measurement.package import (
    PackageError,
    read_session_json,
    validate_manifest,
    verify_files,
)


@pytest.fixture
def manifest(backend, levels, pinned, sessions):
    return record_session(backend, levels, pinned, sessions, skip_free=True)


def mutate(m: dict, path: str, value) -> dict:
    out = copy.deepcopy(m)
    target = out
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    last = keys[-1]
    if value is KeyError:
        del target[last]
    elif isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value
    return out


@pytest.mark.parametrize(
    "path,value,code",
    [
        ("sessionId", "bad id!", "invalid_session_id"),
        ("sessionId", "s-1", "invalid_session_id"),
        ("chainId", "iphone-voicememos", "unknown_chain"),
        ("startedAt", "", "invalid_timestamp"),
        ("capture", KeyError, "capture_required"),
        ("capture.inputVolume", None, "input_level_required"),
        ("capture.inputVolume", True, "input_level_required"),
        ("capture.distanceCm", 0, "distance_required"),
        ("capture.calFileSha256", "ABC", "invalid_cal_file_hash"),
        ("capture.sampleRate", 44100, "sample_rate_mismatch"),
        ("files", [], "invalid_file_list"),
        ("files.0.name", "session.json", "invalid_file_name"),
        ("files.0.name", "../x.wav", "invalid_file_name"),
        ("files.0.sha256", "0" * 63, "invalid_file_sha256"),
        ("files.0.sizeBytes", 0, "invalid_file_size"),
        ("files.0.sizeBytes", 2 * 1024**3, "file_too_large"),
        ("segments", [], "segment_map_required"),
        ("segments.0.file", "99-nope.wav", "segment_file_not_listed"),
    ],
)
def test_validate_manifest_rejects(manifest, path, value, code):
    validate_manifest(manifest.manifest)
    with pytest.raises(PackageError, match=f"^{code}$"):
        validate_manifest(mutate(manifest.manifest, path, value))


def test_duplicate_file_names_are_rejected(manifest):
    m = copy.deepcopy(manifest.manifest)
    m["files"].append(dict(m["files"][0]))
    with pytest.raises(PackageError, match="duplicate_file_name"):
        validate_manifest(m)


def test_verify_files_detects_changed_bytes(manifest):
    assert verify_files(manifest.session_dir, manifest.manifest) == []
    first = manifest.session_dir / manifest.manifest["files"][0]["name"]
    data = bytearray(first.read_bytes())
    data[100] ^= 1
    first.write_bytes(bytes(data))
    (manifest.session_dir / manifest.manifest["files"][1]["name"]).unlink()
    assert verify_files(manifest.session_dir, manifest.manifest) == [
        f["name"] for f in manifest.manifest["files"][:2]
    ]


def test_read_session_json(manifest, tmp_path):
    assert read_session_json(manifest.session_dir) == manifest.manifest
    with pytest.raises(PackageError, match="missing or damaged"):
        read_session_json(tmp_path)
    (tmp_path / "session.json").write_text("[1, 2]\n")  # JSON, but not a session document
    with pytest.raises(PackageError, match="missing or damaged"):
        read_session_json(tmp_path)
