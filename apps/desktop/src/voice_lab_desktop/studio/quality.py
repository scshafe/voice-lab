"""A take's quality at review (D-058; the recording UX review's item 1): flags that never block.

The studio checks a sealed take off the GUI thread and shows what it finds beside the take.
Nothing here holds Save & Next back.

- **The PC's own QC.** ``cole_voice_lab.audio``'s ``qc_metrics``: the rule the PC applies to a
  take (``corpus_takes.take_qc_report``), run here on the same WAV. A finding predicts the PC's
  *Needs review*. Clipping (a sample at full scale, or a peak within ``NEAR_CLIP_DBFS`` of it)
  has a flag of its own; the PC's other findings share **PC QC**.
- **Levels against the room and this session, never against an absolute loudness target.**
  The UMIK-1's fixed gain puts speech near −50 dBFS RMS, and the training copies get a linear
  gain (FINETUNE.md, ``training_gain_db``), so a raw level says little. **Low SNR**: the speech
  stands less than ``LOW_SNR_DB`` over the take's own noise floor (its quietest tenth of a
  second: the start before the voice, or a pause; digital silence, a dropout the PC's QC
  reports, is left out). **Inconsistent**: the speech's level is more than ``DRIFT_DB`` from
  the median of this session's recent kept takes.
- **Length** against the catalog's expected duration: **Short**, **Long**.
- **Edges**: speech in the first or the last ``EDGE_SECONDS`` (a cut onset or a cut tail).

Room tone and procedure steps (not spoken) get the PC's QC (quiet allowed, as there), clipping
and the length; the speech checks are for spoken prompts only.

Every threshold beyond the PC's is a starting point, to be calibrated on the first hundred
real takes on the Mac (the review's advice).
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from cole_voice_lab.audio import inspect_pcm_bytes

from ..capture.types import SAMPLE_RATE
from ..corpus.files import private_dir

type Samples = npt.NDArray[np.float32]

#: The level windows: 20 ms.
WINDOW_FRAMES: Final = SAMPLE_RATE // 50
#: The noise floor: the quietest stretch of this many windows (100 ms).
FLOOR_WINDOWS: Final = 5
#: Speech: the windows this far over the floor.
SPEECH_OVER_FLOOR_DB: Final = 10.0
#: Low SNR under this (Microsoft's dataset guidance: under 20 dB, noise is obvious) ...
LOW_SNR_DB: Final = 20.0
#: ... and good at or over this (35 dB), for Check Input's verdict.
GOOD_SNR_DB: Final = 35.0
#: Inconsistent: this far from the median of the session's recent kept takes ...
DRIFT_DB: Final = 6.0
#: ... once there are this many, over the last ``RECENT_TAKES``.
MIN_RECENT: Final = 3
RECENT_TAKES: Final = 10
#: Short under this share of the expected duration; long over this many times it, plus slack.
SHORT_RATIO: Final = 0.5
LONG_RATIO: Final = 2.0
LONG_SLACK_SECONDS: Final = 2.0
#: Edges: the first and last 100 ms, speech when within this of the speech's level.
EDGE_SECONDS: Final = 0.1
EDGE_BELOW_SPEECH_DB: Final = 12.0
#: Near full scale: a peak at or over this is flagged as clipping too.
NEAR_CLIP_DBFS: Final = -1.0
#: The PC QC's findings, in words (``cole_voice_lab.audio.WavInspection.qc_metrics``).
PC_FINDINGS: Final[dict[str, str]] = {
    "clipped-samples": "clipped samples",
    "peak-too-quiet": "a peak under −35 dBFS",
    "dc-offset": "a DC offset",
    "digital-silence-or-dropout": "a dropout (a quarter of a second or more of digital silence)",
    "short-digital-silence-or-dropout": "a short dropout",
    "repeated-buffer-boundary-discontinuities": "repeated discontinuities at buffer boundaries",
}
#: Under ``<data dir>/studio/``: the verdicts of this machine's takes.
STORE_FILE: Final = "quality.json"
#: The training copies' level (FINETUNE.md, "Level normalization is required"):
#: ``cole_voice_lab.finetune``'s FINETUNE_TARGET_RMS and FINETUNE_PEAK_CEILING_DBFS.
TRAINING_RMS_DBFS: Final = -20.0
TRAINING_PEAK_CEILING_DBFS: Final = -0.5
_SILENT_DB: Final = -300.0


def training_gain_db(rms_dbfs: float, peak_dbfs: float) -> float:
    """The training copies' linear gain (``cole_voice_lab.finetune.normalization_gain_db``):
    RMS raised to −20 dBFS when below, never past a −0.5 dBFS peak."""
    gain = max(0.0, TRAINING_RMS_DBFS - rms_dbfs)
    return round(min(gain, TRAINING_PEAK_CEILING_DBFS - peak_dbfs), 2)


@dataclass(frozen=True)
class Flag:
    """One finding: ``code`` (``clip``, ``pc-qc``, ``low-snr``, ``inconsistent``, ``short``,
    ``long``, ``edge``), its chip's ``title``, a sentence of ``detail`` and its ``tone``."""

    code: str
    title: str
    detail: str
    tone: str = "warn"


