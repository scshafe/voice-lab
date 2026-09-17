"""Read-only per-prompt coverage report for comparing two catalog versions.

@canonical prompt_coverage -- coverage is the index rule (a retained take of the subject
whose recorded prompt hash is the prompt's current hash or one of its accepted aliases,
``CatalogSnapshot.current_prompt_for``) computed from evidence through the canonical legacy
reader and native head reader. It never constructs RecordingIndex, never opens SQLite and
never writes under the asset root. Legacy WAVs and catalog snapshot files are opened
read-only; a caller that names a published version resolves it (``CorpusStore.materialize``)
before calling, and the report only checks that the snapshot is exactly the file the
reference seals. ``prompt_recordings`` counts the same way for one prompt revision (what a
``voice-lab catalog edit-prompt`` keeps or invalidates).
Related: recording_index.py, corpus_store.py (resolve_group_catalog).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import JsonValue

from .canonical import canonical_sha256
from .catalog_ports import CorpusSnapshotSecurityError
from .corpus_store import (
    GroupCatalogCache,
    GroupCatalogKey,
    prompt_catalog_fit,
    require_referenced_snapshot,
)
from .errors import ContractError, VoiceLabError
from .models import Prompt, PromptSuite
from .paths import AssetLayout
from .recording_contracts import (
    CatalogReference,
    CatalogSnapshot,
    PromptAlias,
    RecordingHistoryEntry,
    RecordingTake,
)
from .recording_discovery_contracts import (
    DiscoveryAlias,
    LegacyProjectionRow,
    LegacyProjectionUnknown,
)
from .recording_index import history_entry_for_take, is_retained
from .recording_index_backfill import legacy_projection_records
from .recording_library import RecordingLibrary
from .recording_takes import TAKE_PROVENANCE_ROOT, RecordingTakeStore


class PromptCoverageProjectionError(ContractError):
    """Evidence could not be projected; the report must not read as missing coverage."""


def coverage_catalog(catalog: PromptSuite | CatalogSnapshot) -> CatalogSnapshot:
    """The snapshot a coverage reader matches recordings through.

    Params: ``catalog`` is a plain ``PromptSuite`` (a built-in or embedded catalog, where only
    its own prompts count) or a published ``CatalogSnapshot`` (whose prompt aliases also
    count earlier revisions of a prompt).
    Returns: ``catalog`` itself, or ``CatalogSnapshot.embedded`` of the plain suite.
    Raises: nothing for a validated catalog.
    """
    return catalog if isinstance(catalog, CatalogSnapshot) else CatalogSnapshot.embedded(catalog)


@dataclass(frozen=True, slots=True)
class PromptCoverageRow:
    prompt_id: str
    prompt_sha256: str
    covered: bool
    retained_take_count: int
    changed: bool | None
    covered_in_baseline: bool | None


@dataclass(frozen=True, slots=True)
class PromptCoverageReport:
    subject_id: str
    catalog_sha256: str
    baseline_catalog_sha256: str | None
    prompts: tuple[PromptCoverageRow, ...]
    covered_prompt_count: int
    total_prompt_count: int
    missing_prompt_ids: tuple[str, ...]
    changed_prompt_ids: tuple[str, ...]
    lost_prompt_ids: tuple[str, ...]
    unknown_record_ids: tuple[str, ...]
    baseline_unknown_record_ids: tuple[str, ...]
    # The published versions compared, or None for a built-in or embedded suite.
    catalog_id: str | None = None
    catalog_version: int | None = None
    baseline_catalog_id: str | None = None
    baseline_catalog_version: int | None = None

    @property
    def exit_status(self) -> int:
        """Losses must be explained by changed prompts, and Unknowns must predate the switch."""
        explained = set(self.lost_prompt_ids) <= set(self.changed_prompt_ids)
        preexisting = set(self.unknown_record_ids) <= set(self.baseline_unknown_record_ids)
        return 0 if explained and preexisting else 1

    def to_json(self) -> dict[str, JsonValue]:
        prompts: list[JsonValue] = []
        for row in self.prompts:
            item: dict[str, JsonValue] = {
                "prompt_id": row.prompt_id,
                "prompt_sha256": row.prompt_sha256,
                "covered": row.covered,
                "retained_take_count": row.retained_take_count,
            }
            if self.baseline_catalog_sha256 is not None:
                item |= {"changed": row.changed, "covered_in_baseline": row.covered_in_baseline}
            prompts.append(item)
        return {
            "subject_id": self.subject_id,
            "catalog_id": self.catalog_id,
            "catalog_version": self.catalog_version,
            "catalog_sha256": self.catalog_sha256,
            "baseline_catalog_id": self.baseline_catalog_id,
            "baseline_catalog_version": self.baseline_catalog_version,
            "baseline_catalog_sha256": self.baseline_catalog_sha256,
            "prompts": prompts,
            "covered_prompt_count": self.covered_prompt_count,
            "total_prompt_count": self.total_prompt_count,
            "missing_prompt_ids": list(self.missing_prompt_ids),
            "changed_prompt_ids": list(self.changed_prompt_ids),
            "lost_prompt_ids": list(self.lost_prompt_ids),
            "unknown_record_ids": list(self.unknown_record_ids),
            "baseline_unknown_record_ids": list(self.baseline_unknown_record_ids),
            "exit_status": self.exit_status,
        }


@dataclass(frozen=True, slots=True)
class PromptRecordings:
    """The subject's retained takes that count for one prompt revision now."""

    subject_id: str
    prompt_id: str
    prompt_sha256: str
    take_ids: tuple[str, ...]
    # Records whose prompts cannot be told at all (a native package without its map, a
    # legacy session without provenance); a readable package that simply holds no
    # counting recording of this prompt is not one of them.
    unknown_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CatalogCoverage:
    retained: dict[str, frozenset[str]]  # prompt sha256 -> distinct retained take IDs
    unknown_record_ids: tuple[str, ...]


