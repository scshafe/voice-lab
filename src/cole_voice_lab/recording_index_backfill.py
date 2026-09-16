"""Explicit background legacy import through the canonical recording library.

This permanent reader preserves v1/desktop evidence; it is not a temporary dual
write or old/new routing switch. Interactive history never invokes this module.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import TypeAdapter

from .audio import inspect_canonical_wav
from .canonical import canonical_sha256
from .capture import expected_recording_paths
from .errors import ContractError, SecurityError
from .paths import confined_path
from .prompt_catalogs import PILOT_V1_SHA256
from .recording_contracts import (
    AnalysisState,
    DecisionState,
    ExportState,
    RecordingError,
    RecordingGroupSummary,
    RecordingHistoryEntry,
    RecordingTake,
)
from .recording_discovery_contracts import (
    DiscoveryAlias,
    DiscoverySpoolRef,
    LegacyProjectionGroup,
    LegacyProjectionHeader,
    LegacyProjectionRecord,
    LegacyProjectionRow,
    LegacyProjectionUnknown,
)
from .recording_discovery_spool import iter_spool, spool_records, verify_spool
from .recording_edits import project_legacy_edit
from .recording_index import RecordingIndex, RecordingIndexError, _AliasKind
from .recording_library import DesktopSessionEvidence, RecordingHistory, RecordingLibrary
from .recording_takes import RecordingMetadataBusyError, RecordingTakeStore


def _legacy_identity(entry: RecordingHistory) -> str:
    # raw/tmp path roots are aliases; session, prompt, source date and relative path
    # identify the recording event. Identical PCM from another event stays distinct.
    return "legacy-" + canonical_sha256(
        {
            "session_id": entry.session_id,
            "prompt_id": entry.prompt_id,
            "recorded_at": entry.recorded_at.isoformat(),
            "relative_path": entry.relative_path,
            "sha256": entry.sha256,
        }
    )


def legacy_projection_records(
    library: RecordingLibrary,
    publish_native: Callable[[RecordingTake, DiscoveryAlias], None],
    *,
    take_store: RecordingTakeStore | None = None,
) -> Iterator[LegacyProjectionRecord]:
    """Produce validated legacy metadata; source inspection never requires an index handle.

    Each row keeps the exact prompt its audio was recorded against: the library catalog's
    prompt, or an earlier revision the library's catalog snapshot accepts through a prompt
    alias (the index then counts the row for the current prompt).
    Raises: ``RecordingIndexError`` wrapping any evidence, contract, security or I/O failure
    ("legacy backfill failed; preserved history is Unknown: ...").
    """
    yield LegacyProjectionHeader(catalog_sha256=library.catalog_sha256)
    try:
        history, sessions, evidence = library.verified_history_snapshot()
        sidecars = {item.session_id: item for item in evidence}
        session_dates = {item.session_id: item.session_started_at for item in sessions}
        native_sessions = {item.session_id for item in history if item.native_take_id is not None}
        groups = tuple(
            RecordingGroupSummary(
                group_id=None,
                legacy_session_id=item.session_id,
                opened_at=item.session_started_at,
                date_precision="unknown" if item.session_started_at is None else "session",
                take_count=0,
                kept_count=0,
            )
            for item in sessions
            if item.session_id not in native_sessions
        )
        unknown = tuple(
            (
                f"legacy:{item.session_id}",
                RecordingError(
                    code="RECORDING.HISTORY_UNKNOWN",
                    message=f"Unknown prompts/date for legacy session: {item.session_id}",
                    details=(f"tmp/capture-{item.session_id}",),
                    retryable=False,
                ),
            )
            for item in sessions
            if item.session_started_at is None or not item.catalog_known
        )

        yield from (LegacyProjectionGroup(group=group) for group in groups)
        yield from (
            LegacyProjectionUnknown(record_id=record_id, error=error)
            for record_id, error in unknown
        )

        def entries() -> Iterator[
            tuple[RecordingHistoryEntry, datetime | None, tuple[tuple[str, str, _AliasKind], ...]]
        ]:
            observed: set[tuple[str, str, datetime]] = set()
            for old in history:
                if old.native_take_id is not None:
                    from .recording_packages import resolve_native_package_alias

                    native = resolve_native_package_alias(
                        library.layout,
                        subject_id=library.subject_id,
                        session_id=old.session_id,
                        prompt_id=old.prompt_id,
                        source_sha256=old.sha256,
                        take_store=take_store,
                    )
                    if native is None or native.reservation.take_id != old.native_take_id:
                        raise SecurityError("native package history lost its authoritative take")
                    relative = (
                        (old.session_directory / old.relative_path)
                        .relative_to(library.layout.root)
                        .as_posix()
                    )
                    publish_native(
                        native,
                        DiscoveryAlias(
                            take_id=old.native_take_id,
                            path=relative,
                            sha256=old.sha256,
                            alias_kind="package-raw"
                            if relative.startswith("raw/")
                            else "package-tmp",
                        ),
                    )
                    continue
                sidecar = sidecars.get(old.session_id)
                take_evidence = (
                    None
                    if sidecar is None
                    else next(
                        (
                            take
                            for take in sidecar.takes
                            if take.prompt_id == old.prompt_id
                            and take.take_started_at == old.recorded_at
                            and (old.status == "Rejected" or not take.rejected)
                            and (take.audio_sha256 is None or take.audio_sha256 == old.sha256)
                        ),
                        None,
                    )
                )
                source = confined_path(old.session_directory, old.relative_path, must_exist=True)
                inspection = inspect_canonical_wav(source)
                if inspection.sha256 != old.sha256:
                    raise SecurityError(f"legacy source changed during metadata backfill: {source}")
                decision: DecisionState = (
                    "removed"
                    if old.status == "Removed"
                    else "rejected"
                    if old.status == "Rejected"
                    else "kept"
                    if sidecar is None or (take_evidence is not None and take_evidence.accepted)
                    else "draft"
                )
                metrics = inspection.qc_metrics(
                    old.relative_path, allow_quiet=old.recorded_prompt.allows_quiet
                )
                analysis: AnalysisState = "verified" if metrics.passed else "needs-review"
                source_relative = source.relative_to(library.layout.root).as_posix()
                exported: ExportState = (
                    "accepted-raw"
                    if source_relative.startswith("raw/")
                    else "prepared"
                    if confined_path(old.session_directory, "READY").exists()
                    else "unprepared"
                )
                new = RecordingHistoryEntry(
                    take_id=_legacy_identity(old),
                    subject_id=library.subject_id,
                    group_id=None,
                    legacy_session_id=old.session_id,
                    # The source catalog, never the library's: sidecar-less captures map
                    # only through pinned pilot-v1, so every catalog emits identical rows.
                    catalog_sha256=PILOT_V1_SHA256 if sidecar is None else sidecar.catalog_sha256,
                    prompt=old.recorded_prompt,
                    recorded_at=old.recorded_at,
                    date_precision=old.date_precision,
                    source_sha256=old.sha256,
                    source_frames=inspection.audio.frames,
                    decision=decision,
                    analysis=analysis,
                    export=exported,
                    state_revision=0,
                )
                observed.add((old.session_id, old.prompt_id, old.recorded_at))
                source_relative = source.relative_to(library.layout.root).as_posix()
                kind: _AliasKind = (
                    "legacy-raw"
                    if source_relative.startswith("raw/")
                    else "rejected"
                    if old.session_directory.name.endswith(".rejected")
                    else "legacy-tmp"
                )
                aliases: list[tuple[str, str, _AliasKind]] = [(source_relative, old.sha256, kind)]
                # The strict scan validated both aliases before deduplicating package
                # copies. Bind only the standard same-session finalized tmp path.
                temporary = library.layout.directory("tmp") / f"capture-{old.session_id}"
                if kind == "legacy-raw" and confined_path(temporary, "READY").exists():
                    alias = confined_path(temporary, old.relative_path, must_exist=True)
                    aliases.append(
                        (
                            alias.relative_to(library.layout.root).as_posix(),
                            old.sha256,
                            "legacy-tmp",
                        )
                    )
                yield new, session_dates.get(old.session_id), tuple(aliases)
            for sidecar in evidence:
                if sidecar.subject_id != library.subject_id:
                    continue
                yield from _interrupted_entries(library, sidecar, observed)

        for entry, opened_at, aliases in entries():
            yield LegacyProjectionRow(
                entry=project_legacy_edit(library.layout, entry),
                opened_at=opened_at,
                aliases=cast(
                    tuple[tuple[str, str, Literal["legacy-raw", "legacy-tmp", "rejected"]], ...],
                    aliases,
                ),
            )
    except (OSError, ContractError, SecurityError, ValueError) as error:
        raise RecordingIndexError(
            f"legacy backfill failed; preserved history is Unknown: {error}"
        ) from error


_LEGACY_RECORD: TypeAdapter[LegacyProjectionRecord] = TypeAdapter(LegacyProjectionRecord)


def prepared_legacy_records(
    descriptor: int, source: DiscoverySpoolRef
) -> Iterator[LegacyProjectionRecord]:
    """Decode completed local metadata; never inspect historical sources during publication."""
    for payload in iter_spool(descriptor, source):
        yield _LEGACY_RECORD.validate_json(payload)


def publish_legacy_spool(
    index: RecordingIndex,
    catalog: str,
    descriptor: int,
    source: DiscoverySpoolRef,
    token: tuple[int, int],
) -> int | None:
    """Verify and publish one staged legacy result using only the owner's existing index."""
    verify_spool(descriptor, source)
    # Strictly decode every record before entering any index operation.
    records = prepared_legacy_records(descriptor, source)
    header = next(records, None)
    if not isinstance(header, LegacyProjectionHeader) or header.catalog_sha256 != catalog:
        raise SecurityError("legacy spool requires its exact catalog header first")
    for record in records:
        if isinstance(record, LegacyProjectionHeader):
            raise SecurityError("legacy spool repeats its catalog header")
    return index.replace_legacy_catalog_if_current(
        catalog,
        (
            (row.entry, row.opened_at, row.aliases)
            for row in prepared_legacy_records(descriptor, source)
            if isinstance(row, LegacyProjectionRow)
        ),
        (
            row.group
            for row in prepared_legacy_records(descriptor, source)
            if isinstance(row, LegacyProjectionGroup)
        ),
        (
            (row.record_id, row.error)
            for row in prepared_legacy_records(descriptor, source)
            if isinstance(row, LegacyProjectionUnknown)
        ),
        expected_token=token,
    )


