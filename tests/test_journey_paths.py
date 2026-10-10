"""paths.test.mjs, translated (design §5.3). The two server cases of that file
(an empty data root starts; /healthz answers without identity) are translated with the
HTTP surface, in tests/test_api_journey.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cole_voice_lab.journey.paths import (
    GENERATED_MANIFESTS,
    INPUT_MANIFESTS,
    assert_bind_allowed,
    is_inside_artifacts,
    packaged_chains,
    resolve_paths,
    resolve_tools,
)


def test_without_a_data_root_the_layout_is_the_historical_local_dev_one() -> None:
    paths = resolve_paths({})
    assert paths.data_root is None
    assert paths.manifest("recording-index.json") == Path("manifests/recording-index.json")
    assert paths.artifact("stt-transcripts") == Path("local-artifacts/stt-transcripts")
    assert str(paths.phone_corpus_root).endswith(
        "Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
    )
    assert paths.chains == packaged_chains()
    assert paths.state_dir == paths.artifacts_dir


def test_a_data_root_owns_corpus_artifacts_manifests_and_staging() -> None:
    paths = resolve_paths({"VOICE_JOURNEY_DATA": "/data"})
    assert paths.phone_corpus_root == Path("/data/corpus/phone")
    assert paths.measurement_dir == Path("/data/corpus/measurement")
    assert paths.manifest("song-journeys.json") == Path("/data/manifests/song-journeys.json")
    assert paths.artifact("voice-features") == Path("/data/artifacts/voice-features")
    assert paths.staging_dir == Path("/data/staging")
    assert paths.models_dir == Path("/data/models")
    assert paths.chains == packaged_chains()
    assert resolve_paths({"VOICE_JOURNEY_DATA": ""}).data_root is None


def test_explicit_env_overrides_win_over_the_data_root() -> None:
    paths = resolve_paths(
        {
            "VOICE_JOURNEY_DATA": "/data",
            "VOICE_JOURNEY_CORPUS_ROOT": "/mnt/phone",
            "VOICE_JOURNEY_CHAINS": "/etc/chains.json",
            "VOICE_JOURNEY_STATE_DIR": "/state",
            "VOICE_JOURNEY_MEASUREMENT_DIR": "/m",
            "VOICE_JOURNEY_STAGING_DIR": "/s",
        }
    )
    assert paths.phone_corpus_root == Path("/mnt/phone")
    assert paths.chains == Path("/etc/chains.json")
    assert paths.state_dir == Path("/state")
    assert paths.measurement_dir == Path("/m")
    assert paths.staging_dir == Path("/s")


def test_tool_paths_come_from_the_environment_and_default_to_path_names() -> None:
    tools = resolve_tools({})
    assert tools.ffmpeg_bin == "ffmpeg"
    assert tools.whisper_bin == "whisper-cli"
    assert "homebrew" not in json.dumps(tools.__dict__).lower()
    configured = resolve_tools(
        {
            "FFMPEG_BIN": "/usr/bin/ffmpeg",
            "WHISPER_CPP_BIN": "/usr/local/bin/whisper-cli",
            "WHISPER_CPP_MODEL": "/m.bin",
            "VOICE_JOURNEY_PYTHON": "/opt/venv/bin/python",
        }
    )
    assert configured.__dict__ == {
        "ffmpeg_bin": "/usr/bin/ffmpeg",
        "whisper_bin": "/usr/local/bin/whisper-cli",
        "whisper_model": "/m.bin",
        "whisper_threads": None,
        "venv_python": "/opt/venv/bin/python",
    }
    with_root = resolve_tools({"VOICE_JOURNEY_DATA": "/data"})
    assert with_root.whisper_model == "/data/models/ggml-medium.bin"
    assert with_root.venv_python == "/data/runtimes/analysis/bin/python"


def test_whisper_threads_unset_or_empty_is_none_a_positive_integer_is_a_number() -> None:
    assert resolve_tools({}).whisper_threads is None
    assert resolve_tools({"WHISPER_CPP_THREADS": ""}).whisper_threads is None
    assert resolve_tools({"WHISPER_CPP_THREADS": "2"}).whisper_threads == 2
    for bad in ["0", "-1", "2.5", "two", "4 ", "0x4", "1e1", "04"]:
        with pytest.raises(ValueError, match="WHISPER_CPP_THREADS must be a positive integer"):
            resolve_tools({"WHISPER_CPP_THREADS": bad})


def test_the_manifest_split_is_explicit_and_disjoint() -> None:
    assert list(INPUT_MANIFESTS) == ["chains.json"]
    for name in INPUT_MANIFESTS:
        assert name not in GENERATED_MANIFESTS


def test_is_inside_artifacts_follows_the_artifacts_dir() -> None:
    assert is_inside_artifacts(Path(os.getcwd()) / "local-artifacts" / "x", {})
    assert not is_inside_artifacts("/tmp/x", {})
    assert is_inside_artifacts("/data/artifacts/stt/a.txt", {"VOICE_JOURNEY_DATA": "/data"})
    assert not is_inside_artifacts("/data/manifests/a.json", {"VOICE_JOURNEY_DATA": "/data"})
    assert is_inside_artifacts("/data/artifacts", {"VOICE_JOURNEY_DATA": "/data"})


def test_loopback_only_binding_outside_the_container_any_address_inside_it() -> None:
    assert_bind_allowed("127.0.0.1", {})
    with pytest.raises(ValueError, match="loopback only"):
        assert_bind_allowed("0.0.0.0", {})
    with pytest.raises(ValueError, match="loopback only"):
        assert_bind_allowed("100.64.0.1", {})
    assert_bind_allowed("0.0.0.0", {"VOICE_JOURNEY_CONTAINER": "1"})


def test_the_packaged_chain_registry_is_public_and_has_no_calibration_path() -> None:
    registry = json.loads(packaged_chains().read_text(encoding="utf-8"))
    assert registry["schemaVersion"] == "voice-journey.chains.v1"
    assert [chain["chainId"] for chain in registry["chains"]] == ["iphone-voicememos", "umik1"]
    assert all("calFile" not in chain for chain in registry["chains"])


def test_the_process_environment_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_JOURNEY_DATA", "/srv/x")
    monkeypatch.setenv("VOICE_JOURNEY_CONTAINER", "1")
    assert resolve_paths().data_root == Path("/srv/x")
    assert_bind_allowed("0.0.0.0")
