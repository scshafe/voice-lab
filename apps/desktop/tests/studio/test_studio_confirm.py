"""Leaving a take with a trim edit not saved asks first (NATIVE_RECORDING_UX.md §5: "an unsaved
trim remains an explicit edit with the existing discard confirmation"): selecting another
take or prompt, Continue Missing, Record, or another screen opens "Save the trim?" with
Save trim, Discard edit and Cancel, each doing what it says; without an edit nothing is
asked. The take under review keeps its edit (Save, Retake and leaving carry it)."""

from __future__ import annotations

from typing import Any

from conftest import Window
from corpus_rig import PC
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from studio_fakes import StudioRecorder

from .conftest import wait_phase
from .test_studio_screen import prompt_ids
from .test_studio_takes import recorded, saved


def edited(qtbot: Any, window: Window, start: int = 2_400) -> None:
    takes = window.model.studio.takes
    takes.seekTo(start)
    takes.setIn()
    assert takes.trimDirty


def answer(qtbot: Any, window: Window, button: str) -> None:
    """The question is open; press ``button``."""
    takes = window.model.studio.takes
    dialog = window.find("studioTrimConfirm")
    qtbot.waitUntil(lambda: takes.confirmOpen and dialog.property("visible"), timeout=5000)
    window.items(button)[0].clicked.emit()
    qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=5000)
    assert not takes.confirmOpen


def trim_of(window: Window, take_id: str) -> tuple[int | None, int | None]:
    trim = window.model.studio.client.spool.take(take_id).sync.trim
    return trim.start_frame, trim.end_frame


