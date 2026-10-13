"""The corpus's read model on the PC (D-052): what the API serves without opening the store.

@canonical corpus_read_model -- the network-facing API's lab mount is read-only, and the
corpus store cannot be opened read-only (it takes its locks and WAL on open). So the store's
writers keep a small read model under ``derived/corpus/``, and the API reads only that model
and the catalog snapshots it names. The snapshots are content-addressed and digest-checked by
``corpus_store.read_catalog_snapshot``, so the API never trusts the read model for the
prompts themselves.

Writers: the intake publisher refreshes the model on every pass (the PC's one lab writer),
and ``voice-lab catalog init|publish|activate`` refreshes it at once
(``refresh_corpus_read_model``). The model holds:

- ``active-catalog-<subject>.json``: the subject's active catalog reference
  (``ActiveCatalogRecord``), or nothing when the store or the subject's catalog does not
  exist yet;
- ``library-<subject>.json``: the library of that catalog version (``CorpusLibraryRecord``):
  a row per prompt with its status, coverage totals, and each prompt's take history, built
  from the take store's records (``corpus_take_store``) and the digest-checked snapshot. It
  carries no time of its own, so an unchanged library is never rewritten and its digest
  changes exactly when its content does.

A take counts for a prompt of the active version when ``CatalogSnapshot.current_prompt_for``
says its recorded prompt does (the same prompt, or an earlier revision an alias accepts). A
prompt's status is ``kept`` when a kept take of it passed intake's QC, ``needs-attention`` when
it has kept takes and none passed, ``draft`` when it has no kept take but an undecided one,
and ``missing`` otherwise (rejected and removed takes cover nothing). ``recorded`` coverage is
``kept`` plus ``needs-attention``.

The model is rebuildable from the store and the take records, so the vault never copies it
(``library_classification``: ``corpus-read-model``).
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    write_canonical_json,
)
from .catalog_ports import CorpusStoreError
from .corpus_take_store import CorpusTakeState, TakeDecisionName, TakeTrim, read_take_states
from .corpus_takes import TakeId
from .errors import ContractError, SecurityError
from .models import COLE_SUBJECT, ContractModel, PromptLabel, Sha256, StableId, utc_now
from .paths import AssetLayout
from .recording_contracts import CatalogReference, CatalogSnapshot

READ_MODEL_DIRECTORY: Final = "corpus"
ACTIVE_CATALOG_SCHEMA: Final = "cole-voice-lab.corpus-active-catalog/v1"
LIBRARY_SCHEMA: Final = "cole-voice-lab.corpus-library/v1"
MAX_RECORD_BYTES: Final = 64 * 1024
MAX_LIBRARY_BYTES: Final = 64 * 1024 * 1024

type PromptStatus = Literal["missing", "draft", "kept", "needs-attention"]


class ActiveCatalogRecord(ContractModel):
    """The catalog version a subject records against, as of ``refreshed_at``."""

    schema_version: Literal["cole-voice-lab.corpus-active-catalog/v1"] = ACTIVE_CATALOG_SCHEMA
    subject_id: StableId
    reference: CatalogReference
    refreshed_at: datetime


def read_model_directory(layout: AssetLayout) -> Path:
    return layout.directory("derived") / READ_MODEL_DIRECTORY


def active_catalog_path(layout: AssetLayout, subject_id: str) -> Path:
    return read_model_directory(layout) / f"active-catalog-{subject_id}.json"


def refresh_active_catalog(
    layout: AssetLayout,
    subject_id: str = COLE_SUBJECT,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> ActiveCatalogRecord | None:
    """Record the subject's active catalog reference for the API.

    Opens the corpus store through its port (never creating it). Nothing is written when the
    store does not exist or the subject has no active catalog; an unchanged reference is not
    rewritten.
    Returns: the record now on disk, or ``None``.
    Raises: ``CorpusStoreError`` and ``SecurityError`` as ``open_catalog_reader`` for a store
    that exists but cannot be opened safely; ``OSError`` when the record cannot be written."""
    from .storage_binding import open_catalog_reader  # the writer side only (D-052)

    try:
        with open_catalog_reader(layout) as catalog:
            _suite, reference = catalog.active_catalog(subject_id)
    except CorpusStoreError as error:
        if error.code in ("CORPUS.STORE_MISSING", "CORPUS.NO_ACTIVE_CATALOG"):
            return None
        raise
    current = read_active_catalog(layout, subject_id)
    if current is not None and current.reference == reference:
        return current
    record = ActiveCatalogRecord(subject_id=subject_id, reference=reference, refreshed_at=clock())
    write_canonical_json(active_catalog_path(layout, subject_id), record)
    return record


def read_active_catalog(layout: AssetLayout, subject_id: str) -> ActiveCatalogRecord | None:
    """The subject's active catalog record, or ``None`` when there is none yet.

    Raises: ``SecurityError`` when the record is not an owned private regular file within its
    bound; ``ContractError`` when it is not a canonical ``ActiveCatalogRecord`` of that
    subject."""
    path = active_catalog_path(layout, subject_id)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RECORD_BYTES:
        raise SecurityError(f"the corpus read model is not a bounded regular file: {path}")
    with open_regular_no_follow(path) as stream:
        payload = stream.read(MAX_RECORD_BYTES + 1)
    try:
        record = ActiveCatalogRecord.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"the corpus read model is invalid: {path}: {error}") from error
    if payload != canonical_json_bytes(record) + b"\n" or record.subject_id != subject_id:
        raise ContractError(f"the corpus read model is not this subject's canonical record: {path}")
    return record


# -- The library ---------------------------------------------------------------------------


class LibraryTake(ContractModel):
    """One take in a prompt's history."""

    take_id: TakeId
    requested_at: datetime
    duration_seconds: float = Field(gt=0.0, allow_inf_nan=False)
    capture_host: str = Field(min_length=1, max_length=500)
    device: str = Field(min_length=1, max_length=500)
    decision: TakeDecisionName
    decision_revision: int = Field(ge=0)
    trim: TakeTrim
    qc_status: Literal["pass", "review", "fail"]
    #: The prompt digest the take was recorded against (an earlier revision's when an alias
    #: counts it), and the catalog version it named.
    recorded_prompt_sha256: Sha256
    catalog_version: int = Field(ge=1)


