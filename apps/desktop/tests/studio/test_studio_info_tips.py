"""The studio's explanations behind an ⓘ (D-059, docs/design/INFO-TIPS.md): the waveform's
scale, the inspector's flag details and levels and the sync's message, and Check Input's
two. Each one's text is the tip's, and the tip opens on hover, on a click and from the
keyboard. What it explains stays where it was (the flags, the sync line, the check's stage).
The library's ⓘ is in test_studio_screen.py."""

from __future__ import annotations

from typing import Any

import info_tips
from conftest import Window
from studio_fakes import StudioRecorder, voice_samples

from .conftest import wait_phase
from .test_studio_quality import expected, record_take


def test_the_waveforms_scale_is_its_info_tip(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    tip = studio_window.find("studioWaveInfo")
    note = studio_window.find("studioWaveNote")
    assert not tip.isVisible()  # no waveform yet
    record_take(qtbot, studio_window, recorder)
    qtbot.waitUntil(lambda: len(takes.bins) > 0, timeout=5000)
    assert tip.isVisible()
    assert info_tips.body(tip).startswith("The dB display scale: 0 dBFS reaches the full height")
    assert "dB display scale" not in note.property("text")
    info_tips.opens_on_hover_click_and_keys(qtbot, studio_window, tip)
    # The caption keeps its place whatever it says.
    place = note.property("x")
    studio.save()
    wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: note.property("text").startswith("Kept"), timeout=5000)
    assert note.property("x") == place


def test_the_inspectors_flags_keep_their_names_and_put_why_behind_the_info_tip(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio, takes = studio_window.model.studio, studio_window.model.studio.takes
    flags = studio_window.find("studioInspectorFlags")
    tip = studio_window.find("studioInspectorFlagsInfo")
    assert flags.property("text") == "No take of this machine selected" and not tip.isVisible()
    seconds = expected(studio_window, studio.promptId)
    recorder.voice = lambda seed: voice_samples(seconds, lead=0.0, tail=seconds / 5, seed=seed)
    record_take(qtbot, studio_window, recorder)
    qtbot.waitUntil(lambda: takes.qualityState == "checked", timeout=5000)
    assert flags.property("text") == "Cut start" and flags.property("tone") == "warn"
    detail = info_tips.body(tip).splitlines()
    assert detail[0] == "Speech in the first 0.1 s: a word may be cut."
    assert detail[1].startswith("Speech −40.0 dBFS · room −")
    assert info_tips.body(tip) == takes.qualityDetail
    shown = info_tips.shown_texts(studio_window.find("studioInspector"))
    assert not any(text.startswith("Speech in the first") for text in shown)
    info_tips.opens_on_hover_click_and_keys(qtbot, studio_window, tip)
    # A take with none (a retake).
    recorder.voice = lambda seed: voice_samples(seconds, seed=seed)
    record_take(qtbot, studio_window, recorder)
    qtbot.waitUntil(lambda: takes.qualityState == "checked" and not takes.flags, timeout=5000)
    assert flags.property("text") == "No flags" and info_tips.body(tip).startswith("No flags.")


def test_the_sync_message_is_the_sync_headings_info_tip(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    tip = studio_window.find("studioInspectorSyncInfo")
    record_take(qtbot, studio_window, recorder)
    studio.save()
    wait_phase(qtbot, studio, "ready")
    qtbot.waitUntil(lambda: studio.syncBadge == "Uploads off", timeout=5000)
    assert studio.syncDetail == "" and not tip.isVisible()
    studio.client.spool._notice(
        studio.client.takes()[0], "changed-elsewhere", "Changed elsewhere: the PC's state."
    )
    studio.client.sync_now()
    qtbot.waitUntil(lambda: studio.syncBadge == "A notice", timeout=5000)
    assert studio.syncDetail != "" and tip.isVisible()
    assert info_tips.body(tip) == studio.syncDetail
    assert studio_window.find("studioInspectorSync").property("text") == studio.syncText
    info_tips.opens_on_hover_click_and_keys(qtbot, studio_window, tip)


def test_check_inputs_explanations_are_info_tips(
    qtbot: Any, studio_window: Window, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    dialog = studio_window.find("studioCheckDialog")
    studio.openCheck()
    qtbot.waitUntil(lambda: dialog.property("opened"), timeout=5000)
    about, match = studio_window.find("studioCheckInfo"), studio_window.find("studioMatchLineInfo")
    assert info_tips.body(about).startswith("About ten seconds: two of quiet")
    assert info_tips.body(match).startswith("Keeps your first reading of this line")
    assert studio_window.find("studioCheckTitle").property("text") == "Check input"
    # The dialog's first focus is still its first control, never a tip.
    qtbot.waitUntil(lambda: studio_window.root.activeFocusItem() is not None, timeout=5000)
    focused = studio_window.root.activeFocusItem()
    assert focused.objectName() == "studioMatchLine"
    for tip in (about, match):
        info_tips.opens_on_hover_click_and_keys(qtbot, studio_window, tip)
    texts = info_tips.shown_texts(dialog.property("contentItem"))
    assert not any(text.startswith(("About ten seconds", "Keeps your first")) for text in texts)
