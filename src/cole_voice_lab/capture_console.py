"""Authenticated loopback controller for foreground native voice capture."""

from __future__ import annotations

import hmac
import io
import json
import secrets
import shlex
import threading
import wave
import webbrowser
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from .canonical import open_regular_no_follow
from .capture import CaptureState, TakeEngine
from .errors import ContractError, VoiceLabError
from .resources import resource_path

MAX_JSON_BODY_BYTES = 64 * 1024
LOOPBACK_HOST = "127.0.0.1"

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "media-src blob:; img-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
)

_STATIC_RESOURCES: dict[str, tuple[str, str]] = {
    "/": ("capture-console.html", "text/html; charset=utf-8"),
    "/static/console.js": ("capture-console.js", "text/javascript; charset=utf-8"),
    "/static/console.css": ("capture-console.css", "text/css; charset=utf-8"),
}
_GET_API_PATHS = {"/api/state", "/api/take/preview"}
_POST_API_PATHS = {
    "/api/take/start",
    "/api/take/stop",
    "/api/take/accept",
    "/api/take/retake",
    "/api/finalize",
    "/api/shutdown",
}
_KNOWN_PATHS = set(_STATIC_RESOURCES) | _GET_API_PATHS | _POST_API_PATHS


def _ascii_digest_equal(value: str, expected: str) -> bool:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(encoded, expected.encode("ascii"))


def _metric_payload(metric: object | None) -> object | None:
    if metric is None:
        return None
    model_dump = getattr(metric, "model_dump", None)
    if not callable(model_dump):
        raise ContractError("capture metrics are not serializable")
    return cast(object, model_dump(mode="json"))


def capture_state_payload(state: CaptureState) -> dict[str, object]:
    """Convert the typed engine state into the versionless local UI payload."""

    prompt = state.prompt.model_dump(mode="json") if state.prompt is not None else None
    takes = [
        {
            "index": take.index,
            "number": take.index + 1,
            "total": take.total,
            "prompt": take.prompt.model_dump(mode="json"),
            "kind": take.kind,
            "relative": take.relative,
            "status": take.status,
            "metrics": _metric_payload(take.metrics),
            "findings": list(take.findings),
        }
        for take in state.takes
    ]
    sync_command: str | None = None
    if state.ready:
        sync_command = shlex.join(
            (
                "voice-lab",
                "sync",
                "plan",
                str(state.session_directory.resolve(strict=True)),
                "--live-plan",
                "--json",
            )
        )
    return {
        "session_id": state.session_id,
        "session_directory": str(state.session_directory),
        "status": state.status,
        "index": state.index,
        "number": state.index + 1 if state.index is not None else None,
        "total": state.total,
        "prompt": prompt,
        "kind": state.kind,
        "relative": state.relative,
        "metrics": _metric_payload(state.metrics),
        "findings": list(state.findings),
        "elapsed_seconds": state.elapsed_seconds,
        "takes": takes,
        "manifest_sha256": state.manifest_sha256,
        "qc_report_sha256": state.qc_report_sha256,
        "ready": state.ready,
        "sync_command": sync_command,
    }