def test_another_take_save_discard_or_cancel(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    takes = studio_window.model.studio.takes
    first = saved(qtbot, studio_window, recorder)
    second = saved(qtbot, studio_window, recorder)
    assert takes.selectedKey == second
    # Without an edit nothing is asked.
    takes.selectTake(first)
    assert takes.selectedKey == first and not takes.confirmOpen
    qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
    # Cancel: the take stays selected, its edit kept.
    edited(qtbot, studio_window)
    takes.selectTake(second)
    answer(qtbot, studio_window, "studioConfirmCancel")
    assert takes.selectedKey == first and takes.trimDirty and takes.trimStart == 2_400
    # Discard edit: on to the other take; the edit is gone, nothing was saved.
    takes.selectTake(second)
    answer(qtbot, studio_window, "studioConfirmDiscard")
    assert takes.selectedKey == second and trim_of(studio_window, first) == (None, None)
    # Save trim: saved, then on.
    qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
    edited(qtbot, studio_window, 4_800)
    takes.selectTake(first)
    answer(qtbot, studio_window, "studioConfirmSave")
    assert takes.selectedKey == first and trim_of(studio_window, second) == (4_800, 12_000)


def test_asked_again_as_the_question_closes(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    """Asked again while the dialog's exit transition runs (Cancel, then the other take at
    once): the dialog shows the question again, rather than closing with it still open."""
    takes = studio_window.model.studio.takes
    first = saved(qtbot, studio_window, recorder)
    second = saved(qtbot, studio_window, recorder)
    takes.selectTake(first)
    qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
    edited(qtbot, studio_window)
    dialog = studio_window.find("studioTrimConfirm")
    takes.selectTake(second)
    qtbot.waitUntil(lambda: dialog.property("opened"), timeout=5000)
    studio_window.items("studioConfirmCancel")[0].clicked.emit()
    assert not takes.confirmOpen and dialog.property("visible")  # its exit runs
    takes.selectTake(second)
    assert takes.confirmOpen
    qtbot.waitUntil(lambda: dialog.property("opened"), timeout=5000)
    qtbot.wait(400)  # longer than any transition
    assert takes.confirmOpen and dialog.property("visible") and dialog.property("opened")
    answer(qtbot, studio_window, "studioConfirmCancel")
    assert takes.selectedKey == first and takes.trimDirty


def test_another_prompt_continue_missing_record_and_another_screen(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder, pc: PC
) -> None:
    model = studio_window.model
    studio, takes = model.studio, model.studio.takes
    ids = prompt_ids(pc)
    take_id = saved(qtbot, studio_window, recorder)
    # Another prompt: Save trim, then the prompt.
    edited(qtbot, studio_window)
    studio.selectPrompt(ids[5])
    assert studio.promptId == ids[0]  # not yet
    answer(qtbot, studio_window, "studioConfirmSave")
    assert studio.promptId == ids[5] and trim_of(studio_window, take_id) == (2_400, 12_000)
    # Continue Missing: Discard, then on.
    studio.selectPrompt(ids[0])
    qtbot.waitUntil(lambda: takes.selectedKey == take_id and takes.frameCount > 0, timeout=5000)
    edited(qtbot, studio_window, 4_800)
    studio.continueMissing()
    answer(qtbot, studio_window, "studioConfirmDiscard")
    assert studio.promptId == ids[1] and trim_of(studio_window, take_id) == (2_400, 12_000)
    # Record: Cancel keeps the edit and records nothing; Save trim, then the take starts.
    studio.selectPrompt(ids[0])
    qtbot.waitUntil(lambda: takes.selectedKey == take_id and takes.frameCount > 0, timeout=5000)
    edited(qtbot, studio_window, 4_800)
    count = len(recorder.takes)
    studio.toggleRecord()
    answer(qtbot, studio_window, "studioConfirmCancel")
    assert len(recorder.takes) == count and studio.phase == "ready" and takes.trimDirty
    studio.toggleRecord()
    answer(qtbot, studio_window, "studioConfirmSave")
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    assert trim_of(studio_window, take_id) == (4_800, 12_000)
    recorder.last.advance(4_800)
    wait_phase(qtbot, studio, "recording")
    studio.toggleRecord()
    wait_phase(qtbot, studio, "review")
    studio.save()
    wait_phase(qtbot, studio, "ready")
    # Another screen: Cancel stays; Discard edit leaves.
    takes.selectTake(take_id)
    qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
    edited(qtbot, studio_window, 7_200)
    model.navigate("status")
    answer(qtbot, studio_window, "studioConfirmCancel")
    assert model.screen == "prompts" and takes.trimDirty
    model.navigate("status")
    answer(qtbot, studio_window, "studioConfirmDiscard")
    assert model.screen == "status" and not takes.trimDirty
    assert trim_of(studio_window, take_id) == (4_800, 12_000)


def test_the_take_under_review_keeps_its_edit(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    first = saved(qtbot, studio_window, recorder)
    recorded(qtbot, studio_window, recorder)
    edited(qtbot, studio_window)
    # Looking at another take asks nothing: the edit waits with the take under review.
    takes.selectTake(first)
    assert not takes.confirmOpen and takes.selectedKey == first and not takes.trimDirty
    takes.selectTake("review")
    qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
    assert (takes.trimStart, takes.trimDirty) == (2_400, True)
    # Record again: no question; the replaced take is saved as a draft with its trim.
    count = len(recorder.takes)
    studio.toggleRecord()
    assert not takes.confirmOpen
    qtbot.waitUntil(lambda: len(recorder.takes) == count + 1, timeout=5000)
    qtbot.waitUntil(lambda: len(studio.client.takes()) == 2, timeout=5000)
    draft = next(t for t in studio.client.takes() if t.take_id != first)
    assert draft.sync.decision.decision == "draft"
    assert (draft.sync.trim.start_frame, draft.sync.trim.end_frame) == (2_400, 12_000)


def test_closing_the_window_or_quit_asks_first(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    """With a trim edit not saved, Quit and closing the window ask "Save the trim?" too:
    Cancel keeps the app open, the edit kept; Save trim saves it, then the window closes."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from voice_lab_desktop.app import QuitGuard

    model, takes = studio_window.model, studio_window.model.studio.takes
    root = studio_window.root
    take_id = saved(qtbot, studio_window, recorder)
    qtbot.waitUntil(lambda: takes.frameCount > 0, timeout=5000)
    assert model.mayClose()  # nothing to ask about
    edited(qtbot, studio_window)
    # Quit (what ⌘Q sends the application, as cli.run guards it): held, and asked.
    app = QCoreApplication.instance()
    guard = QuitGuard(model)
    app.installEventFilter(guard)
    try:
        quit_event = QEvent(QEvent.Type.Quit)
        QCoreApplication.sendEvent(app, quit_event)
    finally:
        app.removeEventFilter(guard)
    assert not quit_event.isAccepted()
    answer(qtbot, studio_window, "studioConfirmCancel")
    assert (
        root.isVisible()
        and takes.trimDirty
        and trim_of(studio_window, take_id)
        == (
            None,
            None,
        )
    )
    # Closing the window, from another screen's route too: asked; Save trim, then closed.
    root.close()
    assert root.isVisible()
    answer(qtbot, studio_window, "studioConfirmSave")
    qtbot.waitUntil(lambda: not root.isVisible(), timeout=5000)
    assert trim_of(studio_window, take_id) == (2_400, 12_000)
