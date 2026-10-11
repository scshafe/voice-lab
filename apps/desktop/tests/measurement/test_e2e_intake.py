"""End to end against voice-lab's own intake: the compatibility path into raw/.

A session recorded by the ritual (the simulated UMIK-1) is uploaded with the capture
client's own code (``upload_pending``) to voice-lab's ``ApiServer``, whose ``JourneyIntake``
answers voice-journey's ``/api/intake/…`` protocol (design §3.4, D-035), converts the
session into a capture package in ``incoming/`` and waits for the intake publisher, which
validates it with the unchanged strict validator and publishes it into ``raw/`` with a v2
receipt. The publisher's pass runs where the intake waits for it (its ``sleep``), as on the
PC its host unit does between the client's polls. The token is the backend's test key's,
verified by the API itself (``api/auth.py``). Synthetic lab, synthetic audio; nothing real.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import record_session
from home_host_fixtures import ISSUER, jwks, mint, policy
from voice_lab_desktop.auth import Session
from voice_lab_desktop.config import Config
from voice_lab_desktop.measurement.ritual import Ritual
from voice_lab_desktop.measurement.store import SessionStore
from voice_lab_desktop.measurement.upload import IntakeClient, upload_pending, upload_session
from voice_lab_desktop.tokens import MemoryStore, Tokens

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.journey_intake import JourneyIntake
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.intake import RECEIPT_NAME, IntakeReceipt, validate_capture_package
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.paths import AssetLayout

CHUNK = 300_000  # several PUTs a file, so a cut can fall inside one


@dataclass
class Lab:
    layout: AssetLayout
    server: ApiServer

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def raw(self) -> Path:
        return self.layout.directory("raw")

    def packages(self) -> list[Path]:
        return sorted(path for path in self.raw.iterdir() if path.is_dir())


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[Lab]:
    """voice-lab's API with the compatibility intake over a synthetic lab."""
    layout = AssetLayout.initialize(tmp_path / "voice-lab" / "lab")
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    verifier = TokenVerifier(policy(jwks_path))
    intake = JourneyIntake(
        layout,
        verifier=verifier,
        log=lambda line: None,
        # The publisher host unit's pass, where the intake waits for it.
        sleep=lambda _seconds: publish_pending(layout, min_free_bytes=0),
        publish_wait_seconds=30.0,
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
    try:
        yield Lab(layout, server)
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class Wire:
    """Every request the client sent, and an optional cut after ``cut_after`` PUTs."""

    requests: list[httpx.Request]
    cut_after: int | None = None

    def puts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PUT"]

    def hook(self, request: httpx.Request) -> None:
        self.requests.append(request)
        if self.cut_after is not None and len(self.puts()) > self.cut_after:
            self.cut_after = None
            raise httpx.ReadError("the connection dropped", request=request)


def client_for(lab: Lab, tmp_path: Path, wire: Wire) -> tuple[IntakeClient, httpx.Client]:
    config = Config(
        server_url=lab.url,
        issuer=ISSUER,
        client_id="voice-lab-desktop",
        data_dir=tmp_path / "client",
        file=tmp_path / "client.toml",
    ).validated()
    token = mint({"exp": 4_102_444_800})  # 2100: no refresh during the test
    tokens = Tokens(token, token, 4_102_444_800.0)
    http = httpx.Client(follow_redirects=False, timeout=60, event_hooks={"request": [wire.hook]})
    return IntakeClient(lab.url, Session(config, MemoryStore(tokens), http), http, CHUNK), http


def content_start(request: httpx.Request) -> int:
    return int(request.headers["content-range"].split()[1].split("-")[0])


def assert_published(lab: Lab, ritual: Ritual) -> None:
    """The session is one package in raw/, with a v2 receipt naming it, byte for byte."""
    [package] = lab.packages()
    validated = validate_capture_package(package, allow_intake_receipt=True)
    manifest = validated.manifest
    receipt = IntakeReceipt.model_validate_json((package / RECEIPT_NAME).read_bytes())
    assert manifest.session_id == receipt.session_id == f"vj-{ritual.session_id}"
    assert receipt.manifest_sha256 == validated.manifest_sha256
    assert manifest.prompt_suite_id == "voice-journey-measurement"
    assert manifest.capture_device.device_id == "voice-journey-chain:umik1"
    assert ritual.manifest is not None and ritual.session_dir is not None
    keys = {segment["file"]: segment["key"] for segment in ritual.manifest["segments"]}
    for entry in ritual.manifest["files"]:
        published = package / keys[entry["name"]] / entry["name"]
        assert published.read_bytes() == (ritual.session_dir / entry["name"]).read_bytes()
    kinds = {entry.path.split("/")[0]: entry.kind for entry in manifest.files}
    assert kinds["room-silence"] == "room-tone"
    assert {kind for key, kind in kinds.items() if key != "room-silence"} == {"calibration"}


def test_a_recorded_session_lands_in_raw_with_a_receipt(
    lab: Lab,
    tmp_path: Path,
    backend: Any,
    levels: Any,
    pinned: Any,
    sessions: SessionStore,
) -> None:
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    wire = Wire([])
    client, http = client_for(lab, tmp_path, wire)
    with http:
        summary = upload_pending(client, sessions, lab.url)
        assert (summary.accepted, summary.pending, summary.failed) == (1, 0, 0), summary.last_error
        assert client.status(ritual.session_id).body["state"] == "accepted"
        assert_published(lab, ritual)
        [local] = sessions.sessions()
        assert local.upload_state == "accepted" and local.upload["server"] == lab.url

        # Again: the server already holds it; nothing is sent twice, nothing is published twice.
        sent = len(wire.puts())
        assert upload_session(client, local) == "accepted"
        assert len(wire.puts()) == sent
        assert len(lab.packages()) == 1


def test_a_cut_upload_resumes_from_the_bytes_the_server_holds(
    lab: Lab,
    tmp_path: Path,
    backend: Any,
    levels: Any,
    pinned: Any,
    sessions: SessionStore,
) -> None:
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    wire = Wire([], cut_after=2)  # the third PUT never reaches the server
    client, http = client_for(lab, tmp_path, wire)
    with http:
        first = upload_pending(client, sessions, lab.url)
        assert (first.accepted, first.pending) == (0, 1)
        assert "did not answer" in (first.last_error or "")
        assert sessions.pending()[0].upload["attempts"] == 1
        assert lab.packages() == []
        held = {f["name"]: f for f in client.status(ritual.session_id).body["files"]}
        assert ritual.manifest is not None
        first_file = ritual.manifest["files"][0]["name"]
        assert held[first_file]["receivedBytes"] == 2 * CHUNK and not held[first_file]["complete"]

        before = len(wire.puts())
        second = upload_pending(client, sessions, lab.url)
        assert second.accepted == 1, second.last_error
        resumed = wire.puts()[before:]
        assert content_start(resumed[0]) == 2 * CHUNK  # not from zero
        assert_published(lab, ritual)


def test_the_compatibility_path_is_fail_closed(
    lab: Lab,
    tmp_path: Path,
    backend: Any,
    levels: Any,
    pinned: Any,
    sessions: SessionStore,
) -> None:
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    assert ritual.session_dir is not None and ritual.manifest is not None
    manifest_bytes = (ritual.session_dir / "session.json").read_bytes()
    first = ritual.manifest["files"][0]
    wire = Wire([])
    client, http = client_for(lab, tmp_path, wire)
    with http:
        anonymous = httpx.post(f"{lab.url}/api/intake/sessions", content=manifest_bytes)
        assert (anonymous.status_code, anonymous.json()["error"]) == (401, "identity_required")
        opened = client.open(manifest_bytes)
        assert opened.status == 201 and opened.body["state"] == "open"
        bad = bytearray((ritual.session_dir / first["name"]).read_bytes())
        bad[-1] ^= 0xFF
        answer = client._send(
            "PUT",
            f"{client.base}/{ritual.session_id}/files/{first['name']}",
            content=bytes(bad),
            headers={
                "Content-Range": f"bytes 0-{len(bad) - 1}/{len(bad)}",
                "Content-Type": "application/octet-stream",
            },
        )
        assert (answer.status, answer.body["error"]) == (422, "checksum_mismatch")
        final = client.finalize(ritual.session_id)
        assert (final.status, final.body["error"]) == (409, "incomplete")
        assert lab.packages() == []
        # A different manifest for the same session is a conflict; the client's normal path
        # then completes the session it opened.
        other = json.loads(manifest_bytes)
        other["capture"]["distanceCm"] = 36
        conflict = client._send(
            "POST",
            client.base,
            content=json.dumps(other).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert (conflict.status, conflict.body["error"]) == (409, "session_manifest_conflict")
        assert upload_pending(client, sessions, lab.url).accepted == 1
        assert_published(lab, ritual)
