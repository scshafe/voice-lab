"""voice-journey PR #1's ``test/chains.test.mjs``, translated (design §5.3), then
``/api/v1/chains``, auth, the gates and the cases that hold ``api/chains.py``'s branches.
Every request carries a bearer token unless a case says otherwise."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import mint
from journey_browser_fixtures import fake_index, fake_results, journey_config, running
from journey_browser_fixtures import write_manifests as write_journey_manifests

from cole_voice_lab.api.chains import (
    ChainRegistryError,
    ChainRegistryView,
    chains_for_client,
    read_chain_registry,
    registry_view,
)
from cole_voice_lab.api.contracts import REFUSAL_CODES
from cole_voice_lab.journey.browser import JourneyConfig
from cole_voice_lab.journey.jsonio import stringify
from cole_voice_lab.journey.paths import packaged_chains, resolve_paths

NODE = shutil.which("node")

REGISTRY: dict[str, Any] = {
    "schemaVersion": "voice-journey.chains.v1",
    "chains": [
        {
            "chainId": "iphone-voicememos",
            "kind": "phone",
            "label": "iPhone",
            "controlled": False,
            "era": {"start": "2019-07-14"},
            "notes": "private note",
        },
        {
            "chainId": "umik1",
            "kind": "measurement",
            "label": "UMIK-1",
            "controlled": True,
            "era": {"start": None},
            "deviceName": "umik",
            "usbId": "2752:0007",
            "sampleRate": 48000,
            "bitDepth": 24,
            "distanceCm": 35,
            "pinnedInputVolume": 70,
            "calFile": "local-artifacts/calibration/umik1-cal.txt",
            "notes": "private note",
        },
    ],
}
PRIVATE = ("private note", "calFile", "distanceCm", "cal.txt", "cal_file", "distance")
CHAIN_PATHS = ("/api/chains", "/api/v1/chains")


def _registry_file(tmp_path: Path, payload: Any = REGISTRY, *, text: str | None = None) -> Path:
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(payload) if text is None else text, encoding="utf-8")
    return path


def _config(tmp_path: Path, chains: Path) -> JourneyConfig:
    data = tmp_path / "data"
    write_journey_manifests(data, index=fake_index(), results=fake_results())
    return journey_config(data, chains=chains)


# ---- chains.test.mjs ------------------------------------------------------------------


def test_chains_for_client_projects_only_the_fields_a_client_needs() -> None:
    chains = chains_for_client(REGISTRY)["chains"]
    assert chains[1] == {
        "id": "umik1",
        "kind": "measurement",
        "label": "UMIK-1",
        "device": {"name": "umik", "usb": "2752:0007"},
        "sampleRate": 48000,
        "bitDepth": 24,
        "eraStart": None,
        "pinnedInputVolume": 70,
    }
    assert chains[0]["device"] is None
    assert chains[0]["eraStart"] == "2019-07-14"
    assert chains[0]["pinnedInputVolume"] is None
    text = stringify(chains)
    assert not any(secret in text for secret in ("private note", "calFile", "distanceCm"))
    assert "cal.txt" not in text


def test_chains_for_client_rejects_a_registry_with_the_wrong_schema() -> None:
    with pytest.raises(ChainRegistryError, match="unexpected schema"):
        chains_for_client({"schemaVersion": "nope"})


def test_the_tracked_registry_declares_the_umik1_for_clients() -> None:
    tracked = json.loads(packaged_chains().read_text(encoding="utf-8"))
    umik = next(c for c in chains_for_client(tracked)["chains"] if c["id"] == "umik1")
    assert umik["device"] == {"name": "umik", "usb": "2752:0007"}
    assert umik["sampleRate"] == 48000
    assert umik["bitDepth"] == 24


def test_get_api_chains_serves_the_registry_503_when_it_is_missing(tmp_path: Path) -> None:
    chains_path = _registry_file(tmp_path)
    with running(tmp_path, _config(tmp_path, chains_path)) as api:
        status, body = api.json("GET", "/api/chains")
        assert status == 200
        assert [chain["id"] for chain in body["chains"]] == ["iphone-voicememos", "umik1"]
        assert body["chains"][1]["device"]["usb"] == "2752:0007"
        assert api.json("POST", "/api/chains")[0] != 200
    with running(tmp_path, _config(tmp_path, tmp_path / "missing.json")) as api:
        status, body = api.json("GET", "/api/chains")
        assert status == 503
        assert body["error"] == "chain_registry_unavailable"


# ---- the alias: the JS's exact answer -------------------------------------------------


def test_the_alias_answers_the_js_bytes(tmp_path: Path) -> None:
    with running(tmp_path, _config(tmp_path, _registry_file(tmp_path))) as api:
        status, headers, body = api.call("GET", "/api/chains")
    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert body.decode("utf-8") == stringify(chains_for_client(REGISTRY)) + "\n"
    assert json.loads(body)["schemaVersion"] == "voice-journey.chains-client.v1"
    assert list(json.loads(body)["chains"][1]) == [
        "id",
        "kind",
        "label",
        "device",
        "sampleRate",
        "bitDepth",
        "eraStart",
        "pinnedInputVolume",
    ]


def test_the_alias_refuses_a_bad_registry_with_500(tmp_path: Path) -> None:
    cases: list[tuple[str, str]] = [
        (json.dumps({"schemaVersion": "nope"}), "chain registry has unexpected schema: nope"),
        ("{not json", "chain registry is not readable JSON"),
        (
            json.dumps({"schemaVersion": "voice-journey.chains.v1", "chains": {}}),
            "chain registry's chains is not an array",
        ),
    ]
    for text, message in cases:
        path = _registry_file(tmp_path, text=text)
        with running(tmp_path, _config(tmp_path, path)) as api:
            assert api.json("GET", "/api/chains") == (500, {"error": message})


# ---- /api/v1/chains -------------------------------------------------------------------


def test_v1_chains_serves_the_strict_envelope(tmp_path: Path) -> None:
    with running(tmp_path, _config(tmp_path, _registry_file(tmp_path))) as api:
        status, body = api.json("GET", "/api/v1/chains")
    assert status == 200
    view = ChainRegistryView.model_validate_json(json.dumps(body))
    assert view.schema_version == "cole-voice-lab.api.chains/v1"
    assert [chain.id for chain in view.chains] == ["iphone-voicememos", "umik1"]
    assert body["chains"][1] == {
        "id": "umik1",
        "kind": "measurement",
        "label": "UMIK-1",
        "device": {"name": "umik", "usb": "2752:0007"},
        "sample_rate": 48000,
        "bit_depth": 24,
        "era_start": None,
        "pinned_input_volume": 70,
    }
    assert body["chains"][0]["device"] is None
    assert not any(secret in json.dumps(body) for secret in PRIVATE)


def test_v1_chains_serves_the_packaged_registry_by_default(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_journey_manifests(data, index=fake_index(), results=fake_results())
    config = journey_config(data)
    assert config.chains == packaged_chains()
    with running(tmp_path, config) as api:
        body = api.get("/api/v1/chains")
    umik = next(chain for chain in body["chains"] if chain["id"] == "umik1")
    assert umik["device"] == {"name": "umik", "usb": "2752:0007"}
    assert (umik["sample_rate"], umik["bit_depth"]) == (48000, 24)
    assert "voice-journey-capture" in umik["label"]
    assert not any(secret in json.dumps(body) for secret in PRIVATE)


def test_v1_chains_refusals(tmp_path: Path) -> None:
    assert {"chain_registry_unavailable", "chain_registry_invalid"} <= REFUSAL_CODES
    with running(tmp_path, _config(tmp_path, tmp_path / "missing.json")) as api:
        status, body = api.json("GET", "/api/v1/chains")
        assert (status, body["error"]) == (503, "chain_registry_unavailable")
        status, body = api.json("POST", "/api/v1/chains")
        assert (status, body["error"]) == (405, "method_not_allowed")
    bad = [
        json.dumps({"schemaVersion": "nope"}),
        "{not json",
        json.dumps({"schemaVersion": "voice-journey.chains.v1", "chains": [{"kind": "phone"}]}),
        json.dumps(
            {
                "schemaVersion": "voice-journey.chains.v1",
                "chains": [{"chainId": "x", "sampleRate": "48000"}],
            }
        ),
    ]
    for text in bad:
        path = _registry_file(tmp_path, text=text)
        with running(tmp_path, _config(tmp_path, path)) as api:
            status, body = api.json("GET", "/api/v1/chains")
            assert (status, body["error"]) == (500, "chain_registry_invalid"), text


def test_the_registry_is_read_on_every_request(tmp_path: Path) -> None:
    path = _registry_file(tmp_path)
    with running(tmp_path, _config(tmp_path, path)) as api:
        assert len(api.get("/api/v1/chains")["chains"]) == 2
        _registry_file(tmp_path, {**REGISTRY, "chains": REGISTRY["chains"][:1]})
        assert len(api.get("/api/v1/chains")["chains"]) == 1
        path.unlink()
        assert api.json("GET", "/api/chains")[0] == 503


# ---- auth and the gate ----------------------------------------------------------------


def test_both_chain_paths_need_a_verified_bearer(tmp_path: Path) -> None:
    expired = mint({"exp": 1})
    stranger = mint({"sub": "someone-else"})
    with running(tmp_path, _config(tmp_path, _registry_file(tmp_path))) as api:
        for path in CHAIN_PATHS:
            for token in (None, expired, stranger, "not.a.jwt"):
                status, payload = api.json("GET", path, token=token)
                assert (status, payload["error"]) == (401, "unauthorized"), (path, token)
            forwarded = {"X-Forwarded-User": "cole", "X-Forwarded-Email": "c@example.invalid"}
            assert api.json("GET", path, headers=forwarded, token=None)[0] == 401


def test_without_journey_the_chain_paths_answer_503(tmp_path: Path) -> None:
    with running(tmp_path, None, enabled=False) as api:
        for path in CHAIN_PATHS:
            status, payload = api.json("GET", path)
            assert (status, payload["error"]) == (503, "journey_unavailable"), path


# ---- the projection's branches --------------------------------------------------------


def test_the_projection_follows_the_js_operators() -> None:
    registry = {
        "schemaVersion": "voice-journey.chains.v1",
        "chains": [
            {"chainId": "usb-only", "usbId": "1234:abcd", "era": None},
            {"chainId": "empty-device", "deviceName": "", "usbId": "", "kind": None},
            {"chainId": "falsy-name", "deviceName": 0, "usbId": "1:2"},
            {"kind": "anonymous"},
            {"chainId": "no-label", "pinnedInputVolume": 0.5, "sampleRate": 44100},
            7,
        ],
    }
    chains = chains_for_client(registry)["chains"]
    assert chains[0]["device"] == {"name": None, "usb": "1234:abcd"}
    assert chains[0]["eraStart"] is None
    assert chains[1]["device"] is None and chains[1]["kind"] is None
    assert chains[2]["device"] == {"name": 0, "usb": "1:2"}
    assert chains[3] == {
        "kind": "anonymous",
        "device": None,
        "sampleRate": None,
        "bitDepth": None,
        "eraStart": None,
        "pinnedInputVolume": None,
    }
    assert chains[4]["label"] == "no-label" and chains[4]["pinnedInputVolume"] == 0.5
    assert list(chains[5]) == list(chains[3]) and chains[5]["kind"] is None
    assert chains_for_client({"schemaVersion": "voice-journey.chains.v1"})["chains"] == []
    assert chains_for_client({"schemaVersion": "voice-journey.chains.v1", "chains": None}) == {
        "schemaVersion": "voice-journey.chains-client.v1",
        "chains": [],
    }


def test_the_projection_refuses_what_the_js_threw_on() -> None:
    for registry, message in [
        (None, "unexpected schema: missing"),
        ([], "unexpected schema: missing"),
        ({"schemaVersion": 2}, "unexpected schema: 2"),
        ({"schemaVersion": None}, "unexpected schema: missing"),
        ({"schemaVersion": "voice-journey.chains.v1", "chains": "x"}, "not an array"),
        ({"schemaVersion": "voice-journey.chains.v1", "chains": [None]}, "a null chain"),
    ]:
        with pytest.raises(ChainRegistryError, match=message):
            chains_for_client(registry)


def test_read_chain_registry_reports_missing_unreadable_and_bad_files(tmp_path: Path) -> None:
    assert read_chain_registry(tmp_path / "missing.json") is None
    assert read_chain_registry(tmp_path / "no-dir" / "chains.json") is None
    with pytest.raises(ChainRegistryError, match="not readable JSON"):
        read_chain_registry(tmp_path)
    with pytest.raises(ChainRegistryError, match="not readable JSON"):
        read_chain_registry(_registry_file(tmp_path, text='{"a": NaN}'))
    assert read_chain_registry(_registry_file(tmp_path)) == chains_for_client(REGISTRY)


def test_registry_view_accepts_only_chains_a_client_can_use() -> None:
    view = registry_view(chains_for_client(REGISTRY))
    assert view.chains[1].pinned_input_volume == 70
    assert isinstance(view.chains[1].pinned_input_volume, int)
    for chain in (
        {"kind": "phone"},
        {"chainId": "x", "bitDepth": 0},
        {"chainId": "x", "deviceName": 3},
        {"chainId": "x", "label": ""},
        {"chainId": "x", "pinnedInputVolume": True},
    ):
        projection = chains_for_client(
            {"schemaVersion": "voice-journey.chains.v1", "chains": [chain]}
        )
        with pytest.raises(ChainRegistryError, match="cannot use"):
            registry_view(projection)


def test_the_config_takes_the_registry_from_the_environment(tmp_path: Path) -> None:
    custom = tmp_path / "chains.json"
    paths = resolve_paths(
        {"VOICE_JOURNEY_DATA": str(tmp_path), "VOICE_JOURNEY_CHAINS": str(custom)}
    )
    assert JourneyConfig.from_paths(paths).chains == custom
    assert JourneyConfig.from_paths(resolve_paths({})).chains == packaged_chains()


def test_the_packaged_registry_has_no_calibration_path_and_no_retired_cli() -> None:
    text = packaged_chains().read_text(encoding="utf-8")
    registry = json.loads(text)
    umik = registry["chains"][1]
    assert (umik["chainId"], umik["usbId"], umik["bitDepth"]) == ("umik1", "2752:0007", 24)
    assert "calFile" not in text and "umik1-cal.txt" not in text
    assert "npm run capture" not in text and "vj-capture" not in text


# ---- parity with PR #1's chains.mjs ---------------------------------------------------

# voice-journey PR #1's src/chains.mjs (3d4ee09): the same code, without the export keyword,
# two lines wrapped at 100 columns.
CHAINS_MJS = """
const CHAINS_SCHEMA_VERSION = "voice-journey.chains.v1";
function chainsForClient(registry) {
  if (registry?.schemaVersion !== CHAINS_SCHEMA_VERSION) {
    throw new Error(
      `chain registry has unexpected schema: ${registry?.schemaVersion ?? "missing"}`);
  }
  return {
    schemaVersion: "voice-journey.chains-client.v1",
    chains: (registry.chains ?? []).map((chain) => ({
      id: chain.chainId,
      kind: chain.kind ?? null,
      label: chain.label ?? chain.chainId,
      device: chain.deviceName || chain.usbId
        ? { name: chain.deviceName ?? null, usb: chain.usbId ?? null } : null,
      sampleRate: chain.sampleRate ?? null,
      bitDepth: chain.bitDepth ?? null,
      eraStart: chain.era?.start ?? null,
      pinnedInputVolume: chain.pinnedInputVolume ?? null,
    })),
  };
}
const inputs = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = inputs.map((registry) => {
  try { return { text: JSON.stringify(chainsForClient(registry), null, 2) }; }
  catch (error) { return { error: error.message }; }
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_projection_matches_chains_mjs_under_node() -> None:
    v1 = "voice-journey.chains.v1"
    inputs: list[Any] = [
        REGISTRY,
        json.loads(packaged_chains().read_text(encoding="utf-8")),
        {"schemaVersion": "nope"},
        {"schemaVersion": 2},
        {},
        None,
        {"schemaVersion": v1},
        {
            "schemaVersion": v1,
            "chains": [
                {"chainId": "a", "usbId": "1:2", "era": None},
                {"chainId": "b", "deviceName": "", "usbId": "", "kind": None, "label": None},
                {"chainId": "c", "deviceName": 0, "usbId": "1:2", "era": {}},
                {"kind": "anonymous"},
                {"chainId": "d", "pinnedInputVolume": 0.5, "bitDepth": 0},
                7,
                "s",
                [],
            ],
        },
    ]
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", CHAINS_MJS],
        input=json.dumps(inputs),
        check=True,
        capture_output=True,
        text=True,
    )
    for registry, node in zip(inputs, json.loads(result.stdout), strict=True):
        if "error" in node:
            with pytest.raises(ChainRegistryError) as caught:
                chains_for_client(registry)
            assert str(caught.value) == node["error"]
        else:
            assert stringify(chains_for_client(registry)) == node["text"]
