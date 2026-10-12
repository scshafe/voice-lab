"""The server's extension point: route modules beside ``meta`` and ``intake``.

@canonical api.routing -- ``ApiServer`` asks each of its ``routes`` (in order) to answer
a request it does not route itself; the first that returns a :class:`RouteResponse`
answers. For a path under ``/api/`` the server has already verified the bearer token
(``auth.py``) before any route sees the request, except under the server's
``self_authenticating`` prefixes, whose route checks its own tokens (``/api/v1/speech``).
Any other path (``/healthz`` aside) reaches the routes unauthenticated, so a route that
serves one (the web view) checks identity itself. A route returns ``None`` for a path
that is not its own, raises :class:`RouteRefusal` for a refusal in the server's
``ApiRefusal`` shape, or answers with its own body (the journey routes' voice-journey
aliases answer the JS's JSON).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, Protocol

from .contracts import ApiErrorCode


class RouteRefusal(Exception):
    """A refusal the server answers as ``ApiRefusal`` (a stable code, no secret), with any
    extra response ``headers`` (``Retry-After``)."""

    def __init__(
        self,
        status: int,
        code: ApiErrorCode,
        detail: str,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code: ApiErrorCode = code
        self.detail = detail
        self.headers = headers


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """One request, as a route sees it.

    ``path`` is the raw (percent-encoded) path and ``query`` the raw query string.
    ``read_body(limit)`` returns the whole body (empty without ``Content-Length``) and
    raises ``RouteRefusal`` (413) for a declared length over ``limit``; it may be called
    once."""

    method: str
    path: str
    query: str
    header: Callable[[str], str | None]
    content_length: int | None
    read_body: Callable[[int], bytes]


@dataclass(frozen=True, slots=True)
class RouteResponse:
    """An answer: ``body`` bytes, ``stream`` (``stream_length`` bytes copied from an open
    file, which the server closes), or ``events`` (chunks written as they come)."""

    status: int
    route: str
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()
    stream: IO[bytes] | None = None
    stream_length: int = 0
    # A response of unknown length written as it is produced (server-sent events): no
    # Content-Length, flushed after each chunk, and the connection is closed at its end.
    events: Iterator[bytes] | None = None


class Route(Protocol):
    def __call__(self, request: RouteRequest) -> RouteResponse | None: ...
