"""The corpus's read model on the PC (D-052): what the API serves without opening the store.

@canonical corpus_read_model -- the network-facing API's lab mount is read-only, and the
corpus store cannot be opened read-only (it takes its locks and WAL on open). So the store's
writers keep a small read model under ``derived/corpus/``, and the API reads only that model
and the catalog snapshots it names. The snapshots are content-addressed and digest-checked by
``corpus_store.read_catalog_snapshot``, so the API never trusts the read model for the
prompts themselves.

Writers: the intake publisher refreshes the model on every pass (the PC's one lab writer),
and ``voice-lab catalog publish|activate`` refreshes it at once. The model holds:

- ``active-catalog-<subject>.json``: the subject's active catalog reference
  (``ActiveCatalogRecord``), or nothing when the store or the subject's catalog does not
  exist yet.

The model is rebuildable from the store, so the vault never copies it
(``library_classification``: ``corpus-read-model``).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from .canonical import canonical_json_bytes, open_regular_no_follow, write_canonical_json
from .catalog_ports import CorpusStoreError
from .errors import ContractError, SecurityError
from .models import COLE_SUBJECT, ContractModel, StableId, utc_now
from .paths import AssetLayout
from .recording_contracts import CatalogReference

READ_MODEL_DIRECTORY: Final = "corpus"
ACTIVE_CATALOG_SCHEMA: Final = "cole-voice-lab.corpus-active-catalog/v1"
MAX_RECORD_BYTES: Final = 64 * 1024


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
