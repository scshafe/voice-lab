"""PCM and native audio contract tests; no framework or audio device is opened."""

from __future__ import annotations

import ctypes
import hashlib
import io
import math
import os
import struct
import wave
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import cole_voice_lab.desktop_audio as desktop_audio
from cole_voice_lab.desktop_audio import NativeAudioPlayer, load_pcm_audio
from cole_voice_lab.errors import ContractError, ExternalToolError, SecurityError

PCM_RATE = 48_000
SYNTHETIC_SAMPLES = (0, -32768, 32767, -120, 120, 0, 42)


def wav_payload(samples=SYNTHETIC_SAMPLES, *, width=2, rate=PCM_RATE, channels=1):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(
            b"".join(sample.to_bytes(width, "little", signed=True) for sample in samples)
        )
    return stream.getvalue()


@pytest.fixture
def audio_path(tmp_path):
    path = tmp_path / "synthetic.wav"
    path.write_bytes(wav_payload())
    return path


@pytest.mark.parametrize(
    "width,samples", [(2, SYNTHETIC_SAMPLES), (3, (-8388608, 0, 8388607, -123, 99))]
)
def test_pcm_loader_preserves_all_samples_and_hashes_original_container(tmp_path, width, samples):
    path = tmp_path / "samples.wav"
    payload = wav_payload(samples, width=width)
    path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    clip = load_pcm_audio(path, expected_sha256=expected, bin_count=20)
    assert (clip.sample_rate, clip.sample_width, clip.channels, clip.frame_count) == (
        PCM_RATE,
        width,
        1,
        len(samples),
    )
    scale = 1 << (width * 8 - 1)
    assert clip.waveform_bins == tuple((sample / scale, sample / scale) for sample in samples)
    assert clip.source_sha256 == expected
    assert clip.duration_seconds == len(samples) / PCM_RATE


def test_waveform_scans_every_frame_in_uneven_bins_including_last_transient(tmp_path):
    path = tmp_path / "transients.wav"
    samples = (0, 100, -500, 32767, -32768, 3, 27000)
    path.write_bytes(wav_payload(samples))
    clip = load_pcm_audio(path, bin_count=3)
    assert clip.waveform_bins == (
        (0.0, 100 / 32768),
        (-500 / 32768, 32767 / 32768),
        (-1.0, 27000 / 32768),
    )


def test_single_waveform_bin_keeps_decode_working_set_bounded(tmp_path, monkeypatch):
    path = tmp_path / "wide-bin.wav"
    path.write_bytes(wav_payload((0,) * 70000 + (-32768, 32767)))
    original_decoder = desktop_audio._sample_values
    decoded_sizes = []

    def track_decode(payload, width):
        decoded_sizes.append(len(payload))
        return original_decoder(payload, width)

    monkeypatch.setattr(desktop_audio, "_sample_values", track_decode)
    clip = load_pcm_audio(path, bin_count=1)
    assert clip.waveform_bins == ((-1.0, 32767 / 32768),)
    assert len(decoded_sizes) == 2
    assert max(decoded_sizes) <= desktop_audio.WAVEFORM_DECODE_FRAMES * clip.sample_width


def test_trim_preview_is_exact_frame_slice_and_does_not_change_source(audio_path):
    original = audio_path.read_bytes()
    clip = load_pcm_audio(audio_path)
    selected = clip.wav_bytes(1, 5)
    with wave.open(io.BytesIO(selected), "rb") as reader:
        assert reader.getnframes() == 4
        assert reader.readframes(4) == clip.pcm[2:10]
    assert audio_path.read_bytes() == original
    assert clip.wav_bytes() == original
    with pytest.raises(FrozenInstanceError):
        clip.frame_count = 9


@pytest.mark.parametrize("start,end", [(-1, 2), (1, 1), (3, 2), (0, 99), (True, 2), (0, 2.5)])
def test_trim_rejects_empty_partial_or_out_of_bounds_frame_ranges(audio_path, start, end):
    clip = load_pcm_audio(audio_path)
    with pytest.raises(ContractError, match="complete frames"):
        clip.wav_bytes(start, end)