def preview_wav_bytes(path: Path) -> bytes:
    """Build a mono 48 kHz 16-bit preview in memory from canonical 24-bit PCM."""

    output = io.BytesIO()
    try:
        with open_regular_no_follow(path) as source:
            with wave.open(source, "rb") as reader:
                if (
                    reader.getnchannels() != 1
                    or reader.getframerate() != 48_000
                    or reader.getsampwidth() != 3
                    or reader.getcomptype() != "NONE"
                ):
                    raise ContractError("preview source must be mono 48000 Hz 24-bit PCM WAV")
                with wave.open(output, "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(48_000)
                    while payload := reader.readframes(65_536):
                        if len(payload) % 3:
                            raise ContractError("preview source ends inside a 24-bit PCM sample")
                        converted = bytearray((len(payload) // 3) * 2)
                        target = 0
                        for offset in range(0, len(payload), 3):
                            converted[target] = payload[offset + 1]
                            converted[target + 1] = payload[offset + 2]
                            target += 2
                        writer.writeframesraw(converted)
    except (EOFError, wave.Error) as error:
        raise ContractError(f"invalid preview WAV: {error}") from error
    return output.getvalue()


class CaptureConsoleServer(ThreadingHTTPServer):
    """One loopback-only, per-run authenticated capture console."""

    daemon_threads = True

    def __init__(self, engine: TakeEngine, *, token: str | None = None) -> None:
        selected_token = secrets.token_hex(32) if token is None else token
        if len(selected_token) != 64 or any(
            character not in "0123456789abcdef" for character in selected_token
        ):
            raise ValueError("capture-console token must be 256-bit lowercase hexadecimal")
        self.engine = engine
        self.token = selected_token
        self.operation_lock = threading.Lock()
        self.shutdown_requested = False
        super().__init__((LOOPBACK_HOST, 0), _CaptureConsoleHandler)
        bound_host, bound_port = cast(tuple[str, int], self.server_address)
        if bound_host != LOOPBACK_HOST:
            self.server_close()
            raise RuntimeError("capture console did not bind the IPv4 loopback address")
        self.expected_host = f"{LOOPBACK_HOST}:{bound_port}"
        self.expected_origin = f"http://{self.expected_host}"
        self.url = f"{self.expected_origin}/#{self.token}"


class _CaptureConsoleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del explain
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._dispatch(self.command)
            return
        status = HTTPStatus(code)
        self._error(status, message or status.phrase)

    @property
    def console(self) -> CaptureConsoleServer:
        return cast(CaptureConsoleServer, self.server)

    def _respond(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
            self.close_connection = True
        self.send_response_only(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        if extra_headers is not None:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json_response(
        self,
        status: HTTPStatus,
        value: object,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._respond(
            status,
            payload,
            content_type="application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json_response(status, {"error": message})

    def _host_is_allowed(self) -> bool:
        values = self.headers.get_all("Host", failobj=[])
        return len(values) == 1 and _ascii_digest_equal(values[0], self.console.expected_host)

    def _api_is_authorized(self) -> bool:
        tokens = self.headers.get_all("X-Capture-Token", failobj=[])
        if len(tokens) != 1 or not _ascii_digest_equal(tokens[0], self.console.token):
            return False
        origins = self.headers.get_all("Origin", failobj=[])
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", failobj=[])
        if len(origins) > 1 or len(fetch_sites) > 1:
            return False
        if origins and not _ascii_digest_equal(origins[0], self.console.expected_origin):
            return False
        if fetch_sites and fetch_sites[0] != "same-origin":
            return False
        return bool(origins or fetch_sites)

    def _read_json_body(self) -> dict[str, Any] | None:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, "transfer-encoded request bodies are forbidden")
            return None
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if len(content_types) != 1 or content_types[0].strip().lower() != "application/json":
            self.close_connection = True
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "POST body must be application/json")
            return None
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, "one decimal Content-Length is required")
            return None
        normalized_length = lengths[0].lstrip("0") or "0"
        maximum_length = str(MAX_JSON_BODY_BYTES)
        if len(normalized_length) > len(maximum_length) or (
            len(normalized_length) == len(maximum_length) and normalized_length > maximum_length
        ):
            self.close_connection = True
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "POST body exceeds 64 KiB")
            return None
        length = int(normalized_length)
        payload = self.rfile.read(length)
        if len(payload) != length:
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, "POST body ended before Content-Length")
            return None
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "POST body must be one UTF-8 JSON object")
            return None
        if not isinstance(value, dict):
            self._error(HTTPStatus.BAD_REQUEST, "POST body must be one JSON object")
            return None
        return cast(dict[str, Any], value)

    def _with_engine(self, callback: Callable[[], None]) -> None:
        if not self.console.operation_lock.acquire(blocking=False):
            self._error(HTTPStatus.CONFLICT, "another capture operation is in progress")
            return
        try:
            callback()
        except VoiceLabError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
        except OSError:
            self._error(HTTPStatus.CONFLICT, "capture filesystem operation failed")
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "capture console operation failed")
        finally:
            self.console.operation_lock.release()

    def _state(self) -> None:
        self._json_response(
            HTTPStatus.OK,
            capture_state_payload(self.console.engine.current()),
        )

    def _preview(self) -> None:
        payload = preview_wav_bytes(self.console.engine.preview_path())
        self._respond(HTTPStatus.OK, payload, content_type="audio/wav")

    def _post_operation(self, body: dict[str, Any]) -> None:
        path = self.path
        if path == "/api/take/retake":
            if set(body) != {"reason"} or not isinstance(body.get("reason"), str):
                self._error(HTTPStatus.BAD_REQUEST, "retake requires one string reason")
                return
        elif body:
            self._error(HTTPStatus.BAD_REQUEST, "this operation requires an empty JSON object")
            return

        if path == "/api/take/start":
            self.console.engine.start_take()
        elif path == "/api/take/stop":
            self.console.engine.stop_take()
        elif path == "/api/take/accept":
            self.console.engine.accept_take()
        elif path == "/api/take/retake":
            self.console.engine.reject_take(cast(str, body["reason"]))
        elif path == "/api/finalize":
            self.console.engine.finalize()
        elif path == "/api/shutdown":
            state = self.console.engine.current()
            if not state.ready or state.status != "finalized":
                raise ContractError("capture console may shut down only after finalization")
            if self.console.shutdown_requested:
                raise ContractError("capture console shutdown is already in progress")
            self.console.shutdown_requested = True
        if path == "/api/shutdown":
            try:
                self._state()
            finally:
                threading.Thread(target=self.console.shutdown, daemon=True).start()
        else:
            self._state()

    def _dispatch(self, method: str) -> None:
        if not self._host_is_allowed():
            self._error(HTTPStatus.FORBIDDEN, "request Host is not the bound loopback authority")
            return
        is_api = self.path.startswith("/api/")
        if is_api and not self._api_is_authorized():
            self._error(HTTPStatus.FORBIDDEN, "capture API authorization failed")
            return
        if self.path not in _KNOWN_PATHS:
            self._error(HTTPStatus.NOT_FOUND, "capture-console path not found")
            return
        allowed_method = (
            (self.path in _STATIC_RESOURCES or self.path in _GET_API_PATHS) and method == "GET"
        ) or (self.path in _POST_API_PATHS and method == "POST")
        if not allowed_method:
            allow = (
                "GET" if self.path in _STATIC_RESOURCES or self.path in _GET_API_PATHS else "POST"
            )
            self._json_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method not allowed"},
                extra_headers={"Allow": allow},
            )
            return
        if self.path in _STATIC_RESOURCES:
            name, content_type = _STATIC_RESOURCES[self.path]
            try:
                payload = resource_path(name).read_bytes()
            except OSError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "console resource is unavailable")
                return
            self._respond(HTTPStatus.OK, payload, content_type=content_type)
            return
        if method == "GET":
            operation = self._state if self.path == "/api/state" else self._preview
            self._with_engine(operation)
            return
        body = self._read_json_body()
        if body is not None:
            self._with_engine(lambda: self._post_operation(body))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def do_TRACE(self) -> None:
        self._dispatch("TRACE")

    def do_CONNECT(self) -> None:
        self._dispatch("CONNECT")


def serve_capture_console(
    engine: TakeEngine,
    *,
    open_browser: bool,
    emit: Callable[[str], None] = print,
    browser_open: Callable[[str], object] = webbrowser.open,
) -> None:
    """Print one tokenized URL and serve until finalized shutdown or Ctrl-C."""

    server = CaptureConsoleServer(engine)
    try:
        emit(server.url)
        if open_browser:
            browser_open(server.url)
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        engine.abort()
        raise
    finally:
        server.server_close()
