"""A UMIK-1 measurement session as a voice-lab capture package (design §5.4, D-024).

@canonical journey.measurement_package -- the one mapping from a measurement session
(voice-journey's ``session.json``: its chain, its segments, its WAV files) to the capture
package the unchanged strict validator accepts and the intake publisher publishes into
``raw/``. Two callers build packages with it and must agree byte for byte:

- the API's compatibility path (``api/journey_intake.py``), for the separate capture
  client during phase 3;
- the desktop app (phase 4b), which builds the package itself and sends it through
  ``/api/v1/intake`` with the D-021 ledger entry (``intake_client.upload_session``).

The package: ``session_id`` ``vj-<sessionId>``; ``prompt_suite_id``
``voice-journey-measurement`` (no prompt catalog); ``subject_id`` the configured subject;
``capture_host`` who built it; ``captured_at``/``finalized_at`` the session's
``startedAt``/``endedAt``; ``capture_device`` the chain (``voice-journey-chain:<chainId>``),
the cal file's serial and digest; one file per WAV at ``<segmentKey>/<name>`` (the key of
the last segment naming it) or ``<name>``; ``room-silence`` is the ``room-tone`` file and
every other take ``calibration``; ``qc.json`` the validator's portable metrics; ``READY``.

What the package cannot hold is refused up front (``require_package_mapping``): a session id
that is not lower case or ends in ``-``/``_``, a chain that is not a UMIK-1, a format other
than mono 48 kHz 24-bit, an ``endedAt`` before ``startedAt``, a segment key that is not a
lower-case slug, a session without a ``room-silence`` segment; and, when the package is
written, a WAV that is not canonical (``audio_not_accepted``).
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from ..audio import WavInspection, inspect_canonical_wav
from ..canonical import open_regular_no_follow, write_canonical_json
from ..capture import MANIFEST_NAME, QC_REPORT_NAME, READY_NAME, ReadyMarker
from ..errors import ContractError
from ..models import AudioFileEvidence, CaptureDevice, CaptureManifest, QcReport
from . import jscompat as js
from .corpus import MEASUREMENT_DEVICE_PREFIX, MEASUREMENT_SESSION_PREFIX, MEASUREMENT_SUITE_ID
from .jsonio import MISSING

ROOM_TONE_SEGMENT_KEY: Final = "room-silence"
DEFAULT_SUBJECT_ID: Final = "cole-self"
CANONICAL_FORMAT: Final = "mono-48000-pcm-s24le"

_PORTABLE_SESSION_ID: Final = re.compile(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?\Z")
_SEGMENT_KEY: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_UMIK1: Final = re.compile(r"umik[\s_-]?1(?![0-9])", re.IGNORECASE)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

Json = dict[str, Any]


class PackageRefusal(ValueError):
    """The session cannot become a capture package: a stable ``code`` and its detail."""

    def __init__(self, code: str, detail: Json | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail: Json = detail or {}


def strict_equal(left: Any, right: Any) -> bool:
    """``left === right`` for JSON values that are numbers or strings."""

    if js.is_number(left) and js.is_number(right):
        return bool(left == right)
    return type(left) is type(right) and bool(left == right)


def moment(text: str) -> datetime:
    """A voice-journey timestamp (``Date.parse``) as a UTC time.

    Raises: ``OverflowError`` outside the years 1 to 9999; ``ValueError`` when it does not
    parse."""
    milliseconds = js.date_parse(text)
    if milliseconds is None:
        raise ValueError("not a timestamp")
    return _EPOCH + timedelta(milliseconds=int(milliseconds))


def require_package_mapping(manifest: Json, registry: Any) -> None:
    """The rules a voice-lab capture package adds to voice-journey's manifest rules, for a
    manifest those rules accepted. Raises: ``PackageRefusal``."""

    session_id = manifest["sessionId"]
    if not _PORTABLE_SESSION_ID.match(session_id):
        raise PackageRefusal(
            "invalid_session_id",
            {"reason": "voice-lab session ids are lower case and end in a letter or digit"},
        )
    chain = next(
        (
            candidate
            for candidate in js.coalesce(js.get(registry, "chains"), [])
            if strict_equal(js.get(candidate, "chainId"), manifest["chainId"])
        ),
        None,
    )
    if chain is None:
        raise PackageRefusal(
            "chain_not_accepted", {"chainId": manifest["chainId"], "reason": "not in the registry"}
        )
    identity = " ".join(
        js.to_string(js.coalesce(js.get(chain, key), ""))
        for key in ("chainId", "deviceName", "label")
    )
    if not isinstance(manifest["chainId"], str) or not _UMIK1.search(identity):
        raise PackageRefusal(
            "chain_not_accepted",
            {"chainId": manifest["chainId"], "reason": "the lab accepts UMIK-1 chains only"},
        )
    capture = manifest["capture"]
    declared = (
        (js.get(chain, "sampleRate"), 48000),
        (js.get(capture, "sampleRate"), 48000),
        (js.get(capture, "bitDepth"), 24),
        (js.get(capture, "channels"), 1),
    )
    if any(found is not MISSING and not strict_equal(found, want) for found, want in declared):
        raise PackageRefusal("format_not_accepted", {"expected": CANONICAL_FORMAT})
    try:
        started, ended = moment(manifest["startedAt"]), moment(manifest["endedAt"])
    except OverflowError as error:
        raise PackageRefusal("invalid_timestamp", {"reason": "years 1 to 9999 only"}) from error
    if ended < started:
        raise PackageRefusal(
            "invalid_timestamp", {"field": "endedAt", "reason": "ends before it starts"}
        )
    if any(not _SEGMENT_KEY.match(segment["key"]) for segment in manifest["segments"]):
        raise PackageRefusal("invalid_segment_key", {"pattern": "[a-z0-9][a-z0-9-]{0,63}"})
    if not any(kind == "room-tone" for _, _, kind in package_layout(manifest)):
        raise PackageRefusal("room_tone_required", {"segment": ROOM_TONE_SEGMENT_KEY})


def package_session_id(session_id: str) -> str:
    """The capture package's session id for a voice-journey session."""

    return MEASUREMENT_SESSION_PREFIX + session_id


