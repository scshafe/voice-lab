"""The /api/v1 server's routes, refusals and staging rules (phase 2: meta and intake)."""

from __future__ import annotations

import http.client
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from home_host_fixtures import Rig, mint, start_rig

from cole_voice_lab.api import server as server_module
from cole_voice_lab.api.contracts import (
    REFUSAL_CODES,
    FileDeclaration,
    IntakeSessionRequest,
    validate_package_path,
)
from cole_voice_lab.api.staging import MAX_OPEN_SESSIONS, STALE_SESSION_SECONDS, IntakeStaging
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.intake_client import session_request
from cole_voice_lab.synthetic_capture import build_synthetic_session


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield from start_rig(tmp_path)


def _raw(
    rig: Rig,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    token: str | None = "default",
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=10)
    all_headers = dict(headers or {})
    if token is not None:
        all_headers["Authorization"] = f"Bearer {mint() if token == 'default' else token}"
    connection.request(method, path, body=body, headers=all_headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, (json.loads(data) if data else {})


def _request(rig: Rig, session_id: str = "synthetic-001") -> IntakeSessionRequest:
    session = build_synthetic_session(rig.work / session_id, session_id=session_id, seconds=0.2)
    return session_request(validate_capture_package(session))


def _open(rig: Rig, request: IntakeSessionRequest) -> tuple[int, dict[str, object]]:
    return _raw(rig, "POST", "/api/v1/intake/sessions", canonical_json_bytes(request))


def _put(
    rig: Rig, session_id: str, path: str, body: bytes, content_range: str | None
) -> tuple[int, dict[str, object]]:
    headers = {} if content_range is None else {"Content-Range": content_range}
    quoted = path.replace("/", "%2F")
    return _raw(rig, "PUT", f"/api/v1/intake/sessions/{session_id}/files/{quoted}", body, headers)


def _send_all(rig: Rig, request: IntakeSessionRequest) -> None:
    for declared in request.files:
        data = (rig.work / request.session_id / declared.path).read_bytes()
        status, answer = _put(
            rig,
            request.session_id,
            declared.path,
            data,
            f"bytes 0-{len(data) - 1}/{len(data)}",
        )
        assert status == 200, answer


def test_health_needs_no_token_and_meta_does(rig: Rig) -> None:
    assert _raw(rig, "GET", "/healthz", token=None) == (200, {"status": "ok"})
    assert _raw(rig, "HEAD", "/healthz", token=None)[0] == 200
    status, answer = _raw(rig, "GET", "/api/v1/meta", token=None)
    assert status == 401 and answer["error"] == "unauthorized"
    status, answer = _raw(rig, "GET", "/api/v1/meta")
    assert status == 200
    assert answer["api_versions"] == ["v1"] and answer["server_build"] == "test-build"


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/nowhere", 404, "not_found"),
        ("GET", "/api/v2/meta", 404, "not_found"),
        ("POST", "/api/v1/meta", 405, "method_not_allowed"),
        ("DELETE", "/api/v1/intake/sessions", 405, "method_not_allowed"),
        ("PATCH", "/healthz", 405, "method_not_allowed"),
        ("GET", "/api/v1/intake/sessions/nobody", 404, "session_not_found"),
        ("GET", "/api/v1/intake/sessions/nobody/other", 404, "not_found"),
        ("GET", "/api/v1/intake/sessions/nobody/files/a/b", 404, "not_found"),
        ("GET", "/api/v1/intake/sessions/nobody/receipt", 404, "session_not_found"),
        ("POST", "/api/v1/intake/sessions/nobody/finalize", 404, "session_not_found"),
    ],
)
def test_routes_refuse_with_stable_codes(
    rig: Rig, method: str, path: str, status: int, code: str
) -> None:
    answer = _raw(rig, method, path)
    assert answer[0] == status and answer[1]["error"] == code
    assert code in REFUSAL_CODES


def test_session_open_is_idempotent_and_conflicts_are_refused(rig: Rig) -> None:
    request = _request(rig)
    status, answer = _open(rig, request)
    assert status == 201 and answer["state"] == "open"
    assert _open(rig, request)[0] == 200
    other = request.model_copy(update={"files": request.files[1:]})
    status, answer = _raw(rig, "POST", "/api/v1/intake/sessions", canonical_json_bytes(other))
    assert status == 422 and answer["error"] == "invalid_request"
    changed = request.model_copy(
        update={
            "files": tuple(
                item.model_copy(update={"size_bytes": item.size_bytes + 1})
                if item.path.startswith("audio/")
                else item
                for item in request.files
            )
        }
    )
    status, answer = _open(rig, changed)
    assert status == 409 and answer["error"] == "session_conflict"