@dataclass(frozen=True)
class Levels:
    """A take's levels: the whole take's peak and RMS, the speech's level (``None``: nothing
    stands over the floor), the noise floor, the SNR and the loudest 20 ms at each edge."""

    seconds: float
    peak_dbfs: float
    rms_dbfs: float
    speech_dbfs: float | None
    floor_dbfs: float
    snr_db: float | None
    head_dbfs: float
    tail_dbfs: float


@dataclass(frozen=True)
class Verdict:
    """What the studio found in a take: its levels, the PC QC's findings and the flags."""

    levels: Levels
    pc_findings: tuple[str, ...]
    flags: tuple[Flag, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    def to_json(self) -> dict[str, Any]:
        return {
            "levels": asdict(self.levels),
            "pc_findings": list(self.pc_findings),
            "flags": [asdict(flag) for flag in self.flags],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Verdict:
        return cls(
            levels=Levels(**data["levels"]),
            pc_findings=tuple(str(code) for code in data["pc_findings"]),
            flags=tuple(Flag(**flag) for flag in data["flags"]),
        )


def _db(power: float) -> float:
    return 10.0 * math.log10(power) if power > 0 else _SILENT_DB


def db_text(value: float) -> str:
    """A level in dB, one decimal, with a true minus sign."""
    return f"{value:.1f}".replace("-", "−")


def _window_powers(samples: Samples) -> npt.NDArray[np.float64]:
    """Mean power of each 20 ms window (the last, partial one left out unless it is all)."""
    values = samples.astype(np.float64)
    count = values.shape[0] // WINDOW_FRAMES
    if count == 0:
        return np.array([float(np.mean(values * values)) if values.size else 0.0])
    windows = values[: count * WINDOW_FRAMES].reshape(count, WINDOW_FRAMES)
    return np.mean(windows * windows, axis=1)


def _floor_db(powers: npt.NDArray[np.float64]) -> float:
    """The quietest 100 ms (``FLOOR_WINDOWS`` windows in a row) that is not digital silence."""
    span = min(FLOOR_WINDOWS, powers.size)
    if span == 0:
        return _SILENT_DB
    stretches = np.convolve(powers, np.ones(span) / span, mode="valid")
    heard = stretches[stretches > 0]
    return _db(float(np.min(heard))) if heard.size else _SILENT_DB


def _edge_db(samples: Samples) -> float:
    powers = _window_powers(samples)
    return _db(float(np.max(powers))) if powers.size else _SILENT_DB


def measure(samples: Samples) -> Levels:
    """The take's levels (``samples``: mono, in [-1, 1])."""
    frames = int(samples.shape[0])
    values = samples.astype(np.float64)
    peak = float(np.max(np.abs(values))) if frames else 0.0
    rms_power = float(np.mean(values * values)) if frames else 0.0
    powers = _window_powers(samples)
    decibels = np.array([_db(float(p)) for p in powers])
    floor = _floor_db(powers)
    active = powers[decibels > floor + SPEECH_OVER_FLOOR_DB]
    speech = _db(float(np.mean(active))) if active.size else None
    edge = min(frames, round(EDGE_SECONDS * SAMPLE_RATE))
    return Levels(
        seconds=frames / SAMPLE_RATE,
        peak_dbfs=20.0 * math.log10(peak) if peak > 0 else _SILENT_DB,
        rms_dbfs=_db(rms_power),
        speech_dbfs=speech,
        floor_dbfs=floor,
        snr_db=None if speech is None else speech - floor,
        head_dbfs=_edge_db(samples[:edge]),
        tail_dbfs=_edge_db(samples[frames - edge :]),
    )


def pc_findings(wav: bytes, *, spoken: bool) -> tuple[tuple[str, ...], int]:
    """The PC's QC of the take's WAV (its bytes): its findings and the clipped samples it
    counted. It measures every sample in Python (about 19 ms a second of audio), so the
    studio runs it only between takes. Raises: ``ContractError`` (not a PCM WAV)."""
    metrics = inspect_pcm_bytes(wav).qc_metrics("original.wav", allow_quiet=not spoken)
    return tuple(metrics.findings), int(metrics.clipped_samples)


def judge(
    levels: Levels,
    findings: Sequence[str],
    *,
    clipped: int,
    spoken: bool,
    expected_seconds: float,
    recent: Sequence[float] = (),
) -> tuple[Flag, ...]:
    """The flags of a take with these ``levels`` and PC QC ``findings`` (``clipped``: samples
    at full scale), for a spoken prompt or not, against the catalog's ``expected_seconds`` and
    the speech levels of this session's ``recent`` kept takes (oldest first)."""
    flags: list[Flag] = []
    if clipped or levels.peak_dbfs >= NEAR_CLIP_DBFS:
        detail = (
            f"{clipped:,} sample{'' if clipped == 1 else 's'} at full scale"
            if clipped
            else f"its peak is {db_text(levels.peak_dbfs)} dBFS, within 1 dB of full scale"
        )
        flags.append(Flag("clip", "Clipped", f"Clipped: {detail}.", "danger"))
    others = [code for code in findings if code != "clipped-samples"]
    if others:
        words = ", ".join(PC_FINDINGS.get(code, code) for code in others)
        flags.append(Flag("pc-qc", "PC QC", f"The PC's QC will ask for a review: {words}."))
    if spoken:
        if levels.speech_dbfs is None or levels.snr_db is None:
            flags.append(
                Flag(
                    "low-snr",
                    "Low SNR",
                    "Nothing stands 10 dB over the take's quietest tenth of a second: no "
                    "speech, or no pause to measure the room by.",
                )
            )
        elif levels.snr_db < LOW_SNR_DB:
            flags.append(
                Flag(
                    "low-snr",
                    "Low SNR",
                    f"The speech stands {levels.snr_db:.0f} dB over the room's floor "
                    f"(under {LOW_SNR_DB:.0f}).",
                )
            )
        window = list(recent)[-RECENT_TAKES:]
        if levels.speech_dbfs is not None and len(window) >= MIN_RECENT:
            median = float(np.median(window))
            drift = levels.speech_dbfs - median
            if abs(drift) > DRIFT_DB:
                way = "louder" if drift > 0 else "quieter"
                flags.append(
                    Flag(
                        "inconsistent",
                        "Inconsistent",
                        f"{abs(drift):.1f} dB {way} than your last {len(window)} kept takes "
                        "(their median).",
                    )
                )
    if expected_seconds > 0:
        if levels.seconds < SHORT_RATIO * expected_seconds:
            flags.append(
                Flag(
                    "short",
                    "Short",
                    f"{levels.seconds:.1f} s, for about {expected_seconds:.1f} s expected.",
                )
            )
        elif levels.seconds > LONG_RATIO * expected_seconds + LONG_SLACK_SECONDS:
            flags.append(
                Flag(
                    "long",
                    "Long",
                    f"{levels.seconds:.1f} s, for about {expected_seconds:.1f} s expected.",
                )
            )
    if spoken and levels.speech_dbfs is not None:
        speech, floor = levels.speech_dbfs, levels.floor_dbfs

        def voiced(edge_dbfs: float) -> bool:
            return (
                edge_dbfs >= speech - EDGE_BELOW_SPEECH_DB
                and edge_dbfs > floor + SPEECH_OVER_FLOOR_DB
            )

        head, tail = voiced(levels.head_dbfs), voiced(levels.tail_dbfs)
        if head and tail:
            title, detail = "Cut edges", "the first and the last"
        elif head:
            title, detail = "Cut start", "the first"
        else:
            title, detail = "Cut end", "the last"
        if head or tail:
            flags.append(
                Flag(
                    "edge",
                    title,
                    f"Speech in {detail} {EDGE_SECONDS:.1f} s: a word may be cut.",
                )
            )
    return tuple(flags)


def check(
    wav: bytes,
    samples: Samples,
    *,
    spoken: bool,
    expected_seconds: float,
    recent: Sequence[float] = (),
) -> Verdict:
    """The verdict of the take whose WAV is ``wav``, its ``samples`` decoded (``judge``'s
    other arguments). Raises: what ``pc_findings`` raises."""
    levels = measure(samples)
    findings, clipped = pc_findings(wav, spoken=spoken)
    flags = judge(
        levels,
        findings,
        clipped=clipped,
        spoken=spoken,
        expected_seconds=expected_seconds,
        recent=recent,
    )
    return Verdict(levels, findings, flags)


def describe(verdict: Verdict, *, room_floor_dbfs: float | None = None) -> str:
    """The flags' details, then the take's levels (and Check Input's room, when it was
    measured since the app started), a line each."""
    levels = verdict.levels
    lines = [flag.detail for flag in verdict.flags] or ["No flags."]
    if levels.speech_dbfs is not None and levels.snr_db is not None:
        lines.append(
            f"Speech {db_text(levels.speech_dbfs)} dBFS · room {db_text(levels.floor_dbfs)} "
            f"dBFS · SNR {levels.snr_db:.1f} dB"
        )
    else:
        lines.append(f"Room {db_text(levels.floor_dbfs)} dBFS · no speech over it")
    gain = training_gain_db(levels.rms_dbfs, levels.peak_dbfs)
    lines.append(
        f"Peak {db_text(levels.peak_dbfs)} dBFS · after the training gain (+{gain:.1f} dB): "
        f"RMS {db_text(levels.rms_dbfs + gain)} dBFS"
    )
    if room_floor_dbfs is not None:
        lines.append(f"Check Input's room: {db_text(room_floor_dbfs)} dBFS")
    return "\n".join(lines)


class QualityStore:
    """The verdicts of this machine's takes, by take id, in ``<data dir>/studio/quality.json``
    (0600; this machine's notes, never sent). A take's verdict is the one made at its review,
    against that session's recent takes; a take reviewed before this store existed gets one
    when it is first shown. Thread-safe: verdicts are written from the task pool."""

    VERSION: Final = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._verdicts: dict[str, tuple[str, Verdict]] | None = None

    def _load(self) -> dict[str, tuple[str, Verdict]]:
        if self._verdicts is None:
            verdicts: dict[str, tuple[str, Verdict]] = {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == self.VERSION:
                    for take_id, entry in dict(data.get("takes", {})).items():
                        verdicts[str(take_id)] = (
                            str(entry["prompt_id"]),
                            Verdict.from_json(entry["verdict"]),
                        )
            except (OSError, ValueError, KeyError, TypeError):
                verdicts = {}  # unreadable: verdicts are made again as takes are shown
            self._verdicts = verdicts
        return self._verdicts

    def warm(self) -> None:
        """Read the file now (off the GUI thread: ``peek`` never reads it)."""
        with self._lock:
            self._load()

    def get(self, take_id: str) -> Verdict | None:
        """The take's verdict, the file read first if it was not (a worker's call)."""
        with self._lock:
            known = self._load().get(take_id)
        return known[1] if known is not None else None

    def peek(self, take_id: str) -> Verdict | None:
        """The take's verdict when the file has been read (the GUI thread's call)."""
        with self._lock:
            known = self._verdicts.get(take_id) if self._verdicts is not None else None
        return known[1] if known is not None else None

    def put(self, take_id: str, prompt_id: str, verdict: Verdict) -> None:
        """Keep ``verdict``. Raises: ``OSError``."""
        with self._lock:
            verdicts = self._load()
            verdicts[take_id] = (prompt_id, verdict)
            data = {
                "version": self.VERSION,
                "takes": {
                    key: {"prompt_id": prompt, "verdict": known.to_json()}
                    for key, (prompt, known) in sorted(verdicts.items())
                },
            }
            private_dir(self.path.parent)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, separators=(",", ":"))
            os.replace(temporary, self.path)


#: A 24-bit sample at full scale, as the samples in [-1, 1] hold it.
_FULL_SCALE_LOW: Final = -1.0
_FULL_SCALE_HIGH: Final = (2**23 - 1) / 2**23


@dataclass(frozen=True)
class CheckLevels:
    """Check Input's measurement: the room's floor (the quiet), the speech's level and peak
    (the line read aloud), its SNR, the samples at full scale, and the training gain with the
    levels it would give (FINETUNE.md)."""

    floor_dbfs: float
    speech_dbfs: float | None
    rms_dbfs: float
    peak_dbfs: float
    snr_db: float | None
    clipped: int
    gain_db: float
    #: From the first to the last 20 ms of speech (0 without speech): the line's length.
    speech_seconds: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CheckLevels:
        return cls(**data)

    def verdict(self) -> tuple[str, str]:
        """One line and its tone."""
        if self.clipped or self.peak_dbfs >= NEAR_CLIP_DBFS:
            return "Clipping: move back from the microphone.", "danger"
        if self.snr_db is None:
            return "No speech stood over the room: is the UMIK-1 facing you?", "warn"
        if self.snr_db < LOW_SNR_DB:
            return "The room is loud for this level: quiet it, or move closer.", "warn"
        if self.snr_db < GOOD_SNR_DB:
            return "Good enough to record; a quieter room would help.", "info"
        return "Good to record.", "ok"

    def rows(self) -> list[dict[str, str]]:
        """The results, a label and a value each (with a tone)."""

        db = db_text
        rows = [{"label": "Room", "value": f"{db(self.floor_dbfs)} dBFS", "tone": ""}]
        if self.speech_dbfs is None or self.snr_db is None:
            rows.append({"label": "Speech", "value": "none heard", "tone": "warn"})
        else:
            rows.append(
                {
                    "label": "Speech",
                    "value": f"{db(self.speech_dbfs)} dBFS · peak {db(self.peak_dbfs)} dBFS",
                    "tone": "",
                }
            )
            tone = (
                "ok" if self.snr_db >= GOOD_SNR_DB else "warn" if self.snr_db < LOW_SNR_DB else ""
            )
            rows.append({"label": "SNR", "value": f"{self.snr_db:.1f} dB", "tone": tone})
            after_rms, after_peak = self.rms_dbfs + self.gain_db, self.peak_dbfs + self.gain_db
            limited = " (the peak limits it)" if after_rms < TRAINING_RMS_DBFS - 0.05 else ""
            rows.append(
                {
                    "label": "After the training gain",
                    "value": f"+{self.gain_db:.1f} dB: RMS {db(after_rms)} dBFS, "
                    f"peak {db(after_peak)} dBFS{limited}",
                    "tone": "",
                }
            )
        rows.append(
            {
                "label": "Clipping",
                "value": f"{self.clipped:,} samples at full scale" if self.clipped else "none",
                "tone": "danger" if self.clipped else "",
            }
        )
        return rows


def check_levels(samples: Samples, *, quiet_seconds: float, settle_seconds: float) -> CheckLevels:
    """Check Input's levels: the floor from ``[settle, quiet)`` seconds of ``samples``, the
    speech from the rest."""
    settle, quiet = round(settle_seconds * SAMPLE_RATE), round(quiet_seconds * SAMPLE_RATE)
    room, speech = samples[settle:quiet], samples[quiet:]
    room_powers = _window_powers(room)
    floor = _db(float(np.median(room_powers))) if room_powers.size else _SILENT_DB
    powers = _window_powers(speech)
    decibels = np.array([_db(float(p)) for p in powers])
    active = powers[decibels > floor + SPEECH_OVER_FLOOR_DB]
    speech_dbfs = _db(float(np.mean(active))) if active.size else None
    voiced = np.flatnonzero(decibels > floor + SPEECH_OVER_FLOOR_DB)
    spoken = (voiced[-1] - voiced[0] + 1) * WINDOW_FRAMES / SAMPLE_RATE if voiced.size else 0.0
    values = speech.astype(np.float64)
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    peak_dbfs = 20.0 * math.log10(peak) if peak > 0 else _SILENT_DB
    rms_dbfs = _db(float(np.mean(values * values))) if values.size else _SILENT_DB
    clipped = int(np.count_nonzero((samples <= _FULL_SCALE_LOW) | (samples >= _FULL_SCALE_HIGH)))
    return CheckLevels(
        floor_dbfs=floor,
        speech_dbfs=speech_dbfs,
        rms_dbfs=rms_dbfs,
        peak_dbfs=peak_dbfs,
        snr_db=None if speech_dbfs is None else speech_dbfs - floor,
        clipped=clipped,
        gain_db=training_gain_db(rms_dbfs, peak_dbfs),
        speech_seconds=float(spoken),
    )


#: The match line: a reading this far from the reference is worth a look.
MATCH_LENGTH_SHARE: Final = 0.2


def match_rows(now: CheckLevels, reference: CheckLevels) -> list[dict[str, str]]:
    """Check Input's line against the reference reading of it (the match line): its level
    (Inconsistent's tolerance), its SNR and its length."""
    if now.speech_dbfs is None or reference.speech_dbfs is None:
        return [{"label": "Match line", "value": "no speech to compare", "tone": "warn"}]
    level = now.speech_dbfs - reference.speech_dbfs
    way = "louder" if level > 0 else "quieter"
    rows = [
        {
            "label": "Match line: level",
            "value": "the same" if abs(level) < 0.05 else f"{abs(level):.1f} dB {way}",
            "tone": "warn" if abs(level) > DRIFT_DB else "ok",
        }
    ]
    if now.snr_db is not None and reference.snr_db is not None:
        snr = now.snr_db - reference.snr_db
        rows.append(
            {
                "label": "Match line: SNR",
                "value": f"{abs(snr):.1f} dB {'better' if snr >= 0 else 'worse'}",
                "tone": "warn" if snr < -DRIFT_DB else "",
            }
        )
    if reference.speech_seconds > 0:
        length = now.speech_seconds - reference.speech_seconds
        share = abs(length) / reference.speech_seconds
        rows.append(
            {
                "label": "Match line: length",
                "value": f"{abs(length):.1f} s {'longer' if length >= 0 else 'shorter'}",
                "tone": "warn" if share > MATCH_LENGTH_SHARE else "",
            }
        )
    return rows
