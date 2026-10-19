"""What a corpus client's token may reach (D-056, docs/design/HOUSE-PANEL.md §3).

@canonical api.client_scope -- a token whose audience is a client audience (the house panel's,
``voice-lab-panel``) is scoped ``corpus-client`` (``api.auth``). The server's ``/api/v1`` check
takes such a token only on this allow-list; every other route answers 403 ``client_scope``.

The allow-list:

- ``GET /api/v1/meta``, and ``GET``/``HEAD /api/v1/catalog``;
- ``GET``/``HEAD /api/v1/corpus/library`` and ``…/prompts/{id}/takes``, with exactly
  ``?profile=xvf3800-linear-4``: the client's profile's view, never another's;
- ``/api/v1/intake/sessions…``: open, status, ``PUT`` a file, finalize, receipt. Finalize takes
  only a v2 take of the client's profile (422 ``take_profile_not_allowed``, ``api.staging``);
- ``POST /api/v1/corpus/takes/{id}/decision``, only on a take of the client's profile
  (``api.corpus_request_routes``), and ``GET /api/v1/corpus/requests/{id}``.

Refused: take audio, trims, the journey routes, ``/view/`` and speech.

The journey routes, their intake and ``/view/`` check tokens with ``verify_header``, which
takes no scoped token, and the speech routes have their own verifier and callers (D-046); so
the allow-list is the only door a client's token has, and it opens no audio.
"""

from __future__ import annotations

import re
from typing import Final

from ..capture_profiles import XVF3800_PROFILE
from .routing import RouteRefusal

#: The capture profile a corpus client records with: the house panel's.
CLIENT_PROFILE: Final = XVF3800_PROFILE
_READ: Final = frozenset({"GET", "HEAD"})
_EXACT: Final[dict[str, frozenset[str]]] = {
    "/api/v1/meta": frozenset({"GET"}),
    "/api/v1/catalog": _READ,
    "/api/v1/intake/sessions": frozenset({"POST"}),
}
#: The profile-scoped reads: only the client's profile's view.
_VIEWS: Final = (
    re.compile(r"^/api/v1/corpus/library$"),
    re.compile(r"^/api/v1/corpus/prompts/[^/]+/takes$"),
)
_SESSION: Final = re.compile(r"^/api/v1/intake/sessions/[^/]+(?P<rest>/.+)?$")
_SESSION_METHODS: Final[dict[str | None, frozenset[str]]] = {
    None: frozenset({"GET"}),
    "/finalize": frozenset({"POST"}),
    "/receipt": frozenset({"GET"}),
}
_DECISION: Final = re.compile(r"^/api/v1/corpus/takes/[^/]+/decision$")
_REQUEST_STATUS: Final = re.compile(r"^/api/v1/corpus/requests/[^/]+$")


def client_may(method: str, path: str, query: str) -> bool:
    """Whether a corpus client's token may make this request at all (the routes then apply
    their own checks: the finalize and decision profile rules among them)."""
    if path in _EXACT:
        return method in _EXACT[path] and not query
    if any(view.fullmatch(path) for view in _VIEWS):
        return method in _READ and query == f"profile={CLIENT_PROFILE}"
    if (session := _SESSION.fullmatch(path)) is not None:
        rest = session["rest"]
        if rest is not None and rest.startswith("/files/") and "/" not in rest[len("/files/") :]:
            return method == "PUT" and not query
        allowed = _SESSION_METHODS.get(rest)
        return allowed is not None and method in allowed and not query
    if _DECISION.fullmatch(path) is not None:
        return method == "POST" and not query
    if _REQUEST_STATUS.fullmatch(path) is not None:
        return method == "GET" and not query
    return False


def require_client_may(method: str, path: str, query: str) -> None:
    """Refuse a corpus client's request off its allow-list.

    Raises: ``RouteRefusal`` 403 ``client_scope``."""
    if not client_may(method, path, query):
        raise RouteRefusal(
            403,
            "client_scope",
            "this client's token reaches only the house panel's routes (docs/HOME_HOST_INTAKE.md)",
        )
