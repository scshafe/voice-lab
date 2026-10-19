"""``cole-voice-lab.corpus-take/v2`` and its capture profile ``xvf3800-linear-4`` (D-056): the
contracts alone (the package, its validator and intake: ``test_corpus_take_v2_intake``)."""

from __future__ import annotations

from typing import Any

import pytest
from panel_take_fixtures import (
    PROCESSED,
    manifest,
    profile,
    validated_manifest,
)
from pydantic import ValidationError

from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.capture_profiles import XVF3800_PARAMETERS, Xvf3800Linear4Profile
from cole_voice_lab.corpus_takes import (
    CORPUS_TAKE_SCHEMAS,
    MAX_PROFILE_TAKE_AUDIO_BYTES,
    TAKE_V2_SCHEMA,
    CorpusTakeManifest,
    CorpusTakeManifestV2,
)
from cole_voice_lab.models import AudioSpec


def test_a_v2_manifest_validates_and_is_canonical() -> None:
    record = validated_manifest(manifest())
    assert record.schema_version == TAKE_V2_SCHEMA
    assert record.capture_profile.profile == "xvf3800-linear-4"
    assert record.capture_profile.primary_channel == 3
    assert record.capture_profile.channels[2].mic == 1
    assert CorpusTakeManifestV2.model_validate_json(canonical_json_bytes(record)) == record
    # A v2 manifest is not a v1 one; both are takes of the PC's corpus.
    with pytest.raises(ValidationError):
        CorpusTakeManifest.model_validate_json(canonical_json_bytes(record))
    assert TAKE_V2_SCHEMA in CORPUS_TAKE_SCHEMAS


@pytest.mark.parametrize("alsa_format", ["S16_LE", "S24_3LE", "S32_LE"])
@pytest.mark.parametrize("channels", [2, 6])
def test_every_image_and_width_of_the_profile_is_taken(channels: int, alsa_format: str) -> None:
    record = validated_manifest(manifest(channels=channels, alsa_format=alsa_format))
    assert record.audio.audio.channels == channels
    assert record.audio.audio.sample_rate_hz == 16_000


def test_the_largest_take_fits_its_bound() -> None:
    # 660 s of the 6-channel image in s32le: about 254 MB.
    assert MAX_PROFILE_TAKE_AUDIO_BYTES == 44 + 16_000 * 660 * 6 * 4 == 253_440_044


def _audio(document: dict[str, Any], **spec: Any) -> dict[str, Any]:
    audio = dict(document["audio"])
    audio["audio"] = {**audio["audio"], **spec}
    if "frames" in spec:
        audio["audio"]["duration_seconds"] = spec["frames"] / audio["audio"]["sample_rate_hz"]
    return audio


def test_48_khz_is_refused() -> None:
    # The firmware's 48 kHz image only upsamples its 16 kHz microphones.
    document = manifest()
    frames = document["audio"]["audio"]["frames"]
    audio = _audio(document, sample_rate_hz=48_000, duration_seconds=frames / 48_000)
    with pytest.raises(ValidationError, match="takes 16 kHz audio"):
        validated_manifest({**document, "audio": audio})


def test_the_audio_has_one_channel_per_profile_channel() -> None:
    document = manifest()
    audio = _audio(document, channels=2)
    audio["size_bytes"] = 44 + audio["audio"]["frames"] * 2 * 4
    with pytest.raises(ValidationError, match="one channel per entry of its channels"):
        validated_manifest({**document, "audio": audio})


def test_the_wav_is_a_plain_header_and_its_frames() -> None:
    document = manifest()
    audio = {**document["audio"], "size_bytes": document["audio"]["size_bytes"] + 24}
    with pytest.raises(ValidationError, match="44-byte header"):
        validated_manifest({**document, "audio": audio})


