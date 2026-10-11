"""Referee (design §6.1; checklist R): blind same-song A/B trials and the results by year.

Skeleton: filled in by the 4a work on this screen.
"""

from __future__ import annotations

from PySide6.QtCore import Signal

from ..viewmodel import AppContext, ViewModel, qproperty


class RefereeModel(ViewModel):
    changed = Signal()

    state = qproperty(str, "state", changed)
    error = qproperty(str, "error", changed)

    def __init__(self, context: AppContext) -> None:
        super().__init__(context, {"state": "idle", "error": ""})

    def activate(self) -> None:
        pass

    def forget(self) -> None:
        pass

    def shutdown(self) -> None:
        pass
