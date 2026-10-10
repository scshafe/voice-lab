"""Level readers: ALSA (amixer), PipeWire (pw-dump), CoreAudio (a fake HAL), and choosing one. No tool is run."""

from __future__ import annotations

import json

import pytest

from voice_journey_capture.devices import InputDevice
from voice_journey_capture.levels import (
    AlsaLevelReader,
    CoreAudioLevelReader,
    InputLevel,
    LevelUnreadable,
    PipeWireLevelReader,
    PlatformLevelReader,
    parse_amixer,
    run_tool,
)

ALSA_UMIK = InputDevice(4, "Umik-1  Gain: 18dB: USB Audio (hw:2,0)", "ALSA", 1, 48000.0, "2752:0007")
JACK_UMIK = InputDevice(7, "Umik-1  Gain: 18dB Mono", "JACK Audio Connection Kit", 1, 48000.0)
CA_UMIK = InputDevice(3, "Umik-1  Gain: 18dB", "Core Audio", 1, 48000.0)

AMIXER_UMIK = """Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined cswitch cswitch-joined
  Capture channels: Mono
  Limits: Capture 0 - 23
  Mono: Capture 16 [70%] [12.00dB] [on]
"""
AMIXER_MUTED = AMIXER_UMIK.replace("[on]", "[off]")
AMIXER_STEREO_AND_PLAYBACK = """Simple mixer control 'PCM',0
  Capabilities: pvolume pvolume-joined pswitch
  Playback channels: Mono
  Limits: Playback 0 - 31
  Mono: Playback 31 [100%] [0.00dB] [on]
Simple mixer control 'Capture',0
  Capabilities: cvolume cswitch
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 63
  Front Left: Capture 40 [63%] [18.00dB] [on]
  Front Right: Capture 41 [65%] [18.75dB] [on]
"""


def runner(outputs: dict[str, str], calls: list | None = None):
    def run(argv: list[str]) -> str:
        if calls is not None:
            calls.append(argv)
        if argv[0] not in outputs:
            raise LevelUnreadable(f"{argv[0]} is not installed, so the input level cannot be read")
        return outputs[argv[0]]
    return run


def test_parse_amixer_keeps_only_capture_volume_controls():
    controls = parse_amixer(AMIXER_STEREO_AND_PLAYBACK)
    assert [c["name"] for c in controls] == ["Capture"]
    assert controls[0]["values"] == [40.0, 41.0] and controls[0]["percents"] == [63, 65] and controls[0]["switches"] == [True, True]


def test_alsa_reader_reads_the_umik_card():
    calls: list = []
    level = AlsaLevelReader(runner({"amixer": AMIXER_UMIK}, calls)).read(ALSA_UMIK)
    assert calls == [["amixer", "-c", "2", "scontents"]]
    assert level == InputLevel(70, (16.0,), False, "alsa", "card 2 'Mic',0")


def test_alsa_reader_sees_mute_and_refuses_ambiguity_and_absence():
    assert AlsaLevelReader(runner({"amixer": AMIXER_MUTED})).read(ALSA_UMIK).muted
    with pytest.raises(LevelUnreadable, match="no capture volume control"):
        AlsaLevelReader(runner({"amixer": "Simple mixer control 'PCM',0\n  Capabilities: pvolume\n"})).read(ALSA_UMIK)
    with pytest.raises(LevelUnreadable, match="several capture volume controls"):
        AlsaLevelReader(runner({"amixer": AMIXER_UMIK + AMIXER_STEREO_AND_PLAYBACK})).read(ALSA_UMIK)
    with pytest.raises(LevelUnreadable, match="not an ALSA hw"):
        AlsaLevelReader(runner({"amixer": AMIXER_UMIK})).read(JACK_UMIK)


def pw_node(node_id: int, description: str, volumes: list[float], mute: bool = False, media_class: str = "Audio/Source", props: bool = True) -> dict:
    params = {"Props": [{"volume": 1.0, "mute": mute, "channelVolumes": volumes}]} if props else {}
    return {"id": node_id, "type": "PipeWire:Interface:Node",
            "info": {"props": {"media.class": media_class, "node.description": description,
                               "node.name": "alsa_input.usb-miniDSP_Umik-1_Gain__18dB_00002-00.mono-fallback"},
                     "params": params}}


def test_pipewire_reader_reads_the_umik_source_node():
    dump = [pw_node(40, "Ryzen HD Audio Controller Analog Stereo", [1.0, 1.0]),
            pw_node(41, "Umik-1  Gain: 18dB", [0.125]),
            pw_node(42, "Umik-1 monitor", [1.0], media_class="Audio/Sink")]
    dump[0]["info"]["props"]["node.name"] = "alsa_input.pci-0000_0b_00.6.analog-stereo"
    level = PipeWireLevelReader(runner({"pw-dump": json.dumps(dump)})).read(JACK_UMIK)
    assert level.volume == 50  # cbrt(0.125) = 0.5, as wpctl/pavucontrol show it
    assert level.raw == (1.0, 0.125) and level.source == "pipewire" and level.control.startswith("node 41")