@pytest.mark.parametrize(
    "updates",
    [
        {"sample_rate": 0},
        {"sample_width": 4},
        {"channels": 2},
        {"frame_count": 0},
        {"pcm": b""},
        {"waveform_bins": ()},
        {"waveform_bins": ((0.0, math.nan),)},
        {"waveform_bins": ((1.0, -1.0),)},
        {"source_sha256": "bad"},
    ],
)
def test_immutable_clip_rejects_inconsistent_pcm_and_waveform_contract(audio_path, updates):
    clip = load_pcm_audio(audio_path)
    with pytest.raises(ContractError):
        replace(clip, **updates)


@pytest.mark.parametrize(
    "options",
    [{"expected_sha256": "x"}, {"bin_count": 0}, {"bin_count": True}, {"bin_count": 20000}],
)
def test_pcm_loader_rejects_invalid_expected_evidence_and_bin_budgets(audio_path, options):
    with pytest.raises(ContractError):
        load_pcm_audio(audio_path, **options)


def test_pcm_loader_rejects_digest_mismatch(audio_path):
    with pytest.raises(SecurityError, match="SHA-256"):
        load_pcm_audio(audio_path, expected_sha256="0" * 64)


@pytest.mark.parametrize("parent_link", [False, True])
def test_pcm_loader_rejects_final_and_parent_symlinks(audio_path, tmp_path, parent_link):
    link = tmp_path / "linked"
    link.symlink_to(audio_path.parent if parent_link else audio_path)
    with pytest.raises(SecurityError, match="symlink"):
        load_pcm_audio(link / audio_path.name if parent_link else link)


def test_pcm_loader_rejects_directory_and_missing_paths(tmp_path):
    for path in (tmp_path, tmp_path / "missing.wav"):
        with pytest.raises(SecurityError):
            load_pcm_audio(path)


def test_pcm_loader_rejects_concurrent_path_replacement(audio_path, monkeypatch):
    real_lstat = Path.lstat
    calls = 0

    def changed(path):
        nonlocal calls
        result = real_lstat(path)
        if path == audio_path:
            calls += 1
            if calls >= 3:
                return os.stat_result(
                    (
                        result.st_mode,
                        result.st_ino + 1,
                        result.st_dev,
                        result.st_nlink,
                        result.st_uid,
                        result.st_gid,
                        result.st_size,
                        result.st_atime,
                        result.st_mtime,
                        result.st_ctime,
                    )
                )
        return result

    monkeypatch.setattr(Path, "lstat", changed)
    with pytest.raises(SecurityError, match="changed"):
        load_pcm_audio(audio_path)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not a RIFF file" * 4,
        wav_payload(channels=2),
        wav_payload(width=4),
        wav_payload(rate=1),
        wav_payload(samples=()),
        wav_payload() + b"undeclared",
        wav_payload()[:-1],
    ],
)
def test_pcm_loader_rejects_empty_unsupported_and_truncated_wav(tmp_path, payload):
    path = tmp_path / "malformed.wav"
    path.write_bytes(payload)
    with pytest.raises(ContractError):
        load_pcm_audio(path)


def test_pcm_loader_rejects_partial_pcm_frame_and_inflated_declared_data(tmp_path):
    valid = wav_payload()
    partial = bytearray(valid[:-1])
    partial[4:8] = struct.pack("<I", len(partial) - 8)
    partial[40:44] = struct.pack("<I", len(partial) - 44)
    inflated = bytearray(valid)
    inflated[40:44] = struct.pack("<I", 0xFFFFFFF0)
    for index, payload in enumerate((partial, inflated)):
        path = tmp_path / f"invalid-{index}.wav"
        path.write_bytes(payload)
        with pytest.raises(ContractError):
            load_pcm_audio(path)


def test_pcm_loader_enforces_file_size_budget_before_reading(audio_path, monkeypatch):
    monkeypatch.setattr(desktop_audio, "MAX_PREVIEW_BYTES", 10)
    with pytest.raises(ContractError, match="limit"):
        load_pcm_audio(audio_path)


class FakeAudioBridge:
    def __init__(self):
        self.duration_seconds = 0.0
        self.position_seconds = 0.0
        self.is_playing = False
        self.accept_play = True
        self.close_calls = 0
        self.loaded = []

    def load(self, payload):
        with wave.open(io.BytesIO(payload), "rb") as reader:
            self.duration_seconds = reader.getnframes() / reader.getframerate()
        self.position_seconds = 0.0
        self.loaded.append(payload)

    def play(self):
        self.is_playing = self.accept_play
        return self.accept_play

    def pause(self):
        self.is_playing = False

    def stop(self):
        self.is_playing = False

    def seek(self, seconds):
        self.position_seconds = seconds

    def close(self):
        self.close_calls += 1
        self.is_playing = False