class LibraryRow(ContractModel):
    """One prompt of the active catalog version, in catalog order, and where it stands.

    ``latest_*`` describe the take that stands for the prompt: its newest kept take, else
    its newest undecided one; ``take_count`` counts its whole history."""

    prompt_id: StableId
    prompt_sha256: Sha256
    category: str = Field(min_length=1, max_length=64)
    labels: tuple[PromptLabel, ...]
    holdout: bool
    status: PromptStatus
    take_count: int = Field(ge=0)
    latest_take_id: TakeId | None
    latest_requested_at: datetime | None
    latest_duration_seconds: float | None = Field(gt=0.0, allow_inf_nan=False)


class LibraryCoverage(ContractModel):
    """The totals of the rows (``recorded`` is ``kept`` plus ``needs_attention``), and how
    many takes count for a prompt of this version and how many count for none."""

    prompts: int = Field(ge=0)
    recorded: int = Field(ge=0)
    kept: int = Field(ge=0)
    needs_attention: int = Field(ge=0)
    draft: int = Field(ge=0)
    missing: int = Field(ge=0)
    takes: int = Field(ge=0)
    uncounted_takes: int = Field(ge=0)


class PromptTakes(ContractModel):
    """One prompt's takes, newest first."""

    prompt_id: StableId
    takes: tuple[LibraryTake, ...] = Field(min_length=1)


class CorpusLibraryRecord(ContractModel):
    """The subject's library of one catalog version: rows, coverage and take histories."""

    schema_version: Literal["cole-voice-lab.corpus-library/v1"] = LIBRARY_SCHEMA
    subject_id: StableId
    catalog: CatalogReference
    coverage: LibraryCoverage
    rows: tuple[LibraryRow, ...]
    #: The prompts that have takes, in catalog order.
    histories: tuple[PromptTakes, ...]


def library_path(layout: AssetLayout, subject_id: str) -> Path:
    return read_model_directory(layout) / f"library-{subject_id}.json"


def _library_take(state: CorpusTakeState) -> LibraryTake:
    return LibraryTake(
        take_id=state.take_id,
        requested_at=state.requested_at,
        duration_seconds=state.audio.audio.duration_seconds,
        capture_host=state.capture_host,
        device=state.device,
        decision=state.decision.decision,
        decision_revision=state.decision.revision,
        trim=state.trim,
        qc_status=state.qc_status,
        recorded_prompt_sha256=state.prompt_sha256,
        catalog_version=state.catalog.catalog_version,
    )


def _newest(takes: Sequence[LibraryTake], decision: TakeDecisionName) -> LibraryTake | None:
    chosen = [take for take in takes if take.decision == decision]
    return max(chosen, key=lambda take: (take.requested_at, take.take_id), default=None)


def _status(takes: Sequence[LibraryTake]) -> PromptStatus:
    kept = [take for take in takes if take.decision == "kept"]
    if kept:
        return "kept" if any(take.qc_status == "pass" for take in kept) else "needs-attention"
    return "draft" if any(take.decision == "draft" for take in takes) else "missing"