@pytest.mark.parametrize("dump,match", [
    ([], "no UMIK-1 source node"),
    ([pw_node(1, "Umik-1", [0.5]), pw_node(2, "UMIK-1 #2", [0.5])], "2 UMIK-1 source nodes"),
    ([pw_node(1, "Umik-1", [], props=False)], "no channel volumes"),
])
def test_pipewire_reader_refuses(dump, match):
    with pytest.raises(LevelUnreadable, match=match):
        PipeWireLevelReader(runner({"pw-dump": json.dumps(dump)})).read(JACK_UMIK)


def test_pipewire_reader_refuses_garbage_and_sees_mute():
    with pytest.raises(LevelUnreadable, match="JSON"):
        PipeWireLevelReader(runner({"pw-dump": "not json"})).read(JACK_UMIK)
    assert PipeWireLevelReader(runner({"pw-dump": json.dumps([pw_node(1, "Umik-1", [0.3], mute=True)])})).read(JACK_UMIK).muted


class FakeHal:
    def __init__(self, devices, volumes, mute=None):
        self.devices, self.volumes, self.mute = devices, volumes, mute

    def input_devices(self):
        return self.devices

    def input_volume(self, device_id, element):
        return self.volumes.get((device_id, element))

    def input_mute(self, device_id):
        return self.mute


def test_coreaudio_reads_the_main_element():
    hal = FakeHal([(71, "MacBook Air Microphone", 1), (88, "Umik-1  Gain: 18dB", 1)], {(88, 0): 0.6299999})
    level = CoreAudioLevelReader(hal).read(CA_UMIK)
    assert level == InputLevel(63, (0.63,), False, "coreaudio", "VolumeScalar input element 0")


def test_coreaudio_falls_back_to_channel_elements_and_reports_mute():
    hal = FakeHal([(88, "Umik-1  Gain: 18dB", 2)], {(88, 1): 0.5, (88, 2): 0.5}, mute=True)
    level = CoreAudioLevelReader(hal).read(CA_UMIK)
    assert level.raw == (0.5, 0.5) and level.control.endswith("element 1,2") and level.muted


@pytest.mark.parametrize("devices,volumes,match", [
    ([(71, "MacBook Air Microphone", 1)], {}, "no UMIK-1"),
    ([(88, "Umik-1  Gain: 18dB", 1)], {}, "no input volume"),
    ([(88, "Umik-1 A", 1), (89, "Umik-1 B", 1)], {}, "2 UMIK-1 input devices"),
])
def test_coreaudio_refuses(devices, volumes, match):
    with pytest.raises(LevelUnreadable, match=match):
        CoreAudioLevelReader(FakeHal(devices, volumes)).read(InputDevice(3, "Umik-1", "Core Audio", 1, 48000.0))


def test_platform_reader_dispatch():
    outputs = {"amixer": AMIXER_UMIK, "pw-dump": json.dumps([pw_node(41, "Umik-1", [1.0])])}
    hal = FakeHal([(88, "Umik-1  Gain: 18dB", 1)], {(88, 0): 1.0})
    assert PlatformLevelReader("darwin", runner(outputs), hal).read(CA_UMIK).source == "coreaudio"
    assert PlatformLevelReader("linux", runner(outputs), hal).read(ALSA_UMIK).source == "alsa"
    assert PlatformLevelReader("linux", runner(outputs), hal).read(JACK_UMIK).source == "pipewire"
    with pytest.raises(LevelUnreadable, match="not supported on win32"):
        PlatformLevelReader("win32", runner(outputs), hal).read(CA_UMIK)


def test_platform_reader_turns_any_failure_into_unreadable():
    def broken(argv):
        raise RuntimeError("boom")
    with pytest.raises(LevelUnreadable, match="RuntimeError"):
        PlatformLevelReader("linux", broken).read(ALSA_UMIK)


def test_run_tool_refuses_a_missing_tool_and_a_failing_one():
    with pytest.raises(LevelUnreadable, match="not installed"):
        run_tool(["vjc-no-such-tool-xyz"])
    with pytest.raises(LevelUnreadable, match="exited 1"):
        run_tool(["false"])
    assert run_tool(["echo", "ok"]).strip() == "ok"


def test_input_level_equality_and_json():
    a = InputLevel(63, (0.63,), False, "coreaudio", "x")
    assert a.same_setting(InputLevel.from_json(a.to_json()))
    assert not a.same_setting(InputLevel(63, (0.631,), False, "coreaudio", "x"))  # same percent, moved slider
    assert not a.same_setting(InputLevel(63, (0.63,), False, "alsa", "x"))