def backfill_recording_library(library: RecordingLibrary, index: RecordingIndex) -> int:
    """Standalone compatibility boundary; share worker preparation and parent publication.

    Registers the library's catalog snapshot (with its prompt aliases) before publishing.
    Returns: the index revision after publication.
    Raises: ``RecordingIndexError`` when the library and index disagree on root or subject,
    when the catalog conflicts with its indexed identity, or when backfill fails (the
    catalog is then marked Unknown with record ``legacy-backfill``);
    ``RecordingMetadataBusyError`` when legacy evidence changed during inspection.
    """
    if index.layout != library.layout or index.subject_id != library.subject_id:
        raise RecordingIndexError("legacy backfill context does not match the indexed library")
    catalog = index.register_catalog(library.catalog)
    index.set_completeness(catalog, "building")
    token = index.legacy_publication_token()

    def publish_native(take: RecordingTake, alias: DiscoveryAlias) -> None:
        index.update_take(take)
        index.register_alias(alias.take_id, alias.path, alias.sha256, alias.alias_kind)

    try:
        with tempfile.TemporaryFile(mode="w+b", dir=library.layout.directory("tmp")) as spool:
            source = spool_records(
                spool.fileno(), legacy_projection_records(library, publish_native)
            )
            result = publish_legacy_spool(index, catalog, spool.fileno(), source, token)
            if result is None:
                raise RecordingMetadataBusyError("legacy projection changed during inspection")
            return result
    except RecordingMetadataBusyError:
        raise
    except (OSError, ContractError, SecurityError, ValueError) as error:
        index.mark_unknown(
            catalog,
            "legacy-backfill",
            RecordingError(
                code="RECORDING.HISTORY_UNKNOWN",
                message=f"Legacy metadata backfill failed: {error}"[:1024],
                retryable=False,
            ),
        )
        raise RecordingIndexError(
            f"legacy backfill failed; preserved history is Unknown: {error}"
        ) from error


