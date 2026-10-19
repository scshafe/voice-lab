"""The continuity evidence of a take read from an ALSA PCM (D-056): the contract alone.

@canonical alsa_evidence -- ``cole-voice-lab.alsa-continuity/v1``, what a capture client that
reads an ALSA PCM (the house panel, from its XVF3800) knows about one take, successful or not,
so the PC can judge the take's continuity itself (docs/design/HOUSE-PANEL.md §2). It holds
integers only, so the client formats no floats in it.

After each read of one period, the client records one pair ``[position_frames,
timestamp_ns]``: the stream's hardware position (the frames the device had delivered since the
stream started: the frames read so far plus ``avail``) and ALSA's timestamp of that position
(``snd_pcm_status``'s ``htstamp``, on the stream's ``tstamp_type`` clock). USB audio has no
link timestamps, so it is the timestamp of the hardware pointer's update.

**The rule** (``period_deviations_ns``), which the PC recomputes from the pairs: every pair
after the first is the pair before it plus the frames between them at the nominal rate,

    deviation[i] = (timestamp[i] - timestamp[i-1]) - (position[i] - position[i-1]) * ns_per_frame

within ``tolerance_ns``, where ``ns_per_frame = 1_000_000_000 // rate`` is exact (the rate
must divide a second in nanoseconds: 62,500 ns at 16 kHz), so the client and the PC compute the
same integers. Each pair is compared with the one before it, as D-039's blocks are, so jitter
does not accumulate and the drift of the device's clock does not matter. The tolerance absorbs
timestamp jitter, not lost audio:

- a lost period moves the timestamp by a whole period, at least ``MIN_PERIOD_FRAMES`` (16 ms
  at 16 kHz), beyond the largest tolerance the PC accepts, ``MAX_TOLERANCE_NS`` (4 ms: D-039's
  64 frames, at 16 kHz);
- an overrun is an xrun, and a successful take has none.

The device states the largest deviation it measured; the PC's figure must equal it. The bounds
are revisited with data once the panel's capture (#22) has measured the array, as spike S-1
did for D-039.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from .models import ContractModel, Sha256

SCHEMA_VERSION: Final = "cole-voice-lab.alsa-continuity/v1"
WAV_NAME: Final = "original.wav"
#: The WAV is plain PCM: a 44-byte header (``WAVE_FORMAT_PCM``), then the frames ALSA gave.
WAV_HEADER_BYTES: Final = 44
NANOSECONDS_PER_SECOND: Final = 1_000_000_000
#: The longest take, as a desktop take (D-056).
MAX_TAKE_SECONDS: Final = 660
#: The shortest period: 16 ms at 16 kHz, so a lost period is never within the tolerance.
MIN_PERIOD_FRAMES: Final = 256
MAX_PERIOD_FRAMES: Final = 16_384
MAX_BUFFER_FRAMES: Final = 1 << 20
#: The largest tolerance the PC accepts: 4 ms, D-039's 64 frames at 16 kHz.
MAX_TOLERANCE_NS: Final = 4_000_000
#: One pair per period of a 660 s take at 16 kHz in periods of ``MIN_PERIOD_FRAMES``.
MAX_PERIODS: Final = MAX_TAKE_SECONDS * 16_000 // MIN_PERIOD_FRAMES
#: The file's bound: ``MAX_PERIODS`` pairs of a 10-digit position and a 19-digit timestamp
#: fit in 1.4 MB.
MAX_EVIDENCE_BYTES: Final = 2 * 1024 * 1024
_MAX_POSITION: Final = (1 << 40) - 1
_MAX_TIMESTAMP_NS: Final = (1 << 63) - 1

#: The ALSA sample formats a take may have, and the WAV encoding each gives byte for byte.
type AlsaFormat = Literal["S16_LE", "S24_3LE", "S32_LE"]
ALSA_FORMAT_ENCODINGS: Final[dict[str, Literal["pcm_s16le", "pcm_s24le", "pcm_s32le"]]] = {
    "S16_LE": "pcm_s16le",
    "S24_3LE": "pcm_s24le",
    "S32_LE": "pcm_s32le",
}
ALSA_FORMAT_WIDTHS: Final[dict[str, int]] = {"S16_LE": 2, "S24_3LE": 3, "S32_LE": 4}

type Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type Position = Annotated[int, Field(ge=0, le=_MAX_POSITION)]
type TimestampNs = Annotated[int, Field(ge=0, le=_MAX_TIMESTAMP_NS)]


def period_deviations_ns(periods: Sequence[tuple[int, int]], rate: int) -> tuple[int, ...]:
    """Each pair's deviation from the pair before it at the nominal ``rate``, in nanoseconds.

    ``periods`` are ``(position_frames, timestamp_ns)`` pairs, in order; the result has one
    deviation per pair after the first (none for one pair). Positive means the timestamp
    advanced more than the frames did: audio the stream did not deliver.
    Raises: ``ValueError`` when ``rate`` does not divide a second in nanoseconds.
    """
    if rate <= 0 or NANOSECONDS_PER_SECOND % rate:
        raise ValueError(f"{rate} Hz does not divide a second in whole nanoseconds")
    ns_per_frame = NANOSECONDS_PER_SECOND // rate
    return tuple(
        (timestamp - before_timestamp) - (position - before_position) * ns_per_frame
        for (before_position, before_timestamp), (position, timestamp) in zip(
            periods, periods[1:], strict=False
        )
    )


def wav_size_bytes(frames: int, channels: int, alsa_format: str) -> int:
    """The size of the plain-PCM WAV of ``frames`` frames of ``channels`` channels."""
    return WAV_HEADER_BYTES + frames * channels * ALSA_FORMAT_WIDTHS[alsa_format]


class AlsaWav(ContractModel):
    """The WAV the evidence describes: the take's ``original.wav``."""

    name: Literal["original.wav"] = WAV_NAME
    sha256: Sha256
    size_bytes: int = Field(gt=WAV_HEADER_BYTES)


