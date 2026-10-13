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


def take(prompt: Prompt, decision: str, take_id: str) -> Any:
    """A saved take as the merge reads it (its prompt, its decision here)."""
    return SimpleNamespace(
        take_id=take_id,
        prompt_id=prompt.prompt_id,
        record=SimpleNamespace(prompt=prompt),
        sync=SimpleNamespace(decision=SimpleNamespace(decision=decision)),
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


def test_a_prompt_stands_on_the_better_of_the_pc_and_this_machine() -> None:
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
            row(d, "needs-attention", count=2),
            row(e, "draft", latest="take-pc-e"),
        )
    )
    takes = [
        take(b, "kept", "take-here-b"),
        take(c, "removed", "take-here-c"),  # the PC's one take of it, removed here
        take(e, "rejected", "take-here-e"),
        take(f, "draft", "take-here-f"),
    ]
    states = library.prompt_states(shown, snapshot, pc, takes)  # type: ignore[arg-type]
    assert states["neutral-01"] == PromptState("kept", pc_takes=1)
    assert states["neutral-02"] == PromptState("kept", local_takes=1, here_only=True)
    assert states["neutral-02"].title == "Recorded · this machine"
    assert states["neutral-03"] == PromptState("missing", pc_takes=1, local_takes=1)
    assert states["neutral-04"].status == "needs-attention" and states["neutral-04"].recorded
    assert states["neutral-05"] == PromptState("draft", pc_takes=1, local_takes=1)
    assert states["neutral-06"] == PromptState("draft", local_takes=1)
    assert states["neutral-07"] is library.MISSING
    assert library.covered(states) == 3
    # Without the PC's library, only this machine's takes count.
    alone = library.prompt_states(shown, snapshot, None, takes)  # type: ignore[arg-type]
    assert library.covered(alone) == 1 and alone["neutral-01"] is library.MISSING


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
