"""voice-journey's intake protocol as the API's compatibility path (intake.test.mjs).

The first fourteen tests translate ``legacy/voice-journey/test/intake.test.mjs`` case for
case. Where voice-journey's server differed on purpose (the identity, where a session goes),
the case states the voice-lab behaviour instead. The rest cover the package mapping, a
synthetic end-to-end session through the real publisher, and the refusals.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import shutil
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import SUBJECT, jwks, mint, policy

from cole_voice_lab.api import journey_intake as journey_module
from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.journey_intake import (
    DEFAULT_LIMITS,
    IntakeError,
    IntakeLimits,
    JourneyIntake,
    canonical_json,
    decode_uri_component,
    is_integer,
    require_package_mapping,
    validate_manifest,
)
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.intake import RECEIPT_NAME, IntakeReceipt, validate_capture_package
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.journey import index
from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey.corpus import list_measurement_entries, recording_id
from cole_voice_lab.paths import AssetLayout

REGISTRY: dict[str, Any] = {
    "schemaVersion": "voice-journey.chains.v1",
    "chains": [
        {"chainId": "iphone-voicememos", "kind": "phone", "controlled": False},
        {
            "chainId": "umik1",
            "kind": "measurement",
            "controlled": True,
            "deviceName": "umik",
            "sampleRate": 48000,
        },
        {
            "chainId": "other-mic",
            "kind": "measurement",
            "controlled": True,
            "deviceName": "scarlett",
            "sampleRate": 48000,
        },
    ],
}
SESSION = "s-20261003-101500"
STARTED = "2026-10-03T10:15:00.000Z"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wav(
    seed: int,
    frames: int,
    *,
    rate: int = 48_000,
    width: int = 3,
    channels: int = 1,
    amplitude: float | None = None,
) -> bytes:
    """A tone as the capture client writes it: the 44-byte PCM header, then a RIFF pad
    byte when the data has an odd length (``voice_journey_capture.wav.WavWriter``)."""

    level = amplitude if amplitude is not None else 0.05 + 0.02 * seed
    full_scale = (1 << (8 * width - 1)) - 1
    frequency = 110.0 * (1 + seed % 7)
    data = bytearray()
    for index_ in range(frames):
        value = int(level * full_scale * math.sin(2 * math.pi * frequency * index_ / rate))
        for _ in range(channels):
            data += value.to_bytes(width, "little", signed=True)
    align = channels * width
    header = b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + len(data)),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * align, align, 8 * width),
            b"data",
            struct.pack("<I", len(data)),
        ]
    )
    return header + bytes(data) + (b"\x00" if len(data) % 2 else b"")


ROOM = "01-room-silence.wav"
SONG = "02-anchor-song.wav"
PAYLOADS = {ROOM: wav(1, 1000, amplitude=0.0005), SONG: wav(2, 1667)}


def make_manifest(
    overrides: dict[str, Any] | None = None, payloads: dict[str, bytes] = PAYLOADS
) -> dict[str, Any]:
    files = [
        {"name": name, "sha256": sha(data), "sizeBytes": len(data)}
        for name, data in payloads.items()
    ]
    return {
        "sessionId": SESSION,
        "chainId": "umik1",
        "startedAt": STARTED,
        "endedAt": "2026-10-03T10:25:00.000Z",
        "capture": {
            "sampleRate": 48000,
            "bitDepth": 24,
            "channels": 1,
            "inputVolume": 62,
            "distanceCm": 35,
            "calFileSha256": "a" * 64,
        },
        "segments": [
            {"key": "room-silence", "label": "room silence", "file": ROOM, "seconds": 10},
            {"key": "anchor-song", "label": "anchor song", "file": SONG},
        ],
        "files": files,
        **(overrides or {}),
    }


@dataclass
class Rig:
    layout: AssetLayout
    server: ApiServer
    intake: JourneyIntake
    logs: list[str]
    clock: list[float]

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def raw(self) -> Path:
        return self.layout.directory("raw")

    @property
    def incoming(self) -> Path:
        return self.layout.directory("incoming")

    def publish(self) -> None:
        publish_pending(self.layout, min_free_bytes=0)

    def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        token: str | None = "default",
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        sent: dict[str, str] = {}
        if json_body is not None:
            body = json.dumps(json_body).encode()
            sent["Content-Type"] = "application/json"
        if token is not None:
            sent["Authorization"] = f"Bearer {mint() if token == 'default' else token}"
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, (json.loads(data) if data else {})

    def upload_all(self, session_id: str = SESSION, payloads: dict[str, bytes] = PAYLOADS) -> None:
        for name, data in payloads.items():
            status, answer = self.call(
                "PUT", f"/api/intake/sessions/{session_id}/files/{name}", body=data
            )
            assert status == 200, answer

    def raw_packages(self) -> list[Path]:
        return sorted(self.raw.iterdir())


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    servers: list[ApiServer] = []

    def make(
        *,
        limits: IntakeLimits = DEFAULT_LIMITS,
        wait: float = 30.0,
        registry: dict[str, Any] | None = REGISTRY,
        name: str = "lab",
    ) -> Rig:
        base = tmp_path / name
        layout = AssetLayout.initialize(base / "voice-lab" / "lab")
        jwks_path = base / "jwks.json"
        jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
        chains_path = None
        if registry is not None:
            chains_path = base / "chains.json"
            chains_path.write_text(json.dumps(registry), encoding="utf-8")
        verifier = TokenVerifier(policy(jwks_path))
        logs: list[str] = []
        clock = [float(js.now_ms())]
        intake = JourneyIntake(
            layout,
            verifier=verifier,
            chains_path=chains_path,
            limits=limits,
            log=logs.append,
            now=lambda: clock[0],
            sleep=lambda _seconds: publish_pending(layout, min_free_bytes=0),
            publish_wait_seconds=wait,
        )
        server = ApiServer(
            ("127.0.0.1", 0),
            staging=IntakeStaging(layout, min_free_bytes=0),
            verifier=verifier,
            meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
            log=False,
            journey_intake=intake,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return Rig(layout, server, intake, logs, clock)

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def rig(make_rig: Callable[..., Rig]) -> Rig:
    return make_rig()


def reject(overrides: dict[str, Any], code: str, registry: Any = REGISTRY) -> None:
    with pytest.raises(IntakeError) as caught:
        validate_manifest(make_manifest(overrides), registry)
    assert caught.value.code == code


def refuse_mapping(overrides: dict[str, Any], code: str, registry: Any = REGISTRY) -> None:
    manifest = validate_manifest(make_manifest(overrides), registry)
    with pytest.raises(IntakeError) as caught:
        require_package_mapping(manifest, registry)
    assert caught.value.code == code


# ---- intake.test.mjs, case for case -----------------------------------------------------


def test_a_bearer_token_is_the_identity_and_door_headers_alone_are_not(rig: Rig) -> None:
    door = {"X-Forwarded-User": "cole", "X-Forwarded-Email": "cole@example.test"}
    status, answer = rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    assert status == 201, answer
    raw_bearer = rig.call(
        "GET", f"/api/intake/sessions/{SESSION}", token="not-validated-by-the-app"
    )
    assert raw_bearer == (401, {"error": "identity_required"})
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}", headers=door, token=None)[0] == 401


def test_intake_refuses_without_a_verified_identity(rig: Rig) -> None:
    denied = rig.call("POST", "/api/intake/sessions", json_body=make_manifest(), token=None)
    assert denied == (401, {"error": "identity_required"})
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}", token=None)[0] == 401
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}", token="")[0] == 401
    other = mint({"sub": "someone-else"})
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}", token=other)[0] == 401
    expired = mint({"exp": 1, "iat": 0})
    assert rig.call("GET", "/api/intake/other", token=expired)[0] == 401


def test_a_good_session_is_accepted_atomically_into_raw(rig: Rig) -> None:
    manifest = make_manifest()
    status, opened = rig.call("POST", "/api/intake/sessions", json_body=manifest)
    assert status == 201
    assert opened["state"] == "open"
    assert [file["receivedBytes"] for file in opened["files"]] == [0, 0]

    rig.upload_all()
    # Nothing reaches incoming/ (the publisher) or raw/ before finalize.
    assert sorted(path.name for path in rig.incoming.iterdir()) == [".journey-uploads", ".uploads"]
    assert rig.raw_packages() == []
    assert list_measurement_entries(rig.raw) == []

    status, done = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert status == 200, done
    total = sum(len(data) for data in PAYLOADS.values())
    assert done == {"sessionId": SESSION, "state": "accepted", "files": 2, "bytes": total}

    [package] = rig.raw_packages()
    members = sorted(
        path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()
    )
    assert members == [
        "READY",
        "anchor-song/02-anchor-song.wav",
        "intake-qc.json",
        "intake-receipt.json",
        "manifest.json",
        "qc.json",
        "room-silence/01-room-silence.wav",
    ]
    assert (package / "anchor-song" / SONG).read_bytes() == PAYLOADS[SONG]
    validated = validate_capture_package(package, allow_intake_receipt=True)
    assert validated.manifest.session_id == f"vj-{SESSION}"
    assert validated.manifest.capture_device.device_id == "voice-journey-chain:umik1"
    assert validated.manifest.capture_device.calibration_sha256 == "a" * 64
    assert [entry.kind for entry in validated.manifest.files] == ["room-tone", "calibration"]
    receipt = IntakeReceipt.model_validate_json((package / RECEIPT_NAME).read_bytes())
    assert receipt.schema_version == "cole-voice-lab.intake-receipt/v2"
    assert receipt.raw_path == f"raw/{package.name}"

    record = json.loads(
        (rig.intake.staging_dir / SESSION / "session.json").read_text(encoding="utf-8")
    )
    assert record["chainId"] == "umik1"
    assert record["capture"]["distanceCm"] == 35
    assert record["capture"]["calFileSha256"] == "a" * 64
    assert record["intake"]["finalizedBy"] == SUBJECT
    assert record["intake"]["package"] == {
        "sessionId": f"vj-{SESSION}",
        "manifestSha256": package.name,
    }
    # The publisher took the package; staging keeps only the session's record.
    assert not (rig.incoming / f"vj-{SESSION}").exists()
    assert sorted(path.name for path in (rig.intake.staging_dir / SESSION).iterdir()) == [
        "manifest.json",
        "session.json",
    ]

    # Re-finalize and re-open are idempotent.
    again = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert again == (200, {"sessionId": SESSION, "state": "accepted"})
    reopened = rig.call("POST", "/api/intake/sessions", json_body=manifest)
    assert reopened == (200, {"sessionId": SESSION, "state": "accepted"})
    changed = make_manifest({"capture": {**manifest["capture"], "distanceCm": 50}})
    conflict = rig.call("POST", "/api/intake/sessions", json_body=changed)
    assert conflict == (409, {"error": "session_already_accepted"})

    # Logs carry ids and counts only.
    assert rig.logs
    for line in rig.logs:
        assert not any(word in line for word in ("silence", "song", "cole"))


def test_a_bad_checksum_on_upload_rejects_the_file_and_it_never_finalizes(rig: Rig) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    corrupt = bytearray(PAYLOADS[ROOM])
    corrupt[10] ^= 0xFF
    url = f"/api/intake/sessions/{SESSION}/files/{ROOM}"
    bad = rig.call("PUT", url, body=bytes(corrupt))
    assert bad == (422, {"error": "checksum_mismatch", "name": ROOM})
    rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/{SONG}", body=PAYLOADS[SONG])

    early = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert early == (409, {"error": "incomplete", "missing": [ROOM]})
    assert not (rig.incoming / f"vj-{SESSION}").exists()
    assert rig.raw_packages() == []

    # The right bytes then complete it.
    assert rig.call("PUT", url, body=PAYLOADS[ROOM])[0] == 200
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")[0] == 200


def test_finalize_re_verifies_bytes_on_disk_and_publishes_nothing_when_one_changed(
    rig: Rig,
) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    rig.upload_all()
    staged = rig.intake.staging_dir / SESSION / "files" / SONG
    staged.write_bytes(wav(9, 1667))  # same size, different bytes
    result = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert result == (422, {"error": "checksum_mismatch", "files": [SONG]})
    assert not (rig.incoming / f"vj-{SESSION}").exists()
    assert rig.raw_packages() == []
    # The client re-sends only the dropped file and the session completes.
    _, status = rig.call("GET", f"/api/intake/sessions/{SESSION}")
    assert [file["complete"] for file in status["files"]] == [True, False]
    rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/{SONG}", body=PAYLOADS[SONG])
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")[0] == 200


def test_re_uploading_a_received_file_is_idempotent_and_a_size_mismatch_is_refused(
    rig: Rig,
) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    url = f"/api/intake/sessions/{SESSION}/files/{ROOM}"
    assert rig.call("PUT", url, body=PAYLOADS[ROOM])[1]["state"] == "received"
    again = rig.call("PUT", url, body=PAYLOADS[ROOM])
    assert again == (200, {"name": ROOM, "state": "already_received", "sizeBytes": 3044})
    song = f"/api/intake/sessions/{SESSION}/files/{SONG}"
    short = rig.call("PUT", song, body=wav(2, 30))
    assert short == (422, {"error": "size_mismatch", "sizeBytes": len(PAYLOADS[SONG])})
    long = rig.call("PUT", song, body=wav(2, 3000))
    assert long[0] == 413
    assert long[1]["error"] == "file_too_large"
    unlisted = rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/99-extra.wav", body=b"x")
    assert unlisted == (404, {"error": "file_not_in_manifest"})


def test_uploads_resume_with_content_range_from_the_last_byte_the_server_holds(
    rig: Rig,
) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    url = f"/api/intake/sessions/{SESSION}/files/{SONG}"
    payload = PAYLOADS[SONG]
    size = len(payload)
    first = rig.call(
        "PUT", url, body=payload[:2000], headers={"Content-Range": f"bytes 0-1999/{size}"}
    )
    assert first == (202, {"name": SONG, "state": "partial", "receivedBytes": 2000})
    _, status = rig.call("GET", f"/api/intake/sessions/{SESSION}")
    assert status["files"][1]["receivedBytes"] == 2000
    gap = rig.call(
        "PUT",
        url,
        body=payload[3000:],
        headers={"Content-Range": f"bytes 3000-{size - 1}/{size}"},
    )
    assert gap == (409, {"error": "range_not_contiguous", "receivedBytes": 2000})
    last = rig.call(
        "PUT",
        url,
        body=payload[2000:],
        headers={"Content-Range": f"bytes 2000-{size - 1}/{size}"},
    )
    assert last == (200, {"name": SONG, "state": "received", "sizeBytes": size})


def test_a_resumed_upload_whose_bytes_do_not_add_up_to_the_checksum_is_dropped(
    rig: Rig,
) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    url = f"/api/intake/sessions/{SESSION}/files/{SONG}"
    size = len(PAYLOADS[SONG])
    rig.call(
        "PUT", url, body=wav(7, 1667)[:2000], headers={"Content-Range": f"bytes 0-1999/{size}"}
    )
    last = rig.call(
        "PUT",
        url,
        body=PAYLOADS[SONG][2000:],
        headers={"Content-Range": f"bytes 2000-{size - 1}/{size}"},
    )
    assert last[0] == 422
    _, status = rig.call("GET", f"/api/intake/sessions/{SESSION}")
    assert status["files"][1]["receivedBytes"] == 0


def test_manifests_are_validated_against_the_chain_registry_and_limits() -> None:
    validate_manifest(make_manifest(), REGISTRY)
    reject({"chainId": "nope"}, "unknown_chain")
    reject({"chainId": "iphone-voicememos"}, "chain_not_accepted")
    reject({"chainId": "other-mic"}, "chain_not_accepted")
    reject({"sessionId": "../etc"}, "invalid_session_id")
    base_capture = {"sampleRate": 48000, "inputVolume": 62, "distanceCm": 35}
    reject(
        {"capture": {**base_capture, "calFileSha256": None}, "startedAt": "yesterday"},
        "invalid_timestamp",
    )
    reject(
        {"capture": {**base_capture, "sampleRate": 44100, "calFileSha256": None}},
        "sample_rate_mismatch",
    )
    reject(
        {"capture": {"sampleRate": 48000, "distanceCm": 35, "calFileSha256": None}},
        "input_level_required",
    )
    reject(
        {"capture": {"sampleRate": 48000, "inputVolume": 62, "calFileSha256": None}},
        "distance_required",
    )
    reject({"capture": {**base_capture, "calFileSha256": "xyz"}}, "invalid_cal_file_hash")
    reject({"segments": []}, "segment_map_required")
    reject({"segments": [{"key": "x", "file": "03-missing.wav"}]}, "segment_file_not_listed")
    reject({"files": []}, "invalid_file_list")
    base = make_manifest()["files"][0]
    reject({"files": [{**base, "name": "../evil.wav"}]}, "invalid_file_name")
    reject({"files": [{**base, "name": "session.json"}]}, "invalid_file_name")
    reject({"files": [{**base, "name": "a.m4a"}]}, "invalid_file_name")
    reject({"files": [base, base]}, "duplicate_file_name")
    reject({"files": [{**base, "sha256": "ABC"}]}, "invalid_file_sha256")
    reject({"files": [{**base, "sizeBytes": 0}]}, "invalid_file_size")
    with pytest.raises(IntakeError, match="file_too_large"):
        validate_manifest(make_manifest(), REGISTRY, replace(DEFAULT_LIMITS, max_file_bytes=100))
    with pytest.raises(IntakeError, match="session_too_large"):
        validate_manifest(
            make_manifest(),
            REGISTRY,
            replace(DEFAULT_LIMITS, max_file_bytes=6000, max_session_bytes=6000),
        )


def test_http_surface_bad_ids_names_methods_conflicts_and_the_open_session_cap(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    phone = make_manifest({"chainId": "iphone-voicememos"})
    assert rig.call("POST", "/api/intake/sessions", json_body=phone)[0] == 422
    short_id = make_manifest({"sessionId": "x"})
    assert rig.call("POST", "/api/intake/sessions", json_body=short_id)[0] == 422
    assert rig.call("GET", "/api/intake/sessions/s-20261003-999999")[0] == 404
    bad_name = rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/..%2Fx.wav", body=b"x")
    assert bad_name == (400, {"error": "invalid_file_name"})
    assert rig.call("DELETE", f"/api/intake/sessions/{SESSION}")[0] == 405
    assert rig.call("GET", "/api/intake/other") == (404, {"error": "not_found"})
    manifest = make_manifest()
    assert rig.call("POST", "/api/intake/sessions", json_body=manifest)[0] == 201
    again = rig.call("POST", "/api/intake/sessions", json_body=manifest)
    assert again[0] == 200
    assert again[1]["state"] == "open"
    later = make_manifest({"endedAt": "2026-10-03T11:00:00.000Z"})
    conflict = rig.call("POST", "/api/intake/sessions", json_body=later)
    assert conflict == (409, {"error": "session_manifest_conflict"})

    capped = make_rig(limits=replace(DEFAULT_LIMITS, max_open_sessions=2), name="capped")
    for session_id in ("s-20261003-000001", "s-20261003-000002"):
        opened = capped.call(
            "POST", "/api/intake/sessions", json_body=make_manifest({"sessionId": session_id})
        )
        assert opened[0] == 201
    third = make_manifest({"sessionId": "s-20261003-000003"})
    refused = capped.call("POST", "/api/intake/sessions", json_body=third)
    assert refused == (429, {"error": "too_many_open_sessions"})


def test_the_manifest_body_is_size_limited(rig: Rig) -> None:
    huge = make_manifest({"notes": "x" * (300 * 1024)})
    assert rig.call("POST", "/api/intake/sessions", json_body=huge) == (
        413,
        {"error": "manifest_too_large"},
    )


def test_stale_staging_sessions_are_purged_not_accepted(rig: Rig) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    assert len(list(rig.intake.staging_dir.iterdir())) == 1
    rig.clock[0] += 8 * 24 * 60 * 60 * 1000
    assert rig.intake.purge_stale() == 0
    assert list(rig.intake.staging_dir.iterdir()) == []


def test_an_accepted_session_is_indexed_as_a_measurement_take(
    rig: Rig, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    rig.upload_all()
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")[0] == 200

    entries = list_measurement_entries(rig.raw)
    assert len(entries) == 2
    assert entries[0]["filename"] == f"{SESSION}/{ROOM}"
    assert entries[0]["source"] == {
        "type": "measurement",
        "chainId": "umik1",
        "sessionId": SESSION,
        "segmentKey": "room-silence",
    }
    assert entries[0]["capturedAt"] == STARTED
    assert entries[0]["recordingId"] == recording_id(f"measurement/{SESSION}/{ROOM}")
    [package] = rig.raw_packages()
    assert entries[1]["hostPath"] == str(package / "anchor-song" / SONG)

    # The same session as voice-journey kept it lists the same takes (parity of the sources).
    legacy = tmp_path / "corpus" / "measurement" / SESSION
    legacy.mkdir(parents=True)
    shutil.copy(rig.intake.staging_dir / SESSION / "session.json", legacy / "session.json")
    for name, data in PAYLOADS.items():
        (legacy / name).write_bytes(data)
    keys = ("recordingId", "filename", "capturedAt", "sizeBytes", "source")
    old = list_measurement_entries(legacy.parent)
    assert [{key: entry[key] for key in keys} for entry in old] == [
        {key: entry[key] for key in keys} for entry in entries
    ]

    # The index command lists phone and measurement takes together.
    phone = tmp_path / "phone"
    phone.mkdir()
    (phone / "20190301 090000-0A1B2C3D.m4a").write_text("x")
    out = tmp_path / "manifests" / "recording-index.json"
    argv = ["--corpus-root", str(phone), "--measurement-root", str(rig.raw), "--out", str(out)]
    assert index.main([*argv, "--generated-at", "2026-10-03T00:00:00.000Z"]) == 0
    capsys.readouterr()
    manifest = json.loads(out.read_text())
    kinds = sorted(row.get("source", {}).get("type", "phone") for row in manifest["recordings"])
    assert kinds == ["measurement", "measurement", "phone"]
    assert manifest["totals"]["recordings"] == 3


def test_index_rows_built_from_a_measurement_entry_carry_their_source() -> None:
    entry = {
        "recordingId": "r1",
        "filename": "s-1/01-x.wav",
        "capturedAt": "2026-10-03T10:15:00.000Z",
        "sizeBytes": 5,
        "modifiedAt": "2026-10-03T10:30:00.000Z",
        "source": {
            "type": "measurement",
            "chainId": "umik1",
            "sessionId": "s-1",
            "segmentKey": "x",
        },
    }
    manifest = index.build_manifest([entry], {"generatedAt": "2026-10-03T00:00:00.000Z"})
    assert manifest["recordings"][0]["source"]["type"] == "measurement"


# ---- end to end: a synthetic UMIK-1 session, as the capture client sends it ---------------


RITUAL = (
    ("room-silence", "timed", 0.4, 0.0004),
    ("cal-tone", "timed", 0.3, 0.3),
    ("vowel-soft", "manual", 0.3, 0.02),
    ("vowel-loud", "manual", 0.3, 0.4),
    ("glide", "manual", 0.3, 0.2),
    ("anchor-song", "manual", 0.5, 0.25),
    ("free-practice", "manual", 0.3, 0.1),
)


def client_session(directory: Path) -> tuple[bytes, dict[str, bytes]]:
    """A completed session written the way ``voice_journey_capture.ritual`` writes it:
    ``NN-<key>.wav`` per spine segment and ``session.json`` (``package.encode``)."""

    payloads: dict[str, bytes] = {}
    segments = []
    for number, (key, mode, seconds, level) in enumerate(RITUAL, start=1):
        name = f"{number:02d}-{key}.wav"
        frames = int(seconds * 48_000) + number  # odd and even data lengths alike
        payloads[name] = wav(number, frames, amplitude=level)
        segments.append(
            {
                "key": key,
                "file": name,
                "label": key.replace("-", " "),
                "mode": mode,
                "startedAt": "2026-10-09T08:00:01.000Z",
                "sizeBytes": len(payloads[name]),
                "frames": frames,
                "durationSeconds": round(frames / 48_000, 3),
                "peakDbfs": -12.0,
                "rmsDbfs": -30.0,
                "clippedSamples": 0,
                "inputOverflows": 0,
            }
        )
    manifest = {
        "schemaVersion": "voice-journey.capture-session.v1",
        "sessionId": "s-20261009-080000",
        "chainId": "umik1",
        "chainKind": "measurement",
        "controlled": True,
        "startedAt": "2026-10-09T08:00:00.000Z",
        "endedAt": "2026-10-09T08:04:00.000Z",
        "status": "completed",
        "capture": {
            "sampleRate": 48000,
            "bitDepth": 24,
            "channels": 1,
            "inputVolume": 62,
            "distanceCm": 35,
            "calFile": "7000000.txt",
            "calFileSha256": "b" * 64,
            "calSerial": "7000000",
            "inputLevel": {"muted": False},
            "levelPinnedAt": "2026-10-01T00:00:00.000Z",
            "device": {"name": "Umik-1  Gain: 18dB", "hostApi": "ALSA", "usbId": "2752:0007"},
            "client": {"name": "voice-journey-capture", "version": "0.1.0", "platform": "linux"},
        },
        "segments": segments,
        "files": [
            {"name": name, "sha256": sha(data), "sizeBytes": len(data)}
            for name, data in payloads.items()
        ],
    }
    directory.mkdir(parents=True)
    body = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    (directory / "session.json").write_bytes(body)
    return body, payloads


def test_a_synthetic_umik1_session_lands_in_raw_through_the_compatibility_path(
    make_rig: Callable[..., Rig], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(registry=None, wait=0.0)  # the packaged chain registry
    body, payloads = client_session(tmp_path / "client" / "s-20261009-080000")
    session_id = "s-20261009-080000"
    base = f"/api/intake/sessions/{session_id}"
    status, opened = rig.call("POST", "/api/intake/sessions", body=body)
    assert status == 201, opened
    assert opened["limits"] == {"maxFileBytes": 1024**3}
    chunk = 16 * 1024

    def send(name: str, offset: int, budget: int | None = None) -> int:
        data, puts = payloads[name], 0
        while offset < len(data) and (budget is None or puts < budget):
            piece = data[offset : offset + chunk]
            end = offset + len(piece) - 1
            code, answer = rig.call(
                "PUT",
                f"{base}/files/{name}",
                body=piece,
                headers={"Content-Range": f"bytes {offset}-{end}/{len(data)}"},
            )
            puts += 1
            if code == 202:
                offset = answer["receivedBytes"]
            elif code == 409 and answer["error"] == "range_not_contiguous":
                offset = answer["receivedBytes"]
            else:
                assert (code, answer["state"]) == (200, "received"), answer
                offset = len(data)
        return offset

    # The connection drops after a few chunks of the first files ...
    names = list(payloads)
    assert send(names[0], 0) == len(payloads[names[0]])
    assert 0 < send(names[1], 0, budget=1) < len(payloads[names[1]])
    # ... the client reopens with the same bytes and resumes from what the server holds.
    status, resumed = rig.call("POST", "/api/intake/sessions", body=body)
    assert status == 200 and resumed["state"] == "open"
    held = {item["name"]: item for item in resumed["files"]}
    assert held[names[0]]["complete"] and held[names[1]]["receivedBytes"] == chunk
    # A stale offset is answered with where to continue.
    assert send(names[1], 0, budget=1) == chunk
    for name in names[1:]:
        assert send(name, held[name]["receivedBytes"]) == len(payloads[name])

    # Finalize hands the package to the publisher; until it runs, the session waits.
    pending = rig.call("POST", f"{base}/finalize")
    assert pending == (
        503,
        {"error": "publication_pending", "sessionId": session_id, "state": "finalized"},
    )
    assert rig.call("GET", base) == (200, {"sessionId": session_id, "state": "finalized"})
    package = rig.incoming / f"vj-{session_id}"
    staged_manifest = validate_capture_package(package).manifest

    # The PC's publisher unit: `voice-lab intake publish` (its disk floor met).
    from cole_voice_lab import cli

    disk = shutil.disk_usage(rig.layout.root)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: disk._replace(free=1 << 50))
    assert cli.main(["intake", "publish", "--asset-root", str(rig.layout.root)]) == 0
    monkeypatch.undo()

    accepted = rig.call("POST", f"{base}/finalize")
    assert accepted == (200, {"sessionId": session_id, "state": "accepted"})
    assert rig.call("GET", base) == (200, {"sessionId": session_id, "state": "accepted"})
    [raw] = rig.raw_packages()
    validated = validate_capture_package(raw, allow_intake_receipt=True)
    assert validated.manifest == staged_manifest
    manifest = validated.manifest
    assert manifest.prompt_suite_id == "voice-journey-measurement"
    assert manifest.subject_id == "cole-self"
    assert manifest.capture_device.microphone_serial == "7000000"
    assert [entry.path for entry in manifest.files] == [
        f"{key}/{number:02d}-{key}.wav" for number, (key, *_) in enumerate(RITUAL, start=1)
    ]
    assert [entry.kind for entry in manifest.files] == ["room-tone"] + ["calibration"] * 6
    receipt = IntakeReceipt.model_validate_json((raw / RECEIPT_NAME).read_bytes())
    assert receipt.session_id == f"vj-{session_id}"
    acceptance = rig.layout.directory("provenance") / "intake" / "sessions"
    assert (acceptance / f"vj-{session_id}.json").exists()
    entries = list_measurement_entries(rig.raw)
    assert [entry["source"]["segmentKey"] for entry in entries] == [key for key, *_ in RITUAL]
    assert {entry["capturedAt"] for entry in entries} == {"2026-10-09T08:00:00.000Z"}


# ---- the package mapping's refusals -------------------------------------------------------


def test_open_refuses_what_a_capture_package_cannot_hold() -> None:
    refuse_mapping({"sessionId": "S-20261003-101500"}, "invalid_session_id")
    refuse_mapping({"sessionId": "s-20261003-10150_"}, "invalid_session_id")
    umik2 = {
        "chains": [
            {"chainId": "umik2", "kind": "measurement", "deviceName": "umik", "label": "UMIK-2"}
        ]
    }
    refuse_mapping({"chainId": "umik2"}, "chain_not_accepted", umik2)
    capture = make_manifest()["capture"]
    refuse_mapping({"capture": {**capture, "bitDepth": 16}}, "format_not_accepted")
    refuse_mapping({"capture": {**capture, "channels": 2}}, "format_not_accepted")
    slow = {"chains": [{**REGISTRY["chains"][1], "sampleRate": 44100}]}
    refuse_mapping({"capture": {**capture, "sampleRate": 44100}}, "format_not_accepted", slow)
    refuse_mapping({"endedAt": "2026-10-03T10:00:00.000Z"}, "invalid_timestamp")
    refuse_mapping({"endedAt": "+010000-01-01T00:00:00.000Z"}, "invalid_timestamp")
    keyed = make_manifest()["segments"]
    refuse_mapping(
        {"segments": [{**keyed[0], "key": "Room Silence"}, keyed[1]]}, "invalid_segment_key"
    )
    refuse_mapping({"segments": [{**keyed[0], "key": "silence"}, keyed[1]]}, "room_tone_required")
    # A file no segment names sits at the package root; the last segment naming a file wins.
    layout = journey_module.package_layout(
        validate_manifest(
            make_manifest(
                {
                    "segments": [
                        {"key": "anchor-song", "file": ROOM},
                        {"key": "room-silence", "file": ROOM},
                    ]
                }
            ),
            REGISTRY,
        )
    )
    assert layout == [
        (ROOM, f"room-silence/{ROOM}", "room-tone"),
        (SONG, SONG, "calibration"),
    ]


def test_the_refusals_reach_the_client_in_voice_journeys_shape(rig: Rig) -> None:
    keyed = make_manifest()["segments"]
    quiet = make_manifest({"segments": [{**keyed[0], "key": "silence"}, keyed[1]]})
    refused = rig.call("POST", "/api/intake/sessions", json_body=quiet)
    assert refused == (422, {"error": "room_tone_required", "segment": "room-silence"})
    assert rig.call("POST", "/api/intake/sessions", body=b"{") == (400, {"error": "invalid_json"})
    assert rig.call("POST", "/api/intake/sessions")[1] == {"error": "invalid_json"}
    assert rig.call("GET", "/api/intake/sessions")[0] == 405
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}/finalize")[0] == 405
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/files/{ROOM}")[0] == 405
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}/other")[0] == 404
    assert rig.call("GET", "/api/intake/sessions/x") == (400, {"error": "invalid_session_id"})
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")[0] == 404
    assert rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/{ROOM}", body=b"x")[0] == 404
    # decodeURIComponent throws on a malformed escape: voice-journey's 500.
    assert rig.call("GET", "/api/intake/sessions/s-2026%zz") == (500, {"error": "internal_error"})
    assert rig.logs[-1] == "internal error: ValueError"
    connection = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=10)
    connection.request(
        "HEAD", "/api/intake/sessions", headers={"Authorization": f"Bearer {mint()}"}
    )
    response = connection.getresponse()
    assert (response.status, response.read()) == (405, b"")
    connection.close()


def test_puts_refuse_bad_lengths_and_ranges(rig: Rig) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    url = f"/api/intake/sessions/{SESSION}/files/{SONG}"
    size = len(PAYLOADS[SONG])
    assert rig.call("PUT", url, body=b"")[1] == {"error": "content_length_required"}
    no_range = rig.call("PUT", url, body=b"abc", headers={"Content-Range": "bytes=0-2"})
    assert no_range == (400, {"error": "invalid_content_range"})
    wrong_total = rig.call("PUT", url, body=b"abc", headers={"Content-Range": "bytes 0-2/9"})
    assert wrong_total == (416, {"error": "content_range_out_of_bounds", "sizeBytes": size})
    mismatch = rig.call("PUT", url, body=b"abc", headers={"Content-Range": f"bytes 0-9/{size}"})
    assert mismatch == (400, {"error": "content_length_range_mismatch"})

    def short_body(content_range: str | None, sent: bytes, length: int) -> dict[str, Any]:
        with socket.create_connection(("127.0.0.1", rig.port), timeout=10) as raw:
            lines = [
                f"PUT {url} HTTP/1.1",
                "Host: 127.0.0.1",
                f"Authorization: Bearer {mint()}",
                f"Content-Length: {length}",
            ]
            if content_range is not None:
                lines.append(f"Content-Range: {content_range}")
            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + sent)
            raw.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := raw.recv(65536):
                reply += chunk
        head, _, payload = reply.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.0 400") or head.startswith(b"HTTP/1.1 400"), head
        return dict(json.loads(payload))

    # The connection ends before Content-Length: a first chunk leaves nothing behind ...
    first = short_body(f"bytes 0-1999/{size}", PAYLOADS[SONG][:100], 2000)
    assert first == {"error": "incomplete_body", "receivedBytes": 0}
    whole = short_body(None, PAYLOADS[SONG][:100], size)
    assert whole == {"error": "incomplete_body", "receivedBytes": 0}
    # ... a later one keeps what arrived, so the client resumes from there.
    rig.call(
        "PUT", url, body=PAYLOADS[SONG][:1000], headers={"Content-Range": f"bytes 0-999/{size}"}
    )
    later = short_body(f"bytes 1000-1999/{size}", PAYLOADS[SONG][1000:1100], 1000)
    assert later == {"error": "incomplete_body", "receivedBytes": 1100}


def test_finalize_refuses_audio_the_lab_cannot_keep(rig: Rig) -> None:
    payloads = {ROOM: wav(1, 1000, rate=44_100, amplitude=0.0005), SONG: wav(2, 1667, width=2)}
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest(payloads=payloads))
    rig.upload_all(payloads=payloads)
    refused = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert refused == (
        422,
        {"error": "audio_not_accepted", "files": [ROOM, SONG], "expected": "mono-48000-pcm-s24le"},
    )
    assert not (rig.incoming / f"vj-{SESSION}").exists()
    assert rig.raw_packages() == []


def test_a_package_the_publisher_refuses_is_refused_to_the_client(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(wait=0.0)
    manifest = make_manifest()
    rig.call("POST", "/api/intake/sessions", json_body=manifest)
    rig.upload_all()
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")[0] == 503
    assert rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/{ROOM}", body=b"x") == (
        409,
        {"error": "session_already_accepted"},
    )
    tampered = rig.incoming / f"vj-{SESSION}" / "anchor-song" / SONG
    tampered.write_bytes(tampered.read_bytes() + b"\x00")
    rig.publish()
    _, status = rig.call("GET", f"/api/intake/sessions/{SESSION}")
    assert status["state"] == "refused"
    assert "tampered" in status["reason"]
    finalize = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert finalize[0] == 422
    assert finalize[1]["error"] == "session_refused"
    reopen = rig.call("POST", "/api/intake/sessions", json_body=manifest)
    assert reopen[0] == 422


def test_a_crashed_finalize_is_completed_by_the_next_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    rig.upload_all()
    files = rig.intake.staging_dir / SESSION / "files"
    (files / "leftover.wav.part").write_bytes(b"x")
    (files / "stray").mkdir()

    def crash(_source: Path, _target: Path) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(journey_module, "rename_no_replace", crash)
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize") == (
        500,
        {"error": "internal_error"},
    )
    monkeypatch.undo()
    # Arranged, with its control files and record, but never handed over: still open.
    assert (files / "room-silence" / ROOM).exists() and (files / "READY").exists()
    assert not (files / "stray").exists()
    _, status = rig.call("GET", f"/api/intake/sessions/{SESSION}")
    assert status["state"] == "open"
    assert all(file["complete"] for file in status["files"])
    assert rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/{ROOM}", body=b"x")[0] == 409
    (files / ".manifest.json.tmp-0000").write_bytes(b"{")
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")[1]["state"] == "accepted"


def test_a_package_name_already_waiting_in_incoming_is_never_replaced(rig: Rig) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    rig.upload_all()
    (rig.incoming / f"vj-{SESSION}").mkdir()
    refused = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert refused == (409, {"error": "session_already_accepted"})


def test_concurrent_requests_on_one_session_are_busy(rig: Rig) -> None:
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    rig.intake._busy.add(f"{SESSION}/{ROOM}")
    busy = rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    assert busy == (409, {"error": "session_busy"})
    rig.intake._busy.clear()
    rig.intake._busy.add(SESSION)
    put = rig.call("PUT", f"/api/intake/sessions/{SESSION}/files/{ROOM}", body=PAYLOADS[ROOM])
    assert put == (409, {"error": "session_busy"})
    # A busy session is never purged, however old.
    rig.clock[0] += 30 * 24 * 60 * 60 * 1000
    assert rig.intake.purge_stale() == 1
    rig.intake._busy.clear()


def test_records_are_kept_and_only_open_sessions_count(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(limits=replace(DEFAULT_LIMITS, max_open_sessions=1))
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    rig.upload_all()
    assert rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")[0] == 200
    (rig.intake.staging_dir / "loose-file").write_text("x")
    rig.clock[0] += 30 * 24 * 60 * 60 * 1000
    other = make_manifest({"sessionId": "s-20261004-090000"})
    assert rig.call("POST", "/api/intake/sessions", json_body=other)[0] == 201
    assert (rig.intake.staging_dir / SESSION / "session.json").exists()


def test_a_damaged_record_or_receipt_is_the_servers_failure(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(wait=0.0)
    rig.call("POST", "/api/intake/sessions", json_body=make_manifest())
    rig.upload_all()
    rig.call("POST", f"/api/intake/sessions/{SESSION}/finalize")
    record = json.loads((rig.intake.staging_dir / SESSION / "session.json").read_text())
    digest = record["intake"]["package"]["manifestSha256"]
    forged = rig.raw / digest
    forged.mkdir()
    receipt = IntakeReceipt(
        session_id="someone-else",
        manifest_sha256=digest,
        source_qc_report_sha256="c" * 64,
        intake_qc_report_sha256="d" * 64,
        accepted_at=datetime(2026, 10, 9, tzinfo=UTC),
        raw_path=f"raw/{digest}",
    )
    (forged / RECEIPT_NAME).write_bytes(canonical_json_bytes(receipt) + b"\n")
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}") == (500, {"error": "internal_error"})
    (rig.intake.staging_dir / SESSION / "manifest.json").write_text("[]")
    assert rig.call("GET", f"/api/intake/sessions/{SESSION}")[0] == 500


# ---- JavaScript value rules ---------------------------------------------------------------


def test_the_javascript_rules_of_the_manifest() -> None:
    assert canonical_json({"b": [1, {"y": None, "x": 2.0}], "a": {}, "B": "é"}) == (
        '{"B":"é","a":{},"b":[1,{"x":2,"y":null}]}'
    )
    assert [is_integer(value) for value in (3000, 3000.0, 1.5, True, "5", 2**53, math.inf)] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert decode_uri_component("..%2Fx.wav") == "../x.wav"
    assert decode_uri_component("%C3%A9t%c3%a9") == "été"
    for malformed in ("%zz", "%2", "%FF"):
        with pytest.raises(ValueError):
            decode_uri_component(malformed)
    with pytest.raises(IntakeError, match="manifest_not_object"):
        validate_manifest([], REGISTRY)
    with pytest.raises(IntakeError) as unknown:
        validate_manifest(make_manifest({"chainId": ["a", None, 1]}), REGISTRY)
    assert unknown.value.detail == {"chainId": "a,,1"}
    reject({"capture": None}, "capture_required")
    reject({"capture": [62, 35]}, "input_level_required")
    reject({"capture": {"inputVolume": True, "distanceCm": 35}}, "input_level_required")
    capture = make_manifest()["capture"]
    reject({"capture": {**capture, "sampleRate": "48000"}}, "sample_rate_mismatch")
    reject({"capture": {**capture, "sampleRate": True}}, "sample_rate_mismatch")
    missing_cal = {key: value for key, value in capture.items() if key != "calFileSha256"}
    reject({"capture": missing_cal}, "invalid_cal_file_hash")
    reject({"files": ["01-room-silence.wav"]}, "invalid_file_name")
    reject({"segments": ["room-silence"]}, "invalid_segment")
    reject({"sessionId": "s-20261003-101500\n"}, "invalid_session_id")
    floats = make_manifest()
    floats["files"][0]["sizeBytes"] = float(floats["files"][0]["sizeBytes"])
    normalized = validate_manifest({**floats, "status": "completed"}, REGISTRY)
    assert "status" not in normalized
    assert list(normalized) == [
        "schemaVersion",
        "sessionId",
        "chainId",
        "chainKind",
        "controlled",
        "startedAt",
        "endedAt",
        "capture",
        "segments",
        "files",
    ]
    assert normalized["controlled"] is True
    assert canonical_json(normalized) == canonical_json(
        validate_manifest(make_manifest(), REGISTRY)
    )


def test_a_refused_upload_too_large_to_drain_closes_the_connection(rig: Rig) -> None:
    with socket.create_connection(("127.0.0.1", rig.port), timeout=10) as raw:
        head = [
            f"PUT /api/intake/sessions/{SESSION}/files/{ROOM} HTTP/1.1",
            "Host: 127.0.0.1",
            f"Authorization: Bearer {mint()}",
            f"Content-Length: {100 * 1024 * 1024}",
        ]
        raw.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        reply = b""
        while chunk := raw.recv(65536):  # the server answers, then closes: no drain
            reply += chunk
    assert reply.split(b"\r\n", 1)[0].endswith(b"404 Not Found")
    assert reply.endswith(b'{"error":"session_not_found"}\n')
