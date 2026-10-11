"""Analysis → Journey (design §6.1; checklist J): trends, journeys, charts, the take inspector.

Skeleton: filled in by the 4a work on this screen.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal

from ..viewmodel import AppContext, ViewModel, qproperty


class JourneyModel(ViewModel):
    changed = Signal()

    state = qproperty(str, "state", changed)
    error = qproperty(str, "error", changed)

    def __init__(self, context: AppContext, open_corpus: Callable[[str], None]) -> None:
        super().__init__(context, {"state": "idle", "error": ""})
        self.open_corpus = open_corpus

    def activate(self) -> None:
        pass

    def forget(self) -> None:
        pass
