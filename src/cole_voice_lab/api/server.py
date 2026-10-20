"""The ``/api/v1`` HTTP server: standard library, strict models, stable refusals (D-031).

@canonical api.server -- a ``ThreadingHTTPServer`` in the voice-lab container, behind the
tailnet sidecar and the Pocket ID door. Phase 2 serves:

- ``GET /healthz`` (no token; the door lets it through for the stack's health check);
- ``GET /api/v1/meta``;
- ``POST /api/v1/intake/sessions``, ``GET …/{id}``, ``PUT …/{id}/files/{path}`` (the path
  percent-encoded as one segment, with ``Content-Range``), ``POST …/{id}/finalize``,
  ``GET …/{id}/receipt``;
- phase 3's compatibility path: voice-journey's ``/api/intake/…``, answered by
  ``journey_intake.JourneyIntake`` in voice-journey's shapes (it checks the bearer itself).
- whatever its ``routes`` answer (``api/routing.py``): the journey routes
  (``journey_routes.py``, ``serve --journey``), after the bearer check for ``/api/``,
  and the read-only web view under ``/view/`` (``view/routes.py``), which checks the
  door's forwarded identity or a bearer token itself;
- with ``--speech-config``, ``/api/v1/speech`` (``speech/routes.py``, D-046): a
  self-authenticating prefix, whose route checks the registered callers' machine tokens
  itself instead of the desktop token check below.

Every other ``/api/`` request needs a bearer token that ``TokenVerifier`` accepts. Bodies are
bounded; JSON is validated as its contract; every refusal is ``ApiRefusal`` with a stable
code; responses are canonical JSON with ``Cache-Control: no-store``. The access log names
the method, the route, the status and the time, never a header or a token.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Final, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, ValidationError

from ..canonical import canonical_json_bytes
from ..errors import VoiceLabError
from ..models import ContractModel
from ..paths import AssetLayout
from ..speech.config import load_speech_config
from ..speech.routes import SpeechRoutes, caller_policy
from ..view.routes import ViewRoutes
from .auth import AuthError, AuthPolicy, Scope, TokenVerifier, TrustedIssuer
from .client_scope import CLIENT_PROFILE, require_client_may
from .contracts import (
    ApiErrorCode,
    ApiMeta,
    ApiRefusal,
    ChunkAnswer,
    IntakeSessionRequest,
    validate_package_path,
)
from .corpus_routes import CorpusRoutes
from .journey_intake import PATH_PREFIX as JOURNEY_INTAKE_PREFIX
from .journey_intake import JourneyIntake
from .journey_routes import add_arguments as add_journey_arguments
from .journey_routes import routes_from_options as journey_routes_from_options
from .routing import Route, RouteRefusal, RouteRequest, RouteResponse
from .staging import IntakeStaging, StagingError

MAX_JSON_BODY_BYTES: Final = 1024 * 1024
MAX_CHUNK_BYTES: Final = 64 * 1024 * 1024
DEFAULT_PORT: Final = 8080
_SESSIONS: Final = "/api/v1/intake/sessions"
_SESSION_ROUTE = re.compile(r"^/api/v1/intake/sessions/(?P<id>[^/]+)(?:/(?P<rest>.+))?$")
_CONTENT_RANGE = re.compile(r"^bytes (?P<start>\d{1,15})-(?P<end>\d{1,15})/(?P<total>\d{1,15})$")
_EMPTY_RANGE = re.compile(r"^bytes \*/(?P<total>\d{1,15})$")


class ApiServerConfig(ContractModel):
    """The server's whole configuration, read from one file (a container secret)."""

    schema_version: Literal["cole-voice-lab.api.server-config/v1"] = (
        "cole-voice-lab.api.server-config/v1"
    )
    asset_root: str = Field(min_length=1, max_length=4096)
    server_build: str = Field(min_length=1, max_length=200)
    minimum_client_version: str = Field(default="0.1.0", min_length=1, max_length=64)
    auth: AuthPolicy


