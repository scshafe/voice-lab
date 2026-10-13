"""The studio and the PC's catalog (D-052, RECORDING-V1.md §3 "Clients"): with no cached
catalog and no PC nothing can be recorded, and the studio says so; with a cached one it
records offline; a new active version on the PC is announced and switches at the next
session (or at Switch now, between takes), and the PC's library still counts the prompts
the two versions share. Notices from the client stay until they are dismissed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corpus_rig import PC, client
from corpus_take_fixtures import run_catalog
from studio_fakes import StudioRecorder
from voice_lab_desktop.corpus.status import NO_CATALOG

from cole_voice_lab.models import COLE_SUBJECT

from .conftest import open_studio, ready, studio_app, wait_phase
from .test_studio_screen import click, kept_elsewhere, prompt_ids, take_one


def test_offline_without_a_cached_catalog_nothing_is_recorded(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, recorder: StudioRecorder
) -> None:
    pc.stop()
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            studio = model.studio
            qtbot.waitUntil(lambda: studio.deviceOk and studio.syncText != "", timeout=10000)
            qtbot.waitUntil(lambda: studio.client.status().offline, timeout=10000)
            assert studio.phase == "blocked"
            assert studio.coverageText == "–" and studio.rowCount == 0
            assert studio.catalogText == "No catalog yet"
            assert studio.switchText == "Recording starts once the PC has answered"
            assert window.find("studioPlaceholder").property("text") == NO_CATALOG
            assert studio.statusText == "No catalog yet: nothing can be recorded"
            assert not studio.canRecord and not studio.canContinueMissing
            assert not window.find("studioRecord").property("enabled")
            assert not window.find("studioContinueMissing").property("enabled")
            studio.record()
            assert recorder.takes == []
            # The PC comes back: the next pass pins its catalog and the studio opens on it.
            pc.start()
            studio.syncNow()
            wait_phase(qtbot, studio, "ready")
            assert studio.rowCount == 34 and studio.promptId == prompt_ids(pc)[0]
            assert studio.catalogText == "cole-corpus · v1"
    finally:
        model.shutdown()


def test_with_a_cached_catalog_it_records_offline(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, library: Any
) -> None:
    client(data_dir, pc).run_pass()  # an earlier run fetched the catalog
    pc.stop()
    recorder = StudioRecorder(simulated=False)
    model = studio_app(data_dir, tmp_path, pc, recorder, enabled=True)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            qtbot.waitExposed(window.root)
            studio = model.studio
            qtbot.waitUntil(lambda: studio.phase == "ready" and studio.deviceOk, timeout=10000)
            qtbot.waitUntil(lambda: studio.syncText.startswith("Offline"), timeout=10000)
            # Coverage counts the PC's library as the earlier run read it.
            assert studio.coverageSource.startswith("The PC's library (read ")
            take_one(qtbot, window, recorder)
            click(window, "studioSaveNext")
            wait_phase(qtbot, studio, "ready")
            assert studio.coverageText == "1 / 34"
            assert studio.rowsModel.rows[0]["statusTitle"] == "Recorded · this machine"
            # Its upload and its Save wait in the outbox.
            qtbot.waitUntil(lambda: studio.syncText.startswith("Offline · 2 queued"), timeout=10000)
            assert studio.syncTone == "neutral"
            # Back online: the take goes, and the PC's library counts it.
            pc.start()
            studio.syncNow()
            qtbot.waitUntil(lambda: studio.client.status().sent == 2, timeout=30000)
            qtbot.waitUntil(
                lambda: studio.rowsModel.rows[0]["statusTitle"] == "Recorded", timeout=30000
            )
            assert studio.coverageSource.startswith("The PC's library (read ")
            assert studio.coverageText == "1 / 34"
            assert studio.syncText == "Up to date · 2 sent"
    finally:
        model.shutdown()


def test_a_new_version_on_the_pc_switches_at_the_next_session(
    qtbot: Any,
    data_dir: Path,
    tmp_path: Path,
    pc: PC,
    recorder: StudioRecorder,
) -> None:
    kept_elsewhere(pc, tmp_path, (0,))
    model = studio_app(data_dir, tmp_path, pc, recorder)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio = model.studio
            qtbot.waitUntil(lambda: studio.coverageText == "1 / 34", timeout=10000)
            assert not window.find("studioSwitch").property("visible")
            run_catalog(pc.layout, "add-prompt", "warm-05", "--text", "The kettle clicked off.")
            run_catalog(pc.layout, "publish")
            run_catalog(pc.layout, "activate", "--subject-id", COLE_SUBJECT, "--version", "2")
            studio.syncNow()
            qtbot.waitUntil(lambda: studio.canSwitch, timeout=10000)
            # This session stays on version 1 and says so; the PC's library (version 2)
            # still counts the prompt the two versions share.
            assert (
                studio.switchText
                == "Version 2 is active on the PC: it switches at the next session"
            )
            assert studio.catalogText == "cole-corpus · v1"
            qtbot.waitUntil(lambda: "version 2" in studio.coverageSource, timeout=10000)
            assert studio.coverageText == "1 / 34" and studio.rowCount == 34
            assert window.find("studioSwitch").property("visible")
            # Not while a take waits for review.
            take_one(qtbot, window, recorder)
            assert not window.find("studioSwitch").property("enabled")
            studio.switchCatalog()
            assert studio.catalogText.startswith("cole-corpus · v1")
            click(window, "studioSave")
            wait_phase(qtbot, studio, "ready")
            assert studio.client.takes()[0].record.catalog.catalog_version == 1
            click(window, "studioSwitch")
            qtbot.waitUntil(lambda: studio.rowCount == 35, timeout=5000)
            assert studio.catalogText == "cole-corpus · v2" and not studio.canSwitch
            assert studio.switchText == ""
            # The take of version 1 still counts: its prompt is unchanged in version 2.
            assert studio.coverageText == "2 / 35"
            studio.selectPrompt("warm-05")
            assert window.find("studioScript").property("text") == "The kettle clicked off."
    finally:
        model.shutdown()


def test_a_notice_stays_until_it_is_dismissed(
    qtbot: Any, studio_window: Any, recorder: StudioRecorder
) -> None:
    studio = studio_window.model.studio
    take_one(qtbot, studio_window, recorder)
    studio.save()
    wait_phase(qtbot, studio, "ready")
    take = studio.client.takes()[0]
    # What the client keeps when the PC adopted another client's change of this take.
    studio.client.spool._notice(
        take, "changed-elsewhere", "Changed elsewhere: this take now shows the PC's state."
    )
    studio.client.sync_now()
    qtbot.waitUntil(lambda: studio.noticeId != "", timeout=5000)
    assert studio.syncText == "Reference Neutral 01: changed elsewhere"
    assert studio.syncTone == "warn"
    # Details shows the client's whole notice, and dismisses it.
    details = studio_window.find("studioDetails")
    qtbot.waitUntil(lambda: details.property("visible"), timeout=5000)
    click(studio_window, "studioDetails")
    popup = studio_window.find("studioDetailsPopup")
    qtbot.waitUntil(lambda: popup.property("visible"), timeout=5000)
    assert studio_window.items("studioDetailsText")[0].property("text") == (
        "Reference Neutral 01: Changed elsewhere: this take now shows the PC's state."
    )
    studio_window.items("studioDismissNotice")[0].clicked.emit()
    qtbot.waitUntil(lambda: studio.noticeId == "", timeout=5000)
    assert studio.client.notices() == ()
    assert studio.syncText.startswith("Uploads off until S-1 passes")
    assert not popup.property("visible") and not details.property("visible")