def build_library(
    subject_id: str,
    reference: CatalogReference,
    snapshot: CatalogSnapshot,
    states: Iterable[CorpusTakeState],
) -> CorpusLibraryRecord:
    """The library of ``snapshot`` (the version ``reference`` names) over the subject's takes.

    Raises: nothing (every input is already a validated contract)."""
    by_prompt: defaultdict[str, list[LibraryTake]] = defaultdict(list)
    counted = uncounted = 0
    for state in states:
        if state.subject_id != subject_id:
            continue
        prompt = snapshot.current_prompt_for(state.prompt)
        if prompt is None:
            uncounted += 1
            continue
        counted += 1
        by_prompt[prompt.prompt_id].append(_library_take(state))
    rows: list[LibraryRow] = []
    histories: list[PromptTakes] = []
    for prompt in snapshot.suite.prompts:
        takes = sorted(
            by_prompt.get(prompt.prompt_id, ()),
            key=lambda take: (take.requested_at, take.take_id),
            reverse=True,
        )
        latest = _newest(takes, "kept") or _newest(takes, "draft")
        rows.append(
            LibraryRow(
                prompt_id=prompt.prompt_id,
                prompt_sha256=canonical_sha256(prompt),
                category=prompt.category,
                labels=prompt.labels,
                holdout=prompt.holdout,
                status=_status(takes),
                take_count=len(takes),
                latest_take_id=latest.take_id if latest else None,
                latest_requested_at=latest.requested_at if latest else None,
                latest_duration_seconds=latest.duration_seconds if latest else None,
            )
        )
        if takes:
            histories.append(PromptTakes(prompt_id=prompt.prompt_id, takes=tuple(takes)))
    statuses = [row.status for row in rows]
    coverage = LibraryCoverage(
        prompts=len(rows),
        recorded=statuses.count("kept") + statuses.count("needs-attention"),
        kept=statuses.count("kept"),
        needs_attention=statuses.count("needs-attention"),
        draft=statuses.count("draft"),
        missing=statuses.count("missing"),
        takes=counted,
        uncounted_takes=uncounted,
    )
    return CorpusLibraryRecord(
        subject_id=subject_id,
        catalog=reference,
        coverage=coverage,
        rows=tuple(rows),
        histories=tuple(histories),
    )


def refresh_library(
    layout: AssetLayout,
    record: ActiveCatalogRecord,
    states: Iterable[CorpusTakeState] | None = None,
) -> CorpusLibraryRecord:
    """Rebuild the library of the active version ``record`` names; write it when it changed.

    ``states`` are the take records when the caller has just read them (the publisher);
    otherwise they are read here.
    Raises: ``CorpusStoreError``/``SecurityError`` as ``corpus_store.read_catalog_snapshot``;
    ``ContractError``/``SecurityError`` as ``corpus_take_store.read_take_states``;
    ``OSError`` when the library cannot be written."""
    from .corpus_store import read_catalog_snapshot  # the file-only reader

    snapshot = read_catalog_snapshot(layout, record.reference)
    library = build_library(
        record.subject_id,
        record.reference,
        snapshot,
        read_take_states(layout) if states is None else states,
    )
    path = library_path(layout, record.subject_id)
    payload = canonical_json_bytes(library) + b"\n"
    current = _bounded_read(path, MAX_LIBRARY_BYTES) if os.path.lexists(path) else None
    if current != payload:
        write_canonical_json(path, library)
    return library


def refresh_corpus_read_model(
    layout: AssetLayout,
    subject_id: str = COLE_SUBJECT,
    *,
    states: Iterable[CorpusTakeState] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> ActiveCatalogRecord | None:
    """Refresh the whole read model: the active catalog record, then its library.

    Returns: the active catalog record, or ``None`` when there is none (and so no library).
    Raises: as ``refresh_active_catalog`` and ``refresh_library``."""
    record = refresh_active_catalog(layout, subject_id, clock=clock)
    if record is not None:
        refresh_library(layout, record, states)
    return record


def _bounded_read(path: Path, limit: int) -> bytes:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
        raise SecurityError(f"the corpus read model is not a bounded regular file: {path}")
    with open_regular_no_follow(path) as stream:
        return stream.read(limit + 1)


def read_library(layout: AssetLayout, subject_id: str) -> tuple[CorpusLibraryRecord, str] | None:
    """The subject's library and the SHA-256 of its file, or ``None`` when there is none yet.

    Raises: ``SecurityError`` when the file is not a bounded regular file; ``ContractError``
    when it is not this subject's canonical ``CorpusLibraryRecord``."""
    path = library_path(layout, subject_id)
    if not os.path.lexists(path):
        return None
    payload = _bounded_read(path, MAX_LIBRARY_BYTES)
    try:
        library = CorpusLibraryRecord.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ContractError(f"the corpus library is invalid: {path}: {error}") from error
    if payload != canonical_json_bytes(library) + b"\n" or library.subject_id != subject_id:
        raise ContractError(f"the corpus library is not this subject's canonical record: {path}")
    return library, hashlib.sha256(payload).hexdigest()
