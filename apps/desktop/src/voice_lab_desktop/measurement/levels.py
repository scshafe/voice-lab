"""Reading the input level: the hidden gain stage in front of the capsule.

A moved input slider silently invalidates the SPL reference, so the level is
pinned once (``pin.py``) and re-read before, during and after every segment.
One interface, three small readers, chosen by platform and capture path:

- **macOS, CoreAudio**: the UMIK-1's input-scope ``VolumeScalar`` (the slider
  in Audio MIDI Setup), read through the HAL with ctypes.
- **Linux, ALSA** (when capture uses a ``hw:`` device, bypassing PipeWire):
  the card's capture mixer control, read with ``amixer``.
- **Linux, PipeWire** (when capture goes through PipeWire's JACK): the source
  node's ``channelVolumes`` and ``mute``, read with ``pw-dump``. PipeWire's
  source slider drives the card mixer as well as its own software gain, so the
  node's props are what the recorded signal sees.

A reader either returns an :class:`InputLevel` or raises
:class:`LevelUnreadable`; it never guesses. ``volume`` is the slider as a
whole percent (as voice-journey's ``capture.mjs`` logged the macOS input
volume); ``raw`` keeps the exact values, and the guard compares those.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from . import chain
from .devices import InputDevice


class LevelUnreadable(Exception):
    """The level could not be read: recording is refused (never a silent default)."""


@dataclass(frozen=True)
class InputLevel:
    volume: int
    raw: tuple[float, ...]
    muted: bool
    source: str
    control: str

    def same_setting(self, other: InputLevel) -> bool:
        return (self.source, self.raw, self.muted) == (other.source, other.raw, other.muted)

    def describe(self) -> str:
        return f"{self.volume}%{' (muted)' if self.muted else ''} [{self.source}: {self.control}]"

    def to_json(self) -> dict[str, Any]:
        return {
            "volume": self.volume,
            "raw": list(self.raw),
            "muted": self.muted,
            "source": self.source,
            "control": self.control,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> InputLevel:
        return cls(
            int(data["volume"]),
            tuple(float(v) for v in data["raw"]),
            bool(data["muted"]),
            str(data["source"]),
            str(data["control"]),
        )


class LevelReader(Protocol):
    def read(self, device: InputDevice) -> InputLevel: ...


#: ``run(argv) -> stdout``; raises LevelUnreadable on failure. Injected so tests never run a tool.
Runner = Callable[[list[str]], str]


def run_tool(argv: list[str], timeout: float = 5.0) -> str:
    if shutil.which(argv[0]) is None:
        raise LevelUnreadable(f"{argv[0]} is not installed, so the input level cannot be read")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LevelUnreadable(f"{argv[0]} failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise LevelUnreadable(f"{argv[0]} exited {done.returncode}")
    return done.stdout


# --- ALSA -------------------------------------------------------------------------------

_CONTROL = re.compile(r"^Simple mixer control '([^']*)',(\d+)")
_CAPTURE = re.compile(r"Capture (-?\d+) \[(\d+)%\](?: \[(-?[\d.]+)dB\])?(?: \[(on|off)\])?")


def parse_amixer(text: str) -> list[dict[str, Any]]:
    """``amixer -c N scontents`` → the simple controls with a capture volume (name, values,
    percents, switches)."""
    controls: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        head = _CONTROL.match(line)
        if head:
            current = {
                "name": head.group(1),
                "index": int(head.group(2)),
                "caps": "",
                "values": [],
                "percents": [],
                "switches": [],
            }
            controls.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Capabilities:"):
            current["caps"] = stripped.split(":", 1)[1]
        # channel lines ("Mono: Capture 16 [70%] [12.00dB] [on]"); Limits lines carry no [..%]
        else:
            for match in _CAPTURE.finditer(stripped):
                current["values"].append(float(match.group(1)))
                current["percents"].append(int(match.group(2)))
                if match.group(4):
                    current["switches"].append(match.group(4) == "on")
    return [c for c in controls if "cvolume" in c["caps"].split() and c["values"]]


class AlsaLevelReader:
    def __init__(self, run: Runner = run_tool) -> None:
        self.run = run

    def read(self, device: InputDevice) -> InputLevel:
        card = device.alsa_card
        if card is None:
            raise LevelUnreadable(f"{device.name!r} is not an ALSA hw: device")
        controls = parse_amixer(self.run(["amixer", "-c", str(card), "scontents"]))
        if not controls:
            raise LevelUnreadable(
                f"ALSA card {card} has no capture volume control, so its level cannot be pinned"
            )
        if len(controls) > 1:
            names = ", ".join(c["name"] for c in controls)
            raise LevelUnreadable(
                f"ALSA card {card} has several capture volume controls ({names}); "
                "refusing to guess which one is the gain stage"
            )
        c = controls[0]
        return InputLevel(
            volume=c["percents"][0],
            raw=tuple(c["values"]),
            muted=bool(c["switches"]) and not any(c["switches"]),
            source="alsa",
            control=f"card {card} '{c['name']}',{c['index']}",
        )


# --- PipeWire ---------------------------------------------------------------------------


def parse_pw_dump(text: str, device_name: str) -> InputLevel:
    try:
        objects = json.loads(text)
    except ValueError as exc:
        raise LevelUnreadable("pw-dump answered something other than JSON") from exc
    sources = []
    for obj in objects if isinstance(objects, list) else []:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if (
            obj.get("type") != "PipeWire:Interface:Node"
            or props.get("media.class") != "Audio/Source"
        ):
            continue
        names = [str(props.get(k, "")) for k in ("node.description", "node.nick", "node.name")]
        if any(chain.is_umik1(n) for n in names) or device_name in names:
            sources.append(
                (obj.get("id"), names[0] or names[2], (info.get("params") or {}).get("Props") or [])
            )
    if not sources:
        raise LevelUnreadable(
            "PipeWire has no UMIK-1 source node (is it plugged in and not disabled?)"
        )
    if len(sources) > 1:
        raise LevelUnreadable(
            f"PipeWire has {len(sources)} UMIK-1 source nodes; refusing to guess which one records"
        )
    node_id, label, params = sources[0]
    props = next((p for p in params if isinstance(p, dict) and "channelVolumes" in p), None)
    if props is None or not props["channelVolumes"]:
        raise LevelUnreadable(f"PipeWire node {node_id} reports no channel volumes")
    volumes = [float(v) for v in props["channelVolumes"]]
    master = float(props.get("volume", 1.0))
    # wpctl and pavucontrol show the cubic root of the linear channel volume.
    shown = round((max(volumes) * master) ** (1 / 3) * 100)
    return InputLevel(
        volume=shown,
        raw=tuple(round(v, 6) for v in [master, *volumes]),
        muted=bool(props.get("mute", False)),
        source="pipewire",
        control=f"node {node_id} '{label}'",
    )


class PipeWireLevelReader:
    def __init__(self, run: Runner = run_tool) -> None:
        self.run = run

    def read(self, device: InputDevice) -> InputLevel:
        return parse_pw_dump(self.run(["pw-dump"]), device.name)


# --- CoreAudio --------------------------------------------------------------------------


class CoreAudioHal(Protocol):
    def input_devices(
        self,
    ) -> list[tuple[int, str, int]]: ...  # (AudioObjectID, name, input channels)
    def input_volume(self, device_id: int, element: int) -> float | None: ...
    def input_mute(self, device_id: int) -> bool | None: ...


class CoreAudioLevelReader:
    def __init__(self, hal: CoreAudioHal | None = None) -> None:
        self._hal = hal

    @property
    def hal(self) -> CoreAudioHal:
        if self._hal is None:
            self._hal = CtypesCoreAudio()
        return self._hal

    def read(self, device: InputDevice) -> InputLevel:
        found = [d for d in self.hal.input_devices() if d[1] == device.name] or [
            d for d in self.hal.input_devices() if chain.is_umik1(d[1])
        ]
        if not found:
            raise LevelUnreadable("CoreAudio lists no UMIK-1 input device")
        if len(found) > 1:
            raise LevelUnreadable(
                f"CoreAudio lists {len(found)} UMIK-1 input devices; unplug the others"
            )
        device_id, name, channels = found[0]
        values: list[float] = []
        elements: list[int] = []
        main = self.hal.input_volume(device_id, 0)
        if main is not None:
            values, elements = [main], [0]
        else:
            for element in range(1, max(channels, 1) + 1):
                value = self.hal.input_volume(device_id, element)
                if value is not None:
                    values.append(value)
                    elements.append(element)
        if not values:
            raise LevelUnreadable(
                f"CoreAudio exposes no input volume for {name!r}, so its level cannot be pinned"
            )
        mute = self.hal.input_mute(device_id)
        return InputLevel(
            volume=round(values[0] * 100),
            raw=tuple(round(v, 6) for v in values),
            muted=bool(mute),
            source="coreaudio",
            control=f"VolumeScalar input element {','.join(map(str, elements))}",
        )


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class CtypesCoreAudio:
    """The few HAL calls the reader needs (macOS only). Untested without a Mac: keep it this
    small."""

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        if sys.platform != "darwin":
            raise LevelUnreadable("CoreAudio is only available on macOS")
        self.ct = ctypes
        self.ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        self.cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

        class Address(ctypes.Structure):
            _fields_ = [
                ("selector", ctypes.c_uint32),
                ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32),
            ]

        self.Address = Address
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(Address),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(Address),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self.ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, ctypes.POINTER(Address)]
        self.ca.AudioObjectHasProperty.restype = ctypes.c_ubyte
        self.cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.cf.CFStringGetCString.restype = ctypes.c_ubyte
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _address(self, selector: str, scope: str, element: int = 0) -> Any:
        return self.Address(_fourcc(selector), _fourcc(scope), element)

    def _get(self, obj: int, address: Any, out: Any, size: int) -> None:
        io = self.ct.c_uint32(size)
        status = self.ca.AudioObjectGetPropertyData(
            obj, self.ct.byref(address), 0, None, self.ct.byref(io), self.ct.byref(out)
        )
        if status != 0:
            raise LevelUnreadable(f"CoreAudio error {status}")

    def _has(self, obj: int, address: Any) -> bool:
        return bool(self.ca.AudioObjectHasProperty(obj, self.ct.byref(address)))

    def input_devices(self) -> list[tuple[int, str, int]]:
        ct = self.ct
        system = 1  # kAudioObjectSystemObject
        address = self._address("dev#", "glob")
        size = ct.c_uint32(0)
        if (
            self.ca.AudioObjectGetPropertyDataSize(
                system, ct.byref(address), 0, None, ct.byref(size)
            )
            != 0
        ):
            raise LevelUnreadable("CoreAudio did not list devices")
        ids = (ct.c_uint32 * (size.value // 4))()
        self._get(system, address, ids, size.value)
        out = []
        for device_id in ids:
            channels = self._input_channels(device_id)
            if channels > 0:
                out.append((int(device_id), self._name(device_id), channels))
        return out

    def _name(self, device_id: int) -> str:
        ct = self.ct
        ref = ct.c_void_p()
        self._get(device_id, self._address("lnam", "glob"), ref, ct.sizeof(ref))
        buf = ct.create_string_buffer(512)
        ok = self.cf.CFStringGetCString(ref, buf, 512, 0x08000100)  # kCFStringEncodingUTF8
        self.cf.CFRelease(ref)
        return buf.value.decode("utf-8", "replace") if ok else ""

    def _input_channels(self, device_id: int) -> int:
        ct = self.ct
        address = self._address("slay", "inpt")  # kAudioDevicePropertyStreamConfiguration
        size = ct.c_uint32(0)
        if (
            self.ca.AudioObjectGetPropertyDataSize(
                device_id, ct.byref(address), 0, None, ct.byref(size)
            )
            != 0
            or size.value < 8
        ):
            return 0
        raw = (ct.c_ubyte * size.value)()
        self._get(device_id, address, raw, size.value)
        # AudioBufferList: UInt32 mNumberBuffers, then {UInt32 channels, UInt32 size,
        # void* data} each (16 bytes on 64-bit).
        count = int.from_bytes(bytes(raw[0:4]), "little")
        total = 0
        for i in range(count):
            offset = 8 + i * 16
            total += int.from_bytes(bytes(raw[offset : offset + 4]), "little")
        return total

    def input_volume(self, device_id: int, element: int) -> float | None:
        address = self._address("volm", "inpt", element)  # kAudioDevicePropertyVolumeScalar
        if not self._has(device_id, address):
            return None
        value = self.ct.c_float()
        self._get(device_id, address, value, 4)
        return float(value.value)

    def input_mute(self, device_id: int) -> bool | None:
        address = self._address("mute", "inpt", 0)
        if not self._has(device_id, address):
            return None
        value = self.ct.c_uint32()
        self._get(device_id, address, value, 4)
        return bool(value.value)


# --- choosing one -----------------------------------------------------------------------


class PlatformLevelReader:
    """CoreAudio on macOS; on Linux ALSA for a ``hw:`` device, else PipeWire."""

    def __init__(
        self, platform: str = sys.platform, run: Runner = run_tool, hal: CoreAudioHal | None = None
    ) -> None:
        self.platform = platform
        self.coreaudio = CoreAudioLevelReader(hal)
        self.alsa = AlsaLevelReader(run)
        self.pipewire = PipeWireLevelReader(run)

    def reader_for(self, device: InputDevice) -> LevelReader:
        if self.platform == "darwin":
            return self.coreaudio
        if self.platform.startswith("linux"):
            return self.alsa if device.alsa_card is not None else self.pipewire
        raise LevelUnreadable(f"reading the input level is not supported on {self.platform}")

    def read(self, device: InputDevice) -> InputLevel:
        try:
            return self.reader_for(device).read(device)
        except LevelUnreadable:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure to read is "unreadable", never a default
            raise LevelUnreadable(f"reading the input level failed ({type(exc).__name__})") from exc
