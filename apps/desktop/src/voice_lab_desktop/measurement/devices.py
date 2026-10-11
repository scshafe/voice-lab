"""Input devices and the raw input stream, behind one small interface.

The real backend is PortAudio (``sounddevice``): CoreAudio on macOS, ALSA (a
``hw:`` device, bit-exact) or JACK (PipeWire's JACK) on Linux. A stream
delivers mono signed 32-bit little-endian frames; a 24-bit device fills the
top 24 bits, which the WAV writer keeps. Nothing here resamples: on macOS
PortAudio is told to fail rather than convert, on ALSA ``hw:`` cannot, and on
JACK the graph must already run at 48 kHz (the error says so).

Tests use :class:`voice_lab_desktop.measurement.simulated.SimulatedBackend`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import chain

#: ``callback(frames_bytes, frame_count, overflowed)``, called on the audio thread.
BlockCallback = Callable[[bytes, int, bool], None]

_HW = re.compile(r"\(hw:(\d+),(\d+)\)")


class DeviceError(Exception):
    """The device cannot be used; the message says why and what to do."""


class DeviceRefused(DeviceError):
    """No UMIK-1 (or another device than the UMIK-1 was offered): refused at launch."""


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    host_api: str
    channels: int
    default_rate: float
    usb_id: str | None = None

    @property
    def alsa_card(self) -> int | None:
        """The ALSA card number of a ``hw:`` device (Linux), else None."""
        match = _HW.search(self.name)
        return int(match.group(1)) if match and self.host_api == "ALSA" else None

    def describe(self) -> str:
        return f"{self.name} [{self.host_api}]"


class InputStream(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class AudioBackend(Protocol):
    def list_inputs(self) -> list[InputDevice]: ...
    def open_input(
        self, device: InputDevice, sample_rate: int, callback: BlockCallback
    ) -> InputStream: ...


# Prefer the path with the fewest stages: CoreAudio, then ALSA hw (no graph), then JACK (PipeWire).
_HOST_API_ORDER = {"Core Audio": 0, "ALSA": 1, "JACK Audio Connection Kit": 2}


def choose_umik(devices: list[InputDevice]) -> InputDevice:
    """The UMIK-1 among ``devices``; DeviceRefused, saying what was seen, when there is none."""
    candidates = [d for d in devices if d.channels >= 1 and chain.is_umik1(d.name, d.usb_id)]
    if not candidates:
        others = [d for d in devices if chain.is_other_umik(d.name)]
        if others:
            raise DeviceRefused(
                f"{others[0].name!r} is not a UMIK-1. At launch only the miniDSP UMIK-1 is "
                f"supported (chain {chain.CHAIN_ID}); other chains need a reviewed change in "
                "voice-journey's chain registry first."
            )
        seen = ", ".join(sorted({d.name for d in devices})) or "no input devices"
        raise DeviceRefused(
            "No UMIK-1 found: plug in the miniDSP UMIK-1. At launch it is the only supported "
            f"device (chain {chain.CHAIN_ID}); built-in and other microphones are refused. "
            f"Seen: {seen}."
        )
    return sorted(candidates, key=lambda d: (_HOST_API_ORDER.get(d.host_api, 9), d.index))[0]


def _linux_usb_id(card: int, root: Path = Path("/proc/asound")) -> str | None:
    try:
        return (root / f"card{card}" / "usbid").read_text().strip().lower() or None
    except OSError:
        return None


class PortAudioBackend:
    """The real backend (``sounddevice``). Imported lazily: PortAudio is a system library
    on Linux."""

    def __init__(self) -> None:
        try:
            import sounddevice
        except OSError as exc:  # libportaudio missing
            raise DeviceError(
                "PortAudio is not installed (Arch: sudo pacman -S portaudio; Debian/Ubuntu: "
                "sudo apt install libportaudio2)"
            ) from exc
        self.sd = sounddevice

    def list_inputs(self) -> list[InputDevice]:
        sd = self.sd
        try:
            sd._terminate()  # re-scan: PortAudio lists devices once per initialisation
            sd._initialize()
        except Exception:  # noqa: BLE001 - a re-scan is best effort
            pass
        apis = [api["name"] for api in sd.query_hostapis()]
        out = []
        for info in sd.query_devices():
            if info["max_input_channels"] < 1:
                continue
            device = InputDevice(
                index=int(info["index"]),
                name=str(info["name"]),
                host_api=apis[info["hostapi"]],
                channels=int(info["max_input_channels"]),
                default_rate=float(info["default_samplerate"]),
            )
            if device.alsa_card is not None:
                device = InputDevice(
                    **{**device.__dict__, "usb_id": _linux_usb_id(device.alsa_card)}
                )
            out.append(device)
        return out

    def open_input(
        self, device: InputDevice, sample_rate: int, callback: BlockCallback
    ) -> InputStream:
        sd = self.sd
        extra = None
        if sys.platform == "darwin":
            # Set the device to 48 kHz, and fail instead of converting (no resampling in a
            # measurement chain).
            extra = sd.CoreAudioSettings(
                change_device_parameters=True, fail_if_conversion_required=True
            )

        def on_block(indata: Any, frames: int, _time: Any, status: Any) -> None:
            callback(bytes(indata), int(frames), bool(status.input_overflow))

        try:
            stream: InputStream = sd.RawInputStream(
                samplerate=sample_rate,
                device=device.index,
                channels=1,
                dtype="int32",
                latency="high",
                callback=on_block,
                extra_settings=extra,
            )
            return stream
        except sd.PortAudioError as exc:
            hint = ""
            if device.host_api.startswith("JACK"):
                hint = (
                    " (PipeWire's graph must run at 48 kHz: "
                    "pw-metadata -n settings 0 clock.force-rate 48000)"
                )
            raise DeviceError(
                f"cannot open {device.describe()} at {sample_rate} Hz, 24-bit mono: {exc}{hint}"
            ) from exc
