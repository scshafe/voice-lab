"""Pacing (D-058; the recording UX review's item 6): what this sitting has done, what is left,
and a quiet break hint.

A sitting starts with this run's first take. A pause of ``BREAK_MINUTES`` or more between two
takes is a break: the clock starts again with the next take (the counts go on for the run). The
strip reads *So far · 42 kept · 3 redone · 31 min · 1,610 left ≈ 1 h 58 min reading*: the takes
kept and recorded again since the app started, the minutes since the clock started, and the
prompts not recorded yet with their reading time (the catalog's expected durations). The studio
says no "session": it is corpus-first (D-018), and this is a sitting, not a recording unit.

The strip is read again with each take, so no timer runs for it; after ``HINT_MINUTES`` of a
sitting, the next take adds a quiet hint, never a dialog: a break keeps the voice even
(Microsoft's guidance for dataset talent: two or three hours a day, with regular breaks; Mimic
Recording Studio: every 30 minutes). Dismissed, it comes back after another ``HINT_MINUTES``;
a break clears it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: A pause this long between takes is a break: the session's clock starts again.
BREAK_MINUTES: Final = 10
#: The break hint, this long into a session.
HINT_MINUTES: Final = 30
HINT_TEXT: Final = "30 minutes in · a short break keeps the voice even"


def reading_text(seconds: float) -> str:
    """``≈ 1 h 58 min``, ``≈ 12 min`` or ``≈ 40 s``."""
    total = max(0, round(seconds))
    if total < 60:
        return f"≈ {total} s"
    minutes = round(total / 60)
    if minutes < 60:
        return f"≈ {minutes} min"
    return f"≈ {minutes // 60} h {minutes % 60} min"


@dataclass
class Pacing:
    """This run's sitting. ``clock``: monotonic seconds (a test's own)."""

    clock: Callable[[], float]
    kept: int = 0
    redone: int = 0
    started: float | None = None
    last_take: float | None = None
    hint_due: float | None = None

    def take_started(self) -> None:
        """A take was started: the first, or the first after a break, starts the clock."""
        now = self.clock()
        if self.last_take is None or now - self.last_take >= BREAK_MINUTES * 60:
            self.started = now
            self.hint_due = now + HINT_MINUTES * 60
        self.last_take = now

    def minutes(self) -> int:
        """Minutes since the clock started (0 before the first take)."""
        if self.started is None:
            return 0
        return int((self.clock() - self.started) // 60)

    @property
    def on_break(self) -> bool:
        """No take for ``BREAK_MINUTES``: the next one starts the clock again."""
        return self.last_take is not None and (self.clock() - self.last_take >= BREAK_MINUTES * 60)

    @property
    def hint(self) -> bool:
        """The break hint is due (and not on a break already)."""
        due = self.hint_due
        return due is not None and self.clock() >= due and not self.on_break

    def dismiss_hint(self) -> None:
        if self.hint:
            self.hint_due = self.clock() + HINT_MINUTES * 60

    def text(self, *, left: int, left_seconds: float) -> str:
        """The strip's line."""
        parts = [f"So far · {self.kept:,} kept", f"{self.redone:,} redone"]
        if self.started is not None:
            parts.append(f"{self.minutes()} min")
        parts.append(f"{left:,} left {reading_text(left_seconds)} reading")
        return " · ".join(parts)
