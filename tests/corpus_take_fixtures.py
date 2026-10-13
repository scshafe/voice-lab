"""Synthetic takes for the corpus tests (D-052): generated tones, never a person's voice.

A take names ``COLE_SUBJECT`` (the contract allows no other), so these packages exist only
under a test's temporary directory. Their evidence is the PortAudio worker's shape with
``simulated=False`` unless a test asks otherwise: the PC refuses the simulated device's audio.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import random
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cole_voice_lab import cli
from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.canonical import canonical_json_bytes, write_canonical_json
from cole_voice_lab.capture import READY_NAME, ReadyMarker
from cole_voice_lab.corpus_read_model import read_active_catalog
from cole_voice_lab.corpus_store import read_catalog_snapshot
from cole_voice_lab.corpus_takes import (
    CONTINUITY_NAME,
    CorpusTakeManifest,
    TakeClient,
    ValidatedTake,
    build_take_package,
)
from cole_voice_lab.models import COLE_SUBJECT, CaptureDevice, QcReport
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.portaudio_evidence import PortAudioContinuityEvidence
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
SAMPLE_RATE = 48_000
CLIENT = TakeClient(name="voice-lab-desktop", version="0.0.0-test", platform="Linux x86_64")
DEVICE = CaptureDevice(
    backend="portaudio",
    device_id="umik-1:synthetic",
    display_name="miniDSP UMIK-1 via PortAudio (synthetic)",
)


def run_catalog(layout: AssetLayout, *argv: str) -> dict[str, Any]:
    """``voice-lab catalog <argv> --asset-root <lab>``; its printed JSON."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["catalog", *argv, "--asset-root", str(layout.root)]) == 0
    printed = out.getvalue()
    return dict(json.loads(printed[printed.index("{") :]))


def seed_catalog(layout: AssetLayout) -> tuple[CatalogReference, CatalogSnapshot]:
    """cole-corpus version 1 (pilot-v2 without its room tones), active for Cole."""
    run_catalog(
        layout, "init", "--subject-id", COLE_SUBJECT, "--from-builtin", "pilot-v2",
        "--drop-room-tones",
    )  # fmt: skip
    return active_snapshot(layout)


def active_snapshot(layout: AssetLayout) -> tuple[CatalogReference, CatalogSnapshot]:
    record = read_active_catalog(layout, COLE_SUBJECT)
    assert record is not None
    return record.reference, read_catalog_snapshot(layout, record.reference)


