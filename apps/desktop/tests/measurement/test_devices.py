"""Picking the UMIK-1 (by name or USB id) and refusing everything else at launch."""

from __future__ import annotations

import pytest

from voice_lab_desktop import chain
from voice_lab_desktop.measurement.devices import DeviceRefused, InputDevice, _linux_usb_id, choose_umik


@pytest.mark.parametrize("name,usb,ok", [
    ("Umik-1  Gain: 18dB", None, True),
    ("UMIK-1", None, True),
    ("umik1 measurement mic", None, True),
    ("Umik-1  Gain: 18dB: USB Audio (hw:2,0)", None, True),
    ("USB Audio Device", "2752:0007", True),
    ("UMIK-2", None, False),
    ("Umik-10", None, False),
    ("MacBook Air Microphone", None, False),
    ("USB Audio Device", "2752:0016", False),
])
def test_is_umik1(name, usb, ok):
    assert chain.is_umik1(name, usb) is ok


def dev(index, name, api="ALSA", channels=1, usb=None):
    return InputDevice(index, name, api, channels, 48000.0, usb)


def test_choose_umik_prefers_the_shortest_path():
    devices = [dev(0, "Umik-1  Gain: 18dB Mono", "JACK Audio Connection Kit"), dev(4, "Umik-1  Gain: 18dB: USB Audio (hw:2,0)"),
               dev(1, "pipewire"), dev(2, "Ryzen HD Audio Controller Analog Stereo", "JACK Audio Connection Kit", 2)]
    chosen = choose_umik(devices)
    assert chosen.index == 4 and chosen.alsa_card == 2
    assert choose_umik([devices[0]]).host_api.startswith("JACK")


def test_no_umik_is_refused_with_what_was_seen():
    with pytest.raises(DeviceRefused, match=r"No UMIK-1 found.*built-in and other microphones are refused.*MacBook Air Microphone"):
        choose_umik([dev(1, "MacBook Air Microphone", "Core Audio")])
    with pytest.raises(DeviceRefused, match="no input devices"):
        choose_umik([])


def test_a_umik_2_is_refused_by_name():
    with pytest.raises(DeviceRefused, match="'UMIK-2' is not a UMIK-1"):
        choose_umik([dev(1, "UMIK-2"), dev(2, "MacBook Air Microphone", "Core Audio")])


def test_alsa_card_only_for_alsa_hw_devices():
    assert dev(1, "X (hw:3,0)").alsa_card == 3
    assert dev(1, "X (hw:3,0)", "JACK Audio Connection Kit").alsa_card is None
    assert dev(1, "pipewire").alsa_card is None


def test_linux_usb_id_from_proc(tmp_path):
    (tmp_path / "card2").mkdir()
    (tmp_path / "card2" / "usbid").write_text("2752:0007\n")
    assert _linux_usb_id(2, tmp_path) == "2752:0007"
    assert _linux_usb_id(5, tmp_path) is None
