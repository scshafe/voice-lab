"""A prompt's takes, from every session and every machine (RECORDING-V1.md §5, step 6b).

The take picker and the history list one prompt's takes, newest first, merged by take id:

- **this machine's** (the spool): the decision and trim made here, which are ahead of the PC's
  while a change waits to be sent; the take changed elsewhere when the client adopted the
  PC's state for it (a decision event from the PC, or a *changed elsewhere* notice not yet
  dismissed);
- **the PC's** (``GET /api/v1/corpus/prompts/{id}/takes``): every take that counts for the
  prompt there, wherever it was recorded, with its decision, trim and QC.

A take is *here*, *on the PC*, or both (this machine's, accepted there). The operator's
decisions and trims go through the corpus client, which holds this machine's takes only:
a take recorded elsewhere is shown and played, and decided where it lives.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from cole_voice_lab.corpus_read_model import LibraryTake

from ..capture.types import SAMPLE_RATE
from ..corpus.records import Notice
from ..corpus.spool import SpooledTake

type Where = Literal["here", "pc", "both", "unsaved"]

#: The take under review before Save: not a take yet (it has no id).
REVIEW: Final = "review"
DECISION_TITLES: Final[dict[str, str]] = {
    "draft": "Draft",
    "kept": "Kept",
    "rejected": "Rejected",
    "removed": "Removed",
    "unsaved": "Not saved",
}
WHERE_TITLES: Final[dict[str, str]] = {
    "here": "this machine",
    "pc": "the PC",
    "both": "this machine and the PC",
    "unsaved": "this machine",
}
ELSEWHERE: Final = "Recorded on another machine: it is decided where it lives"


@dataclass(frozen=True)
class TakeEntry:
    """One take of the prompt (``key``: its take id, or ``REVIEW``)."""

    key: str
    requested_at: datetime | None
    frames: int
    decision: str
    reason: str | None
    trim: tuple[int | None, int | None]
    where: Where
    qc: str | None = None
    queued: int = 0
    changed_elsewhere: bool = False
    local: SpooledTake | None = None
    pc: LibraryTake | None = None

    @property
    def take_id(self) -> str:
        return "" if self.key == REVIEW else self.key

    @property
    def seconds(self) -> float:
        return self.frames / SAMPLE_RATE

    @property
    def decidable(self) -> bool:
        """Its decisions and trim go through the client (a saved take of this machine)."""
        return self.local is not None

    @property
    def trimmed(self) -> bool:
        return self.trim != (None, None)

    def trim_frames(self) -> tuple[int, int]:
        start, end = self.trim
        return (start or 0, end if end is not None else self.frames)

    @property
    def status(self) -> str:
        parts = [DECISION_TITLES.get(self.decision, self.decision)]
        if self.decision == "rejected" and self.reason:
            parts[0] += f" ({self.reason})"
        if self.qc in ("review", "fail") and self.decision == "kept":
            parts.append("needs review" if self.qc == "review" else "failed QC")
        if self.trimmed:
            parts.append("trimmed")
        if self.queued:
            parts.append("waiting to sync")
        if self.changed_elsewhere:
            parts.append("changed elsewhere")
        return " · ".join(parts)

    @property
    def when(self) -> str:
        if self.requested_at is None:
            return "now"
        return self.requested_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def label(self) -> str:
        """The picker's line: when, how long, its status, where it lives."""
        length = f"{self.seconds:.1f} s"
        return f"{self.when} · {length} · {self.status} · {WHERE_TITLES[self.where]}"


def _local_entry(take: SpooledTake, pc: LibraryTake | None, notices: frozenset[str]) -> TakeEntry:
    sync = take.sync
    adopted = any(event.source == "pc" for event in sync.history)
    return TakeEntry(
        key=take.take_id,
        requested_at=take.record.requested_at,
        frames=take.record.frames,
        decision=sync.decision.decision,
        reason=sync.decision.reason,
        trim=(sync.trim.start_frame, sync.trim.end_frame),
        where="both" if (sync.upload == "accepted" or pc is not None) else "here",
        qc=pc.qc_status if pc is not None else None,
        queued=len(sync.outbox),
        changed_elsewhere=adopted or take.take_id in notices,
        local=take,
        pc=pc,
    )


def _pc_entry(take: LibraryTake) -> TakeEntry:
    return TakeEntry(
        key=take.take_id,
        requested_at=take.requested_at,
        frames=round(take.duration_seconds * SAMPLE_RATE),
        decision=take.decision,
        reason=None,
        trim=(take.trim.start_frame, take.trim.end_frame),
        where="pc",
        qc=take.qc_status,
        pc=take,
    )


def merge(
    local: Sequence[SpooledTake],
    pc: Sequence[LibraryTake] | None,
    notices: Sequence[Notice] = (),
) -> list[TakeEntry]:
    """The prompt's takes, newest first: this machine's (``local``) and the PC's (``pc``;
    ``None`` while its history is not known)."""
    on_pc = {take.take_id: take for take in pc or ()}
    elsewhere = frozenset(n.take_id for n in notices if n.kind == "changed-elsewhere")
    entries = [_local_entry(take, on_pc.pop(take.take_id, None), elsewhere) for take in local]
    entries.extend(_pc_entry(take) for take in on_pc.values())
    return sorted(
        entries,
        key=lambda entry: (entry.requested_at is not None, entry.requested_at, entry.key),
        reverse=True,
    )


def latest_kept(entries: Sequence[TakeEntry]) -> TakeEntry | None:
    """The take that stands for the prompt: its newest kept take."""
    return next((entry for entry in entries if entry.decision == "kept"), None)
