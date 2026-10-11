"""The session package: ``session.json`` plus SHA-256 and size for every WAV.

``session.json`` carries everything ``capture.mjs`` wrote (chain id, input
level, distance, cal-file hash, the segment map) plus ``files[]``: the
document voice-journey's intake took, and the one ``home_host.py`` turns into
the lab's capture package (``journey.measurement_package``). It is written once,
when the session completes, and never rewritten, so the package built from it is
the same on every retry.

``validate_manifest`` keeps voice-journey's intake rules (``src/intake.mjs``
``validateManifest``) so a bad session fails here, before any package is built.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from . import chain

SESSION_SCHEMA_VERSION = "voice-journey.capture-session.v1"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.wav$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES = 64
MAX_FILE_BYTES = 1024**3
MAX_SESSION_BYTES = 4 * 1024**3
MAX_MANIFEST_BYTES = 256 * 1024
SESSION_FILE = "session.json"


class PackageError(Exception):
    """The package is not acceptable to the intake (or not intact on disk)."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_entries(session_dir: Path, names: list[str]) -> list[dict[str, Any]]:
    out = []
    for name in names:
        path = session_dir / name
        out.append({"name": name, "sha256": sha256_file(path), "sizeBytes": path.stat().st_size})
    return out


def validate_manifest(manifest: dict[str, Any]) -> None:
    """The intake's rules, client side. Raises PackageError naming the first defect."""

    def bad(code: str) -> PackageError:
        return PackageError(code)

    if not isinstance(manifest, dict):
        raise bad("manifest_not_object")
    if not isinstance(manifest.get("sessionId"), str) or not SESSION_ID_RE.match(
        manifest["sessionId"]
    ):
        raise bad("invalid_session_id")
    if manifest.get("chainId") != chain.CHAIN_ID:
        raise bad("unknown_chain")
    for key in ("startedAt", "endedAt"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise bad("invalid_timestamp")
    capture = manifest.get("capture")
    if not isinstance(capture, dict):
        raise bad("capture_required")
    volume = capture.get("inputVolume")
    if (
        isinstance(volume, bool)
        or not isinstance(volume, (int, float))
        or not math.isfinite(volume)
    ):
        raise bad("input_level_required")
    distance = capture.get("distanceCm")
    if (
        isinstance(distance, bool)
        or not isinstance(distance, (int, float))
        or not math.isfinite(distance)
        or distance <= 0
    ):
        raise bad("distance_required")
    cal = capture.get("calFileSha256")
    if cal is not None and not (isinstance(cal, str) and SHA256_RE.match(cal)):
        raise bad("invalid_cal_file_hash")
    if capture.get("sampleRate", chain.SAMPLE_RATE) != chain.SAMPLE_RATE:
        raise bad("sample_rate_mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise bad("invalid_file_list")
    names: set[str] = set()
    total = 0
    for entry in files:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not FILE_NAME_RE.match(name):
            raise bad("invalid_file_name")
        if name in names:
            raise bad("duplicate_file_name")
        names.add(name)
        if not isinstance(entry.get("sha256"), str) or not SHA256_RE.match(entry["sha256"]):
            raise bad("invalid_file_sha256")
        size = entry.get("sizeBytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise bad("invalid_file_size")
        if size > MAX_FILE_BYTES:
            raise bad("file_too_large")
        total += size
    if total > MAX_SESSION_BYTES:
        raise bad("session_too_large")
    segments = manifest.get("segments")
    if not isinstance(segments, list) or not segments:
        raise bad("segment_map_required")
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or not isinstance(segment.get("key"), str)
            or not isinstance(segment.get("file"), str)
        ):
            raise bad("invalid_segment")
        if segment["file"] not in names:
            raise bad("segment_file_not_listed")
    if len(encode(manifest)) > MAX_MANIFEST_BYTES:
        raise bad("manifest_too_large")


def encode(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_session_json(session_dir: Path, manifest: dict[str, Any]) -> Path:
    path = session_dir / SESSION_FILE
    tmp = session_dir / (SESSION_FILE + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(encode(manifest))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    return path


def read_session_json(session_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((session_dir / SESSION_FILE).read_text())
    except (OSError, ValueError) as exc:
        raise PackageError(f"{session_dir.name}: session.json is missing or damaged") from exc
    if not isinstance(data, dict):  # valid JSON, but not a session document
        raise PackageError(f"{session_dir.name}: session.json is missing or damaged")
    return data


def verify_files(session_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """Names whose bytes on disk no longer match the manifest (size or sha256)."""
    bad = []
    for entry in manifest.get("files", []):
        path = session_dir / entry["name"]
        try:
            if path.stat().st_size != entry["sizeBytes"] or sha256_file(path) != entry["sha256"]:
                bad.append(entry["name"])
        except OSError:
            bad.append(entry["name"])
    return bad
