"""Read-only per-prompt coverage report for comparing two catalog versions.

@canonical prompt_coverage -- coverage is the index rule (exact prompt hash, subject,
retained take) computed from evidence through the canonical legacy reader and native
head reader. It never constructs RecordingIndex and never writes under the asset root.
Legacy WAVs are opened read-only for inspection. Related: recording_index.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import JsonValue

from .canonical import canonical_sha256
from .errors import ContractError, VoiceLabError
from .models import PromptSuite
from .paths import AssetLayout
from .recording_contracts import RecordingHistoryEntry, RecordingTake
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
            "catalog_sha256": self.catalog_sha256,
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
class _CatalogCoverage:
    retained: dict[str, frozenset[str]]  # prompt sha256 -> distinct retained take IDs
    unknown_record_ids: tuple[str, ...]


def iter_native_takes(store: RecordingTakeStore | None) -> Iterator[RecordingTake]:
    if store is None:
        return
    after = None
    while page := store.iter_takes_for_projection(after_take_id=after):
        yield from page
        after = page[-1].reservation.take_id


def _coverage(
    layout: AssetLayout,
    subject_id: str,
    catalog: PromptSuite,
    native: tuple[RecordingHistoryEntry, ...],
    store: RecordingTakeStore | None,
) -> _CatalogCoverage:
    hashes = {canonical_sha256(prompt) for prompt in catalog.prompts}
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
    retained: dict[str, set[str]] = {digest: set() for digest in hashes}
    for entry in entries:
        digest = canonical_sha256(entry.prompt)
        if (
            digest in hashes
            and entry.subject_id == subject_id
            and is_retained(entry.decision, entry.analysis)
        ):
            retained[digest].add(entry.take_id)
    return _CatalogCoverage(
        {digest: frozenset(takes) for digest, takes in retained.items()}, tuple(sorted(unknown))
    )


def prompt_coverage_report(
    layout: AssetLayout,
    subject_id: str,
    catalog: PromptSuite,
    baseline: PromptSuite | None = None,
) -> PromptCoverageReport:
    """Compute exact coverage under catalog, and what changes relative to baseline."""
    layout = AssetLayout.validate(layout.root)
    try:
        # A root that never held native evidence has no take store; the read-only store
        # would otherwise wait for a service owner to create its directories.
        store = (
            RecordingTakeStore(layout, read_only=True)
            if os.path.lexists(layout.root / TAKE_PROVENANCE_ROOT)
            else None
        )
        native = tuple(
            history_entry_for_take(take)
            for take in iter_native_takes(store)
            if take.reservation.group.subject_id == subject_id
        )
        current = _coverage(layout, subject_id, catalog, native, store)
        previous = (
            None if baseline is None else _coverage(layout, subject_id, baseline, native, store)
        )
    except (OSError, ValueError, VoiceLabError) as error:
        raise PromptCoverageProjectionError(f"coverage projection failed: {error}") from error

    def covered(coverage: _CatalogCoverage, digest: str) -> bool:
        return bool(coverage.retained[digest])

    baseline_prompts = {} if baseline is None else {p.prompt_id: p for p in baseline.prompts}
    rows = []
    for prompt in catalog.prompts:
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
    if previous is not None and baseline is not None:
        for before in baseline.prompts:
            now = by_id.get(before.prompt_id)
            digest = canonical_sha256(before)
            if now is None or now.prompt_sha256 != digest:
                changed.append(before.prompt_id)
            if covered(previous, digest) and (now is None or not now.covered):
                lost.append(before.prompt_id)
    return PromptCoverageReport(
        subject_id=subject_id,
        catalog_sha256=canonical_sha256(catalog),
        baseline_catalog_sha256=None if baseline is None else canonical_sha256(baseline),
        prompts=tuple(rows),
        covered_prompt_count=sum(row.covered for row in rows),
        total_prompt_count=len(rows),
        missing_prompt_ids=tuple(row.prompt_id for row in rows if not row.covered),
        changed_prompt_ids=tuple(changed),
        lost_prompt_ids=tuple(lost),
        unknown_record_ids=current.unknown_record_ids,
        baseline_unknown_record_ids=() if previous is None else previous.unknown_record_ids,
    )
