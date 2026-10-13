"""The corpus API's answers (D-052, docs/design/RECORDING-V1.md §4): the catalog a subject
records against, its library, and each prompt's takes.

Strict, versioned models like every ``/api/v1`` answer; their JSON schemas are generated into
``schemas/`` (``CORPUS_API_SCHEMA_MODELS``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel

from ..corpus_read_model import LibraryCoverage, LibraryRow, LibraryTake
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


class CorpusLibrary(ContractModel):
    """``GET /api/v1/corpus/library``: the subject's library of the active catalog version.

    ``rows`` are the version's prompts in catalog order, each with its status
    (``missing``/``draft``/``kept``/``needs-attention``), its take count and the take that
    stands for it; ``coverage`` totals them. The answer's ``ETag`` is the read model's digest
    (``corpus_read_model``), which changes exactly when any row or history does."""

    schema_version: Literal["cole-voice-lab.api.corpus-library/v1"] = (
        "cole-voice-lab.api.corpus-library/v1"
    )
    subject_id: StableId
    catalog: CatalogReference
    coverage: LibraryCoverage
    rows: tuple[LibraryRow, ...]


class TakeHistory(ContractModel):
    """``GET /api/v1/corpus/prompts/{prompt_id}/takes``: one prompt's row and every take that
    counts for it under the active version, newest first (an alias's takes included)."""

    schema_version: Literal["cole-voice-lab.api.take-history/v1"] = (
        "cole-voice-lab.api.take-history/v1"
    )
    subject_id: StableId
    catalog: CatalogReference
    row: LibraryRow
    takes: tuple[LibraryTake, ...]


CORPUS_API_SCHEMA_MODELS: Final[dict[str, type[BaseModel]]] = {
    "api-v1-catalog": CatalogView,
    "api-v1-corpus-library": CorpusLibrary,
    "api-v1-take-history": TakeHistory,
}