def test_a_take_lasts_at_most_660_seconds() -> None:
    # In the 2-channel image at 16 bits, so the size is within the 6-channel s32le bound.
    document = manifest(channels=2, alsa_format="S16_LE")
    frames = 16_000 * 660 + 1
    audio = _audio(document, frames=frames)
    audio["size_bytes"] = 44 + frames * 2 * 2
    with pytest.raises(ValidationError, match="at most 660 s"):
        validated_manifest({**document, "audio": audio})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"take_id": "take-" + "0" * 64}, "the take id is take-"),
        ({"subject_id": "someone-else"}, "a take is of cole-self"),
        ({"prompt_sha256": "0" * 64}, "canonical digest of the prompt"),
        ({"requested_at": "2026-10-19T12:00:00"}, "explicit timezone"),
        ({"capture_device": {}}, "Extra inputs"),
        ({"schema_version": "cole-voice-lab.corpus-take/v1"}, "corpus-take/v2"),
    ],
)
def test_each_manifest_rule_is_checked(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        validated_manifest(manifest(**changes))


def test_a_physical_test_catalog_is_refused() -> None:
    document = manifest()
    catalog = {**document["catalog"], "purpose": "physical-test"}
    with pytest.raises(ValidationError, match="physical-test catalog"):
        validated_manifest({**document, "catalog": catalog})


def test_the_continuity_summary_is_bounded() -> None:
    document = manifest()
    for changes, message in (
        ({"tolerance_ns": 4_000_001}, "less than or equal to 4000000"),
        ({"size_bytes": 2 * 1024 * 1024 + 1}, "less than or equal to 2097152"),
        ({"started_at": "2026-10-19T12:00:00"}, "explicit timezone"),
    ):
        continuity = {**document["continuity"], **changes}
        with pytest.raises(ValidationError, match=message):
            validated_manifest({**document, "continuity": continuity})


def _channel(**changes: Any) -> dict[str, Any]:
    return {"source": "mic", "mic": 1, "routing": [1, 0], "processing": [], **changes}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"room": "Bath Room"}, "String should match pattern"),
        ({"primary_channel": 7}, "less than or equal to 6"),
        ({"channels": [_channel(), _channel(mic=2), _channel(mic=3)]}, "2- or the 6-channel"),
        ({"channels": [_channel(), _channel()]}, "each raw microphone is on one channel"),
        ({"channels": [_channel(mic=None), _channel(mic=2)]}, "exactly when it is a raw mic"),
        (
            {"channels": [_channel(source="processed"), _channel(mic=2)]},
            "exactly when it is a raw mic",
        ),
        ({"channels": [_channel(processing=["agc"]), _channel(mic=2)]}, "no processing"),
        ({"channels": [_channel(mic=5), _channel(mic=2)]}, "less than or equal to 4"),
        ({"settings": {}}, "record every parameter"),
        ({"settings": {"USB_BIT_DEPTH": [32]}}, "SHF_BYPASS"),
    ],
)
def test_each_profile_rule_is_checked(changes: dict[str, Any], message: str) -> None:
    if "channels" in changes:
        changes = {"primary_channel": 1, **changes}
    with pytest.raises(ValidationError, match=message):
        profile(**changes)


def test_a_processed_channel_names_each_stage_once() -> None:
    twice = {
        "source": "processed",
        "mic": None,
        "routing": [7, 0],
        "processing": [*PROCESSED, "agc"],
    }
    with pytest.raises(ValidationError, match="each processing stage once"):
        profile(channels=[twice, _channel()], primary_channel=1)


def test_the_primary_channel_is_one_of_the_takes() -> None:
    two = [_channel(), _channel(mic=2)]
    assert profile(channels=two, primary_channel=2).primary_channel == 2
    with pytest.raises(ValidationError, match="one of the take's channels"):
        profile(channels=two, primary_channel=3)


def test_the_device_is_a_seeed_usb_device() -> None:
    device = profile().device.model_dump(mode="json")
    with pytest.raises(ValidationError, match="Seeed USB device"):
        profile(device={**device, "usb_vendor_id": 0x20B1})
    firmware = {**device["firmware"], "version": [1, 0, 256]}
    with pytest.raises(ValidationError, match="less than or equal to 255"):
        profile(device={**device, "firmware": firmware})
    assert profile(device={**device, "serial": None}).device.serial is None


def test_the_settings_are_every_parameter_as_read() -> None:
    record = profile()
    assert set(record.settings) == XVF3800_PARAMETERS
    assert record.settings["PP_AGCGAIN"] == (31.6,)
    with pytest.raises(ValidationError):
        profile(settings={**record.settings, "PP_AGCGAIN": []})


def test_the_profile_allows_its_own_formats_only() -> None:
    record = profile()

    def spec(rate: int, channels: int) -> AudioSpec:
        return AudioSpec(
            sample_rate_hz=rate,
            channels=channels,
            sample_width_bytes=2,
            encoding="pcm_s16le",
            frames=rate,
            duration_seconds=1.0,
        )

    assert record.allows(spec(16_000, 6))
    assert not record.allows(spec(48_000, 6))
    assert not record.allows(spec(16_000, 2))
    assert isinstance(record, Xvf3800Linear4Profile)
