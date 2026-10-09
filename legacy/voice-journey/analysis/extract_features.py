#!/usr/bin/env python3
"""Voice Journey feature-extraction worker (Arc 2, feature contract v1).

Long-lived batch worker: emits a hello line with library versions, then reads
one JSON task per stdin line ({"recordingId", "wavPath"}) and emits one JSON
result line per task. It never reads corpus paths itself — the Node
orchestrator hands it decoded temporary WAV paths — and it retains no audio.

All measurements are local DSP over the provided WAV. No network, no uploads.
"""

import json
import math
import sys
import traceback

import librosa
import numpy as np
import parselmouth
from parselmouth.praat import call

CONFIG = {
    "sampleRate": 22050,
    "f0": {
        "engine": "praat_ac",
        "floorHz": 75.0,
        "ceilingHz": 1200.0,
        "timeStepSeconds": 0.01,
        # Harmonic-lock/octave-error suppression (added after the 5-recording
        # smoke run showed p95 pinned near 6x harmonics on 2019 phone takes):
        # layer 1 rejects frames deviating from the local running median
        # (voiced neighbors within the window) by more than maxDeviationCents —
        # a glissando tracks its own median and survives. Layer 2 rejects
        # SUSTAINED lock regions that dominate their own window: frames beyond
        # maxGlobalDeviationCents from the take's global voiced median (no
        # single take spans three octaves of sung range; genuine whistle-
        # register content shows up as a high rejectedGlobalShare diagnostic
        # and flags the take for listening rather than silently passing).
        "outlier": {"windowSeconds": 0.5, "maxDeviationCents": 600.0, "maxGlobalDeviationCents": 1800.0},
    },
    "vibrato": {
        "minSustainSeconds": 0.8,
        "bandHz": [3.5, 8.5],
        "minExtentCents": 15.0,
        "minBandPowerShare": 0.2,
        "detrendWindowSeconds": 0.5,
    },
    "tuning": {"referenceHz": 440.0, "inTuneCents": 25.0},
    "spectral": {"nFft": 2048, "hopSeconds": 0.01, "singerFormantBandHz": [2800.0, 3400.0], "rolloffShare": 0.85},
    "phrasing": {"minPauseSeconds": 0.3},
    "methodId": "voice-journey.feature-extract.v1",
}


def versions():
    return {
        "python": sys.version.split()[0],
        "parselmouth": getattr(parselmouth, "__version__", None),
        "praat": getattr(parselmouth, "PRAAT_VERSION", None),
        "librosa": getattr(librosa, "__version__", None),
        "numpy": np.__version__,
    }


def rnd(value, places=3):
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, places)


def hz_to_cents(freqs_hz, reference_hz):
    return 1200.0 * np.log2(freqs_hz / reference_hz)


def suppress_f0_outliers(f0_hz, voiced_mask, config):
    """Reject voiced frames that deviate wildly from their local or global
    median pitch.

    Returns (cleaned_voiced_mask, local_rejected_share, global_rejected_share).
    Smooth motion (vibrato, glissandi) tracks its own running median; isolated
    harmonic locks and fry-floor frames fail the local gate, and sustained lock
    regions that dominate their own window fail the global gate. Shares are
    relative to the original voiced frame count.
    """
    outlier = config["f0"]["outlier"]
    half_window = max(1, int(round(outlier["windowSeconds"] / config["f0"]["timeStepSeconds"])))
    voiced_indexes = np.flatnonzero(voiced_mask)
    if voiced_indexes.size < 5:
        return voiced_mask, 0.0, 0.0
    cents = np.full(f0_hz.shape, np.nan)
    cents[voiced_indexes] = hz_to_cents(f0_hz[voiced_indexes], 440.0)
    cleaned = voiced_mask.copy()
    local_rejected = 0
    for i in voiced_indexes:
        lo = max(0, i - half_window)
        hi = min(f0_hz.size, i + half_window + 1)
        neighborhood = cents[lo:hi]
        neighborhood = neighborhood[~np.isnan(neighborhood)]
        if neighborhood.size < 3:
            continue
        if abs(cents[i] - float(np.median(neighborhood))) > outlier["maxDeviationCents"]:
            cleaned[i] = False
            local_rejected += 1
    global_rejected = 0
    remaining = np.flatnonzero(cleaned)
    if remaining.size >= 5:
        global_median = float(np.median(cents[remaining]))
        deviant = remaining[np.abs(cents[remaining] - global_median) > outlier["maxGlobalDeviationCents"]]
        cleaned[deviant] = False
        global_rejected = int(deviant.size)
    return cleaned, local_rejected / voiced_indexes.size, global_rejected / voiced_indexes.size


