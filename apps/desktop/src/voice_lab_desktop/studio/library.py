"""Each prompt's status, coverage, the library's filters and the next missing prompt.

The studio records against the session's pinned catalog version (``corpus/catalog.py``). A
prompt's status comes from two places:

- **the PC's library** as last read (``corpus/library.py``): its row's status (``kept``,
  ``needs-attention``, ``draft`` or ``missing``) and take count. A row counts for a pinned
  prompt only when it names the same prompt, by id and digest: when the PC's active version
  is another one (a switch is pending), unchanged prompts still count, changed ones do not;
- **this machine's takes** (the spool): a take counts for the pinned prompt its recorded
  prompt counts for in the pinned version (its prompt aliases included), with the decision
  made here, which is ahead of the PC's while a change of it waits to be sent.

The better of the two stands (``kept`` > ``needs-attention`` > ``draft`` > ``missing``),
except that the PC's status is not believed when the take it stands on is this machine's and
is decided otherwise here (a change still on its way). ``recorded`` coverage is ``kept`` plus
``needs-attention``, as on the PC. Search, Show and Labels never change coverage, Continue
Missing or Save & Next (NATIVE_RECORDING_UX.md §4, "library filters").
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from cole_voice_lab.api.corpus_contracts import CorpusLibrary
from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.corpus_read_model import LibraryRow
from cole_voice_lab.recording_contracts import CatalogSnapshot

from ..corpus.spool import SpooledTake
from .presentation import PromptView

type Status = Literal["missing", "draft", "kept", "needs-attention"]
type Show = Literal["all", "missing", "attention"]

RANK: Final[dict[str, int]] = {"missing": 0, "draft": 1, "needs-attention": 2, "kept": 3}
RECORDED: Final = frozenset({"kept", "needs-attention"})
#: What Continue Missing and Save & Next go to: no kept take yet.
UNCOVERED: Final = frozenset({"missing", "draft"})
SHOW_OPTIONS: Final = (
    {"value": "all", "label": "All prompts"},
    {"value": "missing", "label": "Missing"},
    {"value": "attention", "label": "Needs attention"},
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

    ``pc_takes``: takes the PC counts for it (as last read); ``local_takes``: this
    machine's; ``here_only``: it is recorded here, and not yet on the PC as last read."""

    status: Status
    pc_takes: int = 0
    local_takes: int = 0
    here_only: bool = False

    @property
    def recorded(self) -> bool:
        return self.status in RECORDED

    @property
    def title(self) -> str:
        title = STATUS_TITLES[self.status]
        return f"{title} · this machine" if self.here_only else title


MISSING: Final = PromptState("missing")


def _local_status(take: SpooledTake) -> Status | None:
    decision = take.sync.decision.decision
    if decision == "kept":
        return "kept"
    if decision == "draft":
        return "draft"
    return None  # rejected and removed takes cover nothing


def prompt_states(
    views: Sequence[PromptView],
    snapshot: CatalogSnapshot,
    pc: CorpusLibrary | None,
    takes: Iterable[SpooledTake],
    *,
    digests: dict[str, str] | None = None,
) -> dict[str, PromptState]:
    """Every pinned prompt's state (by prompt id), from the PC's library ``pc`` (``None``:
    never read) and this machine's ``takes`` (``digests``: each view's prompt digest, when
    the caller keeps them)."""
    if digests is None:
        digests = {view.prompt_id: canonical_sha256(view.prompt) for view in views}
    rows: dict[str, LibraryRow] = {}
    if pc is not None:
        for pc_row in pc.rows:
            if digests.get(pc_row.prompt_id) == pc_row.prompt_sha256:
                rows[pc_row.prompt_id] = pc_row
    local: dict[str, list[SpooledTake]] = {}
    by_id: dict[str, SpooledTake] = {}
    for take in takes:
        current = snapshot.current_prompt_for(take.record.prompt)
        if current is None or current.prompt_id not in digests:
            continue
        local.setdefault(current.prompt_id, []).append(take)
        by_id[take.take_id] = take
    states: dict[str, PromptState] = {}
    for view in views:
        prompt_id = view.prompt_id
        row = rows.get(prompt_id)
        mine = local.get(prompt_id, [])
        pc_status: Status = row.status if row is not None else "missing"
        if row is not None and row.latest_take_id in by_id:
            standing = _local_status(by_id[row.latest_take_id])
            implied = "draft" if row.status == "draft" else "kept"
            if standing != implied and row.take_count <= 1:
                pc_status = "missing"  # its one take is decided otherwise here
        local_status: Status = "missing"
        for take in mine:
            status = _local_status(take)
            if status is not None and RANK[status] > RANK[local_status]:
                local_status = status
        status = pc_status if RANK[pc_status] >= RANK[local_status] else local_status
        here_only = status in RECORDED and pc_status not in RECORDED
        if not mine and row is None:
            states[prompt_id] = MISSING
            continue
        states[prompt_id] = PromptState(
            status=status,
            pc_takes=row.take_count if row is not None else 0,
            local_takes=len(mine),
            here_only=here_only,
        )
    return states


def covered(states: dict[str, PromptState]) -> int:
    return sum(1 for state in states.values() if state.recorded)


def shows(
    view: PromptView,
    state: PromptState,
    *,
    search: str,
    show: str,
    labels: frozenset[str],
) -> bool:
    """Whether the library lists ``view``: search, Show and Labels combine with AND; the
    selected labels with OR (none selected: any)."""
    if show == "missing" and state.status != "missing":
        return False
    if show == "attention" and state.status not in ("draft", "needs-attention"):
        return False
    if labels and labels.isdisjoint(view.labels):
        return False
    return view.matches_search(search)


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
