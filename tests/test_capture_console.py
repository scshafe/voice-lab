from __future__ import annotations

import io
import json
import re
import socket
import threading
import wave
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, cast

import pytest

import cole_voice_lab.capture_console as console_module
from cole_voice_lab.capture import (
    CaptureState,
    TakeEngine,
    TakeView,
    expected_recording_paths,
)
from cole_voice_lab.capture_console import CaptureConsoleServer
from cole_voice_lab.errors import ContractError
from cole_voice_lab.models import AudioQcMetrics, CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout

NOW = datetime(2026, 9, 2, 18, 30, tzinfo=UTC)
TOKEN = "a" * 64
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "media-src blob:; img-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


def _suite() -> PromptSuite:
    return PromptSuite(
        suite_id="console-test-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone-01",
                text="Remain silent for room tone.",
                category="room-tone",
                style="Still and silent",
                expected_seconds=1.0,
            ),
            Prompt(
                prompt_id="neutral-01",
                text="Read this exact sentence.",
                category="neutral",
                style="Natural speaking voice",
                expected_seconds=1.0,
            ),
        ),
    )


def _device() -> CaptureDevice:
    return CaptureDevice(
        device_id="4",
        display_name="miniDSP UMIK-1",
        microphone_serial="console-test-serial",
    )


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _write_wav(path: Path, samples: Sequence[int], *, sample_width: int = 3) -> None:
    assert path.parent.is_dir()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(sample_width)
        writer.setframerate(48_000)
        writer.writeframes(
            b"".join(value.to_bytes(sample_width, "little", signed=True) for value in samples)
        )
    path.chmod(0o600)


def _metrics(relative: str) -> AudioQcMetrics:
    return AudioQcMetrics(
        path=relative,
        duration_seconds=1.0,
        peak_dbfs=-9.0,
        rms_dbfs=-24.0,
        dc_offset=0.0,
        clipped_samples=0,
        longest_zero_run_ms=0.0,
        signal_to_noise_db=24.0 if "room-tone" not in relative else None,
        passed=True,
    )


class _FakeEngine:
    def __init__(
        self,
        session_directory: Path,
        preview_path: Path,
        *,
        status: str = "ready",
    ) -> None:
        self.session_directory = session_directory
        self.preview = preview_path
        self.status = status
        self.accepted = 2 if status in {"complete", "finalized"} else 0
        self.index: int | None = None if self.accepted == 2 else 0
        self.rejection_reasons: list[str] = []
        self.abort_calls = 0

    def current(self) -> CaptureState:
        suite = _suite()
        takes: list[TakeView] = []
        for index, prompt in enumerate(suite.prompts):
            kind = "room-tone" if prompt.category == "room-tone" else "utterance"
            label = "room-tone" if kind == "room-tone" else prompt.prompt_id
            relative = f"audio/{index:03d}-{label}.wav"
            if index < self.accepted or self.status == "finalized":
                take_status = "accepted"
                metrics = _metrics(relative)
                findings: tuple[str, ...] = ()
            elif index == self.index and self.status in {"recording", "review"}:
                take_status = self.status
                metrics = _metrics(relative) if self.status == "review" else None
                findings = ("peak-too-quiet",) if self.status == "review" else ()
            else:
                take_status = "pending"
                metrics = None
                findings = ()
            takes.append(
                TakeView(
                    index=index,
                    total=len(suite.prompts),
                    prompt=prompt,
                    kind=cast(Any, kind),
                    relative=relative,
                    status=cast(Any, take_status),
                    metrics=metrics,
                    findings=findings,
                )
            )

        current_take = takes[self.index] if self.index is not None else None
        finalized = self.status == "finalized"
        return CaptureState(
            session_directory=self.session_directory,
            session_id="console-test-session",
            status=cast(Any, self.status),
            index=self.index,
            total=len(takes),
            prompt=current_take.prompt if current_take is not None else None,
            kind=current_take.kind if current_take is not None else None,
            relative=current_take.relative if current_take is not None else None,
            metrics=current_take.metrics if current_take is not None else None,
            findings=current_take.findings if current_take is not None else (),
            elapsed_seconds=1.25 if self.status == "recording" else None,
            takes=tuple(takes),
            manifest_sha256="b" * 64 if finalized else None,
            qc_report_sha256="c" * 64 if finalized else None,
            ready=finalized,
        )

    def start_take(self) -> CaptureState:
        if self.status != "ready" or self.index is None:
            raise ContractError(f"cannot start while capture is {self.status}")
        self.status = "recording"
        return self.current()

    def stop_take(self) -> CaptureState:
        if self.status != "recording":
            raise ContractError("no recording is in progress")
        self.status = "review"
        return self.current()

    def accept_take(self) -> CaptureState:
        if self.status != "review" or self.index is None:
            raise ContractError("no reviewed take is available to accept")
        self.accepted += 1
        if self.accepted == len(_suite().prompts):
            self.status = "complete"
            self.index = None
        else:
            self.status = "ready"
            self.index = self.accepted
        return self.current()

    def reject_take(self, reason: str) -> CaptureState:
        if self.status != "review":
            raise ContractError("no reviewed take is available to reject")
        self.rejection_reasons.append(reason)
        self.status = "ready"
        return self.current()

    def preview_path(self, index: int | None = None) -> Path:
        if index is not None:
            raise AssertionError("the console previews only the reviewed take")
        if self.status != "review":
            raise ContractError("no reviewed take is available for preview")
        return self.preview

    def finalize(self) -> None:
        if self.status != "complete":
            raise ContractError(f"cannot finalize while capture is {self.status}")
        self.status = "finalized"

    def abort(self) -> None:
        self.abort_calls += 1