def iter_native_takes(store: RecordingTakeStore | None) -> Iterator[RecordingTake]:
    """Every committed native take head, paged in take-id order; nothing without a store.

    Raises: whatever ``RecordingTakeStore.iter_takes_for_projection`` raises
    (``SecurityError`` / ``ContractError`` for unsafe or invalid evidence,
    ``RecordingMetadataBusyError`` while a commit is pending, ``OSError``).
    """
    if store is None:
        return
    after = None
    while page := store.iter_takes_for_projection(after_take_id=after):
        yield from page
        after = page[-1].reservation.take_id


def subject_native_takes(
    store: RecordingTakeStore | None, subject_id: str
) -> Iterator[RecordingTake]:
    """The subject's committed native takes, each after its group's exact catalog resolved.

    A take whose recording-group/v2 references a snapshot that is missing or altered is never
    counted as evidence: the reader fails closed exactly as the index does when it projects
    the take. Each distinct group catalog is resolved once per call, as a file
    (``RecordingTakeStore.group_catalog``), never through the corpus database.
    Params: ``store`` is a read-only take store (or ``None`` for a root without native
    evidence); ``subject_id`` selects the takes whose group belongs to that subject.
    Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``; ``CORPUS.SNAPSHOT_MISSING``
    or ``CORPUS.SNAPSHOT_MISMATCH``) for such a take; otherwise what ``iter_native_takes``
    raises.
    """
    if store is None:
        return
    # Keys, not snapshots: a library with many catalog versions resolves each one once here,
    # whatever the store's bounded cache evicts while takes of different groups interleave.
    resolved: set[GroupCatalogKey] = set()
    for take in iter_native_takes(store):
        group = take.reservation.group
        if group.subject_id != subject_id:
            continue
        key = GroupCatalogCache.key(group)
        if key not in resolved:
            store.group_catalog(group)
            resolved.add(key)
        yield take


def _coverage(
    layout: AssetLayout,
    subject_id: str,
    catalog: CatalogSnapshot,
    native: tuple[RecordingHistoryEntry, ...],
    store: RecordingTakeStore | None,
) -> _CatalogCoverage:
    digests = {prompt.prompt_id: canonical_sha256(prompt) for prompt in catalog.suite.prompts}
    entries = list(native)
    unknown: list[str] = []

    def collect(take: RecordingTake, alias: DiscoveryAlias) -> None:
        # Package aliases resolve to native heads the store enumeration already holds.
        entries.append(history_entry_for_take(take))

    library = RecordingLibrary(layout, catalog, subject_id)
    for record in legacy_projection_records(library, collect, take_store=store):
        if isinstance(record, LegacyProjectionRow):
            entries.append(record.entry)
        elif isinstance(record, LegacyProjectionUnknown):
            unknown.append(record.record_id)
    retained: dict[str, set[str]] = {digest: set() for digest in digests.values()}
    for entry in entries:
        # Rows keep the prompt the audio was recorded against; the snapshot maps it to the
        # current prompt it counts for (itself, or through a keep-recordings alias).
        current = catalog.current_prompt_for(entry.prompt)
        if (
            current is not None
            and entry.subject_id == subject_id
            and is_retained(entry.decision, entry.analysis)
        ):
            retained[digests[current.prompt_id]].add(entry.take_id)
    return _CatalogCoverage(
        {digest: frozenset(takes) for digest, takes in retained.items()}, tuple(sorted(unknown))
    )


