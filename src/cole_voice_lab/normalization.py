"""Non-destructive common-output normalization for blinded comparison."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .audio import WavInspection, inspect_pcm_wav
from .canonical import canonical_sha256, sha256_file
from .errors import ContractError, ExternalToolError

NORMALIZATION_ID = "cole-voice-lab-common-v1"
TARGET_SAMPLE_RATE_HZ = 24_000
TARGET_INTEGRATED_LUFS = -18.0
TARGET_TRUE_PEAK_DBFS = -1.5
TARGET_LOUDNESS_RANGE = 11.0


@dataclass(frozen=True, slots=True)
class NormalizationPlan:
    native_path: Path
    normalized_path: Path
    command: tuple[str, ...]
    settings_sha256: str


@dataclass(frozen=True, slots=True)
class NormalizationEvidence:
    normalization_id: str
    native_path: Path
    native_sha256: str
    normalized_path: Path
    normalized_sha256: str
    normalized_bytes: int
    settings_sha256: str
    inspection: WavInspection


def normalization_settings() -> dict[str, int | float | str]:
    return {
        "normalization_id": NORMALIZATION_ID,
        "channels": 1,
        "sample_rate_hz": TARGET_SAMPLE_RATE_HZ,
        "encoding": "pcm_s16le",
        "integrated_lufs": TARGET_INTEGRATED_LUFS,
        "true_peak_dbfs": TARGET_TRUE_PEAK_DBFS,
        "loudness_range": TARGET_LOUDNESS_RANGE,
    }


def build_normalization_plan(
    native_path: Path,
    normalized_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> NormalizationPlan:
    """Return the exact argv without creating files or invoking FFmpeg."""

    native = native_path.expanduser().resolve(strict=False)
    normalized = normalized_path.expanduser().resolve(strict=False)
    if native == normalized:
        raise ContractError("normalization must preserve a distinct engine-native output")
    filter_graph = (
        f"loudnorm=I={TARGET_INTEGRATED_LUFS:g}:"
        f"TP={TARGET_TRUE_PEAK_DBFS:g}:LRA={TARGET_LOUDNESS_RANGE:g}"
    )
    command = (
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-i",
        str(native),
        "-map_metadata",
        "-1",
        "-vn",
        "-af",
        filter_graph,
        "-ac",
        "1",
        "-ar",
        str(TARGET_SAMPLE_RATE_HZ),
        "-c:a",
        "pcm_s16le",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(normalized),
    )
    return NormalizationPlan(
        native_path=native,
        normalized_path=normalized,
        command=command,
        settings_sha256=canonical_sha256(normalization_settings()),
    )


def execute_normalization(
    plan: NormalizationPlan,
    *,
    timeout_seconds: float = 300.0,
) -> NormalizationEvidence:
    """Create a normalized copy and prove the native input remained unchanged."""

    if plan.normalized_path.exists():
        raise ContractError(f"refusing to overwrite normalized output: {plan.normalized_path}")
    native_before, _ = sha256_file(plan.native_path)
    plan.normalized_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    completed = subprocess.run(
        plan.command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise ExternalToolError(f"normalization failed: {completed.stderr.strip()}")
    native_after, _ = sha256_file(plan.native_path)
    if native_before != native_after:
        raise ContractError("engine-native output changed during normalization")
    inspection = inspect_pcm_wav(plan.normalized_path)
    if (
        inspection.audio.sample_rate_hz != TARGET_SAMPLE_RATE_HZ
        or inspection.audio.channels != 1
        or inspection.audio.sample_width_bytes != 2
    ):
        raise ContractError("normalized output does not match the common PCM contract")
    normalized_digest, normalized_bytes = sha256_file(plan.normalized_path)
    return NormalizationEvidence(
        normalization_id=NORMALIZATION_ID,
        native_path=plan.native_path,
        native_sha256=native_before,
        normalized_path=plan.normalized_path,
        normalized_sha256=normalized_digest,
        normalized_bytes=normalized_bytes,
        settings_sha256=plan.settings_sha256,
        inspection=inspection,
    )