def test_player_is_inert_then_exposes_native_clock_pause_seek_and_stop():
    bridge = FakeAudioBridge()
    player = NativeAudioPlayer(bridge=bridge)
    assert (player.position_seconds, player.duration_seconds, player.is_playing) == (
        0.0,
        0.0,
        False,
    )
    player.stop()
    player.load(wav_payload((0, 200) * 48_000))
    assert not player.is_playing
    assert player.duration_seconds == 2.0
    player.play()
    bridge.position_seconds = 0.731
    assert player.position_seconds == 0.731
    assert player.is_playing
    player.pause()
    assert not player.is_playing
    assert player.position_seconds == 0.731
    player.seek(1.25)
    player.play()
    assert player.position_seconds == 1.25
    player.stop()
    assert not player.is_playing
    assert player.position_seconds == 0.0


def test_player_does_not_invent_success_when_native_playback_is_interrupted():
    bridge = FakeAudioBridge()
    player = NativeAudioPlayer(bridge=bridge)
    player.load(wav_payload((0,) * PCM_RATE))
    player.play()
    bridge.position_seconds = 0.25
    bridge.is_playing = False
    assert not player.is_playing
    assert player.position_seconds == 0.25


@pytest.mark.parametrize("seconds", [-1.0, math.inf, math.nan, 10.0, True])
def test_player_rejects_seek_outside_loaded_exact_preview(seconds):
    player = NativeAudioPlayer(bridge=FakeAudioBridge())
    player.load(wav_payload())
    with pytest.raises(ContractError, match="seek"):
        player.seek(seconds)


def test_player_errors_are_visible_and_closed_player_cannot_restart():
    bridge = FakeAudioBridge()
    player = NativeAudioPlayer(bridge=bridge)
    for action in (player.play, player.pause, lambda: player.seek(0.0)):
        with pytest.raises(ContractError, match="load"):
            action()
    with pytest.raises(ContractError):
        player.load(b"not audio")
    player.load(wav_payload())
    bridge.accept_play = False
    with pytest.raises(ExternalToolError, match="could not start"):
        player.play()
    bridge.position_seconds = math.nan
    with pytest.raises(ExternalToolError, match="invalid position"):
        _ = player.position_seconds
    player.close()
    player.close()
    assert bridge.close_calls == 1
    assert player.duration_seconds == 0.0
    with pytest.raises(ContractError, match="closed"):
        player.load(wav_payload())


def test_player_rejects_native_duration_disagreeing_with_pcm_frames():
    class WrongDuration(FakeAudioBridge):
        def load(self, payload):
            super().load(payload)
            self.duration_seconds = 9.0

    bridge = WrongDuration()
    player = NativeAudioPlayer(bridge=bridge)
    with pytest.raises(ExternalToolError, match="exact preview frames"):
        player.load(wav_payload())
    assert bridge.close_calls == 1
    assert not player.is_playing


def test_failed_preview_replacement_stops_previous_audio():
    bridge = FakeAudioBridge()
    player = NativeAudioPlayer(bridge=bridge)
    player.load(wav_payload())
    player.play()
    with pytest.raises(ContractError):
        player.load(b"invalid replacement")
    assert not bridge.is_playing
    assert not player.is_playing


