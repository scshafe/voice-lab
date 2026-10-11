"""Corpus (design §6.1; checklist C): the recordings table, row detail, playback, verdicts.

Skeleton: filled in by the 4a work on this screen.
"""

from __future__ import annotations

from PySide6.QtCore import Signal

from ..viewmodel import AppContext, ViewModel, qproperty


class CorpusModel(ViewModel):
    changed = Signal()

    state = qproperty(str, "state", changed)
    error = qproperty(str, "error", changed)

    def __init__(self, context: AppContext) -> None:
        super().__init__(context, {"state": "idle", "error": ""})

    def activate(self) -> None:
        pass

    def forget(self) -> None:
        pass

    def show_query(self, query: str) -> None:
        """Open on the rows matching ``query`` (a click-through from Journey or Practice)."""
