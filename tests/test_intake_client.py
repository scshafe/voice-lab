"""The upload client's handling of answers it does not expect (scripted, no server)."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from home_host_fixtures import NOW, Rig, start_rig

from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.intake_client import (
    Answer,
    HomeHostClient,
    UploadError,
    UploadRefused,
    _fail,
    _send_file,
    session_request,
    upload_session,
)
from cole_voice_lab.synthetic_capture import build_synthetic_session


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield from start_rig(tmp_path)


class Scripted(HomeHostClient):
    """A client whose answers are given in advance, in order."""

    def __init__(self, answers: list[Answer]) -> None:
        super().__init__(endpoint="127.0.0.1", resolver=None, bearer=lambda: "t", loopback_port=1)
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, path: str, **options: object) -> Answer:  # type: ignore[override]
        self.calls.append((method, path, dict(options.get("headers") or {})))  # type: ignore[arg-type]
        return self.answers.pop(0)


def test_answers_map_to_retryable_or_final_failures() -> None:
    assert isinstance(_fail(Answer(503, {})), UploadError)
    assert isinstance(
        _fail(Answer(429, {"error": "too_many_open_sessions", "detail": "x"})), UploadError
    )
    refused = _fail(Answer(400, {}))
    assert isinstance(refused, UploadRefused) and refused.code == "status-400"


def test_a_file_restarts_once_after_a_checksum_mismatch(rig: Rig) -> None:
    session = build_synthetic_session(rig.work / "s-1", session_id="s-1", seconds=0.1)
    declared = session_request(validate_capture_package(session)).files[0]
    client = Scripted(
        [
            Answer(422, {"error": "checksum_mismatch", "detail": "x", "received_bytes": 0}),
            Answer(200, {"state": "received", "received_bytes": declared.size_bytes}),
        ]
    )
    _send_file(client, session / declared.path, "s-1", declared, 0)
    assert [call[2]["Content-Range"] for call in client.calls] == [
        f"bytes 0-{declared.size_bytes - 1}/{declared.size_bytes}"
    ] * 2
    twice = Scripted(
        [Answer(422, {"error": "checksum_mismatch", "detail": "x", "received_bytes": 0})] * 2
    )
    with pytest.raises(UploadRefused, match="checksum_mismatch"):
        _send_file(twice, session / declared.path, "s-1", declared, 0)
    done = Scripted([Answer(409, {"error": "file_already_received", "detail": "x"})])
    _send_file(done, session / declared.path, "s-1", declared, 0)
    garbage = Scripted([Answer(200, {"state": "maybe"})])
    with pytest.raises(UploadError, match="invalid ChunkAnswer"):
        _send_file(garbage, session / declared.path, "s-1", declared, 0)


def test_a_package_not_yet_published_is_reported_for_a_later_run(rig: Rig) -> None:
    session = build_synthetic_session(rig.work / "s-2", session_id="s-2", seconds=0.1)
    request = session_request(validate_capture_package(session))
    finalized = {
        "schema_version": "cole-voice-lab.api.intake-session-status/v1",
        "session_id": "s-2",
        "manifest_sha256": request.manifest_sha256,
        "state": "finalized",
        "files": [],
    }
    client = Scripted([Answer(200, finalized), Answer(200, finalized)])
    with pytest.raises(UploadError, match="not yet published"):
        upload_session(
            session,
            layout=rig.client_layout,
            client=client,
            clock=lambda: NOW,
            sleep=lambda _s: None,
            wait_seconds=0.5,
            monotonic=iter([0.0, 0.0, 1.0, 2.0]).__next__,
        )
    refused_open = Scripted([Answer(400, {"error": "invalid_request", "detail": "no"})])
    with pytest.raises(UploadRefused, match="invalid_request"):
        upload_session(session, layout=rig.client_layout, client=refused_open, clock=lambda: NOW)
    failing_finalize = Scripted(
        [
            Answer(200, {**finalized, "state": "open"}),
            Answer(500, {"error": "internal_error", "detail": "OSError"}),
        ]
    )
    with pytest.raises(UploadError, match="internal_error"):
        upload_session(
            session, layout=rig.client_layout, client=failing_finalize, clock=lambda: NOW
        )


def test_redirects_and_closed_ports_are_upload_errors() -> None:
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://id.example.invalid/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HomeHostClient.loopback_for_tests(server.server_address[1], lambda: "t")
        with pytest.raises(UploadError, match="redirected"):
            client.request("GET", "/api/v1/meta")
    finally:
        server.shutdown()
        server.server_close()
    closed = HomeHostClient.loopback_for_tests(server.server_address[1], lambda: "t")
    with pytest.raises(UploadError, match="unreachable"):
        closed.request("GET", "/api/v1/meta")