class FakeSymbol:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeObjCRuntime:
    """Exercise native selectors, ownership and typed floating-point messaging without macOS."""

    def __init__(self):
        self.selectors = {}
        self.classes = {}
        self.objects = {}
        self.next_id = 100
        self.released = []
        self.messages = []
        self.pcm = b""
        self.position = 0.0
        self.playing = False
        self.fail_class = None
        self.fail_data = False
        self.fail_player = False
        self.fail_pool = False
        self.objc_getClass = FakeSymbol(self.get_class)
        self.sel_registerName = FakeSymbol(self.selector)

    def get_class(self, name):
        if name == self.fail_class:
            return None
        if name not in self.classes:
            self.next_id += 1
            self.classes[name] = self.next_id
        return self.classes[name]

    def selector(self, name):
        if name not in self.selectors:
            self.selectors[name] = len(self.selectors) + 1
        return self.selectors[name]

    def prototype(self, return_type, *argument_types):
        def bind(symbol):
            assert symbol == ("objc_msgSend", self)

            def call(receiver, selector, *args):
                name = next(key for key, value in self.selectors.items() if value == selector)
                self.messages.append((name, return_type, argument_types))
                if name == b"alloc":
                    self.next_id += 1
                    self.objects[self.next_id] = next(
                        key for key, value in self.classes.items() if value == receiver
                    )
                    return self.next_id
                if name == b"init":
                    return None if self.fail_pool else receiver
                if name == b"initWithBytes:length:":
                    self.pcm = args[0][: args[1]]
                    return None if self.fail_data else receiver
                if name == b"initWithData:error:":
                    if self.fail_player:
                        ctypes.cast(args[1], ctypes.POINTER(ctypes.c_void_p)).contents.value = 999
                        return None
                    return receiver
                if name == b"localizedDescription":
                    return 998
                if name == b"UTF8String":
                    return b"synthetic Core Audio refusal"
                if name in {b"release", b"drain"}:
                    self.released.append(receiver)
                elif name == b"play":
                    self.playing = True
                    return True
                elif name in {b"stop", b"pause"}:
                    self.playing = False
                elif name == b"setCurrentTime:":
                    self.position = args[0]
                elif name == b"currentTime":
                    return self.position
                elif name == b"duration":
                    with wave.open(io.BytesIO(self.pcm), "rb") as reader:
                        return reader.getnframes() / reader.getframerate()
                elif name == b"isPlaying":
                    return self.playing
                return None

            return call

        return bind


@pytest.fixture
def fake_objc(monkeypatch):
    runtime = FakeObjCRuntime()
    monkeypatch.setattr(desktop_audio.sys, "platform", "darwin")
    monkeypatch.setattr(desktop_audio.ctypes, "CDLL", lambda _path: runtime)
    monkeypatch.setattr(desktop_audio.ctypes, "CFUNCTYPE", runtime.prototype)
    return runtime


def test_native_bridge_uses_typed_media_clock_and_releases_native_resources(fake_objc):
    player = NativeAudioPlayer()
    player.load(wav_payload((0, 300) * PCM_RATE))
    assert not player.is_playing
    player.seek(1.125)
    assert player.position_seconds == 1.125
    player.play()
    assert player.is_playing
    player.pause()
    assert not player.is_playing
    player.load(wav_payload())
    player.close()
    owned = {
        key for key, value in fake_objc.objects.items() if value in {b"NSData", b"AVAudioPlayer"}
    }
    assert owned <= set(fake_objc.released)
    assert len(fake_objc.released) == len(set(fake_objc.released))
    assert any(
        name == b"currentTime" and result is ctypes.c_double
        for name, result, _ in fake_objc.messages
    )


@pytest.mark.parametrize(
    "failure,match",
    [
        ("class", "class unavailable"),
        ("pool", "autorelease pool"),
        ("data", "preview data"),
        ("player", "synthetic Core Audio refusal"),
    ],
)
def test_native_bridge_reports_allocation_and_player_errors(fake_objc, failure, match):
    if failure == "class":
        fake_objc.fail_class = b"AVAudioPlayer"
    else:
        setattr(fake_objc, f"fail_{failure}", True)
    player = NativeAudioPlayer()
    with pytest.raises(ExternalToolError, match=match):
        player.load(wav_payload())
    assert not player.is_playing


def test_native_player_is_lazy_and_missing_platform_or_framework_fails_visibly(monkeypatch):
    monkeypatch.setattr(desktop_audio.sys, "platform", "linux")
    player = NativeAudioPlayer()
    assert not player.is_playing
    with pytest.raises(ExternalToolError, match="requires macOS"):
        player.load(wav_payload())
    monkeypatch.setattr(desktop_audio.sys, "platform", "darwin")

    def fail_library(_path):
        raise OSError("synthetic missing framework")

    monkeypatch.setattr(desktop_audio.ctypes, "CDLL", fail_library)
    with pytest.raises(ExternalToolError, match="missing framework"):
        player.load(wav_payload())