def _native_entries(
    layout: AssetLayout, subject_id: str
) -> tuple[RecordingTakeStore | None, tuple[RecordingHistoryEntry, ...]]:
    # A root that never held native evidence has no take store; the read-only store
    # would otherwise wait for a service owner to create its directories.
    store = (
        RecordingTakeStore(layout, read_only=True)
        if os.path.lexists(layout.root / TAKE_PROVENANCE_ROOT)
        else None
    )
    native = tuple(history_entry_for_take(take) for take in subject_native_takes(store, subject_id))
    return store, native


def prompt_coverage_report(
    layout: AssetLayout,
    subject_id: str,
    catalog: PromptSuite | CatalogSnapshot,
    baseline: PromptSuite | CatalogSnapshot | None = None,
    *,
    reference: CatalogReference | None = None,
    baseline_reference: CatalogReference | None = None,
) -> PromptCoverageReport:
    """Compute coverage under catalog, and what changes relative to baseline.

    A prompt is covered when a retained take of the subject was recorded against its current
    prompt or, for a published ``CatalogSnapshot``, against an earlier revision its prompt
    aliases accept (same prompt id). ``changed`` stays exact: a prompt whose digest differs
    from the baseline's is changed even when its recordings keep counting, so a
    keep-recordings edit reads as changed but not lost.
    Params: ``catalog`` and ``baseline`` are plain suites or published snapshots (see
    ``coverage_catalog``); the caller resolves a published version (file read or
    ``CorpusStore.materialize``) before calling. ``reference`` / ``baseline_reference`` name
    the published version each snapshot was materialized from (``None`` for a built-in);
    the report records their catalog id and version.
    Returns: the report; ``exit_status`` is 0 when every loss is a changed prompt and every
    Unknown predates the switch.
    Raises: ``PromptCoverageProjectionError`` (a ``ContractError``) when evidence cannot be
    read or projected; ``CorpusSnapshotSecurityError`` (a ``SecurityError``,
    ``CORPUS.SNAPSHOT_MISSING`` / ``CORPUS.SNAPSHOT_MISMATCH``), unwrapped, when a native
    take's group references a snapshot that is missing or altered, or when ``reference`` /
    ``baseline_reference`` does not seal exactly its catalog.
    """
    layout = AssetLayout.validate(layout.root)
    snapshot = coverage_catalog(catalog)
    earlier = None if baseline is None else coverage_catalog(baseline)
    if reference is not None:
        require_referenced_snapshot(snapshot, reference)
    if baseline_reference is not None:
        if earlier is None:
            raise ValueError("a baseline reference needs the baseline catalog it names")
        require_referenced_snapshot(earlier, baseline_reference)
    try:
        store, native = _native_entries(layout, subject_id)
        current = _coverage(layout, subject_id, snapshot, native, store)
        previous = (
            None if earlier is None else _coverage(layout, subject_id, earlier, native, store)
        )
    except CorpusSnapshotSecurityError:
        raise  # a sealed reference that no longer resolves is a hard failure, not a gap
    except (OSError, ValueError, VoiceLabError) as error:
        raise PromptCoverageProjectionError(f"coverage projection failed: {error}") from error

    def covered(coverage: _CatalogCoverage, digest: str) -> bool:
        return bool(coverage.retained[digest])

    baseline_prompts = {} if earlier is None else {p.prompt_id: p for p in earlier.suite.prompts}
    rows = []
    for prompt in snapshot.suite.prompts:
        digest = canonical_sha256(prompt)
        before = baseline_prompts.get(prompt.prompt_id)
        rows.append(
            PromptCoverageRow(
                prompt_id=prompt.prompt_id,
                prompt_sha256=digest,
                covered=covered(current, digest),
                retained_take_count=len(current.retained[digest]),
                changed=None
                if previous is None
                else before is None or canonical_sha256(before) != digest,
                covered_in_baseline=None
                if previous is None or before is None
                else covered(previous, canonical_sha256(before)),
            )
        )
    by_id = {row.prompt_id: row for row in rows}
    changed: list[str] = []
    lost: list[str] = []
    if previous is not None and earlier is not None:
        for before in earlier.suite.prompts:
            now = by_id.get(before.prompt_id)
            digest = canonical_sha256(before)
            if now is None or now.prompt_sha256 != digest:
                changed.append(before.prompt_id)
            if covered(previous, digest) and (now is None or not now.covered):
                lost.append(before.prompt_id)
    return PromptCoverageReport(
        subject_id=subject_id,
        catalog_sha256=snapshot.catalog_sha256,
        baseline_catalog_sha256=None if earlier is None else earlier.catalog_sha256,
        prompts=tuple(rows),
        covered_prompt_count=sum(row.covered for row in rows),
        total_prompt_count=len(rows),
        missing_prompt_ids=tuple(row.prompt_id for row in rows if not row.covered),
        changed_prompt_ids=tuple(changed),
        lost_prompt_ids=tuple(lost),
        unknown_record_ids=current.unknown_record_ids,
        baseline_unknown_record_ids=() if previous is None else previous.unknown_record_ids,
        catalog_id=None if reference is None else reference.catalog_id,
        catalog_version=None if reference is None else reference.catalog_version,
        baseline_catalog_id=None if baseline_reference is None else baseline_reference.catalog_id,
        baseline_catalog_version=None
        if baseline_reference is None
        else baseline_reference.catalog_version,
    )


