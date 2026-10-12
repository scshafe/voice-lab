"""``/api/v1/speech``: the routes registered callers use (docs/SPEECH_API.md §5, §6).

@canonical speech.routes -- a ``Route`` (api/routing.py) under a self-authenticating prefix:
the server skips its global bearer check for these paths, and this route checks the token
itself against the caller registry. A token is accepted when its signature, issuer and
lifetime verify and its issuer, subject and one audience match a registered caller; the
caller is then the principal for limits, idempotency, job visibility and webhook URLs. A
speech caller's token never passes the global check, so it reaches nothing else.

The route claims every path under its prefix (``404`` for one it does not know), so no
other route ever sees a request that skipped the global check. The API only enqueues and
serves finished files: it reads the asset root (read-only) for the voice release and the
consent record, and the GPU slot's holder record; the job runner renders.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Final

from pydantic import ValidationError

from ..api.auth import AuthError, AuthPolicy, Principal, TokenVerifier, TrustedIssuer
from ..api.routing import RouteRefusal, RouteRequest, RouteResponse
from ..canonical import canonical_json_bytes
from ..errors import VoiceLabError
from ..models import ContractModel, utc_now
from ..paths import AssetLayout
from .config import SpeechCaller, SpeechConfig, webhook_url_problem
from .contracts import JOB_ID_PATTERN, SpeechRequest
from .gpu import busy_with_long_job
from .release import VoiceReleaseError, current_release, require_release_consent
from .store import Job, SpeechRefusal, SpeechStore, open_audio, to_status

PREFIX: Final = "/api/v1/speech"
MAX_BODY_BYTES: Final = 64 * 1024
_RANGE: Final = re.compile(r"^bytes=(?P<start>\d{0,15})-(?P<end>\d{0,15})$")


def caller_policy(config: SpeechConfig, jwks_path: str) -> AuthPolicy:
    """The token policy of the speech routes: the callers' issuer, audiences and subjects.

    Raises: ``ValueError`` when callers name more than one issuer (the API holds one key file).
    """
    issuers = {caller.issuer for caller in config.callers}
    if len(issuers) != 1:
        raise ValueError("speech callers share the API's one identity provider")
    audiences = tuple(
        sorted({audience for caller in config.callers for audience in caller.audiences})
    )
    return AuthPolicy(
        issuers=(
            TrustedIssuer(issuer=issuers.pop(), audiences=audiences[:8], jwks_path=jwks_path),
        ),
        allowed_subjects=tuple(caller.subject for caller in config.callers),
    )


def match_caller(config: SpeechConfig, principal: Principal) -> SpeechCaller | None:
    """The registered caller ``principal`` is, by issuer, subject and an audience."""
    return next(
        (
            caller
            for caller in config.callers
            if caller.issuer == principal.issuer
            and caller.subject == principal.subject
            and set(caller.audiences) & set(principal.audiences)
        ),
        None,
    )


def _json(status: int, route: str, model: ContractModel) -> RouteResponse:
    return RouteResponse(status=status, route=route, body=canonical_json_bytes(model) + b"\n")


def _refuse(refusal: SpeechRefusal) -> RouteRefusal:
    headers = () if refusal.retry_after is None else (("Retry-After", str(refusal.retry_after)),)
    return RouteRefusal(refusal.status, refusal.code, refusal.detail, headers)


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The inclusive ``(start, end)`` of a single ``Range: bytes=…``, or ``None`` for all.

    Raises: ``SpeechRefusal`` 416 ``range_not_satisfiable``.
    """
    if header is None:
        return None
    match = _RANGE.fullmatch(header.strip())
    if match is None or (not match["start"] and not match["end"]):
        raise SpeechRefusal(416, "range_not_satisfiable", "one bytes=start-end range is served")
    if not match["start"]:
        suffix = int(match["end"])
        if suffix == 0:
            raise SpeechRefusal(416, "range_not_satisfiable", "an empty suffix range")
        return max(0, size - suffix), size - 1
    start = int(match["start"])
    end = int(match["end"]) if match["end"] else size - 1
    if start >= size or end < start:
        raise SpeechRefusal(416, "range_not_satisfiable", f"the audio is {size} bytes")
    return start, min(end, size - 1)