def _interrupted_entries(
    library: RecordingLibrary,
    sidecar: DesktopSessionEvidence,
    observed: set[tuple[str, str, datetime]],
) -> Iterator[
    tuple[RecordingHistoryEntry, datetime | None, tuple[tuple[str, str, _AliasKind], ...]]
]:
    paths = dict(
        zip(
            (p.prompt_id for p in sidecar.selected_suite.prompts),
            expected_recording_paths(sidecar.selected_suite),
            strict=True,
        )
    )
    selected = {item.prompt_id: item for item in sidecar.selected_suite.prompts}
    for take in sidecar.takes:
        if (
            sidecar.session_id,
            take.prompt_id,
            take.take_started_at,
        ) in observed or not library.counts(selected[take.prompt_id]):
            continue
        if take.audio_sha256 is not None or take.accepted or take.rejected:
            raise SecurityError(
                f"legacy take evidence disappeared: {sidecar.session_id}/{take.prompt_id}"
            )
        partial_name = Path(paths[take.prompt_id]).with_suffix(".partial.wav").as_posix()
        relative = f"tmp/capture-{sidecar.session_id}/{partial_name}"
        partial = confined_path(library.layout.root, relative)
        error = RecordingError(
            code="RECORDING.CONTINUITY_FAILED",
            message="Interrupted legacy take has no validated sealed source",
            details=(relative,),
            retryable=False,
        )
        yield (
            RecordingHistoryEntry(
                take_id="legacy-"
                + canonical_sha256(
                    {
                        "session_id": sidecar.session_id,
                        "prompt_id": take.prompt_id,
                        "recorded_at": take.take_started_at.isoformat(),
                        "partial_path": relative,
                    }
                ),
                subject_id=library.subject_id,
                group_id=None,
                legacy_session_id=sidecar.session_id,
                catalog_sha256=sidecar.catalog_sha256,
                prompt=selected[take.prompt_id],
                recorded_at=take.take_started_at,
                date_precision="take",
                source_sha256=None,
                source_frames=None,
                decision="draft",
                analysis="failed" if partial.exists() else "pending",
                export="unprepared",
                state_revision=0,
                error=error,
            ),
            sidecar.session_started_at,
            (),
        )
