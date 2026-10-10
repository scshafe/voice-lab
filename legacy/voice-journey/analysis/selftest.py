#!/usr/bin/env python3
"""Synthetic-audio self-test for the feature extractor.

Generates known signals (no corpus audio involved) and asserts the extractor
recovers ground truth within tolerance. Run on the host inside the venv:

    analysis/.venv/bin/python analysis/selftest.py
"""

import math
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from extract_features import CONFIG, analyze_wav, suppress_f0_outliers

SAMPLE_RATE = CONFIG["sampleRate"]
FAILURES = []


def check(label, condition, detail):
    status = "pass" if condition else "FAIL"
    print(f"{status}: {label} ({detail})")
    if not condition:
        FAILURES.append(label)


def write_wav(directory, name, samples):
    path = Path(directory) / name
    sf.write(path, samples.astype(np.float32), SAMPLE_RATE)
    return str(path)


def tone(duration, freq_hz, vibrato_rate_hz=0.0, vibrato_extent_cents=0.0):
    t = np.arange(int(duration * SAMPLE_RATE)) / SAMPLE_RATE
    if vibrato_rate_hz > 0:
        cents = vibrato_extent_cents * np.sin(2 * math.pi * vibrato_rate_hz * t)
        inst_freq = freq_hz * np.power(2.0, cents / 1200.0)
    else:
        inst_freq = np.full_like(t, freq_hz)
    phase = 2 * math.pi * np.cumsum(inst_freq) / SAMPLE_RATE
    fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.05)
    return 0.4 * np.sin(phase) * fade


def glissando(duration, start_hz, end_hz):
    t = np.arange(int(duration * SAMPLE_RATE)) / SAMPLE_RATE
    inst_freq = start_hz * np.power(end_hz / start_hz, t / duration)
    phase = 2 * math.pi * np.cumsum(inst_freq) / SAMPLE_RATE
    return 0.4 * np.sin(phase)


