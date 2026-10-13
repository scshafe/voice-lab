"""What the corpus client says about itself, for the UI to poll or observe (no QObject)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from cole_voice_lab.recording_contracts import CatalogReference

from .records import Notice

HELD_FOR_S1: Final = (
    "Takes, decisions and trims stay on this machine until spike S-1 passes on this platform "
    "(D-042); then turn on corpus sync (corpus_sync = true in config.toml)."
)
NO_CATALOG: Final = (
    "No catalog yet: the app needs to reach the PC once to fetch the catalog it records against."
)


@dataclass(frozen=True)
class SyncStatus:
    """The corpus client at one moment.

    Counts: ``queued`` operations wait in the outboxes (of ``waiting_takes`` takes, of which
    ``awaiting_receipt`` wait for the PC to publish their upload); ``sent`` uploads were
    accepted and changes applied; the PC ``refused`` some, and ``dropped`` changes were queued
    behind a refusal; ``local_only`` takes are never sent (simulated audio). Catalog:
    ``pinned_catalog`` is the version this session records against, ``pc_catalog`` the PC's
    active version as last seen; ``switch_pending`` means the next session records against
    the PC's. ``notices`` are the ones not dismissed yet, oldest first."""

    enabled: bool
    #: Why nothing is sent ("" when sending is on).
    held: str
    busy: bool
    offline: bool
    sign_in_required: bool
    last_sync_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str
    #: Why no upload can start ("" when the home-host approval stands for this server).
    upload_blocked: str
    takes: int
    queued: int
    waiting_takes: int
    awaiting_receipt: int
    sent: int
    refused: int
    dropped: int
    local_only: int
    pinned_catalog: CatalogReference | None
    pc_catalog: CatalogReference | None
    pc_catalog_unavailable: bool
    switch_pending: bool
    can_record: bool
    #: Why nothing can be recorded ("" when a catalog is pinned).
    record_blocked: str
    notices: tuple[Notice, ...]
