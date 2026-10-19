"""Synthetic ``corpus-take/v2`` contracts of profile ``xvf3800-linear-4`` (D-056).

The house panel's take, as its capture would describe it: the array, its six channels (two
processed, four raw microphones), its settings, and ALSA continuity evidence whose periods
follow the rule exactly (every period 1,024 frames, 64 ms at 16 kHz, with a little jitter).
No audio file is in the repository: tests write their WAVs from integers.
"""

from __future__ import annotations

import hashlib
import json
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cole_voice_lab.alsa_evidence import (
    ALSA_FORMAT_WIDTHS,
    AlsaContinuityEvidence,
    period_deviations_ns,
    wav_size_bytes,
)
from cole_voice_lab.audio import inspect_pcm_wav
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
from cole_voice_lab.capture import ReadyMarker
from cole_voice_lab.capture_profiles import Xvf3800Linear4Profile
from cole_voice_lab.corpus_takes import (
    CorpusTakeManifestV2,
    ValidatedProfileTake,
    take_id_for,
    take_qc_report,
    validate_profile_take_package,
)
from cole_voice_lab.models import COLE_SUBJECT, Prompt
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.recording_contracts import (
    CatalogReference,
    CatalogSnapshot,
    catalog_snapshot_path,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
RATE = 16_000
PERIOD_FRAMES = 1_024
#: One period at 16 kHz, in nanoseconds.
PERIOD_NS = PERIOD_FRAMES * 62_500
#: CLOCK_MONOTONIC_RAW some hours after boot.
FIRST_TIMESTAMP_NS = 9_876_543_210_000
SETTINGS: dict[str, tuple[int | float, ...]] = {
    "SHF_BYPASS": (0,),
    "PP_AGCONOFF": (1,),
    "PP_AGCGAIN": (31.6,),
    "PP_MIN_NS": (0.15,),
    "PP_MIN_NN": (0.51,),
    "PP_ECHOONOFF": (1,),
    "PP_NLATTENONOFF": (1,),
    "PP_LIMITONOFF": (0,),
    "AEC_HPFONOFF": (1,),
}
PROCESSED = ("aec", "agc", "noise-suppression", "echo-suppression", "high-pass")


def profile(**changes: Any) -> Xvf3800Linear4Profile:
    """The 6-channel image: two processed channels, then raw microphones 1 to 4; the primary
    channel is microphone 1 (channel 3)."""
    document: dict[str, Any] = {
        "profile": "xvf3800-linear-4",
        "device": {
            "model": "reSpeaker Flex XVF3800 Linear-4",
            "usb_vendor_id": 0x2886,
            "usb_product_id": 0x001A,
            "serial": "SYNTHETIC-0001",
            "firmware": {
                "version": [1, 0, 4],
                "build_message": "reSpeaker_Flex v1.0.4",
                "build_repo_hash": "0123abcd",
            },
        },
        "room": "bathroom",
        "channels": [
            {"source": "processed", "mic": None, "routing": [7, 0], "processing": PROCESSED},
            {"source": "processed", "mic": None, "routing": [7, 1], "processing": PROCESSED},
            *(
                {"source": "mic", "mic": mic, "routing": [1, mic - 1], "processing": []}
                for mic in range(1, 5)
            ),
        ],
        "primary_channel": 3,
        "settings": SETTINGS,
        **changes,
    }
    return Xvf3800Linear4Profile.model_validate_json(canonical_json_bytes(document))


def periods(
    count: int, *, jitter_ns: tuple[int, ...] = (0, 120_000, -80_000, 40_000)
) -> list[tuple[int, int]]:
    """``count`` pairs, one per period read: the position (frames read plus the 256 still in
    the buffer) and its timestamp, displaced by a repeating ``jitter_ns``."""
    return [
        (
            (index + 1) * PERIOD_FRAMES + 256,
            FIRST_TIMESTAMP_NS + index * PERIOD_NS + jitter_ns[index % len(jitter_ns)],
        )
        for index in range(count)
    ]


def evidence(
    *,
    period_count: int = 8,
    channels: int = 6,
    alsa_format: str = "S32_LE",
    wav_sha256: str = "a" * 64,
    pairs: list[tuple[int, int]] | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """A successful evidence document (as JSON values) for ``period_count`` periods, its
    largest deviation the rule's; ``changes`` override fields (validate it yourself)."""
    pairs = periods(period_count) if pairs is None else pairs
    frames = period_count * PERIOD_FRAMES
    deviations = period_deviations_ns(pairs, RATE)
    return {
        "schema_version": "cole-voice-lab.alsa-continuity/v1",
        "simulated": False,
        "pcm": "hw:CARD=Array,DEV=0",
        "access": "rw_interleaved",
        "format": alsa_format,
        "rate": RATE,
        "channels": channels,
        "period_frames": PERIOD_FRAMES,
        "buffer_frames": 4 * PERIOD_FRAMES,
        "tstamp_type": "monotonic_raw",
        "xruns": 0,
        "periods": [list(pair) for pair in pairs],
        "tolerance_ns": 1_000_000,
        "max_abs_deviation_ns": max((abs(value) for value in deviations), default=None),
        "frames": frames,
        "wav": {
            "name": "original.wav",
            "sha256": wav_sha256,
            "size_bytes": wav_size_bytes(frames, channels, alsa_format),
        },
        "started_at": "2026-10-19T12:00:00Z",
        "stop_reason": "requested",
        "success": True,
        "error": None,
        **changes,
    }


def validated(document: dict[str, Any]) -> AlsaContinuityEvidence:
    """``document`` through the strict JSON path, as the PC reads the file."""
    return AlsaContinuityEvidence.model_validate_json(canonical_json_bytes(document))


def frame_bytes(frames: int, channels: int, alsa_format: str, *, seed: int = 0) -> bytes:
    """Interleaved PCM of an integer recipe: channel ``c`` of frame ``f`` is
    ``((f * (c + 3) + seed) % 2001 - 1000) * (c + 1) * 16``, little-endian."""
    width = ALSA_FORMAT_WIDTHS[alsa_format]
    return b"".join(
        (((frame * (channel + 3) + seed) % 2001 - 1000) * (channel + 1) * 16).to_bytes(
            width, "little", signed=True
        )
        for frame in range(frames)
        for channel in range(channels)
    )


def write_wav(path: Path, payload: bytes, *, channels: int, alsa_format: str) -> str:
    """A plain-PCM WAV of ``payload`` at 16 kHz; its SHA-256."""
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(ALSA_FORMAT_WIDTHS[alsa_format])
        writer.setframerate(RATE)
        writer.writeframes(payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def catalog() -> tuple[CatalogReference, Prompt]:
    """A reference to pilot-v2 as a published catalog version, and its first spoken prompt."""
    suite = load_builtin_prompt_suite("pilot-v2.json")
    digest = canonical_sha256(suite)
    snapshot = canonical_json_bytes(CatalogSnapshot.embedded(suite)) + b"\n"
    reference = CatalogReference(
        catalog_id="cole-corpus",
        catalog_version=1,
        catalog_sha256=digest,
        snapshot_sha256=hashlib.sha256(snapshot).hexdigest(),
        suite_id=suite.suite_id,
        purpose=suite.purpose,
        snapshot_path=catalog_snapshot_path(digest),
    )
    return reference, next(prompt for prompt in suite.prompts if prompt.spoken)


def manifest(
    *,
    period_count: int = 8,
    channels: int = 6,
    alsa_format: str = "S32_LE",
    audio_sha256: str = "b" * 64,
    **changes: Any,
) -> dict[str, Any]:
    """A v2 manifest document (as JSON values) whose audio and continuity agree with
    ``evidence``'s; ``changes`` override fields (validate it yourself)."""
    reference, prompt = catalog()
    frames = period_count * PERIOD_FRAMES
    width = ALSA_FORMAT_WIDTHS[alsa_format]
    encoding = {2: "pcm_s16le", 3: "pcm_s24le", 4: "pcm_s32le"}[width]
    take_profile = (
        profile()
        if channels == 6
        else profile(
            channels=[
                {"source": "processed", "mic": None, "routing": [7, side], "processing": PROCESSED}
                for side in range(2)
            ],
            primary_channel=1,
        )
    )
    return {
        "schema_version": "cole-voice-lab.corpus-take/v2",
        "take_id": take_id_for(audio_sha256),
        "subject_id": COLE_SUBJECT,
        "prompt": prompt.model_dump(mode="json"),
        "prompt_sha256": canonical_sha256(prompt),
        "catalog": reference.model_dump(mode="json"),
        "requested_at": "2026-10-19T12:00:00Z",
        "capture_host": "house-panel",
        "client": {
            "name": "embedded-house-panel",
            "version": "0.0.0-test",
            "platform": "Linux aarch64",
        },
        "capture_profile": take_profile.model_dump(mode="json"),
        "audio": {
            "path": "original.wav",
            "sha256": audio_sha256,
            "size_bytes": wav_size_bytes(frames, channels, alsa_format),
            "audio": {
                "sample_rate_hz": RATE,
                "channels": channels,
                "sample_width_bytes": width,
                "encoding": encoding,
                "frames": frames,
                "duration_seconds": frames / RATE,
            },
        },
        "continuity": {
            "path": "continuity.json",
            "sha256": "c" * 64,
            "size_bytes": 4_096,
            "pcm": "hw:CARD=Array,DEV=0",
            "simulated": False,
            "started_at": "2026-10-19T12:00:00Z",
            "stop_reason": "requested",
            "tolerance_ns": 1_000_000,
            "max_abs_deviation_ns": max(
                abs(value) for value in period_deviations_ns(periods(period_count), RATE)
            ),
        },
        **changes,
    }


def validated_manifest(document: dict[str, Any]) -> CorpusTakeManifestV2:
    """``document`` through the strict JSON path, as the PC reads ``manifest.json``."""
    return CorpusTakeManifestV2.model_validate_json(canonical_json_bytes(document))


def make_profile_take(
    directory: Path,
    *,
    catalog: tuple[CatalogReference, CatalogSnapshot] | None = None,
    prompt: Prompt | None = None,
    period_count: int = 4,
    channels: int = 6,
    alsa_format: str = "S32_LE",
    seed: int = 0,
    payload: bytes | None = None,
    simulated: bool = False,
    requested_at: str = "2026-10-19T12:00:00Z",
    evidence_changes: dict[str, Any] | None = None,
) -> ValidatedProfileTake:
    """A complete v2 take package in ``directory`` (a new path), as the panel would upload
    it: its WAV from ``frame_bytes`` (or ``payload``), its evidence, manifest, QC and READY,
    validated by ``validate_profile_take_package``. ``catalog`` is a seeded catalog
    (``corpus_take_fixtures.seed_catalog``); its first spoken prompt is taken unless
    ``prompt`` is given. Without it, pilot-v2 as version 1 (``catalog()``)."""
    directory.mkdir(mode=0o700)
    frames = period_count * PERIOD_FRAMES
    audio = frame_bytes(frames, channels, alsa_format, seed=seed) if payload is None else payload
    audio_sha256 = write_wav(
        directory / "original.wav", audio, channels=channels, alsa_format=alsa_format
    )
    record = validated(
        evidence(
            period_count=period_count,
            channels=channels,
            alsa_format=alsa_format,
            wav_sha256=audio_sha256,
            simulated=simulated,
            **(evidence_changes or {}),
        )
    )
    evidence_sha256 = write_canonical_json(directory / "continuity.json", record)
    document = manifest(
        period_count=period_count,
        channels=channels,
        alsa_format=alsa_format,
        audio_sha256=audio_sha256,
        requested_at=requested_at,
    )
    if catalog is not None:
        reference, snapshot = catalog
        chosen = prompt or next(item for item in snapshot.suite.prompts if item.spoken)
        document["catalog"] = reference.model_dump(mode="json")
        document["prompt"] = chosen.model_dump(mode="json")
        document["prompt_sha256"] = canonical_sha256(chosen)
    elif prompt is not None:
        document["prompt"] = prompt.model_dump(mode="json")
        document["prompt_sha256"] = canonical_sha256(prompt)
    document["continuity"] = {
        **document["continuity"],
        "sha256": evidence_sha256,
        "size_bytes": sha256_file(directory / "continuity.json")[1],
        "simulated": simulated,
        "tolerance_ns": record.tolerance_ns,
        "max_abs_deviation_ns": record.max_abs_deviation_ns,
    }
    take_manifest = validated_manifest(document)
    manifest_sha256 = write_canonical_json(directory / "manifest.json", take_manifest)
    inspection = inspect_pcm_wav(
        directory / "original.wav", channel=take_manifest.capture_profile.primary_channel
    )
    report = take_qc_report(
        take_manifest.take_id, take_manifest.prompt, manifest_sha256, inspection, NOW
    )
    report_sha256 = write_canonical_json(directory / "qc.json", report)
    write_canonical_json(
        directory / "READY",
        ReadyMarker(
            session_id=take_manifest.take_id,
            manifest_sha256=manifest_sha256,
            qc_report_sha256=report_sha256,
            finalized_at=NOW,
        ),
    )
    return validate_profile_take_package(directory)


def reseal_profile_take(directory: Path, **manifest_changes: Any) -> None:
    """Rewrite ``directory``'s manifest with ``manifest_changes`` (JSON values, merged one
    level deep) and bind the QC report and READY to it again, as a client could."""
    document = json.loads((directory / "manifest.json").read_bytes())
    for key, value in manifest_changes.items():
        document[key] = {**document[key], **value} if isinstance(value, dict) else value
    manifest_sha256 = write_canonical_json(directory / "manifest.json", document)
    report = json.loads((directory / "qc.json").read_bytes())
    report["manifest_sha256"] = manifest_sha256
    report["session_id"] = document["take_id"]
    report_sha256 = write_canonical_json(directory / "qc.json", report)
    ready = json.loads((directory / "READY").read_bytes())
    write_canonical_json(
        directory / "READY",
        {
            **ready,
            "session_id": document["take_id"],
            "manifest_sha256": manifest_sha256,
            "qc_report_sha256": report_sha256,
        },
    )