class ApiServer(ThreadingHTTPServer):
    """The server, holding the staging, the token verifier and the meta answer."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        staging: IntakeStaging,
        verifier: TokenVerifier,
        meta: ApiMeta,
        log: bool = True,
        journey_intake: JourneyIntake | None = None,
        routes: Sequence[Route] = (),
        self_authenticating: tuple[str, ...] = (),
    ) -> None:
        self.staging = staging
        self.self_authenticating = self_authenticating
        self.journey_intake = journey_intake
        self.verifier = verifier
        self.meta = meta
        self.log = log
        self.routes = tuple(routes)
        super().__init__(address, _ApiHandler)


class _Refusal(Exception):
    def __init__(
        self,
        status: int,
        code: ApiErrorCode,
        detail: str,
        received_bytes: int | None = None,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code: ApiErrorCode = code
        self.detail = detail
        self.received_bytes = received_bytes
        self.headers = headers


class _Body(io.RawIOBase):
    """The request body, counting what is read so a refusal can drain exactly the rest."""

    def __init__(self, handler: _ApiHandler) -> None:
        super().__init__()
        self._handler = handler

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        remaining = self._handler._unread
        if remaining <= 0:
            return b""
        wanted = remaining if size < 0 else min(size, remaining)
        data = self._handler.rfile.read(wanted)
        self._handler._unread -= len(data)
        return data


def _close_response(response: RouteResponse) -> None:
    """Close what a response that will never be sent holds."""
    try:
        if response.stream is not None:
            response.stream.close()
    finally:
        close = getattr(response.events, "close", None)
        if close is not None:
            close()


class _ApiHandler(BaseHTTPRequestHandler):
    server: ApiServer
    server_version = "voice-lab-api"
    sys_version = ""
    _route = "-"
    _started = 0.0
    _unread = 0
    #: The verified token's scope for the request in hand (``_authorize``).
    _scope: Scope = "full"

    # -- plumbing ---------------------------------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (base signature)
        """Silence the base class's log, which quotes the request line."""

    def _access_log(self, status: int) -> None:
        if self.server.log:
            elapsed = (time.monotonic() - self._started) * 1000
            sys.stderr.write(f"api {self.command} {self._route} {status} {elapsed:.0f}ms\n")

    def _send(
        self, status: int, model: BaseModel, headers: tuple[tuple[str, str], ...] = ()
    ) -> None:
        body = canonical_json_bytes(model) + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._access_log(status)

    def _refuse(self, refusal: _Refusal) -> None:
        if self._unread > MAX_CHUNK_BYTES:
            # Too much to drain: answer, and close the connection on the unread body.
            self.close_connection = True
        else:
            self._discard_body()
        self._send(
            refusal.status,
            ApiRefusal(
                error=refusal.code,
                detail=refusal.detail,
                received_bytes=refusal.received_bytes,
            ),
            refusal.headers,
        )

    def _content_length(self) -> int | None:
        value = self.headers.get("Content-Length")
        if value is None:
            return None
        if not value.isdigit() or len(value) > 15:
            raise _Refusal(400, "bad_request", "Content-Length is not a byte count")
        return int(value)

    def _discard_body(self) -> None:
        """Read what is left of a refused request's body so the client sees the answer."""
        body = _Body(self)
        while body.read(1024 * 1024):
            pass

    def _read_json[ModelT: BaseModel](self, model: type[ModelT]) -> ModelT:
        length = self._content_length()
        if length is None:
            raise _Refusal(411, "content_length_required", "a JSON body needs Content-Length")
        if length > MAX_JSON_BODY_BYTES:
            raise _Refusal(413, "body_too_large", f"a JSON body is at most {MAX_JSON_BODY_BYTES}")
        data = _Body(self).read(length)
        try:
            return model.model_validate_json(data)
        except ValidationError as error:
            raise _Refusal(
                422, "invalid_request", f"the body is not a valid {model.__name__}"
            ) from error

    def _authorize(self, path: str, query: str) -> None:
        """The global ``/api/`` check: any verified token, a corpus client's only on its
        allow-list (``api.client_scope``, D-056); the scope is kept for the routes.

        Raises: ``_Refusal`` 401 ``unauthorized``; ``RouteRefusal`` 403 ``client_scope``."""
        try:
            principal = self.server.verifier.verify_any_header(self.headers.get("Authorization"))
        except AuthError as error:
            raise _Refusal(401, "unauthorized", str(error)) from error
        self._scope = principal.scope
        if principal.scope == "corpus-client":
            require_client_may(self.command, path, query)

    def _dispatch(self) -> None:
        self._started = time.monotonic()
        self._unread = self._content_length_or_zero()
        # One handler serves every request of a keep-alive connection: no scope carries over.
        self._scope = "full"
        try:
            self._handle()
        except _Refusal as refusal:
            self._refuse(refusal)
        except StagingError as error:
            self._refuse(_Refusal(error.status, error.code, error.detail, error.received_bytes))
        except RouteRefusal as refusal:
            self._refuse(
                _Refusal(refusal.status, refusal.code, refusal.detail, headers=refusal.headers)
            )
        except Exception as error:  # noqa: BLE001 (any other failure is the server's)
            self._refuse(_Refusal(500, "internal_error", type(error).__name__))

    def _content_length_or_zero(self) -> int:
        try:
            return self._content_length() or 0
        except _Refusal:
            return 0

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's naming)
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    # -- routing ----------------------------------------------------------------------------

    def _handle(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/healthz":
            self._route = "healthz"
            self._only("GET", "HEAD")
            self._send(200, _Health())
            return
        api = path.startswith("/api/")
        if (
            api
            and path.startswith(JOURNEY_INTAKE_PREFIX)
            and self.server.journey_intake is not None
        ):
            self._journey_intake(self.server.journey_intake, path)
            return
        if api and not self._self_authenticating(path):
            self._authorize(path, parts.query)
        if self._extension(parts.path, parts.query):
            return
        if not api:
            raise _Refusal(404, "not_found", "no such route")
        if path == "/api/v1/meta":
            self._route = "meta"
            self._only("GET")
            self._send(200, self.server.meta)
            return
        if path == _SESSIONS:
            self._route = "intake.open"
            self._only("POST")
            request = self._read_json(IntakeSessionRequest)
            status, created = self.server.staging.open_session(request)
            self._send(201 if created else 200, status)
            return
        match = _SESSION_ROUTE.fullmatch(path)
        if match is None:
            raise _Refusal(404, "not_found", "no such route")
        session_id, rest = match["id"], match["rest"]
        staging = self.server.staging
        if rest is None:
            self._route = "intake.status"
            self._only("GET")
            self._send(200, staging.status(session_id))
        elif rest == "finalize":
            self._route = "intake.finalize"
            self._only("POST")
            # A corpus client finalizes only a v2 take of its own profile (D-056).
            profile = CLIENT_PROFILE if self._scope == "corpus-client" else None
            self._send(200, staging.finalize(session_id, profile=profile))
        elif rest == "receipt":
            self._route = "intake.receipt"
            self._only("GET")
            self._send(200, staging.receipt(session_id))
        elif rest.startswith("files/") and "/" not in rest.removeprefix("files/"):
            self._route = "intake.put"
            self._only("PUT")
            try:
                member = unquote(rest.removeprefix("files/"), errors="strict")
            except UnicodeDecodeError as error:
                raise _Refusal(404, "file_not_declared", "that is not a package path") from error
            self._put(session_id, member)
        else:
            raise _Refusal(404, "not_found", "no such route")

    def _journey_intake(self, intake: JourneyIntake, path: str) -> None:
        """Hand a ``/api/intake/…`` request to voice-journey's protocol and send its answer."""
        self._route = "journey.intake"
        status, body = intake.handle(self.command, path, self.headers, _Body(self))
        if self._unread > MAX_CHUNK_BYTES:
            self.close_connection = True
        else:
            self._discard_body()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._access_log(status)

    def _self_authenticating(self, path: str) -> bool:
        """Whether ``path`` belongs to a route that checks its own tokens (the speech routes,
        whose machine callers never pass the global check)."""
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in self.server.self_authenticating
        )

    # -- extensions (api/routing.py) --------------------------------------------------------

    def _extension(self, path: str, query: str) -> bool:
        """Offer the request to the server's routes; True when one answered."""
        if not self.server.routes:
            return False
        request = RouteRequest(
            method=self.command,
            path=path,
            query=query,
            header=self.headers.get,
            content_length=self._content_length(),
            read_body=self._read_body,
            scope=self._scope,
        )
        for route in self.server.routes:
            response = route(request)
            if response is not None:
                self._route = response.route
                self._send_route(response)
                return True
        return False

    def _read_body(self, limit: int) -> bytes:
        length = self._content_length() or 0
        if length > limit:
            raise RouteRefusal(413, "body_too_large", f"a body is at most {limit} bytes")
        return _Body(self).read(length)

    def _send_route(self, response: RouteResponse) -> None:
        try:
            if self._unread > MAX_CHUNK_BYTES:
                self.close_connection = True
            else:
                self._discard_body()
        except BaseException:
            # The client went away while its body drained: the answer is never sent, so what
            # it holds (an open file, an event stream's place) is given back here.
            _close_response(response)
            raise
        if response.events is not None:
            self._send_events(response)
            return
        length = response.stream_length if response.stream is not None else len(response.body)
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command == "HEAD":
                pass
            elif response.stream is None:
                self.wfile.write(response.body)
            else:
                self._stream(response.stream, length)
        except OSError:
            self.close_connection = True  # the client went away mid-answer
        finally:
            if response.stream is not None:
                response.stream.close()
        self._access_log(response.status)

    def _send_events(self, response: RouteResponse) -> None:
        """Write an event stream as it is produced, then close the connection."""
        events = response.events
        assert events is not None
        self.close_connection = True
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.flush()
            for chunk in events:
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass  # the client went away
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                close()
        self._access_log(response.status)

    def _stream(self, source: IO[bytes], length: int) -> None:
        remaining = length
        while remaining > 0:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                self.close_connection = True  # the file shrank: the length was a promise
                return
            self.wfile.write(chunk)
            remaining -= len(chunk)

    def _only(self, *methods: str) -> None:
        if self.command not in methods:
            raise _Refusal(405, "method_not_allowed", f"this route answers {', '.join(methods)}")

    def _put(self, session_id: str, path: str) -> None:
        try:
            validate_package_path(path)
        except ValueError as error:
            raise _Refusal(404, "file_not_declared", "that is not a package path") from error
        length = self._content_length()
        if length is None:
            raise _Refusal(411, "content_length_required", "a PUT needs Content-Length")
        if length > MAX_CHUNK_BYTES:
            raise _Refusal(413, "body_too_large", f"one PUT carries at most {MAX_CHUNK_BYTES}")
        content_range = self.headers.get("Content-Range")
        if content_range is None:
            raise _Refusal(400, "invalid_content_range", "a PUT needs Content-Range")
        empty = _EMPTY_RANGE.fullmatch(content_range)
        parsed = _CONTENT_RANGE.fullmatch(content_range)
        if empty is not None and length == 0:
            start, total = 0, int(empty["total"])
            if total != 0:
                raise _Refusal(400, "invalid_content_range", "an empty range is for empty files")
        elif parsed is not None:
            start, end, total = int(parsed["start"]), int(parsed["end"]), int(parsed["total"])
            if end < start or end - start + 1 != length:
                raise _Refusal(400, "invalid_content_range", "the range and the length disagree")
        else:
            raise _Refusal(400, "invalid_content_range", "Content-Range is bytes S-E/T")
        result = self.server.staging.put_chunk(
            session_id, path, _Body(self), length=length, start=start, total=total
        )
        self._send(
            200 if result.complete else 202,
            ChunkAnswer(
                state="received" if result.complete else "partial",
                received_bytes=result.received_bytes,
            ),
        )