class AlsaContinuityEvidence(ContractModel):
    """What the capture client knows about one take read from an ALSA PCM."""

    schema_version: Literal["cole-voice-lab.alsa-continuity/v1"] = SCHEMA_VERSION
    #: True when a simulated source produced the take: never hardware evidence.
    simulated: bool
    #: The PCM the client opened, by its ALSA name (``hw:CARD=Array,DEV=0``).
    pcm: Text
    #: The hardware parameters the PCM was opened with.
    access: Literal["rw_interleaved", "mmap_interleaved"]
    format: AlsaFormat
    rate: int = Field(ge=8_000, le=384_000)
    channels: int = Field(ge=1, le=32)
    period_frames: int = Field(ge=MIN_PERIOD_FRAMES, le=MAX_PERIOD_FRAMES)
    buffer_frames: int = Field(le=MAX_BUFFER_FRAMES)
    #: The clock of the timestamps (``SND_PCM_TSTAMP_TYPE_MONOTONIC_RAW`` preferred).
    tstamp_type: Literal["monotonic_raw", "monotonic"]
    xruns: int = Field(ge=0)
    #: One ``[position_frames, timestamp_ns]`` pair after each period read, in order.
    periods: tuple[tuple[Position, TimestampNs], ...] = Field(max_length=MAX_PERIODS)
    tolerance_ns: int = Field(ge=1, le=MAX_TOLERANCE_NS)
    #: The largest ``abs(deviation)`` the client measured; ``None`` for a single pair.
    max_abs_deviation_ns: int | None = Field(ge=0)
    #: Frames written to the WAV: every frame read, as ALSA delivered it.
    frames: int = Field(ge=0)
    wav: AlsaWav | None
    #: When the stream started (UTC, the capturing host's clock).
    started_at: datetime | None
    stop_reason: Literal["requested", "maximum-duration"] | None
    success: bool
    error: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None

    @field_validator("started_at")
    @classmethod
    def _started_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("the stream's start requires an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if NANOSECONDS_PER_SECOND % self.rate:
            raise ValueError(f"{self.rate} Hz does not divide a second in whole nanoseconds")
        if self.buffer_frames < 2 * self.period_frames:
            raise ValueError("the buffer holds at least two periods")
        for (before_position, before_timestamp), (position, timestamp) in zip(
            self.periods, self.periods[1:], strict=False
        ):
            if position <= before_position or timestamp <= before_timestamp:
                raise ValueError("the periods' positions and timestamps strictly increase")
        if self.success:
            self._success_is_complete()
        elif self.error is None:
            raise ValueError("an unsuccessful take names its error")
        return self

    def _success_is_complete(self) -> None:
        if self.error is not None:
            raise ValueError("a successful take has no error")
        if self.xruns:
            raise ValueError("a successful take has no xrun")
        if not self.periods or self.frames != len(self.periods) * self.period_frames:
            raise ValueError("a successful take's frames are its periods' frames")
        if self.frames > MAX_TAKE_SECONDS * self.rate:
            raise ValueError(f"a take lasts at most {MAX_TAKE_SECONDS} s")
        last_position = self.periods[-1][0]
        if not 0 <= last_position - self.frames <= self.buffer_frames:
            raise ValueError("the last position is the frames read plus at most a buffer")
        if self.wav is None or self.wav.size_bytes != wav_size_bytes(
            self.frames, self.channels, self.format
        ):
            raise ValueError("a successful take describes its WAV: a 44-byte header and its frames")
        if self.started_at is None or self.stop_reason is None:
            raise ValueError("a successful take has its start and its stop reason")
        deviations = period_deviations_ns(self.periods, self.rate)
        measured = max((abs(deviation) for deviation in deviations), default=None)
        if measured != self.max_abs_deviation_ns:
            raise ValueError(
                f"the largest deviation is {measured} ns, not the {self.max_abs_deviation_ns} "
                "the client measured"
            )
        if measured is not None and measured > self.tolerance_ns:
            raise ValueError(
                f"a period deviates by {measured} ns, beyond the tolerance of "
                f"{self.tolerance_ns} ns"
            )