def _counting_suite(prompt: Prompt) -> PromptSuite:
    """The one-prompt catalog a corpus-store revision is counted through.

    Recordings count by the recorded prompt alone, never by the catalog around it, so any
    suite that validates with the prompt gives the same answer: the first prompt-suite/v2
    purpose that holds it (``corpus_store.prompt_catalog_fit``). The store never holds a
    revision that no such purpose can hold.
    Raises: ``ValueError`` for a prompt no prompt-suite/v2 catalog can hold (not a store
    revision; a caller bug).
    """
    fit = prompt_catalog_fit(prompt)
    suite = next(iter(fit.suites.values()), None)
    if suite is None:
        raise ValueError(
            f"prompt {prompt.prompt_id} fits no prompt-suite/v2 catalog ({fit.explanation()}), "
            "so it is no corpus-store revision whose recordings could be counted"
        )
    return suite


def _unassignable(layout: AssetLayout, record_ids: tuple[str, ...]) -> tuple[str, ...]:
    """The Unknown records of a one-prompt projection that are Unknown in every catalog.

    A subject's native package carries a map naming the exact prompt of every take, so it
    is read completely; under a one-prompt catalog it only reads as Unknown ("catalog
    unknown") because it holds no recording that counts for that prompt. Sessions without
    a map or without provenance stay Unknown: their prompts cannot be told.
    """
    from .recording_packages import load_recording_package_map

    kept: list[str] = []
    for record_id in record_ids:
        if (
            record_id.startswith("legacy:native-")
            and load_recording_package_map(layout, record_id.removeprefix("legacy:")) is not None
        ):
            continue
        kept.append(record_id)
    return tuple(kept)


def prompt_recordings(
    layout: AssetLayout,
    subject_id: str,
    prompt: Prompt,
    accepted_prompt_sha256s: tuple[str, ...] = (),
) -> PromptRecordings:
    """The subject's retained takes (native and legacy) that count for one prompt revision.

    The coverage rule, for one prompt: a retained take counts when it was recorded against
    ``prompt`` itself or against an earlier revision whose digest the revision accepts
    (``accepted_prompt_sha256s``) and which keeps the prompt's identity. File-only, like the
    report: no SQLite, no writes.
    Params: ``layout`` is the asset root; ``subject_id`` selects the takes; ``prompt`` is a
    corpus-store revision (``PromptRecord.prompt``: some prompt-suite/v2 catalog can hold
    it); ``accepted_prompt_sha256s`` are the earlier digests it accepts (the store's
    ``PromptRecord.accepted_prompt_sha256s``).
    Returns: the counted take ids (sorted) and the records whose prompts cannot be told
    (a subject's native package whose map validated is read exactly, so it is never one of
    them even when it holds no recording of this prompt).
    Raises: ``ValueError`` for a prompt no prompt-suite/v2 catalog can hold, before any
    evidence is read; ``PromptCoverageProjectionError`` when the accepted digests do not
    form valid aliases or evidence cannot be read or projected;
    ``CorpusSnapshotSecurityError``, unwrapped, as ``prompt_coverage_report``.
    """
    layout = AssetLayout.validate(layout.root)
    suite = _counting_suite(prompt)
    digest = canonical_sha256(prompt)
    try:
        snapshot = CatalogSnapshot(
            suite=suite,
            catalog_sha256=canonical_sha256(suite),
            prompt_aliases=tuple(
                PromptAlias(prompt_sha256=digest, accepted_prompt_sha256=accepted)
                for accepted in sorted(set(accepted_prompt_sha256s))
            ),
        )
        store, native = _native_entries(layout, subject_id)
        coverage = _coverage(layout, subject_id, snapshot, native, store)
        unknown = _unassignable(layout, coverage.unknown_record_ids)
    except CorpusSnapshotSecurityError:
        raise  # a sealed reference that no longer resolves is a hard failure, not a gap
    except (OSError, ValueError, VoiceLabError) as error:
        raise PromptCoverageProjectionError(f"recording count failed: {error}") from error
    return PromptRecordings(
        subject_id=subject_id,
        prompt_id=prompt.prompt_id,
        prompt_sha256=digest,
        take_ids=tuple(sorted(coverage.retained[digest])),
        unknown_record_ids=unknown,
    )