class _Health(ContractModel):
    status: Literal["ok"] = "ok"


def build_server(
    config: ApiServerConfig,
    address: tuple[str, int],
    *,
    log: bool = True,
    routes: Sequence[Route] = (),
    verifier: TokenVerifier | None = None,
    self_authenticating: tuple[str, ...] = (),
) -> ApiServer:
    """The server for ``config``, bound to ``address`` but not yet serving; ``verifier``
    (default: one for ``config.auth``) is shared with routes that check tokens, and the
    paths under ``self_authenticating`` belong to routes that check their own."""
    layout = AssetLayout.validate(Path(config.asset_root))
    verifier = verifier or TokenVerifier(config.auth)
    return ApiServer(
        address,
        staging=IntakeStaging(layout),
        verifier=verifier,
        meta=ApiMeta(
            server_build=config.server_build,
            minimum_client_version=config.minimum_client_version,
        ),
        log=log,
        journey_intake=JourneyIntake(layout, verifier=verifier),
        routes=routes,
        self_authenticating=self_authenticating,
    )


def speech_routes(path: Path, *, issuer: str, jwks: str) -> SpeechRoutes:
    """The speech routes for the configuration at ``path``; its callers must use ``issuer``.

    Raises: ``ContractError`` for an invalid configuration, ``ValueError`` for callers of
    another identity provider.
    """
    speech_config = load_speech_config(path)
    if any(caller.issuer != issuer for caller in speech_config.callers):
        raise ValueError("every speech caller uses the API's identity provider (--issuer)")
    return SpeechRoutes(speech_config, TokenVerifier(caller_policy(speech_config, jwks)))


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m cole_voice_lab.api serve …``: serve ``/api/v1`` until stopped.

    The configuration is given as flags (the stack's compose ``command``), never read from
    the environment.
    """
    parser = argparse.ArgumentParser(prog="python -m cole_voice_lab.api")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve /api/v1 until stopped")
    serve.add_argument("--asset-root", required=True)
    serve.add_argument("--jwks", required=True, help="the identity provider's key file")
    serve.add_argument("--issuer", required=True)
    serve.add_argument("--audience", action="append", required=True)
    serve.add_argument(
        "--client-audience",
        action="append",
        default=[],
        help="an audience whose tokens are scoped to a corpus client (D-056: voice-lab-panel)",
    )
    serve.add_argument("--allowed-subject", action="append", required=True)
    serve.add_argument("--server-build", required=True)
    serve.add_argument("--minimum-client-version", default="0.1.0")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--once", action="store_true", help="bind, report, and exit")
    serve.add_argument(
        "--speech-config",
        type=Path,
        help="serve /api/v1/speech for the callers this configuration registers (D-046)",
    )
    add_journey_arguments(serve)
    options = parser.parse_args(argv)
    config = ApiServerConfig(
        asset_root=options.asset_root,
        server_build=options.server_build,
        minimum_client_version=options.minimum_client_version,
        auth=AuthPolicy(
            issuers=(
                TrustedIssuer(
                    issuer=options.issuer,
                    audiences=tuple(options.audience),
                    jwks_path=options.jwks,
                    client_audiences=tuple(options.client_audience),
                ),
            ),
            allowed_subjects=tuple(options.allowed_subject),
        ),
    )
    try:
        journey = journey_routes_from_options(options)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    verifier = TokenVerifier(config.auth)
    view = ViewRoutes(journey.store, verifier)
    # Take audio needs the same recorded playback approval as journey playback (§4.2).
    corpus = CorpusRoutes(
        AssetLayout.validate(Path(config.asset_root)),
        playback_approval=options.playback_approval,
    )
    routes: tuple[Route, ...] = (corpus, journey, view)
    self_authenticating: tuple[str, ...] = ()
    if options.speech_config is not None:
        try:
            speech = speech_routes(options.speech_config, issuer=options.issuer, jwks=options.jwks)
        except (OSError, ValueError, VoiceLabError) as error:
            sys.stderr.write(f"error: {error}\n")
            return 1
        routes = (speech, *routes)  # first: it owns every path under its prefix
        self_authenticating = (speech.prefix,)
    server = build_server(
        config,
        (options.host, options.port),
        routes=routes,
        verifier=verifier,
        self_authenticating=self_authenticating,
    )
    sys.stderr.write(f"api serving on {options.host}:{server.server_address[1]}\n")
    summary = journey.describe()
    if summary is not None:
        sys.stderr.write(f"api journey {json.dumps(summary, sort_keys=True)}\n")
    if options.once:
        server.server_close()
        return 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
