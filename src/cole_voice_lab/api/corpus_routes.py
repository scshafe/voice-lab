"""The corpus routes (D-052, docs/design/RECORDING-V1.md): ``GET /api/v1/catalog``.

@canonical api.corpus_routes -- the API's lab mount is read-only, so these routes read only
what the PC's one lab writer (the intake publisher) keeps for them: the read model
(``corpus_read_model``) and the content-addressed catalog snapshots, which
``corpus_store.read_catalog_snapshot`` checks byte for byte against the reference. They never
open the corpus store. The subject is the lab's one subject (``COLE_SUBJECT``, D-050).

``GET|HEAD /api/v1/catalog``:

- 200 ``CatalogView``, with ``ETag: "<snapshot_sha256>"``;
- 304 when ``If-None-Match`` names that tag (the client already holds this version);
- 503 ``catalog_unavailable`` until a catalog is active on the PC (the owner seeds it,
  RECORDING-V1.md §7);
- 500 ``catalog_invalid`` when the read model or the snapshot it names fails its checks.

Like every ``/api/`` route, it is reached only with a verified bearer token (``auth.py``).
"""

from __future__ import annotations

from typing import Final

from ..canonical import canonical_json_bytes
from ..catalog_ports import CorpusStoreError
from ..corpus_read_model import read_active_catalog
from ..corpus_store import read_catalog_snapshot
from ..errors import ContractError, SecurityError
from ..models import COLE_SUBJECT
from ..paths import AssetLayout
from .corpus_contracts import CatalogView
from .routing import RouteRefusal, RouteRequest, RouteResponse

CATALOG_PATH: Final = "/api/v1/catalog"
_METHODS: Final = ("GET", "HEAD")


def _matches(header: str | None, tag: str) -> bool:
    """Whether an ``If-None-Match`` header names ``tag`` (weak or strong, or ``*``)."""
    if header is None:
        return False
    candidates = {candidate.strip().removeprefix("W/") for candidate in header.split(",")}
    return "*" in candidates or tag in candidates


class CorpusRoutes:
    """The corpus routes over the lab at ``layout``, for ``subject_id``."""

    def __init__(self, layout: AssetLayout, subject_id: str = COLE_SUBJECT) -> None:
        self.layout = layout
        self.subject_id = subject_id

    def __call__(self, request: RouteRequest) -> RouteResponse | None:
        if request.path != CATALOG_PATH:
            return None
        if request.method not in _METHODS:
            allowed = ", ".join(_METHODS)
            raise RouteRefusal(405, "method_not_allowed", f"this route answers {allowed}")
        return self._catalog(request)

    def _catalog(self, request: RouteRequest) -> RouteResponse:
        try:
            record = read_active_catalog(self.layout, self.subject_id)
        except (ContractError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "catalog_invalid", f"the corpus read model: {error}") from error
        if record is None:
            raise RouteRefusal(
                503,
                "catalog_unavailable",
                "no catalog is active on this server yet (docs/design/RECORDING-V1.md §7)",
            )
        tag = f'"{record.reference.snapshot_sha256}"'
        headers = (("ETag", tag),)
        if _matches(request.header("If-None-Match"), tag):
            return RouteResponse(status=304, route="corpus.catalog", headers=headers)
        try:
            snapshot = read_catalog_snapshot(self.layout, record.reference)
        except (CorpusStoreError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "catalog_invalid", f"the catalog snapshot: {error}") from error
        view = CatalogView(
            subject_id=record.subject_id,
            reference=record.reference,
            suite=snapshot.suite,
            prompt_aliases=snapshot.prompt_aliases,
            refreshed_at=record.refreshed_at,
        )
        return RouteResponse(
            status=200,
            route="corpus.catalog",
            body=canonical_json_bytes(view) + b"\n",
            headers=headers,
        )