def voiced_runs(voiced_mask):
    """Yield (start_index, end_index_exclusive) for contiguous voiced runs."""
    runs = []
    start = None
    for i, voiced in enumerate(voiced_mask):
        if voiced and start is None:
            start = i
        elif not voiced and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(voiced_mask)))
    return runs


def tuning_stats(cents, config):
    """Infer a tuning offset against the equal-tempered grid, then measure
    deviation from the nearest semitone. Handles a-cappella drift from A440
    honestly: the offset is the singer's own reference, not a fixed standard."""
    if cents.size < 10:
        return {"offsetCents": None, "medianAbsCentError": None, "within25CentsShare": None}
    angles = (cents % 100.0) * (2.0 * math.pi / 100.0)
    offset = math.atan2(np.mean(np.sin(angles)), np.mean(np.cos(angles))) * (100.0 / (2.0 * math.pi))
    deviation = ((cents - offset + 50.0) % 100.0) - 50.0
    within = float(np.mean(np.abs(deviation) <= config["tuning"]["inTuneCents"]))
    return {
        "offsetCents": rnd(offset, 1),
        "medianAbsCentError": rnd(float(np.median(np.abs(deviation))), 1),
        "within25CentsShare": rnd(within),
    }


def vibrato_stats(f0_hz, voiced_mask, config):
    vib = config["vibrato"]
    time_step = config["f0"]["timeStepSeconds"]
    min_frames = int(round(vib["minSustainSeconds"] / time_step))
    detrend_frames = max(3, int(round(vib["detrendWindowSeconds"] / time_step)) | 1)
    sustained_runs = [(a, b) for a, b in voiced_runs(voiced_mask) if b - a >= min_frames]
    segments = []
    vibrato_frames = 0
    sustained_frames = sum(b - a for a, b in sustained_runs)
    for a, b in sustained_runs:
        cents = hz_to_cents(f0_hz[a:b], config["tuning"]["referenceHz"])
        kernel = np.ones(detrend_frames) / detrend_frames
        trend = np.convolve(cents, kernel, mode="same")
        residual = cents - trend
        n = residual.size
        window = np.hanning(n)
        spectrum = np.abs(np.fft.rfft(residual * window))
        freqs = np.fft.rfftfreq(n, d=time_step)
        band = (freqs >= vib["bandHz"][0]) & (freqs <= vib["bandHz"][1])
        if not np.any(band) or spectrum.sum() <= 0:
            continue
        peak_index = np.argmax(np.where(band, spectrum, 0.0))
        rate = float(freqs[peak_index])
        # Amplitude of the dominant modulation component, corrected for the
        # Hann window's coherent gain (0.5): half peak-to-peak extent in cents.
        extent = float(2.0 * spectrum[peak_index] / (n * 0.5))
        band_share = float(np.sum(spectrum[band] ** 2) / np.sum(spectrum**2))
        present = extent >= vib["minExtentCents"] and band_share >= vib["minBandPowerShare"]
        if present:
            vibrato_frames += b - a
            segments.append({"startSeconds": rnd(a * time_step, 2), "durationSeconds": rnd((b - a) * time_step, 2), "rateHz": rnd(rate, 2), "extentCents": rnd(extent, 1)})
    rates = [seg["rateHz"] for seg in segments]
    extents = [seg["extentCents"] for seg in segments]
    return {
        "sustainedSegmentCount": len(sustained_runs),
        "vibratoSegmentCount": len(segments),
        "meanRateHz": rnd(float(np.mean(rates)), 2) if rates else None,
        "meanExtentCents": rnd(float(np.mean(extents)), 1) if extents else None,
        "vibratoTimeShare": rnd(vibrato_frames / sustained_frames) if sustained_frames else None,
    }, segments


