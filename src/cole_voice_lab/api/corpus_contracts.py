"""The corpus API's answers (D-052, docs/design/RECORDING-V1.md §4): the catalog a subject
records against, and (as the plan's later steps land) its library and its takes.

Strict, versioned models like every ``/api/v1`` answer; their JSON schemas are generated into
``schemas/`` (``CORPUS_API_SCHEMA_MODELS``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel

from ..models import ContractModel, PromptSuite, StableId
from ..recording_contracts import CatalogReference, PromptAlias


class CatalogView(ContractModel):
    """``GET /api/v1/catalog``: the active catalog version of the lab's subject.

    ``reference`` pins the version (its ``snapshot_sha256`` is also the answer's ``ETag``);
    ``suite`` is the exact prompt suite and ``prompt_aliases`` the earlier prompt digests a
    keep-recordings revision still counts, both from the digest-checked snapshot.
    ``refreshed_at`` is when the PC's writer last recorded this version as active."""

    schema_version: Literal["cole-voice-lab.api.catalog/v1"] = "cole-voice-lab.api.catalog/v1"
    subject_id: StableId
    reference: CatalogReference
    suite: PromptSuite
    prompt_aliases: tuple[PromptAlias, ...]
    refreshed_at: datetime


CORPUS_API_SCHEMA_MODELS: Final[dict[str, type[BaseModel]]] = {
    "api-v1-catalog": CatalogView,
}
