"""``/view/``: the read-only web view's route (an ``api.routing`` route of ``ApiServer``).

@canonical view.routes -- server-rendered HTML for the phone (design §6.5, §8.2;
D-025, D-035), from the same ``JourneyStore`` as the journey API routes:

==================  ===========================================================
``/view/``           summary: totals, classification buckets, recordings per year
``/view/recordings`` the recordings, newest first, 50 a page (``page``, ``bucket``,
                     ``year``): date, duration, bucket, labels, cluster
``/view/journeys``   same-song journeys and the improvement index
``/view/trends``     voice trends: pitch range, sustain, clarity, vibrato, cadence
``/view/goals``      goal status (creating and abandoning goals is the API's)
``/view/report``     the evidence report (``view/report.py``), rendered live
==================  ===========================================================

**Read only.** ``GET`` and ``HEAD``; any other method is ``405`` (``Allow: GET, HEAD``)
before anything is read. No page reads or links to audio, transcript text, lyric text
or feature detail (D-025), or anything under ``raw/``, ``derived/`` or ``memos/``.

**Auth (design §3.5).** ``/view`` sits behind the Pocket ID door (oauth2-proxy, cookie
session), which forwards the signed-in user as ``X-Forwarded-User``. The server only
hands ``/api/`` paths to its bearer check, so this route checks identity itself:

- a request with ``Authorization`` must carry a bearer token the API's
  ``TokenVerifier`` accepts (issuer, audience, lifetime, signature, the allowed
  subject); otherwise ``401``;
- without one, ``X-Forwarded-User`` must equal one of the server's allowed subjects
  (``serve --allowed-subject``): missing is ``401``, another user ``403``.

A refusal is a short HTML page with no data. The forwarded header is trusted only
because the API is reachable from the door alone (its own network, deploy/stack);
the check is defence in depth, not a replacement for the door.

**Headers.** ``Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`` (no
script runs), ``X-Content-Type-Options: nosniff``, ``Referrer-Policy: no-referrer``,
``X-Frame-Options: DENY``; the server adds ``Cache-Control: no-store``.

Without ``serve --journey`` the pages answer ``503``. ``/view`` redirects to ``/view/``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Final
from urllib.parse import parse_qsl

from ..api.auth import AuthError, TokenVerifier
from ..api.routing import RouteRequest, RouteResponse
from ..journey.browser import JourneyStore
from . import pages
from .render import ROOT, message_page

CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS: Final = (
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)
HTML: Final = "text/html; charset=utf-8"
METHODS: Final = frozenset({"GET", "HEAD"})


class _Refused(Exception):
    def __init__(self, status: int, title: str, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.title = title
        self.text = text


class ViewRoutes:
    """The ``/view`` route. ``store`` is ``None`` when the server runs without
    ``--journey`` (the pages answer ``503``); ``verifier`` is the API's token verifier,
    whose policy names the allowed subjects."""

    def __init__(
        self,
        store: JourneyStore | None,
        verifier: TokenVerifier,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.verifier = verifier
        self.report_cache = pages.ReportCache()
        self._log = log or (lambda message: sys.stderr.write(f"{message}\n"))

    def __call__(self, request: RouteRequest) -> RouteResponse | None:
        path = request.path
        if path != "/view" and not path.startswith(ROOT):
            return None
        try:
            if request.method not in METHODS:
                raise _Refused(405, "Read only", "The web view answers GET and HEAD only.")
            self._authorize(request)
            if path == "/view":
                return RouteResponse(
                    status=308,
                    route="view.redirect",
                    content_type="text/plain; charset=utf-8",
                    headers=(("Location", ROOT), *SECURITY_HEADERS),
                )
            if self.store is None:
                raise _Refused(503, "Not available", "The server runs without the journey routes.")
            return self._page(self.store, path, request.query)
        except _Refused as refusal:
            extra = (("Allow", "GET, HEAD"),) if refusal.status == 405 else ()
            return _html(
                refusal.status,
                "view.refused",
                message_page(refusal.title, refusal.text),
                extra,
            )

    def _authorize(self, request: RouteRequest) -> None:
        authorization = request.header("Authorization")
        if authorization is not None:
            try:
                self.verifier.verify_header(authorization)
            except AuthError as error:
                raise _Refused(401, "Not signed in", "The bearer token was refused.") from error
            return
        user = request.header("X-Forwarded-User")
        if not user:
            raise _Refused(401, "Not signed in", "Sign in through the voice-lab door.")
        if user not in self.verifier.policy.allowed_subjects:
            raise _Refused(403, "Not allowed", "This account may not use the web view.")

    def _page(self, store: JourneyStore, path: str, query: str) -> RouteResponse:
        name = path.removeprefix(ROOT).rstrip("/")
        render = {
            "": lambda: pages.summary_page(store),
            "recordings": lambda: pages.recordings_page(store, parse_qsl(query)),
            "journeys": lambda: pages.journeys_page(store),
            "trends": lambda: pages.trends_page(store),
            "goals": lambda: pages.goals_page(store),
            "report": lambda: pages.report_page(store, self.report_cache),
        }.get(name)
        if render is None:
            raise _Refused(404, "Not found", "There is no such page.")
        try:
            document = render()
        except Exception as error:  # noqa: BLE001 (a page that fails is the server's 500)
            self._log(f"view {name or 'summary'} failed: {type(error).__name__}: {error}")
            raise _Refused(
                500, "Something went wrong", "The page could not be rendered."
            ) from error
        return _html(200, f"view.{name or 'summary'}", document)


def _html(
    status: int, route: str, document: str, extra: tuple[tuple[str, str], ...] = ()
) -> RouteResponse:
    return RouteResponse(
        status=status,
        route=route,
        body=document.encode("utf-8"),
        content_type=HTML,
        headers=(*SECURITY_HEADERS, *extra),
    )
