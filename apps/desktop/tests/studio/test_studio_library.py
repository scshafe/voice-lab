"""The studio's rules without a window: how a prompt is shown (its kind, banner, sections,
labels and search), each prompt's status from the PC's library and this machine's takes,
the Show/Labels/search filters, and where Continue Missing and Save & Next go."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from voice_lab_desktop.studio import library, presentation
from voice_lab_desktop.studio.library import PromptState
from voice_lab_desktop.studio.presentation import PromptView

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.models import Prompt, PromptSuite
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.recording_contracts import CatalogSnapshot


def pilot() -> PromptSuite:
    return load_builtin_prompt_suite("pilot-v2.json")


def views() -> tuple[PromptView, ...]:
    suite = pilot()
    return presentation.present(suite.prompts, suite.purpose)


def view(prompt_id: str) -> PromptView:
    return next(v for v in views() if v.prompt_id == prompt_id)


def test_the_category_alone_decides_what_is_read_aloud() -> None:
    spoken = view("reference-neutral-01")
    assert (spoken.kind, spoken.banner) == ("spoken", "")
    assert spoken.display_prompt.startswith("A quiet morning")
    assert spoken.display_instructions == ""
    assert spoken.title == "Reference Neutral 01"
    assert spoken.guidance.startswith("Guidance: ")
    assert spoken.duration == f"About {round(spoken.prompt.expected_seconds)} sec"
    # Room tone: a banner, no Prompt; its text shows as Instructions, never to read aloud.
    room = view("room-tone-start")
    assert (room.kind, room.banner) == ("room-tone", "SILENT ROOM TONE · DO NOT SPEAK")
    assert room.display_prompt == ""
    assert room.display_instructions.startswith("Remain still and silent")
    # A procedure step: its own banner, its instructions.
    step = Prompt(
        prompt_id="check-level",
        category="procedure",
        style="test step",
        expected_seconds=5.0,
        instructions="Set the input level, then wait.",
    )
    shown = PromptView(step, 0, "physical-test")
    assert (shown.kind, shown.banner) == ("procedure", "TEST STEP · FOLLOW INSTRUCTIONS")
    assert (shown.display_prompt, shown.display_instructions) == (
        "",
        "Set the input level, then wait.",
    )
    assert shown.labels == ("procedure", "test") and shown.pinned_labels == 2


def test_labels_are_in_effective_order_and_titled_verbatim() -> None:
    prompt = Prompt(
        prompt_id="arctic-a0001",
        text="Author of the danger trail.",
        category="neutral",
        style="read plainly",
        expected_seconds=3.0,
        labels=("arctic", "test", "phonetic-balance"),
        reference_candidate=True,
        holdout=True,
    )
    shown = PromptView(prompt, 3, "capture-corpus")
    # The category, the catalog's labels (a catalog "test" label only in a test catalog),
    # then reference candidate and holdout.
    assert shown.labels == (
        "neutral",
        "arctic",
        "phonetic-balance",
        "reference-candidate",
        "holdout",
    )
    assert [presentation.label_title(t) for t in shown.labels] == [
        "Neutral",
        "Arctic",
        "Phonetic Balance",
        "Reference Candidate",
        "Holdout",
    ]
    assert presentation.labels_menu_title(()) == "Labels: Any"
    assert presentation.labels_menu_title(("arctic",)) == "Labels: Arctic"
    assert presentation.labels_menu_title(("arctic", "holdout")) == "Labels: Arctic, Holdout"
    assert presentation.labels_menu_title(("a1", "b2", "c3")) == "Labels: 3 selected"
    assert presentation.labels_button_text(()) == "Any"
    assert presentation.labels_button_text(("arctic", "holdout")) == "Arctic, Holdout"
    assert presentation.labels_button_text(("reference-candidate", "technical")) == "2 selected"
    options = presentation.label_options(views())
    assert options[:3] == ("room-tone", "reference", "reference-candidate")
    assert len(options) == len(set(options))


def test_search_matches_title_id_text_instructions_guidance_and_labels() -> None:
    spoken = view("reference-neutral-01")
    for needle in ("neutral 01", "REFERENCE-NEUTRAL", "quiet morning", "candidate", ""):
        assert spoken.matches_search(needle), needle
    assert view("room-tone-start").matches_search("still and silent")
    assert view("technical-01").matches_search(view("technical-01").prompt.style.upper())
    assert not spoken.matches_search("kettle")


def take(
    prompt: Prompt,
    decision: str,
    take_id: str,
    *,
    upload: str = "pending",
    revision: int = 0,
    outbox: tuple[Any, ...] = (),
) -> Any:
    """A take of this machine as the merge reads it: its prompt, its decision here at its
    revision, whether the PC has it (``accepted`` or ``remote``) and what waits to be sent."""
    return SimpleNamespace(
        take_id=take_id,
        prompt_id=prompt.prompt_id,
        record=SimpleNamespace(prompt=prompt),
        sync=SimpleNamespace(
            decision=SimpleNamespace(decision=decision, revision=revision),
            upload=upload,
            outbox=outbox,
        ),
        counts_for=lambda snapshot: snapshot.current_prompt_for(prompt),
    )


def row(prompt: Prompt, status: str, *, count: int = 1, latest: str | None = None) -> Any:
    return SimpleNamespace(
        prompt_id=prompt.prompt_id,
        prompt_sha256=canonical_sha256(prompt),
        status=status,
        take_count=count,
        latest_take_id=latest,
    )


def on_pc(take_id: str, decision: str, revision: int = 1, qc: str | None = "pass") -> Any:
    """A take in the PC's history of its prompt."""
    return SimpleNamespace(
        take_id=take_id, decision=decision, decision_revision=revision, qc_status=qc
    )