def praat_quality(snd, config):
    floor = config["f0"]["floorHz"]
    ceiling = config["f0"]["ceilingHz"]
    quality = {"jitterLocal": None, "shimmerLocal": None, "meanHnrDb": None, "cpps": None}
    notes = []
    try:
        point_process = call(snd, "To PointProcess (periodic, cc)", floor, ceiling)
        quality["jitterLocal"] = rnd(call(point_process, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3), 5)
        quality["shimmerLocal"] = rnd(call([snd, point_process], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6), 5)
    except Exception as error:  # noqa: BLE001 — record and continue
        notes.append(f"jitter_shimmer_unavailable: {error}")
    try:
        harmonicity = call(snd, "To Harmonicity (cc)", 0.01, floor, 0.1, 1.0)
        quality["meanHnrDb"] = rnd(call(harmonicity, "Get mean", 0, 0), 2)
    except Exception as error:  # noqa: BLE001
        notes.append(f"hnr_unavailable: {error}")
    try:
        cepstrogram = call(snd, "To PowerCepstrogram", 60, 0.002, 5000, 50)
        quality["cpps"] = rnd(
            call(cepstrogram, "Get CPPS", False, 0.02, 0.0005, 60, 330, 0.05, "parabolic", 0.001, 0.05, "Straight", "Robust"),
            2,
        )
    except Exception as error:  # noqa: BLE001
        notes.append(f"cpps_unavailable: {error}")
    return quality, notes


def spectral_stats(samples, sample_rate, frame_times, voiced_mask, config):
    spec = config["spectral"]
    hop = int(round(spec["hopSeconds"] * sample_rate))
    magnitude = np.abs(librosa.stft(samples, n_fft=spec["nFft"], hop_length=hop))
    if magnitude.shape[1] == 0:
        return {"centroidHzMean": None, "centroidHzSd": None, "rolloffHzMean": None, "singerFormantRatio": None}
    stft_times = librosa.frames_to_time(np.arange(magnitude.shape[1]), sr=sample_rate, hop_length=hop)
    voiced_at = np.interp(stft_times, frame_times, voiced_mask.astype(float), left=0.0, right=0.0) >= 0.5
    if not np.any(voiced_at):
        return {"centroidHzMean": None, "centroidHzSd": None, "rolloffHzMean": None, "singerFormantRatio": None}
    voiced_mag = magnitude[:, voiced_at]
    freqs = librosa.fft_frequencies(sr=sample_rate, n_fft=spec["nFft"])
    centroid = librosa.feature.spectral_centroid(S=voiced_mag, sr=sample_rate)[0]
    rolloff = librosa.feature.spectral_rolloff(S=voiced_mag, sr=sample_rate, roll_percent=spec["rolloffShare"])[0]
    power = voiced_mag**2
    band = (freqs >= spec["singerFormantBandHz"][0]) & (freqs <= spec["singerFormantBandHz"][1])
    total_power = float(power.sum())
    ratio = float(power[band, :].sum() / total_power) if total_power > 0 else None
    return {
        "centroidHzMean": rnd(float(np.mean(centroid)), 1),
        "centroidHzSd": rnd(float(np.std(centroid)), 1),
        "rolloffHzMean": rnd(float(np.mean(rolloff)), 1),
        "singerFormantRatio": rnd(ratio, 4),
    }


def dynamics_stats(samples, sample_rate, frame_times, voiced_mask, config):
    hop = int(round(config["spectral"]["hopSeconds"] * sample_rate))
    rms = librosa.feature.rms(y=samples, frame_length=config["spectral"]["nFft"], hop_length=hop)[0]
    rms_times = librosa.frames_to_time(np.arange(rms.size), sr=sample_rate, hop_length=hop)
    voiced_at = np.interp(rms_times, frame_times, voiced_mask.astype(float), left=0.0, right=0.0) >= 0.5
    voiced_rms = rms[voiced_at]
    if voiced_rms.size == 0:
        return {"voicedRmsDbP10": None, "voicedRmsDbP50": None, "voicedRmsDbP90": None, "dynamicSpreadDb": None}
    db = 20.0 * np.log10(np.maximum(voiced_rms, 1e-5))
    p10, p50, p90 = (float(np.percentile(db, q)) for q in (10, 50, 90))
    return {
        "voicedRmsDbP10": rnd(p10, 1),
        "voicedRmsDbP50": rnd(p50, 1),
        "voicedRmsDbP90": rnd(p90, 1),
        "dynamicSpreadDb": rnd(p90 - p10, 1),
    }