def package_layout(manifest: Json) -> list[tuple[str, str, str]]:
    """``(name, package path, kind)`` per file, in the manifest's order.

    A file sits under the key of the last segment naming it (the index's rule), or at the
    package root when no segment names it.
    """

    segment_by_file = {segment["file"]: segment["key"] for segment in manifest["segments"]}
    layout: list[tuple[str, str, str]] = []
    for file in manifest["files"]:
        key = segment_by_file.get(file["name"])
        path = file["name"] if key is None else f"{key}/{file['name']}"
        layout.append(
            (file["name"], path, "room-tone" if key == ROOM_TONE_SEGMENT_KEY else "calibration")
        )
    return layout


def capture_manifest(
    manifest: Json,
    inspections: Mapping[str, WavInspection],
    *,
    subject_id: str,
    capture_host: str,
) -> CaptureManifest:
    """The capture manifest of a verified session (see the module docstring)."""

    capture = manifest["capture"]
    serial = js.get(capture, "calSerial")
    files = tuple(
        AudioFileEvidence(
            path=path,
            kind="room-tone" if kind == "room-tone" else "calibration",
            sha256=inspections[name].sha256,
            size_bytes=inspections[name].size_bytes,
            audio=inspections[name].audio,
        )
        for name, path, kind in package_layout(manifest)
    )
    return CaptureManifest(
        session_id=package_session_id(manifest["sessionId"]),
        subject_id=subject_id,
        prompt_suite_id=MEASUREMENT_SUITE_ID,
        capture_host=capture_host,
        captured_at=moment(manifest["startedAt"]),
        finalized_at=moment(manifest["endedAt"]),
        capture_device=CaptureDevice(
            device_id=MEASUREMENT_DEVICE_PREFIX + manifest["chainId"],
            display_name=f"miniDSP UMIK-1 (voice-journey chain {manifest['chainId']})",
            microphone_serial=serial
            if isinstance(serial, str) and 0 < len(serial) <= 500
            else None,
            calibration_sha256=js.get(capture, "calFileSha256") or None,
        ),
        files=files,
    )


def qc_report(
    manifest: CaptureManifest, manifest_sha256: str, inspections: Mapping[str, WavInspection]
) -> QcReport:
    """The package's ``qc.json``: the validator's portable metrics, generated at
    ``finalized_at`` (a measurement package has no utterance, so no signal-to-noise)."""

    metrics = tuple(
        inspections[Path(evidence.path).name].qc_metrics(
            evidence.path, allow_quiet=evidence.kind == "room-tone"
        )
        for evidence in manifest.files
    )
    return QcReport(
        session_id=manifest.session_id,
        manifest_sha256=manifest_sha256,
        generated_at=manifest.finalized_at,
        status="pass" if all(metric.passed for metric in metrics) else "review",
        metrics=metrics,
    )


def inspect_session(located: Mapping[str, Path]) -> dict[str, WavInspection]:
    """Every WAV of a verified session, inspected as the validator will.

    Raises: ``PackageRefusal`` (``audio_not_accepted``) naming every WAV that is not
    canonical mono 48 kHz s24le."""

    inspections: dict[str, WavInspection] = {}
    refused: list[str] = []
    for name, path in located.items():
        try:
            inspections[name] = inspect_canonical_wav(path)
        except ContractError:
            refused.append(name)
    if refused:
        raise PackageRefusal("audio_not_accepted", {"files": refused, "expected": CANONICAL_FORMAT})
    return inspections


def write_package(
    manifest: Json,
    located: Mapping[str, Path],
    directory: Path,
    *,
    subject_id: str,
    capture_host: str,
    move: bool,
) -> tuple[CaptureManifest, str]:
    """Write the capture package of a verified session into ``directory``.

    ``located`` names each WAV's file now; it is renamed (``move``) or copied (never through
    a symlink, never over an existing file) into its place in the package, then
    ``manifest.json``, ``qc.json`` and ``READY`` are written last.
    Returns the capture manifest and its file's digest.
    Raises: ``PackageRefusal`` for audio that is not canonical (nothing is placed)."""

    inspections = inspect_session(located)
    for name, member, _ in package_layout(manifest):
        target = directory / member
        if located[name] == target:
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if move:
            os.rename(located[name], target)
        else:
            with open_regular_no_follow(located[name]) as source, open(target, "xb") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)
            os.chmod(target, 0o600)
    capture = capture_manifest(
        manifest, inspections, subject_id=subject_id, capture_host=capture_host
    )
    manifest_sha256 = write_canonical_json(directory / MANIFEST_NAME, capture)
    report_sha256 = write_canonical_json(
        directory / QC_REPORT_NAME, qc_report(capture, manifest_sha256, inspections)
    )
    write_canonical_json(
        directory / READY_NAME,
        ReadyMarker(
            session_id=capture.session_id,
            manifest_sha256=manifest_sha256,
            qc_report_sha256=report_sha256,
            finalized_at=capture.finalized_at,
        ),
    )
    return capture, manifest_sha256
