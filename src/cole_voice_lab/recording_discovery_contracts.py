"""Typed, bounded discovery publications; no worker operation exposes an index API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .models import ContractModel, Sha256, StableId
from .recording_contracts import (
    RecordingError,
    RecordingGroupSummary,
    RecordingHistoryEntry,
    RelativePath,
    UtcTimestamp,
)

MAX_DISCOVERY_RECORDS = 200_000
MAX_DISCOVERY_RECORD_BYTES = 1_048_576
MAX_DISCOVERY_SPOOL_BYTES = MAX_DISCOVERY_RECORDS * (MAX_DISCOVERY_RECORD_BYTES + 4)


class DiscoveryRequest(ContractModel):
    schema_version: Literal["cole-voice-lab.discovery-worker/v1"] = (
        "cole-voice-lab.discovery-worker/v1"
    )
    generation: StableId
    group_id: StableId
    subject_id: StableId
    catalog_sha256: Sha256
    index_instance: StableId
    legacy_fingerprint: Sha256 | None = None
    native_fingerprint: Sha256 | None = None
    legacy_token: tuple[int, int]


class DiscoverySpoolRef(ContractModel):
    offset: int = Field(ge=0)
    size_bytes: int = Field(ge=1, le=MAX_DISCOVERY_SPOOL_BYTES)
    sha256: Sha256
    record_count: int = Field(ge=1, le=MAX_DISCOVERY_RECORDS)


class DiscoveryNativeInventory(ContractModel):
    take_id: StableId
    state_revision: int = Field(ge=0)


class DiscoveryWorkerStart(ContractModel):
    request: DiscoveryRequest
    inventory: DiscoverySpoolRef | None


class DiscoveryAlias(ContractModel):
    kind: Literal["alias"] = "alias"
    take_id: StableId
    path: RelativePath
    sha256: Sha256
    alias_kind: Literal["package-raw", "package-tmp"]


class DiscoveryBegin(ContractModel):
    kind: Literal["begin"] = "begin"


class DiscoveryNative(ContractModel):
    kind: Literal["native"] = "native"
    source: DiscoverySpoolRef


class DiscoveryInvalid(ContractModel):
    kind: Literal["invalid"] = "invalid"
    take_id: StableId
    expected_state_revision: int = Field(ge=0)
    error: RecordingError


class DiscoveryLegacy(ContractModel):
    kind: Literal["legacy"] = "legacy"
    source: DiscoverySpoolRef
    expected_token: tuple[int, int]


class DiscoveryFinished(ContractModel):
    kind: Literal["finished"] = "finished"
    legacy_fingerprint: Sha256 | None = None
    native_fingerprint: Sha256 | None = None
    native_reconciled: bool = False
    legacy_backfilled: bool = False
    deferred: bool = False
    error: RecordingError | None = None


type DiscoveryPublication = Annotated[
    DiscoveryBegin
    | DiscoveryNative
    | DiscoveryAlias
    | DiscoveryInvalid
    | DiscoveryLegacy
    | DiscoveryFinished,
    Field(discriminator="kind"),
]


class DiscoveryEnvelope(ContractModel):
    generation: StableId
    sequence: int = Field(ge=1, le=MAX_DISCOVERY_RECORDS)
    publication: DiscoveryPublication


class DiscoveryAck(ContractModel):
    generation: StableId
    sequence: int = Field(ge=1, le=MAX_DISCOVERY_RECORDS)
    applied: bool


class LegacyProjectionRow(ContractModel):
    kind: Literal["row"] = "row"
    entry: RecordingHistoryEntry
    opened_at: UtcTimestamp | None
    aliases: tuple[
        tuple[RelativePath, Sha256, Literal["legacy-raw", "legacy-tmp", "rejected"]], ...
    ] = Field(max_length=32)


class LegacyProjectionHeader(ContractModel):
    kind: Literal["header"] = "header"
    catalog_sha256: Sha256


class LegacyProjectionGroup(ContractModel):
    kind: Literal["group"] = "group"
    group: RecordingGroupSummary


class LegacyProjectionUnknown(ContractModel):
    kind: Literal["unknown"] = "unknown"
    record_id: str = Field(min_length=1, max_length=1024)
    error: RecordingError


type LegacyProjectionRecord = Annotated[
    LegacyProjectionHeader | LegacyProjectionRow | LegacyProjectionGroup | LegacyProjectionUnknown,
    Field(discriminator="kind"),
]