def merge(*, rows: Any = (), takes: Any = (), histories: Any = None) -> library.States:
    suite = pilot()
    shown = presentation.present(suite.prompts, suite.purpose)
    pc = SimpleNamespace(rows=tuple(rows))
    return library.prompt_states(
        shown, CatalogSnapshot.embedded(suite), pc, takes, histories=histories
    )  # type: ignore[arg-type]


def test_a_prompt_stands_on_the_best_known_state_of_each_take() -> None:
    suite = pilot()
    snapshot = CatalogSnapshot.embedded(suite)
    shown = presentation.present(suite.prompts, suite.purpose)
    by_id = {p.prompt_id: p for p in suite.prompts}
    a, b, c, d, e, f = (by_id[i] for i in ("neutral-01", "neutral-02", "neutral-03",
                                           "neutral-04", "neutral-05", "neutral-06"))  # fmt: skip
    changed = b.model_copy(update={"text": "Another sentence the PC holds."})
    pc = SimpleNamespace(
        rows=(
            row(a, "kept", latest="take-pc-a"),
            row(changed, "kept"),  # another revision of neutral-02: not this prompt
            row(c, "kept", latest="take-here-c"),
            row(d, "needs-attention", count=2, latest="take-pc-d"),
            row(e, "draft", latest="take-pc-e"),
        )
    )
    takes = [
        take(b, "kept", "take-here-b"),
        take(c, "removed", "take-here-c", upload="accepted", revision=1),  # the PC's one take
        take(e, "rejected", "take-here-e"),
        take(f, "draft", "take-here-f"),
    ]
    answer = library.prompt_states(shown, snapshot, pc, takes)  # type: ignore[arg-type]
    states = answer.states
    assert states["neutral-01"] == PromptState("kept", takes=1)
    assert states["neutral-02"] == PromptState("kept", takes=1, here_only=True)
    assert states["neutral-02"].title == "Recorded · this machine"
    # The PC's one take, removed here: missing, and one take (never two).
    assert states["neutral-03"] == PromptState("missing", takes=1)
    assert states["neutral-04"] == PromptState("needs-attention", takes=2)
    assert states["neutral-05"] == PromptState("draft", takes=2)
    assert states["neutral-06"] == PromptState("draft", takes=1)
    assert states["neutral-07"] is library.MISSING
    assert library.covered(states) == 3 and answer.needs_history == []
    # Without the PC's library, only this machine's takes count.
    alone = library.prompt_states(shown, snapshot, None, takes)  # type: ignore[arg-type]
    assert library.covered(alone.states) == 1 and alone.states["neutral-01"] is library.MISSING


