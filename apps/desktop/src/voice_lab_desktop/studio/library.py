"""Each prompt's status, coverage, the library's filters and the next missing prompt.

The studio records against the session's pinned catalog version (``corpus/catalog.py``). A
prompt's status comes from the best-known state of **each of its takes**:

- a take this machine knows (the spool: recorded here, or recorded elsewhere and noted
  here) stands with its state here, which is ahead of the PC's while a change of it waits
  to be sent, unless the PC's history shows it at a newer revision (another client changed
  it); a take the PC no longer has (``gone``) counts for nothing;
- a take only the PC knows stands with the PC's state, read from the prompt's history
  (``GET /api/v1/corpus/prompts/{id}/takes``, cached per read of the PC's library).

Without that history, the PC's library row (``kept``, ``needs-attention``, ``draft`` or
``missing``; ``latest_take_id``, its newest kept, else undecided, take; ``take_count``)
stands only for what it can say about takes this machine does not know: when every take the
PC counts is known here, the takes here decide; when the row's newest kept (else undecided)
take is one this machine does not know, or there is none, the PC's status stands with the
takes here; otherwise the prompt's
history is needed (``needs_history``), and until it is read the takes here decide. A status
is never inferred from a count. A take counts for the pinned prompt its recorded prompt
counts for (aliases included); a row counts only for the same prompt, by id and digest.

``kept`` is a kept take that passed the PC's QC (or one the PC has not checked yet);
``needs-attention`` kept takes none of which passed (the PC's row says so for its takes: a kept
take here that the PC has, with no change waiting, then failed); ``draft`` an undecided take;
``missing`` otherwise. ``recorded`` coverage is ``kept`` plus ``needs-attention``, as on the
PC. Search, Show and Labels never change coverage, Continue Missing or Save & Next
(NATIVE_RECORDING_UX.md §4, "library filters").

Flagged prompts (D-058, item 5): a prompt is *flagged here* when the take that stands for it
on this machine (its newest kept take, else its newest draft) has quality flags
(``studio/quality.py``), and it *failed the PC's QC* when its kept takes all did
(``needs-attention``). Show splits Needs attention three ways (Flagged here, Failed PC QC,
Drafts only), and Next Flagged walks both kinds in catalog order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from cole_voice_lab.api.corpus_contracts import CorpusLibrary
from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.corpus_read_model import LibraryRow, LibraryTake
from cole_voice_lab.recording_contracts import CatalogSnapshot

from ..corpus.spool import SpooledTake
from .presentation import PromptView

type Status = Literal["missing", "draft", "kept", "needs-attention"]
type Show = Literal["all", "missing", "attention", "flagged", "failed", "drafts"]

RANK: Final[dict[str, int]] = {"missing": 0, "draft": 1, "needs-attention": 2, "kept": 3}
RECORDED: Final = frozenset({"kept", "needs-attention"})
#: What Continue Missing and Save & Next go to: no kept take yet.
UNCOVERED: Final = frozenset({"missing", "draft"})
SHOW_OPTIONS: Final = (
    {"value": "all", "label": "All prompts"},
    {"value": "missing", "label": "Missing"},
    {"value": "attention", "label": "Needs attention"},
    {"value": "flagged", "label": "Flagged here"},
    {"value": "failed", "label": "Failed PC QC"},
    {"value": "drafts", "label": "Drafts only"},
)
STATUS_TITLES: Final[dict[str, str]] = {
    "missing": "Missing",
    "draft": "Draft",
    "kept": "Recorded",
    "needs-attention": "Needs review",
}


@dataclass(frozen=True)
class PromptState:
    """Where one prompt of the pinned version stands.

    ``takes``: its takes, each counted once (this machine's and the PC's); ``here_only``: it
    is recorded here, and not yet on the PC as last read."""

    status: Status
    takes: int = 0
    here_only: bool = False

    @property
    def recorded(self) -> bool:
        return self.status in RECORDED

    @property
    def title(self) -> str:
        title = STATUS_TITLES[self.status]
        return f"{title} · this machine" if self.here_only else title


MISSING: Final = PromptState("missing")


@dataclass
class States:
    """``prompt_states``' answer: each prompt's state, and the prompts whose history the PC
    must be asked for before their status is certain."""

    states: dict[str, PromptState]
    needs_history: list[str] = field(default_factory=list)


def _status(decisions: Iterable[tuple[str, str | None]]) -> Status:
    """The status of takes in the given (decision, QC) states (QC ``None``: not checked)."""
    kept = [qc for decision, qc in decisions if decision == "kept"]
    if any(qc in (None, "pass") for qc in kept):
        return "kept"
    if kept:
        return "needs-attention"
    return "draft" if any(d == "draft" for d, _ in decisions) else "missing"


def _judged(take: SpooledTake, on_pc: set[str]) -> str | None:
    """A kept take's QC when the PC's row says none of its kept takes passed: failed, for a
    take the PC has with no change of it waiting to be sent; else not known here."""
    return "review" if take.take_id in on_pc and not take.sync.outbox else None


def _best(first: Status, second: Status) -> Status:
    return first if RANK[first] >= RANK[second] else second


def prompt_states(
    views: Sequence[PromptView],
    snapshot: CatalogSnapshot,
    pc: CorpusLibrary | None,
    takes: Iterable[SpooledTake],
    *,
    digests: dict[str, str] | None = None,
    histories: Mapping[str, Sequence[LibraryTake]] | None = None,
) -> States:
    """Every pinned prompt's state (by prompt id), from the PC's library ``pc`` (``None``:
    never read), the prompts' ``histories`` read from the PC for this library, and this
    machine's ``takes`` (``digests``: each view's prompt digest, when the caller keeps
    them)."""
    if digests is None:
        digests = {view.prompt_id: canonical_sha256(view.prompt) for view in views}
    histories = histories or {}
    rows: dict[str, LibraryRow] = {}
    if pc is not None:
        for pc_row in pc.rows:
            if digests.get(pc_row.prompt_id) == pc_row.prompt_sha256:
                rows[pc_row.prompt_id] = pc_row
    local: dict[str, list[SpooledTake]] = {}
    for take in takes:
        if take.sync.upload == "gone":
            continue  # the PC no longer has it: it counts for nothing
        current = take.counts_for(snapshot)
        if current is None or current.prompt_id not in digests:
            continue
        local.setdefault(current.prompt_id, []).append(take)
    answer = States({})
    for view in views:
        prompt_id = view.prompt_id
        row = rows.get(prompt_id)
        mine = {take.take_id: take for take in local.get(prompt_id, [])}
        history = histories.get(prompt_id)
        if not mine and row is None:
            answer.states[prompt_id] = MISSING
            continue
        here = [(t.sync.decision.decision, None) for t in mine.values()]
        if history is not None:
            on_pc = {t.take_id: t for t in history}
            states: list[tuple[str, str | None]] = []
            for take_id in {*mine, *on_pc}:
                known, there = mine.get(take_id), on_pc.get(take_id)
                qc = there.qc_status if there is not None else None
                if known is not None and (
                    there is None or known.sync.decision.revision >= there.decision_revision
                ):
                    states.append((known.sync.decision.decision, qc))
                elif there is not None:
                    states.append((there.decision, qc))
            status = _status(states)
            pc_status = _status([(t.decision, t.qc_status) for t in history])
            count = len({*mine, *on_pc})
        else:
            pc_ids = {i for i, t in mine.items() if t.sync.upload in ("accepted", "remote")}
            pc_count = row.take_count if row is not None else 0
            pc_status = row.status if row is not None else "missing"
            count = pc_count + (len(mine) - len(pc_ids))
            if row is None or pc_count == 0 or len(pc_ids) >= pc_count:
                # Every take the PC counts is known here. QC is the PC's alone: when none of
                # its kept takes passed, a kept take here that the PC has, with no change
                # waiting to be sent, is one that failed.
                failed = row is not None and row.status == "needs-attention"
                status = _status(
                    [
                        (t.sync.decision.decision, _judged(t, pc_ids) if failed else None)
                        for t in mine.values()
                    ]
                )
            elif row.latest_take_id not in mine:
                # The PC's newest kept (or undecided) take is one this machine does not know,
                # or it has none: the PC's status stands for takes not known here.
                status = _best(pc_status, _status(here))
            else:
                answer.needs_history.append(prompt_id)
                status = _status(here)
        answer.states[prompt_id] = PromptState(
            status=status,
            takes=count,
            here_only=status in RECORDED and pc_status not in RECORDED,
        )
    return answer


def covered(states: dict[str, PromptState]) -> int:
    return sum(1 for state in states.values() if state.recorded)


def shows(
    view: PromptView,
    state: PromptState,
    *,
    search: str,
    show: str,
    labels: frozenset[str],
    flagged: frozenset[str] = frozenset(),
) -> bool:
    """Whether the library lists ``view``: search, Show and Labels combine with AND; the
    selected labels with OR (none selected: any). ``flagged``: the prompts flagged here."""
    if show == "missing" and state.status != "missing":
        return False
    if show == "attention" and state.status not in ("draft", "needs-attention"):
        return False
    if show == "flagged" and view.prompt_id not in flagged:
        return False
    if show == "failed" and state.status != "needs-attention":
        return False
    if show == "drafts" and state.status != "draft":
        return False
    if labels and labels.isdisjoint(view.labels):
        return False
    return view.matches_search(search)


def standing_takes(takes: Iterable[SpooledTake]) -> dict[str, str]:
    """Each prompt's take that stands for it on this machine: its newest kept take, else its
    newest draft (a take recorded elsewhere has no audio here, so no flags: left out)."""
    best: dict[str, tuple[int, datetime, str]] = {}
    for take in takes:
        decision = take.sync.decision.decision
        if take.remote or decision not in ("kept", "draft"):
            continue
        rank = (1 if decision == "kept" else 0, take.requested_at, take.take_id)
        known = best.get(take.prompt_id)
        if known is None or rank > known:
            best[take.prompt_id] = rank
    return {prompt_id: rank[2] for prompt_id, rank in best.items()}


def is_flagged(prompt_id: str, flagged: frozenset[str], states: Mapping[str, PromptState]) -> bool:
    """Flagged here, or failed the PC's QC: what Next Flagged goes to."""
    return prompt_id in flagged or states.get(prompt_id, MISSING).status == "needs-attention"


def next_flagged(
    order: Sequence[str],
    flagged: frozenset[str],
    states: Mapping[str, PromptState],
    after: str | None,
) -> str | None:
    """The next flagged prompt after ``after`` in catalog order, wrapping once (``after``
    itself last). ``None``: none is flagged."""
    start = order.index(after) if after in order else -1
    for prompt_id in (*order[start + 1 :], *order[: start + 1]):
        if is_flagged(prompt_id, flagged, states):
            return prompt_id
    return None


def next_missing(
    order: Sequence[str], states: dict[str, PromptState], after: str | None
) -> str | None:
    """The next prompt with no kept take after ``after`` in catalog order, wrapping once
    (``after`` itself last); from the start when ``after`` is ``None``. ``None``: every
    prompt is recorded."""
    start = order.index(after) if after in order else -1
    for prompt_id in (*order[start + 1 :], *order[: start + 1]):
        if states.get(prompt_id, MISSING).status in UNCOVERED:
            return prompt_id
    return None
