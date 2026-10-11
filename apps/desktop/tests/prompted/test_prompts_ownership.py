"""Record → Prompts' model is C++-owned and survives QML's garbage collector.

QML reads it through the app's ``promptsModel`` property; a parentless ``QObject`` handed to
QML that way may become JavaScript-owned under QML's ownership heuristics, so the app sets
its ownership explicitly. (The flaky "Unable to assign [undefined]" failures first blamed
on this had another cause: per-instance metaobjects, see ``test_qml_metaobjects.py``.)
This test is a guard: QML's heuristics do not flip the ownership on the reads made here.
"""

from __future__ import annotations

from typing import Any

import shiboken6
from conftest import Window
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)


def test_the_prompts_model_outlives_a_qml_garbage_collection(
    qtbot: Any, prompts_window: Window
) -> None:
    model = prompts_window.model
    context = prompts_window.engine.rootContext()
    for _ in range(3):
        # Read it as the screen does, then let QML collect what JavaScript owns.
        read, _undefined = QQmlExpression(context, None, "app.promptsModel").evaluate()
        assert read is model.prompts
        del read
        prompts_window.engine.collectGarbage()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
    assert shiboken6.isValid(model.prompts)
    ownership = QQmlEngine.objectOwnership(model.prompts)
    assert ownership == QQmlEngine.ObjectOwnership.CppOwnership
    assert model.promptsModel is model.prompts
    assert prompts_window.find("promptsScreen").property("visible")