def analyze_wav(wav_path, config=CONFIG):
    snd = parselmouth.Sound(wav_path)
    if snd.n_channels > 1:
        snd = snd.convert_to_mono()
    sample_rate = snd.sampling_frequency
    samples = snd.values[0].astype(np.float64)
    duration = snd.get_total_duration()

    pitch = snd.to_pitch_ac(
        time_step=config["f0"]["timeStepSeconds"],
        pitch_floor=config["f0"]["floorHz"],
        pitch_ceiling=config["f0"]["ceilingHz"],
    )
    f0_hz = pitch.selected_array["frequency"]
    frame_times = pitch.xs()
    raw_voiced_mask = f0_hz > 0
    voiced_mask, rejected_local_share, rejected_global_share = suppress_f0_outliers(f0_hz, raw_voiced_mask, config)
    voiced_f0 = f0_hz[voiced_mask]
    voiced_share = float(np.mean(voiced_mask)) if f0_hz.size else 0.0

    if voiced_f0.size >= 5:
        percentiles = {f"p{q:02d}": rnd(float(np.percentile(voiced_f0, q)), 1) for q in (5, 25, 50, 75, 95)}
        semitone_range = rnd(12.0 * math.log2(np.percentile(voiced_f0, 95) / np.percentile(voiced_f0, 5)), 2)
        cents_all = hz_to_cents(voiced_f0, config["tuning"]["referenceHz"])
        median_hz = float(np.median(voiced_f0))
        mad_semitones = rnd(float(np.median(np.abs(12.0 * np.log2(voiced_f0 / median_hz)))), 3)
        tuning = tuning_stats(cents_all, config)
    else:
        percentiles = {f"p{q:02d}": None for q in (5, 25, 50, 75, 95)}
        semitone_range = None
        mad_semitones = None
        tuning = {"offsetCents": None, "medianAbsCentError": None, "within25CentsShare": None}

    vibrato, vibrato_segments = vibrato_stats(f0_hz, voiced_mask, config)
    quality, quality_notes = praat_quality(snd, config)
    spectral = spectral_stats(samples, sample_rate, frame_times, voiced_mask, config)
    dynamics = dynamics_stats(samples, sample_rate, frame_times, voiced_mask, config)

    time_step = config["f0"]["timeStepSeconds"]
    runs = voiced_runs(voiced_mask)
    run_durations = [(b - a) * time_step for a, b in runs]
    gaps = 0
    for (_, prev_end), (next_start, _) in zip(runs, runs[1:]):
        if (next_start - prev_end) * time_step >= config["phrasing"]["minPauseSeconds"]:
            gaps += 1
    phrasing = {
        "longestSustainedSeconds": rnd(max(run_durations), 2) if run_durations else None,
        "meanVoicedSegmentSeconds": rnd(float(np.mean(run_durations)), 2) if run_durations else None,
        "pauseRatePerMinute": rnd(gaps / (duration / 60.0), 2) if duration > 0 else None,
    }

    contour_step = max(1, int(round(0.05 / time_step)))
    contour = [
        [rnd(frame_times[i], 2), rnd(float(f0_hz[i]), 1)]
        for i in range(0, f0_hz.size, contour_step)
        if voiced_mask[i]
    ]

    summary = {
        "durationSeconds": rnd(duration),
        "voicing": {
            "voicedShare": rnd(voiced_share),
            "frameCount": int(f0_hz.size),
            "rejectedOutlierShare": rnd(rejected_local_share),
            "rejectedGlobalOutlierShare": rnd(rejected_global_share),
        },
        "pitch": {"f0Hz": percentiles, "rangeSemitonesP05P95": semitone_range, "madSemitones": mad_semitones},
        "tuning": tuning,
        "vibrato": vibrato,
        "quality": quality,
        "spectral": spectral,
        "dynamics": dynamics,
        "phrasing": phrasing,
    }
    detail = {
        "methodId": config["methodId"],
        "f0ContourVoiced50ms": contour,
        "vibratoSegments": vibrato_segments,
        "qualityNotes": quality_notes,
        "config": config,
    }
    return summary, detail


def main():
    sys.stdout.write(json.dumps({"hello": {"versions": versions(), "methodId": CONFIG["methodId"], "config": CONFIG}}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        task = json.loads(line)
        recording_id = task.get("recordingId")
        try:
            summary, detail = analyze_wav(task["wavPath"])
            result = {"recordingId": recording_id, "status": "completed", "summary": summary, "detail": detail}
        except Exception as error:  # noqa: BLE001 — a failed recording must not kill the batch
            result = {
                "recordingId": recording_id,
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
                "trace": traceback.format_exc(limit=3),
            }
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
