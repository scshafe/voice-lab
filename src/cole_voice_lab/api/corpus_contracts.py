"""The corpus API's bodies and answers (D-052, docs/design/RECORDING-V1.md §4): the catalog a
subject records against, its library, each prompt's takes, and the decisions and trims a
client asks for (``corpus_requests``).

Strict, versioned models like every ``/api/v1`` answer; their JSON schemas are generated into
``schemas/`` (``CORPUS_API_SCHEMA_MODELS``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from ..corpus_read_model import LibraryCoverage, LibraryRow, LibraryTake
from ..corpus_requests import (
    CorpusRequestOutcome,
    DecisionChange,
    RequestId,
    TrimChange,
    check_decision,
    check_trim,
)
from ..corpus_take_store import TakeDecisionName
from ..corpus_takes import MAX_TAKE_FRAMES, TakeId
from ..models import ContractModel, PromptSuite, StableId
from ..recording_contracts import CatalogReference, PromptAlias

type _Reason = Annotated[str, StringConstraints(min_length=1, max_length=500)]


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
    (``corpus_read_model``), which changes exactly when any row or history does.

    It is one capture profile's view (D-056): ``profile`` is absent in the default view
    (``umik-1``), byte for byte the answer before profiles existed, and names any other."""

    schema_version: Literal["cole-voice-lab.api.corpus-library/v1"] = (
        "cole-voice-lab.api.corpus-library/v1"
    )
    subject_id: StableId
    catalog: CatalogReference
    coverage: LibraryCoverage
    rows: tuple[LibraryRow, ...]
    profile: Literal["xvf3800-linear-4"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class TakeHistory(ContractModel):
    """``GET /api/v1/corpus/prompts/{prompt_id}/takes``: one prompt's row and every take that
    counts for it under the active version, newest first (an alias's takes included), in one
    capture profile's view (``profile``, as ``CorpusLibrary``'s)."""

    schema_version: Literal["cole-voice-lab.api.take-history/v1"] = (
        "cole-voice-lab.api.take-history/v1"
    )
    subject_id: StableId
    catalog: CatalogReference
    row: LibraryRow
    takes: tuple[LibraryTake, ...]
    profile: Literal["xvf3800-linear-4"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class TakeDecisionRequest(ContractModel):
    """``POST /api/v1/corpus/takes/{take_id}/decision``: set the take's decision.

    ``request_id`` is the client's id for this change (``req-`` and 32 lower-case hex digits),
    the same on every retry. ``expected_revision`` is the take's decision revision the client
    last saw (``LibraryTake.decision_revision``): the PC applies the change only at that
    revision. ``reason`` is a rejection's, and only a rejection's. A restore sends the decision
    the take had before (``kept``, or ``draft``); ``removed`` never deletes anything."""

    schema_version: Literal["cole-voice-lab.api.take-decision-request/v1"] = (
        "cole-voice-lab.api.take-decision-request/v1"
    )
    request_id: RequestId
    decision: TakeDecisionName
    reason: _Reason | None = None
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> TakeDecisionRequest:
        check_decision(self.decision, self.reason)
        return self

    def change(self) -> DecisionChange:
        return DecisionChange(
            decision=self.decision, reason=self.reason, expected_revision=self.expected_revision
        )


class TakeTrimRequest(ContractModel):
    """``PUT /api/v1/corpus/takes/{take_id}/trim``: keep the frames ``[start_frame,
    end_frame)`` of the take, or all of them (both ``null``: a reset), at the trim revision the
    client last saw (``expected_revision``, ``LibraryTake.trim.revision``)."""

    schema_version: Literal["cole-voice-lab.api.take-trim-request/v1"] = (
        "cole-voice-lab.api.take-trim-request/v1"
    )
    request_id: RequestId
    start_frame: int | None = Field(ge=0, lt=MAX_TAKE_FRAMES)
    end_frame: int | None = Field(gt=0, le=MAX_TAKE_FRAMES)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> TakeTrimRequest:
        check_trim(self.start_frame, self.end_frame)
        return self

    def change(self) -> TrimChange:
        return TrimChange(
            start_frame=self.start_frame,
            end_frame=self.end_frame,
            expected_revision=self.expected_revision,
        )


class CorpusRequestStatus(ContractModel):
    """A decision or trim request as the server sees it: the answer (202) to its ``POST`` or
    ``PUT`` and to ``GET /api/v1/corpus/requests/{request_id}``.

    ``queued``: it waits in ``incoming/`` for the PC's writer (the intake publisher), which
    applies a take's requests in the order they arrived. ``applied`` or ``refused``:
    ``outcome`` says what the writer did (an applied change's new revision; a refusal's code,
    such as ``revision_conflict`` with the take's current revision)."""

    schema_version: Literal["cole-voice-lab.api.corpus-request-status/v1"] = (
        "cole-voice-lab.api.corpus-request-status/v1"
    )
    request_id: RequestId
    take_id: TakeId | None
    status: Literal["queued", "applied", "refused"]
    received_at: datetime | None
    outcome: CorpusRequestOutcome | None


CORPUS_API_SCHEMA_MODELS: Final[dict[str, type[BaseModel]]] = {
    "api-v1-catalog": CatalogView,
    "api-v1-corpus-library": CorpusLibrary,
    "api-v1-take-history": TakeHistory,
    "api-v1-take-decision-request": TakeDecisionRequest,
    "api-v1-take-trim-request": TakeTrimRequest,
    "api-v1-corpus-request-status": CorpusRequestStatus,
}
