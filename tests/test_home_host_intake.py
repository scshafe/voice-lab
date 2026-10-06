"""End to end: the upload client, the /api/v1 intake server and the publisher (phase 2)."""

from __future__ import annotations

import json
import os
import shutil
import ssl
import stat
import urllib.parse
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from home_host_fixtures import (
    ENDPOINT,
    NODE,
    NOW,
    Rig,
    bind_approval,
    make_approval,
    mint,
    start_rig,
)

from cole_voice_lab import intake_client
from cole_voice_lab.api.contracts import IntakeSessionStatus
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.home_host import HomeHostLedger
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.intake_client import (
    HomeHostClient,
    UploadError,
    UploadRefused,
    session_request,
    upload_session,
)
from cole_voice_lab.intake_publisher import publish_pending, read_refusal
from cole_voice_lab.synthetic_capture import build_synthetic_session
from cole_voice_lab.tailnet import ResolvedPeer


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield from start_rig(tmp_path)


def _session(rig: Rig, session_id: str = "synthetic-001", *, seconds: float = 0.5) -> Path:
    return build_synthetic_session(rig.work / session_id, session_id=session_id, seconds=seconds)


def _upload(rig: Rig, session: Path, client: HomeHostClient | None = None) -> IntakeSessionStatus:
    return upload_session(
        session,
        layout=rig.client_layout,
        client=client or rig.client(),
        clock=lambda: NOW,
        sleep=lambda _seconds: rig.publish(),
        wait_seconds=30,
    )


def _raw_modes(raw: Path) -> set[tuple[str, int]]:
    modes = {("root", stat.S_IMODE(raw.stat().st_mode))}
    for root, directories, files in os.walk(raw):
        for name in directories:
            modes.add(("dir", stat.S_IMODE((Path(root) / name).stat().st_mode)))
        for name in files:
            modes.add(("file", stat.S_IMODE((Path(root) / name).stat().st_mode)))
    return modes


def test_a_synthetic_session_is_accepted_read_only_with_a_matching_receipt(rig: Rig) -> None:
    session = _session(rig)
    local = validate_capture_package(session)

    status = _upload(rig, session)

    assert status.state == "accepted"
    assert status.receipt is not None
    assert status.receipt.manifest_sha256 == local.manifest_sha256
    raw = rig.server_layout.root / status.receipt.raw_path
    assert _raw_modes(raw) == {("root", 0o500), ("dir", 0o500), ("file", 0o400)}
    assert validate_capture_package(raw, allow_intake_receipt=True).manifest == local.manifest
    assert not (rig.server_layout.directory("incoming") / "synthetic-001").exists()
    events = [entry.event for entry, _ in HomeHostLedger(rig.client_layout).entries()]
    assert events == ["upload-started", "upload-completed"]
    record = rig.server_layout.directory("provenance") / "intake/sessions/synthetic-001.json"
    assert json.loads(record.read_text())["manifest_sha256"] == local.manifest_sha256
    # Running it again collects the same receipt and sends nothing.
    again = _upload(rig, session)
    assert again.receipt == status.receipt
    # The take stays in the capturing host's library.
    assert validate_capture_package(session).manifest_sha256 == local.manifest_sha256


