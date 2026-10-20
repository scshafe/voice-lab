"""The capture profiles of the PC's corpus: the device and room a take was recorded with (D-056).

@canonical capture_profiles -- a ``cole-voice-lab.corpus-take/v2`` take declares its capture
profile (``corpus_takes.CorpusTakeManifestV2.capture_profile``): what recorded it, where, and
what each channel of its WAV carries. Every take before v2 (voice-lab-desktop's
``corpus-take/v1`` and D-053's imported takes) is profile ``umik-1``, which no record states.

v2 defines one profile, ``xvf3800-linear-4``: the reSpeaker Flex XVF3800 Linear-4 on USB (UAC
2.0), as the house panel records with it (docs/design/HOUSE-PANEL.md §2). The profile is a
tagged union by ``profile``, of one member until ``umik-1`` joins v2 when the desktop moves to
it.

The XVF3800 decimates its microphones to 16 kHz, so the profile's audio is 16 kHz only, in the
2- or 6-channel image (in the 6-channel image, channels 3 to 6 can carry the four raw
microphones), at the sample width ``USB_BIT_DEPTH`` sets. Its 48 kHz image only upsamples the
16 kHz content, so a take in it would claim a band it does not have, and the profile refuses
it.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self, get_args

from pydantic import Field, StringConstraints, model_validator

from .models import AudioSpec, ContractModel

XVF3800_PROFILE: Final = "xvf3800-linear-4"
#: Seeed Studio's USB vendor id: the XVF3800 arrays are Seeed's reSpeaker devices.
SEEED_USB_VENDOR_ID: Final = 0x2886
#: The reSpeaker Flex XVF3800 Linear-4's USB product id, as the house panel's array reports it
#: (measured on the panel, 2026-10-19).
XVF3800_LINEAR4_USB_PRODUCT_ID: Final = 0x0022
XVF3800_SAMPLE_RATE_HZ: Final = 16_000
#: The firmware's 16 kHz images: 2 channels, or 6 (2 processed, then 4 that can be raw).
XVF3800_CHANNEL_COUNTS: Final = frozenset({2, 6})
XVF3800_MICROPHONES: Final = 4

type RoomSlug = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=1, max_length=64)
]
type _Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type _Byte = Annotated[int, Field(ge=0, le=255)]
type _UsbId = Annotated[int, Field(ge=0, le=0xFFFF)]
type _Setting = int | Annotated[float, Field(allow_inf_nan=False)]

#: The XVF3800 parameters a take records, read from the device when the take started.
type Xvf3800Parameter = Literal[
    "SHF_BYPASS",
    "PP_AGCONOFF",
    "PP_AGCGAIN",
    "PP_MIN_NS",
    "PP_MIN_NN",
    "PP_ECHOONOFF",
    "PP_NLATTENONOFF",
    "PP_LIMITONOFF",
    "AEC_HPFONOFF",
]
XVF3800_PARAMETERS: Final = frozenset(get_args(Xvf3800Parameter.__value__))
#: A processing stage that ran on a processed channel, by the switch that controls it:
#: ``aec`` (``SHF_BYPASS`` off), ``agc`` (``PP_AGCONOFF``), ``noise-suppression``
#: (``PP_MIN_NS``/``PP_MIN_NN``), ``echo-suppression`` (``PP_ECHOONOFF``),
#: ``nonlinear-attenuation`` (``PP_NLATTENONOFF``), ``limiter`` (``PP_LIMITONOFF``) and
#: ``high-pass`` (``AEC_HPFONOFF``).
type ProcessingStage = Literal[
    "aec",
    "agc",
    "noise-suppression",
    "echo-suppression",
    "nonlinear-attenuation",
    "limiter",
    "high-pass",
]


class Xvf3800Firmware(ContractModel):
    """The array's firmware, as it reports itself."""

    #: ``VERSION``'s three bytes (major, minor, patch).
    version: tuple[_Byte, _Byte, _Byte]
    #: ``BLD_MSG``.
    build_message: _Text
    #: ``BLD_REPO_HASH``.
    build_repo_hash: _Text