def test_bodies_are_bounded_and_validated(rig: Rig) -> None:
    status, answer = _raw(rig, "POST", "/api/v1/intake/sessions", b"{not json")
    assert status == 422 and answer["error"] == "invalid_request"
    big = b" " * (server_module.MAX_JSON_BODY_BYTES + 1)
    status, answer = _raw(rig, "POST", "/api/v1/intake/sessions", big)
    assert status == 413 and answer["error"] == "body_too_large"
    connection = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=10)
    connection.putrequest("POST", "/api/v1/intake/sessions")
    connection.putheader("Authorization", f"Bearer {mint()}")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 411
    assert json.loads(response.read())["error"] == "content_length_required"
    connection.close()
    status, answer = _raw(rig, "GET", "/api/v1/meta", headers={"Content-Length": "abc"}, body=None)
    assert status == 200


def test_chunks_must_be_contiguous_and_within_the_declaration(rig: Rig) -> None:
    request = _request(rig)
    _open(rig, request)
    declared = next(item for item in request.files if item.path.startswith("audio/"))
    data = (rig.work / "synthetic-001" / declared.path).read_bytes()
    size = len(data)

    assert _put(rig, "synthetic-001", declared.path, data[:10], None)[1]["error"] == (
        "invalid_content_range"
    )
    assert _put(rig, "synthetic-001", declared.path, data[:10], "bytes 0-10/9")[1]["error"] == (
        "invalid_content_range"
    )
    assert _put(rig, "synthetic-001", declared.path, data[:10], "bytes 0-9/5")[1]["error"] == (
        "content_range_out_of_bounds"
    )
    assert _put(rig, "synthetic-001", declared.path, b"", "bytes */5")[1]["error"] == (
        "invalid_content_range"
    )
    status, answer = _put(rig, "synthetic-001", declared.path, data[:100], f"bytes 0-99/{size}")
    assert (status, answer) == (202, {"state": "partial", "received_bytes": 100})
    status, answer = _put(
        rig, "synthetic-001", declared.path, data[200:300], f"bytes 200-299/{size}"
    )
    assert status == 409 and answer["error"] == "range_not_contiguous"
    assert answer["received_bytes"] == 100
    status, answer = _put(
        rig, "synthetic-001", declared.path, data[100:], f"bytes 100-{size - 1}/{size}"
    )
    assert (status, answer) == (200, {"state": "received", "received_bytes": size})
    status, answer = _put(rig, "synthetic-001", declared.path, data, f"bytes 0-{size - 1}/{size}")
    assert status == 409 and answer["error"] == "file_already_received"
    assert _put(rig, "synthetic-001", "audio/../x.wav", b"x", "bytes 0-0/1")[1]["error"] == (
        "file_not_declared"
    )
    assert _put(rig, "synthetic-001", "audio/%ff.wav", b"x", "bytes 0-0/1")[1]["error"] == (
        "file_not_declared"
    )
    big = server_module.MAX_CHUNK_BYTES + 1
    connection = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=10)
    connection.putrequest("PUT", "/api/v1/intake/sessions/synthetic-001/files/READY")
    connection.putheader("Authorization", f"Bearer {mint()}")
    connection.putheader("Content-Length", str(big))
    connection.putheader("Content-Range", f"bytes 0-{big - 1}/{big}")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413
    connection.close()


def test_finalize_waits_for_every_file_then_hands_the_package_over(rig: Rig) -> None:
    request = _request(rig)
    _open(rig, request)
    status, answer = _raw(rig, "POST", "/api/v1/intake/sessions/synthetic-001/finalize")
    assert status == 409 and answer["error"] == "incomplete"
    _send_all(rig, request)
    status, answer = _raw(rig, "POST", "/api/v1/intake/sessions/synthetic-001/finalize")
    assert status == 200 and answer["state"] == "finalized"
    assert (rig.server_layout.directory("incoming") / "synthetic-001" / "READY").exists()
    assert _raw(rig, "POST", "/api/v1/intake/sessions/synthetic-001/finalize")[0] == 200
    status, answer = _raw(rig, "GET", "/api/v1/intake/sessions/synthetic-001/receipt")
    assert status == 409 and answer["error"] == "not_accepted"
    declared = request.files[0]
    status, answer = _put(rig, "synthetic-001", declared.path, b"x", "bytes 0-0/1")
    assert status == 409 and answer["error"] == "session_finalized"
    # A new session with this id cannot be opened while the package waits for intake.
    rig.staging.uploads.joinpath("synthetic-001", "FINALIZED").unlink()
    (rig.staging.uploads / "synthetic-001").rename(rig.work / "moved-away")
    status, answer = _open(rig, request)
    assert status == 409 and answer["error"] == "session_conflict"
    rig.publish()
    accepted = rig.server_layout.directory("raw") / request.manifest_sha256
    assert accepted.is_dir()


