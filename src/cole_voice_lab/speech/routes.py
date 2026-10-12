"""``/api/v1/speech``: the routes registered callers use (docs/SPEECH_API.md §5, §6).

@canonical speech.routes -- a ``Route`` (api/routing.py) under a self-authenticating prefix:
the server skips its global bearer check for these paths, and this route checks the token
itself against the caller registry. A token is accepted when its signature, issuer and
lifetime verify and its issuer, subject and one audience match a registered caller; the
caller is then the principal for limits, idempotency, job visibility and webhook URLs. A
speech caller's token never passes the global check, so it reaches nothing else.

The route claims every path under its prefix (``404`` for one it does not know), so no
other route ever sees a request that skipped the global check. Besides submit, status,
cancel and the joined audio, it serves each published sentence
(``…/segments/{n}/audio``) and a per-job server-sent event stream (``…/events``) that
announces them and the job's end; it only ever reports files the job runner has finished.
A caller holds at most ``MAX_EVENT_STREAMS`` streams at once (``429 too_many_streams``), so
one caller cannot take every one of the API's threads.

The API only enqueues and serves finished files: it reads the asset root (read-only) for the
voice release and the consent record, and the GPU slot's holder record; the job runner
renders.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from functools import partial
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
from .contracts import JOB_ID_PATTERN, TERMINAL_STATES, SpeechRequest
from .gpu import busy_with_long_job
from .release import VoiceReleaseError, current_release, require_release_consent, require_subject
from .store import (
    Job,
    SpeechRefusal,
    SpeechStore,
    log_to_stderr,
    open_audio,
    open_segment,
    to_status,
)

PREFIX: Final = "/api/v1/speech"
EVENTS_MAX_SECONDS: Final = 600.0
KEEPALIVE_SECONDS: Final = 15.0
# Open event streams per caller (each holds one of the API's threads for up to 10 minutes).
MAX_EVENT_STREAMS: Final = 4
# How many audiences one trusted issuer takes (``api.auth.TrustedIssuer``).
MAX_POLICY_AUDIENCES: Final = 8
_SEGMENT_INDEX: Final = re.compile(r"^[1-9][0-9]{0,3}$")
MAX_BODY_BYTES: Final = 64 * 1024
_RANGE: Final = re.compile(r"^bytes=(?P<start>[0-9]{0,15})-(?P<end>[0-9]{0,15})$")
_LAST_EVENT_ID: Final = re.compile(r"^[0-9]{1,4}$")


def caller_policy(
    config: SpeechConfig, jwks_path: str, *, log: Callable[[str], None] = log_to_stderr
) -> AuthPolicy:
    """The token policy of the speech routes: the callers' issuer, audiences and subjects.

    The issuer takes at most ``MAX_POLICY_AUDIENCES`` audiences (the first, sorted); when the
    callers name more, the rest are dropped and the log says how many and which callers lose
    an audience (never a token or a secret).
    Raises: ``ValueError`` when callers name more than one issuer (the API holds one key file).
    """
    issuers = {caller.issuer for caller in config.callers}
    if len(issuers) != 1:
        raise ValueError("speech callers share the API's one identity provider")
    audiences = tuple(
        sorted({audience for caller in config.callers for audience in caller.audiences})
    )
    kept = audiences[:MAX_POLICY_AUDIENCES]
    if len(kept) < len(audiences):
        affected = sorted(
            caller.caller_id for caller in config.callers if not set(caller.audiences) <= set(kept)
        )
        log(
            f"the speech callers name {len(audiences)} audiences; the token policy takes"
            f" {MAX_POLICY_AUDIENCES}, so tokens for the other {len(audiences) - len(kept)}"
            f" are refused (callers losing an audience: {', '.join(affected)})"
        )
    return AuthPolicy(
        issuers=(TrustedIssuer(issuer=issuers.pop(), audiences=kept, jwks_path=jwks_path),),
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
    headers = refusal.headers
    if refusal.retry_after is not None:
        headers = (*headers, ("Retry-After", str(refusal.retry_after)))
    return RouteRefusal(refusal.status, refusal.code, refusal.detail, headers)


def _unsatisfiable(size: int, detail: str) -> SpeechRefusal:
    return SpeechRefusal(
        416, "range_not_satisfiable", detail, headers=(("Content-Range", f"bytes */{size}"),)
    )


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The inclusive ``(start, end)`` of a single ``Range: bytes=…``, or ``None`` for all.

    Several ranges, another unit or a header that is not a byte range are answered with the
    whole file (RFC 9110 lets a server ignore ``Range``); only a single byte range that the
    file cannot satisfy is refused.
    Raises: ``SpeechRefusal`` 416 ``range_not_satisfiable`` (with ``Content-Range: bytes
    */<size>``).
    """
    if header is None:
        return None
    match = _RANGE.fullmatch(header.strip())
    if match is None or (not match["start"] and not match["end"]):
        return None
    if not match["start"]:
        suffix = int(match["end"])
        if suffix == 0 or size == 0:
            raise _unsatisfiable(size, "an empty suffix range")
        return max(0, size - suffix), size - 1
    start = int(match["start"])
    end = int(match["end"]) if match["end"] else size - 1
    if start >= size or end < start:
        raise _unsatisfiable(size, f"the audio is {size} bytes")
    return start, min(end, size - 1)