def test_an_interrupted_upload_resumes_from_the_bytes_the_server_holds(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(rig, seconds=1.0)
    monkeypatch.setattr(intake_client, "CHUNK_BYTES", 64 * 1024)
    client = rig.client()
    sent: list[str] = []
    original = client.request

    def interrupt_after_three(method: str, path: str, **options: object) -> intake_client.Answer:
        if method == "PUT":
            sent.append(options["headers"]["Content-Range"])  # type: ignore[index]
            if len(sent) == 3:
                raise UploadError("the network went away")
        return original(method, path, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(client, "request", interrupt_after_three)
    with pytest.raises(UploadError, match="network went away"):
        _upload(rig, session, client)
    status = rig.staging.status("synthetic-001")
    held = {item.path: item.received_bytes for item in status.files}
    assert status.state == "open" and sum(held.values()) > 0

    resumed = rig.client()
    puts: list[str] = []
    original_resumed = resumed.request

    def record(method: str, path: str, **options: object) -> intake_client.Answer:
        if method == "PUT":
            puts.append(options["headers"]["Content-Range"])  # type: ignore[index]
        return original_resumed(method, path, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(resumed, "request", record)
    final = _upload(rig, session, resumed)
    assert final.state == "accepted"

    # The resumed run sent exactly the bytes the server did not hold yet.
    def length(content_range: str) -> int:
        start, _, end = content_range.removeprefix("bytes ").partition("/")[0].partition("-")
        return int(end) - int(start) + 1

    total = sum(item.size_bytes for item in status.files)
    assert sum(length(item) for item in puts) == total - sum(held.values())


def test_a_tampered_recording_is_refused_before_a_byte_is_sent(rig: Rig) -> None:
    session = _session(rig)
    take = next((session / "audio").iterdir())
    data = bytearray(take.read_bytes())
    data[-1] ^= 0xFF
    take.write_bytes(bytes(data))
    with pytest.raises(SecurityError, match="tampered"):
        _upload(rig, session)
    assert HomeHostLedger(rig.client_layout).entries() == []


def test_the_server_refuses_bytes_that_do_not_hash_to_their_declaration(rig: Rig) -> None:
    session = _session(rig)
    request = session_request(validate_capture_package(session))
    client = rig.client()
    opened = client.request(
        "POST", "/api/v1/intake/sessions", body=request.model_dump_json().encode()
    )
    assert opened.status == 201
    declared = request.files[0]
    wrong = b"\0" * declared.size_bytes
    path = (
        f"/api/v1/intake/sessions/synthetic-001/files/{urllib.parse.quote(declared.path, safe='')}"
    )
    answer = client.request(
        "PUT",
        path,
        body=wrong,
        headers={"Content-Range": f"bytes 0-{declared.size_bytes - 1}/{declared.size_bytes}"},
    )
    assert answer.status == 422 and answer.document["error"] == "checksum_mismatch"
    assert rig.staging.status("synthetic-001").files[0].received_bytes == 0


def test_an_undeclared_file_is_refused_by_client_server_and_publisher(rig: Rig) -> None:
    session = _session(rig)
    (session / "audio" / "extra.wav").write_bytes(b"not declared")
    with pytest.raises(SecurityError, match="undeclared"):
        _upload(rig, session)
    (session / "audio" / "extra.wav").unlink()

    request = session_request(validate_capture_package(session))
    client = rig.client()
    client.request("POST", "/api/v1/intake/sessions", body=request.model_dump_json().encode())
    answer = client.request(
        "PUT",
        "/api/v1/intake/sessions/synthetic-001/files/audio%2Fextra.wav",
        body=b"x",
        headers={"Content-Range": "bytes 0-0/1"},
    )
    assert answer.status == 404 and answer.document["error"] == "file_not_declared"

    planted = rig.server_layout.directory("incoming") / "synthetic-002"
    shutil.copytree(_session(rig, "synthetic-002"), planted)
    (planted / "audio" / "extra.wav").write_bytes(b"not declared")
    summary = publish_pending(rig.server_layout, min_free_bytes=0)
    assert [record.session_id for record in summary.refused] == ["synthetic-002"]
    refusal = read_refusal(rig.server_layout, "synthetic-002")
    assert refusal is not None and "undeclared" in refusal.error
    quarantined = rig.server_layout.root / refusal.quarantined_as
    assert (quarantined / "audio" / "extra.wav").exists()
    assert not any(rig.server_layout.directory("raw").iterdir())


def test_a_symlink_is_refused_by_client_and_publisher(rig: Rig) -> None:
    session = _session(rig)
    take = next((session / "audio").iterdir())
    outside = rig.work / "outside.wav"
    take.rename(outside)
    take.symlink_to(outside)
    with pytest.raises(SecurityError, match="regular file"):
        _upload(rig, session)

    linked = rig.server_layout.directory("incoming") / "synthetic-003"
    linked.symlink_to(_session(rig, "synthetic-003"), target_is_directory=True)
    summary = publish_pending(rig.server_layout, min_free_bytes=0)
    assert [record.session_id for record in summary.refused] == ["synthetic-003"]
    assert not os.path.lexists(linked)
    assert not any(rig.server_layout.directory("raw").iterdir())


def test_a_session_the_publisher_refuses_is_reported_to_the_client(rig: Rig) -> None:
    session = _session(rig, "synthetic-004")
    client = rig.client()

    def tamper_then_publish(_seconds: float) -> None:
        package = rig.server_layout.directory("incoming") / "synthetic-004"
        if package.exists():
            (package / "audio" / "late.wav").write_bytes(b"planted after finalize")
        rig.publish()

    with pytest.raises(UploadRefused, match="refused_by_intake"):
        upload_session(
            session,
            layout=rig.client_layout,
            client=client,
            clock=lambda: NOW,
            sleep=tamper_then_publish,
            wait_seconds=30,
        )
    events = [entry.event for entry, _ in HomeHostLedger(rig.client_layout).entries()]
    assert events == ["upload-started", "upload-refused"]


def test_an_expired_or_unbound_approval_sends_nothing(rig: Rig, tmp_path: Path) -> None:
    from cole_voice_lab.paths import AssetLayout

    unbound = AssetLayout.initialize(tmp_path / "other" / "voice-lab" / "lab")
    with pytest.raises(ContractError, match="no such home-host record"):
        upload_session(_session(rig), layout=unbound, client=rig.client(), clock=lambda: NOW)
    late = NOW.replace(year=2027, month=9)
    with pytest.raises(SecurityError, match="does not stand now"):
        upload_session(
            _session(rig, "synthetic-005"),
            layout=rig.client_layout,
            client=rig.client(),
            clock=lambda: late,
        )
    measurement_only = AssetLayout.initialize(tmp_path / "third" / "voice-lab" / "lab")
    bind_approval(measurement_only, make_approval(classes=("measurement",)))
    with pytest.raises(SecurityError, match="does not cover capture"):
        upload_session(
            _session(rig, "synthetic-006"),
            layout=measurement_only,
            client=rig.client(),
            clock=lambda: NOW,
        )


def test_a_wrong_token_is_refused(rig: Rig) -> None:
    session = _session(rig)
    with pytest.raises(UploadRefused, match="unauthorized"):
        _upload(rig, session, rig.client(mint({"sub": "someone-else"})))


class _Resolver:
    def __init__(self, peer: ResolvedPeer) -> None:
        self.peer = peer

    def resolve(self, endpoint: str) -> ResolvedPeer:
        return self.peer


def _tls_pair(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ENDPOINT)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(ENDPOINT)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(cert_path))
    return server, client


def test_the_client_reaches_only_the_approved_node_over_verified_tls(tmp_path: Path) -> None:
    rig_iterator = start_rig(tmp_path)
    rig = next(rig_iterator)
    try:
        server_context, client_context = _tls_pair(tmp_path)
        rig.server.socket = server_context.wrap_socket(rig.server.socket, server_side=True)
        peer = ResolvedPeer(
            address="127.0.0.1",
            node_stable_id=NODE,
            tags=("tag:voice-lab",),
            dns_name=f"{ENDPOINT}.",
            port=rig.port,
        )
        token = mint()
        client = HomeHostClient(
            endpoint=ENDPOINT,
            resolver=_Resolver(peer),
            bearer=lambda: token,
            tls_context=lambda: client_context,
        )
        with pytest.raises(SecurityError, match="not checked"):
            client.request("GET", "/api/v1/meta")
        status = _upload(rig, _session(rig), client)
        assert status.state == "accepted"

        impostor = HomeHostClient(
            endpoint=ENDPOINT,
            resolver=_Resolver(replace(peer, node_stable_id="nIMPOSTOR1CNTRL")),
            bearer=lambda: token,
            tls_context=lambda: client_context,
        )
        with pytest.raises(SecurityError, match="identity check"):
            _upload(rig, _session(rig, "synthetic-002"), impostor)
        untagged = HomeHostClient(
            endpoint=ENDPOINT,
            resolver=_Resolver(replace(peer, tags=("tag:container",))),
            bearer=lambda: token,
            tls_context=lambda: client_context,
        )
        with pytest.raises(SecurityError, match="identity check"):
            _upload(rig, _session(rig, "synthetic-003"), untagged)
        elsewhere = HomeHostClient(
            endpoint="other.example.ts.net",
            resolver=_Resolver(peer),
            bearer=lambda: token,
        )
        with pytest.raises(SecurityError, match="not the approved endpoint"):
            _upload(rig, _session(rig, "synthetic-004"), elsewhere)
        untrusting = HomeHostClient(
            endpoint=ENDPOINT, resolver=_Resolver(peer), bearer=lambda: token
        )
        with pytest.raises(UploadError, match="unreachable"):
            _upload(rig, _session(rig, "synthetic-005"), untrusting)
    finally:
        next(rig_iterator, None)
    with pytest.raises(ValueError, match="resolver"):
        HomeHostClient(endpoint=ENDPOINT, resolver=None, bearer=lambda: token)


def test_the_upload_and_sign_in_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cole_voice_lab import cli_home_host
    from cole_voice_lab.cli import main
    from cole_voice_lab.home_host import HomeHostSignIn, save_sign_in
    from cole_voice_lab.paths import AssetLayout

    layout = AssetLayout.initialize(tmp_path / "air" / "voice-lab" / "lab")
    bind_approval(layout)
    monkeypatch.setattr(cli_home_host, "utc_now", lambda: NOW)
    seen: list[object] = []

    def fake_upload(session: Path, *, layout: object, client: HomeHostClient, wait_seconds: float):  # type: ignore[no-untyped-def]
        seen.append((session, client.endpoint, client._bearer(), wait_seconds))
        return IntakeSessionStatus(
            session_id="synthetic-001", manifest_sha256="a" * 64, state="finalized", files=()
        )

    monkeypatch.setattr(cli_home_host, "upload_session", fake_upload)
    bearer_file = tmp_path / "bearer"
    bearer_file.write_text("a.b.c\n")
    bearer_file.chmod(0o600)
    argv = ["intake", "upload", str(tmp_path / "s"), "--asset-root", str(layout.root)]
    assert main([*argv, "--bearer-file", str(bearer_file), "--wait", "5"]) == 0
    assert seen[-1] == (tmp_path / "s", ENDPOINT, "a.b.c", 5.0)
    assert json.loads(capsys.readouterr().out)["state"] == "finalized"

    assert main(argv) == 2
    assert "no such home-host record" in capsys.readouterr().err
    save_sign_in(layout, HomeHostSignIn(issuer="https://id.example.invalid", client_id="c"))
    monkeypatch.setattr(cli_home_host, "bearer", lambda **options: "x.y.z")
    assert main(argv) == 0
    assert seen[-1][2] == "x.y.z"  # type: ignore[index]
    capsys.readouterr()

    monkeypatch.setattr(cli_home_host, "sign_in", lambda **options: {"sub": "subject-cole"})
    other = AssetLayout.initialize(tmp_path / "pc" / "voice-lab" / "lab")
    assert (
        main(
            [
                "home-host",
                "sign-in",
                "--asset-root",
                str(other.root),
                "--issuer",
                "https://id.example.invalid",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["subject"] == "subject-cole"