def test_finalize_rechecks_staged_bytes(rig: Rig) -> None:
    request = _request(rig)
    _open(rig, request)
    _send_all(rig, request)
    staged = rig.staging.uploads / "synthetic-001" / "package" / "READY"
    staged.write_bytes(staged.read_bytes() + b" ")
    status, answer = _raw(rig, "POST", "/api/v1/intake/sessions/synthetic-001/finalize")
    assert status == 422 and answer["error"] == "checksum_mismatch"
    assert not staged.exists()


def test_open_sessions_are_limited_and_stale_ones_removed(rig: Rig) -> None:
    for index in range(MAX_OPEN_SESSIONS):
        assert _open(rig, _request(rig, f"synthetic-{index:03d}"))[0] == 201
    status, answer = _open(rig, _request(rig, "synthetic-900"))
    assert status == 429 and answer["error"] == "too_many_open_sessions"
    old = time.time() - STALE_SESSION_SECONDS - 60
    for root, directories, files in os.walk(rig.staging.uploads / "synthetic-000"):
        for name in (*directories, *files):
            os.utime(Path(root) / name, (old, old))
    os.utime(rig.staging.uploads / "synthetic-000", (old, old))
    assert _open(rig, _request(rig, "synthetic-901"))[0] == 201
    assert not (rig.staging.uploads / "synthetic-000").exists()


def test_a_session_beyond_the_disk_floor_is_refused(rig: Rig) -> None:
    staging = IntakeStaging(rig.server_layout, min_free_bytes=10, free_bytes=lambda _path: 5)
    rig.server.staging = staging
    status, answer = _open(rig, _request(rig))
    assert status == 507 and answer["error"] == "insufficient_storage"


def test_an_empty_declared_file_is_received_with_an_empty_range(rig: Rig) -> None:
    request = _request(rig)
    empty_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    extended = request.model_copy(
        update={
            "files": tuple(
                sorted(
                    (
                        *request.files,
                        FileDeclaration(path="empty", sha256=empty_hash, size_bytes=0),
                    ),
                    key=lambda item: item.path,
                )
            )
        }
    )
    _open(rig, extended)
    assert _put(rig, "synthetic-001", "empty", b"", "bytes */0") == (
        200,
        {"state": "received", "received_bytes": 0},
    )


@pytest.mark.parametrize(
    "path",
    ["", "/abs", "a/../b", ".hidden", "a/.b", "x.part", "a/b/c/d/e", "a" * 256, "a//b"],
)
def test_package_paths_are_canonical_and_bounded(path: str) -> None:
    with pytest.raises(ValueError):
        validate_package_path(path)


def test_a_request_declares_its_control_files_sorted_and_once() -> None:
    digest = "a" * 64
    control = [
        FileDeclaration(path=name, sha256=digest, size_bytes=1)
        for name in ("READY", "manifest.json", "qc.json")
    ]
    IntakeSessionRequest(session_id="s-1", manifest_sha256=digest, files=tuple(control))
    for files, problem in [
        (control[:2], "at least 3"),
        ([*control, control[0]], "once"),
        ([control[1], control[0], control[2]], "sorted"),
        (
            [
                *control,
                FileDeclaration(path="qc.json/x", sha256=digest, size_bytes=1),
            ],
            "directory",
        ),
    ]:
        with pytest.raises(ValueError, match=problem):
            IntakeSessionRequest(session_id="s-1", manifest_sha256=digest, files=tuple(files))
    with pytest.raises(ValueError, match="control files"):
        IntakeSessionRequest(
            session_id="s-1",
            manifest_sha256=digest,
            files=(
                control[0],
                FileDeclaration(path="audio.wav", sha256=digest, size_bytes=1),
                control[2],
            ),
        )
    with pytest.raises(ValueError, match="manifest_sha256"):
        IntakeSessionRequest(session_id="s-1", manifest_sha256="b" * 64, files=tuple(control))


def test_the_serve_command_builds_the_server_from_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.paths import AssetLayout

    layout = AssetLayout.initialize(tmp_path / "srv" / "voice-lab" / "lab")
    served: list[server_module.ApiServer] = []

    def serve_forever(self: server_module.ApiServer, poll_interval: float = 0.5) -> None:
        served.append(self)

    monkeypatch.setattr(server_module.ApiServer, "serve_forever", serve_forever)
    argv = [
        "serve",
        "--asset-root",
        str(layout.root),
        "--jwks",
        str(tmp_path / "jwks.json"),
        "--issuer",
        "https://id.example.invalid",
        "--audience",
        "voice-lab-desktop",
        "--allowed-subject",
        "subject-cole",
        "--server-build",
        "voice-lab:test",
        "--port",
        "0",
    ]
    assert server_module.main(argv) == 0
    assert served[0].meta.server_build == "voice-lab:test"
    assert served[0].verifier.policy.allowed_subjects == ("subject-cole",)