def main():
    with tempfile.TemporaryDirectory(prefix="voice-journey-selftest-") as tmp:
        steady, _ = analyze_wav(write_wav(tmp, "steady.wav", tone(3.0, 440.0)))
        p50 = steady["pitch"]["f0Hz"]["p50"]
        check("steady tone f0 median ~440 Hz", p50 is not None and abs(p50 - 440.0) < 5.0, f"p50={p50}")
        check("steady tone is voiced", steady["voicing"]["voicedShare"] > 0.8, f"voicedShare={steady['voicing']['voicedShare']}")
        check(
            "steady tone in tune with its own grid",
            steady["tuning"]["medianAbsCentError"] is not None and steady["tuning"]["medianAbsCentError"] < 10.0,
            f"medianAbsCentError={steady['tuning']['medianAbsCentError']}",
        )
        check(
            "steady tone has no vibrato",
            (steady["vibrato"]["vibratoTimeShare"] or 0.0) < 0.2,
            f"vibratoTimeShare={steady['vibrato']['vibratoTimeShare']}",
        )

        vib, _ = analyze_wav(write_wav(tmp, "vibrato.wav", tone(3.0, 440.0, vibrato_rate_hz=5.5, vibrato_extent_cents=40.0)))
        rate = vib["vibrato"]["meanRateHz"]
        extent = vib["vibrato"]["meanExtentCents"]
        check("vibrato rate ~5.5 Hz", rate is not None and abs(rate - 5.5) < 0.8, f"rate={rate}")
        check("vibrato extent ~40 cents", extent is not None and 20.0 <= extent <= 60.0, f"extent={extent}")
        check(
            "vibrato time share is high",
            (vib["vibrato"]["vibratoTimeShare"] or 0.0) > 0.5,
            f"vibratoTimeShare={vib['vibrato']['vibratoTimeShare']}",
        )

        gliss, _ = analyze_wav(write_wav(tmp, "gliss.wav", glissando(3.0, 220.0, 880.0)))
        rng = gliss["pitch"]["rangeSemitonesP05P95"]
        check("glissando spans ~2 octaves (p05–p95 > 18 st)", rng is not None and 18.0 <= rng <= 24.5, f"range={rng}")

        rng_state = np.random.default_rng(2026)
        noise, _ = analyze_wav(write_wav(tmp, "noise.wav", 0.2 * rng_state.standard_normal(int(2.0 * SAMPLE_RATE))))
        check("noise is mostly unvoiced", noise["voicing"]["voicedShare"] < 0.3, f"voicedShare={noise['voicing']['voicedShare']}")

        # Harmonic-lock outliers: a steady 220 Hz tone with two short bursts at
        # the 6x harmonic must not inflate the p05-p95 range once outlier
        # suppression rejects the burst frames.
        base = tone(3.0, 220.0)
        burst = tone(0.15, 1320.0)
        contaminated = base.copy()
        for start_s in (1.0, 2.0):
            start = int(start_s * SAMPLE_RATE)
            contaminated[start:start + burst.size] = burst
        jump, _ = analyze_wav(write_wav(tmp, "jump.wav", contaminated))
        jump_range = jump["pitch"]["rangeSemitonesP05P95"]
        check(
            "harmonic bursts do not inflate the range",
            jump_range is not None and jump_range < 6.0,
            f"range={jump_range}, rejectedShare={jump['voicing']['rejectedOutlierShare']}",
        )

    # Direct unit check of the suppression layer with a synthetic f0 track:
    # 300 frames at 220 Hz with 10 isolated frames locked to the 6x harmonic
    # (praat's own tracker often absorbs synthetic bursts, so the array-level
    # test is the deterministic guard for this code path).
    f0_track = np.full(300, 220.0)
    f0_track[140:150] = 1320.0
    f0_track[::37] = 0.0  # scattered unvoiced frames
    mask = f0_track > 0
    cleaned, local_share, global_share = suppress_f0_outliers(f0_track, mask, CONFIG)
    check(
        "suppression rejects harmonic-locked frames",
        not np.any(cleaned[140:150]) and 0.02 <= local_share + global_share <= 0.06,
        f"localShare={round(local_share, 4)}, globalShare={round(global_share, 4)}",
    )
    voiced_burst_frames = int(mask[140:150].sum())
    check(
        "suppression keeps honest frames",
        int(cleaned.sum()) == int(mask.sum()) - voiced_burst_frames,
        f"kept={int(cleaned.sum())} of {int(mask.sum())}, burstVoiced={voiced_burst_frames}",
    )
    # A SUSTAINED lock region (1.2 s at the 6x harmonic) dominates its own
    # local window and must be caught by the global gate instead.
    sustained = np.full(600, 220.0)
    sustained[300:420] = 1320.0
    sustained_mask = sustained > 0
    cleaned_sustained, _, sustained_global_share = suppress_f0_outliers(sustained, sustained_mask, CONFIG)
    check(
        "global gate rejects sustained lock regions",
        not np.any(cleaned_sustained[300:420]) and sustained_global_share > 0.15,
        f"globalShare={round(sustained_global_share, 4)}",
    )
    # A glissando-shaped track (smooth 800 cents/sec sweep) must survive intact.
    sweep = 220.0 * np.power(2.0, np.linspace(0.0, 2.0, 300))
    sweep_mask = np.ones(300, dtype=bool)
    cleaned_sweep, sweep_local, sweep_global = suppress_f0_outliers(sweep, sweep_mask, CONFIG)
    check(
        "suppression keeps smooth glissandi",
        bool(np.all(cleaned_sweep)) and sweep_local == 0.0 and sweep_global == 0.0,
        f"localShare={sweep_local}, globalShare={sweep_global}",
    )

    quality = steady["quality"]
    check("praat quality metrics computed", all(quality[key] is not None for key in ("jitterLocal", "shimmerLocal", "meanHnrDb")), f"quality={quality}")
    check("CPPS computed", quality["cpps"] is not None, f"cpps={quality['cpps']}")

    if FAILURES:
        print(f"\nselftest FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("\nselftest passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