class SpeechRoutes:
    """The speech routes over one configuration (built by ``api.server`` with ``--speech``)."""

    prefix: Final = PREFIX

    def __init__(
        self,
        config: SpeechConfig,
        verifier: TokenVerifier,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.verifier = verifier
        self.clock = clock
        self.store = SpeechStore(config)
        self.store.ensure()
        self.asset_root = Path(config.asset_root)

    def __call__(self, request: RouteRequest) -> RouteResponse | None:
        path = request.path
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            return None
        try:
            return self._handle(request)
        except SpeechRefusal as refusal:
            raise _refuse(refusal) from None

    # -- plumbing ------------------------------------------------------------------------------

    def _caller(self, request: RouteRequest) -> SpeechCaller:
        try:
            principal = self.verifier.verify_header(request.header("Authorization"))
        except AuthError as error:
            raise SpeechRefusal(401, "unauthorized", str(error)) from error
        caller = match_caller(self.config, principal)
        if caller is None:
            raise SpeechRefusal(403, "caller_not_registered", "this token is no speech caller's")
        return caller

    @staticmethod
    def _only(request: RouteRequest, *methods: str) -> None:
        if request.method not in methods:
            raise SpeechRefusal(
                405, "method_not_allowed", f"this route answers {', '.join(methods)}"
            )

    def _handle(self, request: RouteRequest) -> RouteResponse:
        caller = self._caller(request)
        rest = request.path.removeprefix(PREFIX)
        if rest in {"", "/"}:
            self._only(request, "POST")
            return self._submit(caller, request)
        parts = rest.removeprefix("/").split("/")
        job_id = parts[0]
        if not JOB_ID_PATTERN.fullmatch(job_id) or len(parts) > 2:
            raise SpeechRefusal(404, "not_found", "no such route")
        action = parts[1] if len(parts) == 2 else None
        if action is None:
            self._only(request, "GET")
            job = self.store.for_caller(caller, job_id)
            return _json(200, "speech.status", to_status(job))
        if action == "audio":
            self._only(request, "GET", "HEAD")
            return self._audio(caller, job_id, request.header("Range"))
        if action == "cancel":
            self._only(request, "POST")
            job = self.store.cancel(caller, job_id, now=self.clock())
            return _json(202, "speech.cancel", to_status(job))
        raise SpeechRefusal(404, "not_found", "no such route")

    # -- submit --------------------------------------------------------------------------------

    def _submit(self, caller: SpeechCaller, request: RouteRequest) -> RouteResponse:
        if "private-assistant-speech" not in caller.allowed_purposes:
            raise SpeechRefusal(403, "purpose_not_allowed", "this caller may not request speech")
        body = request.read_body(MAX_BODY_BYTES)
        try:
            speech = SpeechRequest.model_validate_json(body)
        except ValidationError as error:
            raise SpeechRefusal(
                422, "invalid_request", "the body is not a valid SpeechRequest"
            ) from (error)
        if speech.format != "wav":
            raise SpeechRefusal(422, "unsupported_format", "only wav is served (ogg-opus later)")
        if len(speech.text) > caller.max_text_characters:
            raise SpeechRefusal(
                422, "invalid_request", f"at most {caller.max_text_characters} characters"
            )
        if speech.callback_url is not None and (
            webhook_url_problem(speech.callback_url, self.config.tailnet_suffix) is not None
            or not caller.allows_callback(speech.callback_url)
        ):
            raise SpeechRefusal(
                422, "callback_not_allowed", "the callback URL is not on this caller's allowlist"
            )
        now = self.clock()
        replay = self.store.replay(caller, speech)
        if replay is not None:
            return _json(200, "speech.submit", to_status(replay))
        try:
            layout = AssetLayout.validate(self.asset_root)
            release, _ = current_release(layout)
        except (VoiceLabError, OSError) as error:
            raise SpeechRefusal(
                503, "voice_release_unavailable", "no voice release is designated"
            ) from error
        try:
            require_release_consent(layout, release, at=now)
        except VoiceReleaseError as error:
            raise SpeechRefusal(
                503,
                "consent_unavailable",
                "no approved consent record allows assistant speech; designate again if renewed",
            ) from error
        wait = busy_with_long_job(
            Path(self.config.gpu_slot_root),
            now=now,
            default_seconds=self.config.gpu_busy_retry_seconds,
        )
        if wait is not None:
            raise SpeechRefusal(
                503, "gpu_busy", "a long job holds the GPU; answer in text", retry_after=wait
            )
        job, created = self.store.submit(caller, speech, now=now)
        return _json(202 if created else 200, "speech.submit", to_status(job))

    # -- audio ---------------------------------------------------------------------------------

    def _audio(self, caller: SpeechCaller, job_id: str, range_header: str | None) -> RouteResponse:
        job: Job = self.store.for_caller(caller, job_id)
        stream, size = open_audio(self.store, job)
        try:
            chosen = byte_range(range_header, size)
        except SpeechRefusal:
            stream.close()
            raise
        assert job.state.audio is not None
        headers: list[tuple[str, str]] = [
            ("Accept-Ranges", "bytes"),
            ("ETag", f'"{job.state.audio.sha256}"'),
        ]
        if chosen is None:
            return self._stream(200, stream, size, headers)
        start, end = chosen
        stream.seek(start, os.SEEK_SET)
        headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        return self._stream(206, stream, end - start + 1, headers)

    @staticmethod
    def _stream(
        status: int, stream: BinaryIO, length: int, headers: list[tuple[str, str]]
    ) -> RouteResponse:
        return RouteResponse(
            status=status,
            route="speech.audio",
            content_type="audio/wav",
            headers=tuple(headers),
            stream=stream,
            stream_length=length,
        )