class Xvf3800Device(ContractModel):
    """The array that recorded the take."""

    model: _Text
    usb_vendor_id: _UsbId
    usb_product_id: _UsbId
    #: The USB serial, when the device reports one.
    serial: _Text | None
    firmware: Xvf3800Firmware

    @model_validator(mode="after")
    def _seeed(self) -> Self:
        if self.usb_vendor_id != SEEED_USB_VENDOR_ID:
            raise ValueError("an XVF3800 take is recorded by a Seeed USB device (vendor 0x2886)")
        if self.usb_product_id != XVF3800_LINEAR4_USB_PRODUCT_ID:
            raise ValueError(
                "an XVF3800 Linear-4 take is recorded by the Flex Linear-4 (product 0x0022)"
            )
        return self


class Xvf3800Channel(ContractModel):
    """One channel of the take's WAV: where its output came from, and what ran on it."""

    #: ``processed`` audio, or a raw ``mic``.
    source: Literal["processed", "mic"]
    #: The raw microphone (1 to 4), for a ``mic`` channel only.
    mic: Annotated[int, Field(ge=1, le=XVF3800_MICROPHONES)] | None
    #: The output's routing as the device reports it (``AUDIO_MGR_OP_*``): its category and
    #: its source within the category.
    routing: tuple[_Byte, _Byte]
    #: The processing that ran on the channel, in no particular order: none on a raw
    #: microphone.
    processing: tuple[ProcessingStage, ...]

    @model_validator(mode="after")
    def _source(self) -> Self:
        if (self.source == "mic") != (self.mic is not None):
            raise ValueError("a channel names its microphone exactly when it is a raw mic")
        if self.source == "mic" and self.processing:
            raise ValueError("a raw microphone channel has no processing")
        if len(set(self.processing)) != len(self.processing):
            raise ValueError("a channel names each processing stage once")
        return self


class Xvf3800Linear4Profile(ContractModel):
    """Profile ``xvf3800-linear-4``: the take's array, room, channels and settings."""

    profile: Literal["xvf3800-linear-4"] = XVF3800_PROFILE
    device: Xvf3800Device
    room: RoomSlug
    #: One entry per WAV channel, in the WAV's order.
    channels: tuple[Xvf3800Channel, ...] = Field(min_length=2, max_length=6)
    #: The channel (1-based) the take is judged by (QC) and later derived from.
    primary_channel: int = Field(ge=1, le=6)
    #: Each parameter's values as the device read them when the take started.
    settings: dict[
        Xvf3800Parameter,
        Annotated[tuple[_Setting, ...], Field(min_length=1, max_length=16)],
    ]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(self.channels) not in XVF3800_CHANNEL_COUNTS:
            raise ValueError("an XVF3800 take has the 2- or the 6-channel image")
        if self.primary_channel > len(self.channels):
            raise ValueError("the primary channel is one of the take's channels")
        mics = [channel.mic for channel in self.channels if channel.mic is not None]
        if len(set(mics)) != len(mics):
            raise ValueError("each raw microphone is on one channel at most")
        if set(self.settings) != XVF3800_PARAMETERS:
            raise ValueError(
                "the settings record every parameter: " + ", ".join(sorted(XVF3800_PARAMETERS))
            )
        return self

    def allows(self, audio: AudioSpec) -> bool:
        """Whether a WAV of format ``audio`` is one this profile's take may have: 16 kHz,
        one channel per entry of ``channels``, at any of the firmware's sample widths."""
        return audio.sample_rate_hz == XVF3800_SAMPLE_RATE_HZ and audio.channels == len(
            self.channels
        )


#: Every capture profile a v2 take may declare (a tagged union by ``profile``).
type CaptureProfile = Xvf3800Linear4Profile
