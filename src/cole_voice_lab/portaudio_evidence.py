"""The PortAudio capture worker's continuity evidence (D-039): the contract alone.

@canonical portaudio_evidence -- ``voice-lab-desktop.portaudio-continuity/v1``, what the
desktop app's capture worker knows about one take, successful or not. It was the app's own
until the corpus moved to the PC (D-052): a take package (``corpus_takes``) now carries it,
so the PC's validator checks it too. Writing, reading and binding the file on the capturing
machine stay in the app (``voice_lab_desktop.capture.evidence``).

This is not D-015's ``capture-continuity/v1`` (Audio Queue sample times, exact to 1e-6
frames), which the PortAudio worker never writes. Imports stay light: the capture worker
loads this module lazily at the end of a take.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .models import ContractModel, Sha256

SCHEMA_VERSION: Final = "voice-lab-desktop.portaudio-continuity/v1"
MAX_EVIDENCE_BYTES: Final = 16 * 1024
WAV_HEADER_BYTES: Final = 44
SAMPLE_RATE: Final = 48_000
FRAME_BYTES: Final = 3

type Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")]
type Seconds = Annotated[float, Field(allow_inf_nan=False)]


class PortAudioContinuityEvidence(ContractModel):
    """What the worker knows about one take, successful or not."""

    schema_version: Literal["voice-lab-desktop.portaudio-continuity/v1"] = SCHEMA_VERSION
    backend: Literal["portaudio"] = "portaudio"
    #: PortAudio's host API ("Core Audio", "ALSA", "JACK Audio Connection Kit"), or
    #: "Core Audio"/"ALSA" as the simulated device reports it.
    host_api: Text
    #: True when the simulated device produced the take: never hardware evidence.
    simulated: bool
    device_name: Text
    device_usb_id: Text | None
    output_name: FileName
    wav_sha256: Sha256 | None
    wav_size_bytes: int | None = Field(ge=0)
    #: Frames written to the WAV.
    frames: int = Field(ge=0)
    #: Frames accepted from PortAudio (a refused block is never written).
    received_frames: int = Field(ge=0)
    callback_count: int = Field(ge=0)
    block_frames: int = Field(gt=0, le=65536)
    sample_rate: Literal[48000] = SAMPLE_RATE
    first_adc_time: Seconds | None
    last_adc_end: Seconds | None
    adc_time_valid: bool
    tolerance_frames: float = Field(gt=0, allow_inf_nan=False)
    max_abs_deviation_frames: float | None = Field(ge=0, allow_inf_nan=False)
    input_overflows: int = Field(ge=0)
    input_underflows: int = Field(ge=0)
    queue_capacity: int = Field(gt=0)
    queue_high_water: int = Field(ge=0)
    success: bool
    #: Every accepted block's ADC time was checked against the block before it.
    continuity_checked: bool
    stop_reason: Literal["requested", "maximum-duration"] | None
    error: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None
    #: When the stream started (UTC, the worker's clock).
    started_at: AwareDatetime | None

    @model_validator(mode="after")
    def _success_is_complete(self) -> Self:
        if self.queue_high_water > self.queue_capacity:
            raise ValueError("the queue cannot hold more blocks than its capacity")
        if not self.success:
            return self
        problems = [
            problem
            for problem, broken in (
                ("an error", self.error is not None),
                ("no continuity check", not self.continuity_checked),
                ("an invalid ADC time", not self.adc_time_valid),
                ("no frames", self.frames <= 0),
                ("unwritten frames", self.frames != self.received_frames),
                ("an overflow", self.input_overflows > 0),
                ("an underflow", self.input_underflows > 0),
                ("no stop reason", self.stop_reason is None),
                ("no start time", self.started_at is None),
                ("no WAV digest", self.wav_sha256 is None),
                (
                    "a WAV size that is not 44 + 3 x frames",
                    self.wav_size_bytes != WAV_HEADER_BYTES + self.frames * FRAME_BYTES,
                ),
                ("no ADC span", self.first_adc_time is None or self.last_adc_end is None),
                (
                    "a deviation beyond the tolerance",
                    self.max_abs_deviation_frames is not None
                    and self.max_abs_deviation_frames > self.tolerance_frames,
                ),
                (
                    "no deviation measured over several blocks",
                    self.callback_count > 1 and self.max_abs_deviation_frames is None,
                ),
            )
            if broken
        ]
        if problems:
            raise ValueError("successful evidence with " + ", ".join(problems))
        return self

    @property
    def device(self) -> str:
        return f"{self.device_name} [{self.host_api}]"
