"""How the studio shows a catalog prompt (NATIVE_RECORDING_UX.md §4, PROMPT_CATALOG_V2.md §6).

The Swift app's ``RecordingPromptPresentation``, in Python. The category alone decides the
kind (D-013): a spoken prompt has a Prompt section (READ ALOUD) and no banner; room tone and a
procedure step have a banner and no Prompt, and their text shows as Instructions, never as
something to read aloud. Every prompt has an Instructions section (its instructions, the
guidance and the duration). Catalog text and labels are shown verbatim (plain text).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from cole_voice_lab.models import Prompt

type PromptKind = Literal["spoken", "room-tone", "procedure"]

BANNERS: Final[dict[str, str]] = {
    "room-tone": "SILENT ROOM TONE · DO NOT SPEAK",
    "procedure": "TEST STEP · FOLLOW INSTRUCTIONS",
}
PROMPT_HEADING: Final = "PROMPT · READ ALOUD"
INSTRUCTIONS_HEADING: Final = "INSTRUCTIONS · NOT READ ALOUD"
NO_SPOKEN_PROMPT: Final = "No spoken prompt"


def label_title(token: str) -> str:
    """A label token as a title: ``reference-candidate`` → ``Reference Candidate``."""
    return " ".join(word[:1].upper() + word[1:].lower() for word in token.split("-") if word)


def kind_of(prompt: Prompt) -> PromptKind:
    if prompt.category == "room-tone":
        return "room-tone"
    if prompt.category == "procedure":
        return "procedure"
    return "spoken"


def duration_text(seconds: float) -> str:
    return f"About {max(1, round(seconds))} sec"


@dataclass(frozen=True)
class PromptView:
    """One prompt of the pinned catalog version, as the studio shows it (``index``: its place
    in catalog order)."""

    prompt: Prompt
    index: int
    purpose: str

    @property
    def prompt_id(self) -> str:
        return self.prompt.prompt_id

    @property
    def kind(self) -> PromptKind:
        return kind_of(self.prompt)

    @property
    def title(self) -> str:
        return label_title(self.prompt.prompt_id)

    @property
    def banner(self) -> str:
        return BANNERS.get(self.kind, "")

    @property
    def display_prompt(self) -> str:
        """The read-aloud text; empty for room tone and procedure steps."""
        return (self.prompt.text or "") if self.kind == "spoken" else ""

    @property
    def display_instructions(self) -> str:
        """The silent instructions (a v1 room tone's text shows here, never as a Prompt)."""
        if self.prompt.instructions:
            return self.prompt.instructions
        return "" if self.kind == "spoken" else (self.prompt.text or "")

    @property
    def guidance(self) -> str:
        return f"Guidance: {self.prompt.style}"

    @property
    def duration(self) -> str:
        return duration_text(self.prompt.expected_seconds)

    @property
    def labels(self) -> tuple[str, ...]:
        """Effective labels, in order: the category, ``test`` in a physical-test catalog,
        the catalog's labels, then reference candidate and holdout."""
        tokens: list[str] = [self.prompt.category]
        if self.purpose == "physical-test":
            tokens.append("test")
        tokens.extend(label for label in self.prompt.labels if label != "test")
        if self.prompt.reference_candidate:
            tokens.append("reference-candidate")
        if self.prompt.holdout:
            tokens.append("holdout")
        return tuple(tokens)

    @property
    def pinned_labels(self) -> int:
        """The chips that never collapse into ``+N``: the category (and ``Test``)."""
        return 2 if self.purpose == "physical-test" else 1

    @property
    def haystack(self) -> tuple[str, ...]:
        """What search matches (case-insensitive): the title, the id, the Prompt, the
        Instructions, the guidance and every label's title."""
        fields = (
            self.title,
            self.prompt_id,
            self.display_prompt,
            self.display_instructions,
            self.prompt.style,
            *(label_title(token) for token in self.labels),
        )
        return tuple(field.casefold() for field in fields if field)

    def matches_search(self, text: str) -> bool:
        needle = text.strip().casefold()
        return not needle or any(needle in field for field in self.haystack)


def present(prompts: tuple[Prompt, ...], purpose: str) -> tuple[PromptView, ...]:
    return tuple(PromptView(prompt, index, purpose) for index, prompt in enumerate(prompts))


def label_options(views: tuple[PromptView, ...]) -> tuple[str, ...]:
    """Every effective label, in first-appearance catalog order."""
    seen: dict[str, None] = {}
    for view in views:
        for token in view.labels:
            seen.setdefault(token, None)
    return tuple(seen)


def labels_menu_title(selected: tuple[str, ...]) -> str:
    """``Labels: Any``, up to two titles, else the count (``selected`` in option order)."""
    if not selected:
        return "Labels: Any"
    if len(selected) <= 2:
        return "Labels: " + ", ".join(label_title(token) for token in selected)
    return f"Labels: {len(selected)} selected"
