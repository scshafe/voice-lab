"""A prompt's takes, from every session and every machine (RECORDING-V1.md §5, step 6b).

The take picker and the history list one prompt's takes, newest first, merged by take id:

- **this machine's** (the spool): the decision and trim made here, which are ahead of the PC's
  while a change waits to be sent; the take changed elsewhere when the client adopted the
  PC's state for it (a decision event from the PC, or a *changed elsewhere* notice not yet
  dismissed);
- **the PC's** (``GET /api/v1/corpus/prompts/{id}/takes``): every take that counts for the
  prompt there, wherever it was recorded, with its decision, trim and QC.

A take is *here*, *on the PC*, or both (this machine's, accepted there). Every saved take is
decided and trimmed through the corpus client: a take recorded elsewhere (another client's,
or a Swift take imported from the Mac, D-053) is noted in the spool when it is first changed
(``CorpusClient.remote_take``), and played from the PC.
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
#: The picker's short words for where a take lives.
WHERE_SHORT: Final[dict[str, str]] = {
    "here": "here",
    "pc": "PC",
    "both": "here + PC",
    "unsaved": "here",
}
ORIGIN_TITLES: Final[dict[str, str]] = {"imported": "imported from the Mac"}


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
    #: Where the PC says it came from (``imported``: a Swift take of the Mac).
    origin: str = ""

    @property
    def take_id(self) -> str:
        return "" if self.key == REVIEW else self.key

    @property
    def seconds(self) -> float:
        return self.frames / SAMPLE_RATE

    @property
    def decidable(self) -> bool:
        """Its decisions and trim go through the client: any saved take (one recorded
        elsewhere is noted in the spool first)."""
        return self.local is not None or self.pc is not None

    @property
    def audio_here(self) -> bool:
        """Its audio is on this machine (else it is played from the PC)."""
        return self.key == REVIEW or (self.local is not None and not self.local.remote)

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
        if self.origin:
            parts.append(ORIGIN_TITLES.get(self.origin, f"{self.origin} take"))
        return " · ".join(parts)

    @property
    def when(self) -> str:
        if self.requested_at is None:
            return "now"
        return self.requested_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def where_title(self) -> str:
        return WHERE_TITLES[self.where]

    @property
    def label(self) -> str:
        """The picker's short line (it fits the smallest window): when, how long, the
        decision, where (``Mac`` for a take imported from it). History has the rest."""
        when = "now" if self.requested_at is None else self._short_when()
        where = "Mac import" if self.origin == "imported" else WHERE_SHORT[self.where]
        decision = DECISION_TITLES.get(self.decision, self.decision)
        return f"{when} · {self.seconds:.1f} s · {decision} · {where}"

    def _short_when(self) -> str:
        assert self.requested_at is not None
        local = self.requested_at.astimezone()
        return f"{local.day} {local.strftime('%b %H:%M')}"


def origin_of(pc: LibraryTake | None) -> str:
    """The PC's origin kind of a take (when the PC's contracts carry it, D-053)."""
    kind = getattr(getattr(pc, "origin", None), "kind", None)
    return kind if isinstance(kind, str) else ""


def _local_entry(take: SpooledTake, pc: LibraryTake | None, notices: frozenset[str]) -> TakeEntry:
    sync = take.sync
    adopted = any(event.source == "pc" for event in sync.history)
    record = take.record
    if take.remote:
        where: Where = "pc"
        stored = getattr(record, "origin", None)
        origin = stored.kind if stored is not None else origin_of(pc)
    else:
        where = "both" if (sync.upload == "accepted" or pc is not None) else "here"
        origin = ""
    return TakeEntry(
        key=take.take_id,
        requested_at=take.requested_at,
        frames=take.frames,
        decision=sync.decision.decision,
        reason=sync.decision.reason,
        trim=(sync.trim.start_frame, sync.trim.end_frame),
        where=where,
        qc=pc.qc_status if pc is not None else None,
        queued=len(sync.outbox),
        changed_elsewhere=adopted or take.take_id in notices,
        local=take,
        pc=pc,
        origin=origin,
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
        origin=origin_of(take),
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