class _BlockingEngine(_FakeEngine):
    def __init__(self, session_directory: Path, preview_path: Path) -> None:
        super().__init__(session_directory, preview_path)
        self.entered = threading.Event()
        self.release = threading.Event()

    def start_take(self) -> CaptureState:
        self.entered.set()
        if not self.release.wait(timeout=5.0):
            raise AssertionError("test did not release the blocked capture operation")
        return super().start_take()


@dataclass(frozen=True, slots=True)
class _HttpResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        lowered = name.casefold()
        return next((value for key, value in self.headers if key.casefold() == lowered), None)

    def json(self) -> dict[str, Any]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return cast(dict[str, Any], value)


def _assert_security_headers(result: _HttpResult) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert result.header(name) == value


def _request(
    server: CaptureConsoleServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> _HttpResult:
    host, port = cast(tuple[str, int], server.server_address)
    connection = HTTPConnection(host, port, timeout=2.0)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = _HttpResult(response.status, tuple(response.getheaders()), response.read())
    finally:
        connection.close()
    _assert_security_headers(result)
    return result


def _raw_request(
    server: CaptureConsoleServer,
    method: str,
    path: str,
    headers: Sequence[tuple[str, str]],
    *,
    body: bytes = b"",
) -> _HttpResult:
    host, port = cast(tuple[str, int], server.server_address)
    connection = HTTPConnection(host, port, timeout=2.0)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        result = _HttpResult(response.status, tuple(response.getheaders()), response.read())
    finally:
        connection.close()
    _assert_security_headers(result)
    return result


def _api_headers(
    server: CaptureConsoleServer,
    *,
    content_type: bool = False,
    origin: bool = False,
) -> dict[str, str]:
    headers = {"X-Capture-Token": server.token}
    if origin:
        headers["Origin"] = server.expected_origin
    else:
        headers["Sec-Fetch-Site"] = "same-origin"
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _api_get(server: CaptureConsoleServer, path: str) -> _HttpResult:
    return _request(server, "GET", path, headers=_api_headers(server))


def _api_post(
    server: CaptureConsoleServer,
    path: str,
    value: object,
) -> _HttpResult:
    return _request(
        server,
        "POST",
        path,
        body=json.dumps(value).encode("utf-8"),
        headers=_api_headers(server, content_type=True),
    )


@contextmanager
def _running_server(
    engine: _FakeEngine | TakeEngine,
    *,
    token: str = TOKEN,
) -> Iterator[tuple[CaptureConsoleServer, threading.Thread]]:
    server = CaptureConsoleServer(cast(Any, engine), token=token)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield server, thread
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()
        assert not thread.is_alive()


def _fake_engine(tmp_path: Path, *, status: str = "ready") -> _FakeEngine:
    session = _private_directory(tmp_path / "fake-session")
    preview = session / "review.wav"
    _write_wav(preview, (-8_388_608, -256, 0, 256, 8_388_607))
    return _FakeEngine(session, preview, status=status)


def test_server_hard_binds_loopback_and_uses_a_fragment_token(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    server = CaptureConsoleServer(cast(Any, engine))
    try:
        host, port = cast(tuple[str, int], server.server_address)
        assert host == "127.0.0.1"
        assert port > 0
        assert re.fullmatch(r"[0-9a-f]{64}", server.token)
        assert server.url == f"http://127.0.0.1:{port}/#{server.token}"
        assert server.token not in server.url.split("#", maxsplit=1)[0]
        assert server.daemon_threads
    finally:
        server.server_close()


@pytest.mark.parametrize("token", ["", "a" * 63, "A" * 64, "g" * 64])
def test_server_rejects_non_256_bit_lowercase_hex_tokens(tmp_path: Path, token: str) -> None:
    engine = _fake_engine(tmp_path)
    with pytest.raises(ValueError, match="256-bit lowercase hexadecimal"):
        CaptureConsoleServer(cast(Any, engine), token=token)


def test_static_page_and_assets_ship_with_security_headers(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        page = _request(server, "GET", "/")
        script = _request(server, "GET", "/static/console.js")
        stylesheet = _request(server, "GET", "/static/console.css")

    assert page.status == 200
    assert page.header("Content-Type") == "text/html; charset=utf-8"
    assert b"capture" in page.body.lower()
    assert script.status == 200
    assert script.header("Content-Type") == "text/javascript; charset=utf-8"
    assert stylesheet.status == 200
    assert stylesheet.header("Content-Type") == "text/css; charset=utf-8"


def test_api_state_serializes_prompt_progress_metrics_and_elapsed_time(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        ready = _api_get(server, "/api/state")
        started = _api_post(server, "/api/take/start", {})
        reviewed = _api_post(server, "/api/take/stop", {})

    assert ready.status == 200
    ready_json = ready.json()
    assert ready_json["status"] == "ready"
    assert ready_json["index"] == 0
    assert ready_json["number"] == 1
    assert ready_json["total"] == 2
    assert ready_json["prompt"]["text"] == "Remain silent for room tone."
    assert ready_json["takes"][0]["status"] == "pending"
    assert ready_json["sync_command"] is None
    assert started.status == 200
    assert started.json()["status"] == "recording"
    assert started.json()["elapsed_seconds"] == 1.25
    assert reviewed.status == 200
    assert reviewed.json()["status"] == "review"
    assert reviewed.json()["metrics"]["peak_dbfs"] == -9.0
    assert reviewed.json()["findings"] == ["peak-too-quiet"]


def test_every_request_requires_exact_host_and_api_provenance(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        wrong_host = _request(
            server,
            "GET",
            "/",
            headers={"Host": f"localhost:{server.server_address[1]}"},
        )
        missing_host = _raw_request(server, "GET", "/", ())
        duplicate_host = _raw_request(
            server,
            "GET",
            "/",
            (("Host", server.expected_host), ("Host", server.expected_host)),
        )
        missing_token = _request(
            server,
            "GET",
            "/api/state",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        wrong_token = _request(
            server,
            "GET",
            "/api/state",
            headers={"X-Capture-Token": "b" * 64, "Sec-Fetch-Site": "same-origin"},
        )
        missing_provenance = _request(
            server,
            "GET",
            "/api/state",
            headers={"X-Capture-Token": server.token},
        )
        wrong_origin = _request(
            server,
            "GET",
            "/api/state",
            headers={
                "X-Capture-Token": server.token,
                "Origin": "http://attacker.invalid",
            },
        )
        cross_site = _request(
            server,
            "GET",
            "/api/state",
            headers={"X-Capture-Token": server.token, "Sec-Fetch-Site": "cross-site"},
        )
        exact_origin = _request(
            server,
            "GET",
            "/api/state",
            headers=_api_headers(server, origin=True),
        )

    assert wrong_host.status == 403
    assert missing_host.status == 403
    assert duplicate_host.status == 403
    assert missing_token.status == 403
    assert wrong_token.status == 403
    assert missing_provenance.status == 403
    assert wrong_origin.status == 403
    assert cross_site.status == 403
    assert exact_origin.status == 200


def test_duplicate_api_security_headers_fail_closed(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        common = (("Host", server.expected_host), ("Sec-Fetch-Site", "same-origin"))
        duplicate_token = _raw_request(
            server,
            "GET",
            "/api/state",
            (*common, ("X-Capture-Token", server.token), ("X-Capture-Token", server.token)),
        )
        duplicate_origin = _raw_request(
            server,
            "GET",
            "/api/state",
            (
                ("Host", server.expected_host),
                ("X-Capture-Token", server.token),
                ("Origin", server.expected_origin),
                ("Origin", server.expected_origin),
            ),
        )
        both_valid = _raw_request(
            server,
            "GET",
            "/api/state",
            (
                ("Host", server.expected_host),
                ("X-Capture-Token", server.token),
                ("Origin", server.expected_origin),
                ("Sec-Fetch-Site", "same-origin"),
            ),
        )

    assert duplicate_token.status == 403
    assert duplicate_origin.status == 403
    assert both_valid.status == 200


@pytest.mark.parametrize(
    "headers",
    [
        (("Host", "\xff"),),
        (
            ("Host", "127.0.0.1:unused"),
            ("X-Capture-Token", "\xff"),
            ("Sec-Fetch-Site", "same-origin"),
        ),
        (
            ("Host", "127.0.0.1:unused"),
            ("X-Capture-Token", TOKEN),
            ("Origin", "http://127.0.0.1:\xff"),
        ),
    ],
)
def test_non_ascii_security_headers_fail_closed(
    tmp_path: Path,
    headers: tuple[tuple[str, str], ...],
) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        resolved_headers = tuple(
            (name, server.expected_host if value == "127.0.0.1:unused" else value)
            for name, value in headers
        )
        result = _raw_request(server, "GET", "/api/state", resolved_headers)

    assert result.status == 403
    assert result.json() == {
        "error": (
            "request Host is not the bound loopback authority"
            if headers[0][1] == "\xff"
            else "capture API authorization failed"
        )
    }


def test_http_0_9_request_is_upgraded_to_a_secured_rejection(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        host, port = cast(tuple[str, int], server.server_address)
        with socket.create_connection((host, port), timeout=2.0) as connection:
            connection.sendall(b"GET /\r\n")
            connection.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while chunk := connection.recv(65_536):
                chunks.append(chunk)

    response = b"".join(chunks)
    assert response.startswith(b"HTTP/1.1 403 ")
    header_block = response.split(b"\r\n\r\n", maxsplit=1)[0].decode("ascii")
    for name, value in SECURITY_HEADERS.items():
        assert f"{name}: {value}" in header_block


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("POST", "/api/state", "GET"),
        ("GET", "/api/take/start", "POST"),
        ("HEAD", "/", "GET"),
        ("PUT", "/api/state", "GET"),
        ("PATCH", "/api/take/start", "POST"),
        ("DELETE", "/api/state", "GET"),
        ("OPTIONS", "/api/state", "GET"),
    ],
)
def test_known_routes_reject_other_methods(
    tmp_path: Path,
    method: str,
    path: str,
    allowed: str,
) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        headers = _api_headers(server, content_type=method == "POST")
        result = _request(
            server,
            method,
            path,
            body=b"{}" if method == "POST" else None,
            headers=headers,
        )

    assert result.status == 405
    assert result.header("Allow") == allowed


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "BREW"])
def test_trace_connect_and_unknown_methods_are_guarded_405(
    tmp_path: Path,
    method: str,
) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        result = _request(server, method, "/api/state", headers=_api_headers(server))

    assert result.status == 405
    assert "error" in result.json()


def test_default_unknown_method_still_enforces_host_and_api_authorization(
    tmp_path: Path,
) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        missing_host = _raw_request(server, "BREW", "/api/state", ())
        missing_token = _request(
            server,
            "BREW",
            "/api/state",
            headers={"Sec-Fetch-Site": "same-origin"},
        )

    assert missing_host.status == 403
    assert missing_token.status == 403


def test_unknown_routes_return_json_404(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        public = _request(server, "GET", "/not-found")
        api = _request(
            server,
            "POST",
            "/api/not-found",
            body=b"{}",
            headers=_api_headers(server, content_type=True),
        )

    assert public.status == 404
    assert api.status == 404
    assert "error" in public.json()
    assert "error" in api.json()


@pytest.mark.parametrize(
    ("body", "content_type", "expected_status"),
    [
        (b"{}", None, 415),
        (b"{}", "text/plain", 415),
        (b"{}", "application/json; charset=utf-8", 415),
        (b"{", "application/json", 400),
        (b"\xff", "application/json", 400),
        (b"[]", "application/json", 400),
        (b'{"unexpected":true}', "application/json", 400),
    ],
)
def test_post_requires_exact_bounded_json_object(
    tmp_path: Path,
    body: bytes,
    content_type: str | None,
    expected_status: int,
) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        headers = _api_headers(server)
        if content_type is not None:
            headers["Content-Type"] = content_type
        result = _request(server, "POST", "/api/take/start", body=body, headers=headers)

    assert result.status == expected_status
    assert "error" in result.json()
    assert engine.status == "ready"


def test_post_rejects_ambiguous_length_transfer_encoding_and_oversize_body(
    tmp_path: Path,
) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        common = (
            ("Host", server.expected_host),
            ("X-Capture-Token", server.token),
            ("Sec-Fetch-Site", "same-origin"),
            ("Content-Type", "application/json"),
        )
        missing_length = _raw_request(
            server,
            "POST",
            "/api/take/start",
            common,
            body=b"{}",
        )
        duplicate_length = _raw_request(
            server,
            "POST",
            "/api/take/start",
            (*common, ("Content-Length", "2"), ("Content-Length", "2")),
            body=b"{}",
        )
        transfer_encoded = _raw_request(
            server,
            "POST",
            "/api/take/start",
            (*common, ("Transfer-Encoding", "chunked")),
            body=b"0\r\n\r\n",
        )
        oversized_decimal = _raw_request(
            server,
            "POST",
            "/api/take/start",
            (*common, ("Content-Length", "9" * 5_000)),
        )
        oversized = _request(
            server,
            "POST",
            "/api/take/start",
            body=b" " * (64 * 1024 + 1),
            headers=_api_headers(server, content_type=True),
        )

    assert missing_length.status == 400
    assert duplicate_length.status == 400
    assert transfer_encoded.status == 400
    assert oversized_decimal.status == 413
    assert oversized.status == 413
    assert engine.status == "ready"


@pytest.mark.parametrize(
    "value",
    [{}, {"reason": 7}, {"reason": "retry", "extra": True}],
)
def test_retake_requires_exactly_one_string_reason(tmp_path: Path, value: object) -> None:
    engine = _fake_engine(tmp_path, status="review")
    with _running_server(engine) as (server, _thread):
        result = _api_post(server, "/api/take/retake", value)

    assert result.status == 400
    assert engine.status == "review"
    assert engine.rejection_reasons == []


def test_state_transitions_and_conflicts_are_explicit(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    with _running_server(engine) as (server, _thread):
        invalid_stop = _api_post(server, "/api/take/stop", {})
        assert invalid_stop.status == 409

        assert _api_post(server, "/api/take/start", {}).json()["status"] == "recording"
        assert _api_post(server, "/api/take/start", {}).status == 409
        assert _api_post(server, "/api/take/stop", {}).json()["status"] == "review"
        assert _api_post(server, "/api/take/accept", {}).json()["index"] == 1
        assert _api_post(server, "/api/take/retake", {"reason": "too late"}).status == 409

        _api_post(server, "/api/take/start", {})
        _api_post(server, "/api/take/stop", {})
        retaken = _api_post(server, "/api/take/retake", {"reason": "pilot requested retake"})

    assert retaken.status == 200
    assert retaken.json()["status"] == "ready"
    assert retaken.json()["index"] == 1
    assert engine.rejection_reasons == ["pilot requested retake"]


def test_concurrent_engine_operation_answers_conflict_without_waiting(tmp_path: Path) -> None:
    session = _private_directory(tmp_path / "blocking-session")
    preview = session / "review.wav"
    _write_wav(preview, (0, 256))
    engine = _BlockingEngine(session, preview)
    first_result: list[_HttpResult] = []
    first_done = threading.Event()
    second_result: list[_HttpResult] = []
    second_done = threading.Event()

    with _running_server(engine) as (server, _server_thread):

        def first_request() -> None:
            first_result.append(_api_post(server, "/api/take/start", {}))
            first_done.set()

        def second_request() -> None:
            second_result.append(_api_post(server, "/api/take/stop", {}))
            second_done.set()

        first_thread = threading.Thread(target=first_request)
        second_thread = threading.Thread(target=second_request)
        first_thread.start()
        assert engine.entered.wait(timeout=2.0)
        second_thread.start()
        try:
            assert second_done.wait(timeout=1.0), "concurrent request blocked behind engine lock"
        finally:
            engine.release.set()
            first_thread.join(timeout=2.0)
            second_thread.join(timeout=2.0)

    assert first_done.is_set()
    assert first_result[0].status == 200
    assert second_result[0].status == 409


def test_preview_is_16_bit_mono_48k_in_memory_and_source_is_unchanged(
    tmp_path: Path,
) -> None:
    engine = _fake_engine(tmp_path, status="review")
    source_before = engine.preview.read_bytes()
    files_before = tuple(sorted(path.name for path in engine.session_directory.iterdir()))

    with _running_server(engine) as (server, _thread):
        result = _api_get(server, "/api/take/preview")

    assert result.status == 200
    assert result.header("Content-Type") == "audio/wav"
    assert result.body != source_before
    with wave.open(io.BytesIO(result.body), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 48_000
        assert reader.getcomptype() == "NONE"
        assert reader.getnframes() == 5
        frames = reader.readframes(reader.getnframes())
    converted = [
        int.from_bytes(frames[offset : offset + 2], "little", signed=True)
        for offset in range(0, len(frames), 2)
    ]
    assert converted == [-32_768, -1, 0, 1, 32_767]
    assert engine.preview.read_bytes() == source_before
    assert tuple(sorted(path.name for path in engine.session_directory.iterdir())) == files_before


def test_preview_rejects_noncanonical_or_symlink_sources(tmp_path: Path) -> None:
    session = _private_directory(tmp_path / "invalid-preview-session")
    invalid = session / "invalid.wav"
    _write_wav(invalid, (-32_768, 0, 32_767), sample_width=2)
    engine = _FakeEngine(session, invalid, status="review")
    with _running_server(engine) as (server, _thread):
        wrong_format = _api_get(server, "/api/take/preview")
        link = session / "linked.wav"
        link.symlink_to(invalid)
        engine.preview = link
        symlink = _api_get(server, "/api/take/preview")

    assert wrong_format.status == 409
    assert symlink.status == 409
    assert wrong_format.header("Content-Type") == "application/json; charset=utf-8"
    assert "error" in wrong_format.json()


def test_real_engine_finalizes_ready_then_allows_shutdown(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "voice-assets")
    suite = _suite()
    engine = TakeEngine(
        layout=layout,
        prompt_suite=suite,
        subject_id="cole",
        device=_device(),
        session_id="console-real-finalize",
        ffmpeg="/test/ffmpeg",
        process_factory=lambda _command: pytest.fail("finalization must not capture hardware"),
        clock=lambda: NOW,
        monotonic=lambda: 42.0,
        capture_host="console-test-host",
    )
    for index, relative in enumerate(expected_recording_paths(suite)):
        values = (100, -100) * 480 if index == 0 else (2_000_000, -2_000_000) * 480
        _write_wav(engine.session_directory / relative, values)

    assert engine.current().status == "complete"
    assert not (engine.session_directory / "manifest.json").exists()
    assert not (engine.session_directory / "qc.json").exists()
    assert not (engine.session_directory / "READY").exists()

    with _running_server(engine) as (server, server_thread):
        before_finalize_shutdown = _api_post(server, "/api/shutdown", {})
        finalized = _api_post(server, "/api/finalize", {})
        shutdown = _api_post(server, "/api/shutdown", {})
        server_thread.join(timeout=2.0)

    assert before_finalize_shutdown.status == 409
    assert finalized.status == 200
    finalized_json = finalized.json()
    assert finalized_json["status"] == "finalized"
    assert finalized_json["ready"] is True
    assert finalized_json["manifest_sha256"] == engine.finalize().manifest_sha256
    assert finalized_json["qc_report_sha256"] == engine.finalize().qc_report_sha256
    assert finalized_json["sync_command"] == (
        f"voice-lab sync plan {engine.session_directory} --remote-user MINI_USER "
        "--remote-root /Users/MINI_USER/.local/share/cole-voice-lab/incoming --live-plan --json"
    )
    assert shutdown.status == 200
    assert not server_thread.is_alive()
    assert (engine.session_directory / "manifest.json").is_file()
    assert (engine.session_directory / "qc.json").is_file()
    assert (engine.session_directory / "READY").is_file()
    assert json.loads((engine.session_directory / "manifest.json").read_bytes())["captured_at"] == (
        "2026-09-02T18:30:00Z"
    )
    assert json.loads((engine.session_directory / "READY").read_bytes())["finalized_at"] == (
        "2026-09-02T18:30:00Z"
    )


def test_filesystem_errors_are_reported_without_details(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)

    def fail_current() -> CaptureState:
        raise OSError("sensitive local path")

    engine.current = fail_current  # type: ignore[method-assign]
    with _running_server(engine) as (server, _thread):
        result = _api_get(server, "/api/state")

    assert result.status == 409
    assert result.json() == {"error": "capture filesystem operation failed"}
    assert b"sensitive local path" not in result.body


def test_missing_static_resource_is_a_guarded_server_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _fake_engine(tmp_path)
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(console_module, "resource_path", lambda _name: missing)
    with _running_server(engine) as (server, _thread):
        result = _request(server, "GET", "/")

    assert result.status == 500
    assert result.json() == {"error": "console resource is unavailable"}


@pytest.mark.parametrize("open_browser", [False, True])
def test_serve_capture_console_emits_url_once_and_optionally_opens_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    open_browser: bool,
) -> None:
    engine = _fake_engine(tmp_path)
    emitted: list[str] = []
    opened: list[str] = []

    class FakeServer:
        url = f"http://127.0.0.1:32123/#{TOKEN}"

        def __init__(self, selected_engine: object) -> None:
            assert selected_engine is engine
            self.serve_calls: list[float] = []
            self.closed = False

        def serve_forever(self, *, poll_interval: float) -> None:
            self.serve_calls.append(poll_interval)

        def server_close(self) -> None:
            self.closed = True

    server = FakeServer(engine)
    monkeypatch.setattr(console_module, "CaptureConsoleServer", lambda _engine: server)

    console_module.serve_capture_console(
        cast(Any, engine),
        open_browser=open_browser,
        emit=emitted.append,
        browser_open=lambda url: opened.append(url),
    )

    assert emitted == [server.url]
    assert opened == ([server.url] if open_browser else [])
    assert server.serve_calls == [0.1]
    assert server.closed


def test_serve_capture_console_aborts_and_propagates_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _fake_engine(tmp_path)

    class InterruptingServer:
        url = f"http://127.0.0.1:32123/#{TOKEN}"

        def __init__(self) -> None:
            self.closed = False

        def serve_forever(self, *, poll_interval: float) -> None:
            assert poll_interval == 0.1
            raise KeyboardInterrupt

        def server_close(self) -> None:
            self.closed = True

    server = InterruptingServer()
    monkeypatch.setattr(console_module, "CaptureConsoleServer", lambda _engine: server)

    with pytest.raises(KeyboardInterrupt):
        console_module.serve_capture_console(
            cast(Any, engine),
            open_browser=False,
            emit=lambda _url: None,
            browser_open=lambda _url: pytest.fail("browser must not open"),
        )

    assert engine.abort_calls == 1
    assert server.closed