def last_event_id(header: str | None) -> int:
    """The segment a resumed event stream follows: ``Last-Event-ID`` when it is 1 to 4 ASCII
    digits, else 0 (from the start)."""
    if header is None or not _LAST_EVENT_ID.fullmatch(header):
        return 0
    return int(header)


class SpeechRoutes:
    """The speech routes over one configuration (built by ``api.server`` with ``--speech``)."""

    prefix: Final = PREFIX

    def __init__(
        self,
        config: SpeechConfig,
        verifier: TokenVerifier,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        events_poll_seconds: float = 0.2,
    ) -> None:
        self.config = config
        self.verifier = verifier
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.events_poll_seconds = events_poll_seconds
        self.store = SpeechStore(config)
        self.store.ensure()
        self.asset_root = Path(config.asset_root)
        self._streams: dict[str, int] = {}
        self._streams_lock = threading.Lock()

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
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise SpeechRefusal(404, "not_found", "no such route")
        if len(parts) == 4 and parts[1] == "segments" and parts[3] == "audio":
            if not _SEGMENT_INDEX.fullmatch(parts[2]):
                raise SpeechRefusal(404, "not_found", "no such segment")
            self._only(request, "GET", "HEAD")
            return self._segment(caller, job_id, int(parts[2]), request.header("Range"))
        if len(parts) > 2:
            raise SpeechRefusal(404, "not_found", "no such route")
        action = parts[1] if len(parts) == 2 else None
        if action is None:
            self._only(request, "GET")
            job = self.store.for_caller(caller, job_id)
            return _json(200, "speech.status", to_status(job))
        if action == "audio":
            self._only(request, "GET", "HEAD")
            return self._audio(caller, job_id, request.header("Range"))
        if action == "events":
            self._only(request, "GET")
            self.store.for_caller(caller, job_id)  # refused before the stream starts
            resume = last_event_id(request.header("Last-Event-ID"))  # parsed before headers
            self._open_stream(caller)
            return RouteResponse(
                status=200,
                route="speech.events",
                content_type="text/event-stream; charset=utf-8",
                headers=(("X-Accel-Buffering", "no"),),
                events=_CountedStream(
                    self._events(caller, job_id, resume),
                    partial(self._close_stream, caller.caller_id),
                ),
            )
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
            or not caller.allows_callback(speech.callback_url, self.config.tailnet_suffix)
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
            require_subject(release, self.config.subject_id)
        except (VoiceLabError, OSError, ValueError) as error:
            raise SpeechRefusal(
                503, "voice_release_unavailable", "no usable voice release is designated"
            ) from error
        try:
            require_release_consent(layout, release, at=now, subject_id=self.config.subject_id)
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
        assert job.state.audio is not None
        return self._ranged(stream, size, job.state.audio.sha256, range_header)

    def _segment(
        self, caller: SpeechCaller, job_id: str, index: int, range_header: str | None
    ) -> RouteResponse:
        job: Job = self.store.for_caller(caller, job_id)
        stream, size, segment = open_segment(self.store, job, index)
        return self._ranged(stream, size, segment.sha256, range_header)

    def _ranged(
        self, stream: BinaryIO, size: int, sha256: str, range_header: str | None
    ) -> RouteResponse:
        try:
            chosen = byte_range(range_header, size)
        except SpeechRefusal:
            stream.close()
            raise
        headers: list[tuple[str, str]] = [
            ("Accept-Ranges", "bytes"),
            ("ETag", f'"{sha256}"'),
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

    # -- events --------------------------------------------------------------------------------

    def _open_stream(self, caller: SpeechCaller) -> None:
        """Count one more open stream for ``caller``.

        Raises: ``SpeechRefusal`` 429 ``too_many_streams`` at ``MAX_EVENT_STREAMS``.
        """
        with self._streams_lock:
            open_now = self._streams.get(caller.caller_id, 0)
            if open_now >= MAX_EVENT_STREAMS:
                raise SpeechRefusal(
                    429,
                    "too_many_streams",
                    f"at most {MAX_EVENT_STREAMS} event streams at once; close one or poll",
                )
            self._streams[caller.caller_id] = open_now + 1

    def _close_stream(self, caller_id: str) -> None:
        with self._streams_lock:
            left = self._streams.get(caller_id, 0) - 1
            if left > 0:
                self._streams[caller_id] = left
            else:
                self._streams.pop(caller_id, None)

    def open_streams(self, caller_id: str) -> int:
        with self._streams_lock:
            return self._streams.get(caller_id, 0)

    def _events(self, caller: SpeechCaller, job_id: str, sent: int) -> Iterator[bytes]:
        """``segment.ready`` for each published segment after segment ``sent``, then the
        terminal event; a comment every ``KEEPALIVE_SECONDS``; closed after
        ``EVENTS_MAX_SECONDS`` in any case."""
        started = last_beat = self.monotonic()
        yield b"retry: 2000\n\n"
        while True:
            try:
                job = self.store.for_caller(caller, job_id)
            except SpeechRefusal:
                return  # removed by retention while streaming
            status = to_status(job)
            for segment in status.segments:
                if segment.index > sent:
                    yield _event("segment.ready", str(segment.index), segment)
                    sent = segment.index
            if job.state.state in TERMINAL_STATES:
                yield _event(f"speech.{job.state.state}", "end", status)
                return
            now = self.monotonic()
            if now - started >= EVENTS_MAX_SECONDS:
                return
            if now - last_beat >= KEEPALIVE_SECONDS:
                last_beat = now
                yield b": keepalive\n\n"
            self.sleep(self.events_poll_seconds)


class _CountedStream:
    """An event stream that gives its caller's stream count back exactly once: when it ends,
    or when the server closes it (the server always does, even before the first chunk)."""

    def __init__(self, events: Iterator[bytes], done: Callable[[], None]) -> None:
        self._events = events
        self._done: Callable[[], None] | None = done

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        try:
            return next(self._events)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        done, self._done = self._done, None
        if done is None:
            return
        try:
            close = getattr(self._events, "close", None)
            if close is not None:
                close()
        finally:
            done()


def _event(name: str, identifier: str, model: ContractModel) -> bytes:
    data = canonical_json_bytes(model).decode("utf-8")
    return f"id: {identifier}\nevent: {name}\ndata: {data}\n\n".encode()