def test_the_pcs_count_never_makes_a_take_removed_here_kept() -> None:
    """The reviewer's case: the PC's only kept take, with two rejected siblings recorded
    elsewhere, removed here (not sent yet). The PC's row still says kept, of three takes."""
    prompt = next(p for p in pilot().prompts if p.prompt_id == "neutral-01")
    pc_row = row(prompt, "kept", count=3, latest="take-k")
    removed = take(prompt, "removed", "take-k", upload="remote", revision=2)
    answer = merge(rows=[pc_row], takes=[removed])
    # Not certain from the row: the PC's history is asked for; until then, never kept.
    assert answer.needs_history == ["neutral-01"]
    assert answer.states["neutral-01"] == PromptState("missing", takes=3)
    # With the history: the removal here (revision 2) is ahead of the PC's kept (1).
    history = {
        "neutral-01": [
            on_pc("take-k", "kept", 1),
            on_pc("take-r1", "rejected"),
            on_pc("take-r2", "rejected"),
        ]
    }
    answer = merge(rows=[pc_row], takes=[removed], histories=history)
    assert answer.needs_history == []
    assert answer.states["neutral-01"] == PromptState("missing", takes=3)
    # Another client kept a sibling since: the PC's newer state of that take stands.
    history["neutral-01"][1] = on_pc("take-r1", "kept", 2)
    assert merge(rows=[pc_row], takes=[removed], histories=history).states[
        "neutral-01"
    ] == PromptState("kept", takes=3)
    # And a take changed on the PC after this machine's last note of it: the PC's state.
    stale = take(prompt, "kept", "take-k", upload="remote", revision=1)
    history["neutral-01"] = [on_pc("take-k", "removed", 2), on_pc("take-r1", "rejected")]
    assert (
        merge(rows=[pc_row], takes=[stale], histories=history).states["neutral-01"].status
        == "missing"
    )


def test_a_take_the_pc_no_longer_has_counts_for_nothing() -> None:
    prompt = next(p for p in pilot().prompts if p.prompt_id == "neutral-01")
    gone = take(prompt, "kept", "take-g", upload="gone", revision=1)
    assert merge(takes=[gone]).states["neutral-01"] is library.MISSING


def test_continue_missing_walks_catalog_order_and_wraps_once() -> None:
    order = ("a", "b", "c", "d")
    states = {
        "a": PromptState("kept"),
        "b": PromptState("missing"),
        "c": PromptState("needs-attention"),
        "d": PromptState("draft"),
    }
    assert library.next_missing(order, states, None) == "b"
    assert library.next_missing(order, states, "b") == "d"
    assert library.next_missing(order, states, "d") == "b"  # wraps
    assert library.next_missing(order, {"a": PromptState("kept")}, "a") == "b"
    done = {key: PromptState("kept") for key in order}
    assert library.next_missing(order, done, "c") is None
    # The selected prompt itself comes last.
    assert library.next_missing(order, {**done, "c": PromptState("missing")}, "c") == "c"


def test_show_labels_and_search_combine_with_and() -> None:
    spoken = view("reference-neutral-01")
    missing, draft = PromptState("missing"), PromptState("draft")
    attention, kept = PromptState("needs-attention"), PromptState("kept")
    none: frozenset[str] = frozenset()

    def shown(state: PromptState, **options: Any) -> bool:
        values = {"search": "", "show": "all", "labels": none, **options}
        return library.shows(spoken, state, **values)

    assert all(shown(state) for state in (missing, draft, attention, kept))
    assert [shown(s, show="missing") for s in (missing, draft, attention, kept)] == [
        True,
        False,
        False,
        False,
    ]
    assert [shown(s, show="attention") for s in (missing, draft, attention, kept)] == [
        False,
        True,
        True,
        False,
    ]
    assert shown(kept, labels=frozenset({"technical", "reference-candidate"}))  # OR
    assert not shown(kept, labels=frozenset({"technical"}))
    assert not shown(missing, show="missing", search="kettle")
    assert shown(missing, show="missing", search="quiet", labels=frozenset({"reference"}))


def test_a_take_here_that_failed_the_pcs_qc_needs_review() -> None:
    """QC is the PC's alone: when its row says none of its kept takes passed, a kept take
    here that the PC has, with no change waiting, failed it (D-058, item 5: Failed PC QC)."""
    by_id = {p.prompt_id: p for p in pilot().prompts}
    a, b, c = by_id["neutral-01"], by_id["neutral-02"], by_id["neutral-03"]
    rows = (row(a, "needs-attention"), row(b, "needs-attention"), row(c, "kept"))
    takes = [
        take(a, "kept", "take-a", upload="accepted"),
        take(b, "kept", "take-b", upload="accepted", outbox=("a trim waits",)),
        take(c, "kept", "take-c", upload="accepted"),
    ]
    states = merge(rows=rows, takes=takes).states
    assert states["neutral-01"] == PromptState("needs-attention", takes=1)
    # A change waits: what the PC judged may not be this take as it stands here.
    assert states["neutral-02"].status == "kept"
    assert states["neutral-03"].status == "kept"
    # A kept take the PC does not have yet is not judged: it stands.
    pending = merge(rows=rows[:1], takes=[*takes[:1], take(a, "kept", "take-a2")]).states
    assert pending["neutral-01"].status == "kept"