def write_tone(
    path: Path,
    *,
    seconds: float = 0.25,
    frequency: float | None = 220.0,
    seed: int = 0,
    width: int = 3,
) -> Path:
    """A mono 48 kHz WAV: a sine at ``frequency`` over faint noise, or faint noise alone."""
    noise = random.Random(seed)
    count = int(seconds * SAMPLE_RATE)
    scale = 1_000_000.0 if width == 3 else 4_000.0
    frames = bytearray()
    for index in range(count):
        sample = noise.uniform(-200.0, 200.0) * (scale / 1_000_000.0)
        if frequency is not None:
            fade = min(1.0, index / 480, (count - index) / 480)
            sample += scale * fade * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)
        frames += int(sample).to_bytes(width, "little", signed=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(width)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(bytes(frames))
    return path


def evidence_for(
    wav: Path, *, simulated: bool = False, started_at: datetime = NOW
) -> PortAudioContinuityEvidence:
    """Successful worker evidence bound to ``wav``."""
    inspection = inspect_canonical_wav(wav)
    frames = inspection.audio.frames
    return PortAudioContinuityEvidence(
        host_api="ALSA",
        simulated=simulated,
        device_name="Umik-1  Gain: 18dB",
        device_usb_id=None,
        output_name="original.partial.wav",
        wav_sha256=inspection.sha256,
        wav_size_bytes=inspection.size_bytes,
        frames=frames,
        received_frames=frames,
        callback_count=math.ceil(frames / 1024),
        block_frames=1024,
        first_adc_time=10.0,
        last_adc_end=10.0 + frames / SAMPLE_RATE,
        adc_time_valid=True,
        tolerance_frames=2.0,
        max_abs_deviation_frames=0.25,
        input_overflows=0,
        input_underflows=0,
        queue_capacity=64,
        queue_high_water=2,
        success=True,
        continuity_checked=True,
        stop_reason="requested",
        error=None,
        started_at=started_at,
    )


def make_take(
    directory: Path,
    *,
    prompt_id: str | None = None,
    catalog: tuple[CatalogReference, CatalogSnapshot],
    seconds: float = 0.25,
    frequency: float | None = 220.0,
    seed: int = 0,
    requested_at: datetime = NOW,
    simulated: bool = False,
    **changes: Any,
) -> ValidatedTake:
    """A take package of the catalog's prompt ``prompt_id`` (its first prompt by default),
    built in ``directory`` (a new path) by ``build_take_package``. ``changes`` override the
    builder's other arguments (``prompt``, ``device``, ...)."""
    reference, snapshot = catalog
    prompt = (
        snapshot.suite.prompts[0]
        if prompt_id is None
        else next(p for p in snapshot.suite.prompts if p.prompt_id == prompt_id)
    )
    source = directory.parent / f".{directory.name}.source.wav"
    write_tone(source, seconds=seconds, frequency=frequency, seed=seed)
    arguments: dict[str, Any] = {
        "wav": source,
        "evidence": evidence_for(source, simulated=simulated, started_at=requested_at),
        "prompt": prompt,
        "catalog": reference,
        "requested_at": requested_at,
        "capture_host": "synthetic-host",
        "client": CLIENT,
        "device": DEVICE,
        "clock": lambda: requested_at,
        **changes,
    }
    try:
        return build_take_package(directory, **arguments)
    finally:
        source.unlink()


def writable(package: Path) -> None:
    os.chmod(package, 0o700)
    for child in package.iterdir():
        os.chmod(child, 0o600)


def reseal(
    package: Path,
    *,
    manifest: dict[str, Any] | None = None,
    evidence: PortAudioContinuityEvidence | None = None,
    qc: dict[str, Any] | None = None,
) -> None:
    """Rewrite a package's control files after a test's change, so that only the change it
    makes is wrong: new evidence is bound into the manifest, and qc.json and READY are
    re-bound to the manifest. ``manifest`` and ``qc`` are field updates (raw JSON values, so
    a test can write what no contract would build)."""
    writable(package)
    document = json.loads((package / "manifest.json").read_bytes())
    if evidence is not None:
        payload = canonical_json_bytes(evidence) + b"\n"
        (package / CONTINUITY_NAME).write_bytes(payload)
        document["continuity"]["sha256"] = hashlib.sha256(payload).hexdigest()
        document["continuity"]["size_bytes"] = len(payload)
    document.update(manifest or {})
    manifest_bytes = canonical_json_bytes(document) + b"\n"
    (package / "manifest.json").write_bytes(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    report = json.loads((package / "qc.json").read_bytes())
    report["manifest_sha256"] = manifest_sha256
    report["session_id"] = document["take_id"]
    report.update(qc or {})
    report_bytes = canonical_json_bytes(report) + b"\n"
    (package / "qc.json").write_bytes(report_bytes)
    ready = ReadyMarker(
        session_id=document["take_id"],
        manifest_sha256=manifest_sha256,
        qc_report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        finalized_at=NOW,
    )
    (package / READY_NAME).unlink()
    write_canonical_json(package / READY_NAME, ready)


def manifest_of(package: Path) -> CorpusTakeManifest:
    return CorpusTakeManifest.model_validate_json((package / "manifest.json").read_bytes())


def qc_of(package: Path) -> QcReport:
    return QcReport.model_validate_json((package / "qc.json").read_bytes())
