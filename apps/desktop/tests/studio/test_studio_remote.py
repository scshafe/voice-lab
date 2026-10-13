"""Takes recorded elsewhere, reviewed in the studio: a take another client uploaded and a take
whose record the PC's importer wrote (step 9, D-053) are listed with the PC's state, played
from the PC, and rejected, trimmed, restored and kept from here through the corpus client;
the PC's history follows each change; offline, the changes wait and go later. A take the PC
says was imported from the Mac says so in the picker and its list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import Window
from corpus_rig import PC, bind_library, client, record
from PySide6.QtQuick import QQuickWindow  # noqa: F401  (root objects come back as QQuickWindow)
from studio_fakes import StudioRecorder
from test_corpus_remote import imported_style, pc_take, prompt_of, recorded_elsewhere
from voice_lab_desktop.corpus.records import RemoteOrigin

from .conftest import open_studio, ready, studio_app
from .test_studio_screen import click, prompt_ids


def pc_state(window: Window, pc: PC, take_id: str) -> tuple[str, int, tuple[Any, Any, int]]:
    there = pc_take(window.model.studio.client, pc, take_id)
    trim = there.trim
    return (
        there.decision,
        there.decision_revision,
        (trim.start_frame, trim.end_frame, trim.revision),
    )


def test_takes_recorded_elsewhere_are_decided_and_trimmed_here(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, library: Any
) -> None:
    other = recorded_elsewhere(pc, tmp_path, seed=81)
    imported = recorded_elsewhere(pc, tmp_path, seed=82)
    imported_style(pc, imported)  # kept, trimmed [2400, 9600), at revision 1
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, StudioRecorder(), enabled=True)
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio, takes = model.studio, model.studio.takes
            client = studio.client
            qtbot.waitUntil(lambda: studio.coverageText == "1 / 34", timeout=10000)
            # The prompt's latest kept take is the imported one, played from the PC.
            studio.selectPrompt(ids[0])
            qtbot.waitUntil(lambda: takes.selectedKey == imported, timeout=10000)
            qtbot.waitUntil(lambda: takes.frameCount == 12_000, timeout=10000)
            assert (takes.trimStart, takes.trimEnd) == (2_400, 9_600)
            assert [row["where"] for row in takes.history] == ["the PC", "the PC"]
            assert takes.canRemove and takes.canReject and takes.canEditTrim
            assert not any(take.remote for take in client.takes())  # nothing noted yet
            # Rejected from here, with its reason: noted here, sent, applied on the PC.
            click(window, "studioRejectTake")
            reason = window.items("studioRejectReason")[0]
            qtbot.waitUntil(lambda: reason.hasActiveFocus(), timeout=5000)
            reason.setProperty("text", "the wrong word")
            window.items("studioConfirmReject")[0].clicked.emit()
            noted = client.spool.take(imported)
            assert noted.remote and noted.sync.outbox[0].expected_revision == 1
            qtbot.waitUntil(
                lambda: pc_state(window, pc, imported)[:2] == ("rejected", 2), timeout=30000
            )
            qtbot.waitUntil(
                lambda: takes.history[0]["status"].startswith("Rejected (the wrong word)"),
                timeout=10000,
            )
            # Trimmed again (wider, with the handles; then In from the playhead), and saved:
            # the PC's trim follows.
            takes.restore()
            takes.setTrimRange(600, 10_800)
            takes.seekTo(1_200)
            takes.setIn()
            click(window, "studioSaveTrim")
            qtbot.waitUntil(
                lambda: pc_state(window, pc, imported) == ("kept", 3, (1_200, 10_800, 2)),
                timeout=30000,
            )
            # The other client's draft: selected and kept with Save.
            takes.selectTake(other)
            qtbot.waitUntil(lambda: takes.frameCount == 12_000, timeout=10000)
            assert studio.canSave
            click(window, "studioSave")
            qtbot.waitUntil(lambda: pc_state(window, pc, other)[:2] == ("kept", 1), timeout=30000)
            # Neither has audio here; both are this machine's to decide, not its recordings.
            for take in client.takes():
                assert take.remote and not (take.path / "original.wav").exists()
            assert client.status().takes == 0
            # Offline: a removal waits, then goes when the PC is back.
            pc.stop()
            takes.remove()
            assert client.spool.take(other).sync.outbox
            qtbot.waitUntil(lambda: studio.syncText.startswith("Offline"), timeout=30000)
            pc.start()
            studio.syncNow()
            qtbot.waitUntil(
                lambda: pc_state(window, pc, other)[:2] == ("removed", 2), timeout=30000
            )
            # Its receipt is recorded here once the answer is in.
            qtbot.waitUntil(lambda: client.spool.take(other).sync.outbox == (), timeout=30000)
    finally:
        model.shutdown()


def test_a_take_imported_from_the_mac_says_so(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, library: Any
) -> None:
    imported = recorded_elsewhere(pc, tmp_path, seed=83)
    imported_style(pc, imported)
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, StudioRecorder())
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio, takes = model.studio, model.studio.takes
            studio.selectPrompt(ids[0])
            qtbot.waitUntil(lambda: takes.selectedKey == imported, timeout=10000)
            # The PC's origin (D-053) as the client keeps it for the take.
            origin = RemoteOrigin(kind="imported", source_take_id="swift-take-0001")
            studio.client.spool.learn_remote(
                pc_take(studio.client, pc, imported), ids[0], origin=origin
            )
            studio._client_changed()
            qtbot.waitUntil(
                lambda: takes.history[0]["origin"] == "imported from the Mac", timeout=5000
            )
            assert takes.takeOptions[0]["label"].endswith("· Kept · Mac import")
            assert "imported from the Mac" in takes.history[0]["status"]
            assert window.find("studioTakeSelectText").property("text").endswith("Mac import")
    finally:
        model.shutdown()


def decided_elsewhere(pc: PC, tmp_path: Path, decisions: list[str], *, seed: int) -> list[str]:
    """Takes of the first prompt another machine recorded and decided (``decisions``, oldest
    first; a rejection with its reason) on the PC. Their ids, in that order."""
    other_dir = tmp_path / f"decided-{seed}"
    other_dir.mkdir()
    bind_library(other_dir)
    other = client(other_dir, pc, enabled=True)
    other.run_pass()
    prompt = prompt_of(pc)
    taken = [record(other, prompt, seed=seed + i) for i in range(len(decisions))]
    assert other.run_pass().uploaded == len(taken)
    for take, decision in zip(taken, decisions, strict=True):
        other.decide(take.take_id, decision, "a noise" if decision == "rejected" else None)  # type: ignore[arg-type]
    assert other.run_pass().applied == len(taken)
    return [take.take_id for take in taken]


def test_a_removal_here_is_never_hidden_by_the_pcs_kept(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, library: Any
) -> None:
    """The PC's only kept take of a prompt, with two rejected siblings, removed here while
    nothing is sent (corpus_sync = false): the prompt is missing here at once, counted as
    three takes, and stays missing once the PC's history of it is read."""
    kept, *_rejected = decided_elsewhere(pc, tmp_path, ["kept", "rejected", "rejected"], seed=90)
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, StudioRecorder())  # sync off
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio, takes = model.studio, model.studio.takes
            qtbot.waitUntil(lambda: studio.coverageText == "1 / 34", timeout=10000)
            studio.selectPrompt(ids[0])
            qtbot.waitUntil(lambda: takes.selectedKey == kept, timeout=10000)
            assert studio.rowsModel.rows[0]["statusTitle"] == "Recorded"
            assert studio.takesText == "3 takes of this prompt"
            click(window, "studioRemoveTake")
            noted = studio.client.spool.take(kept)
            assert noted.remote and noted.sync.decision.decision == "removed"
            assert pc.library().rows[0].status == "kept"  # nothing sent
            qtbot.waitUntil(lambda: studio.coverageText == "0 / 34", timeout=10000)
            assert studio.rowsModel.rows[0]["statusTitle"] == "Missing"
            assert studio.takesText == "3 takes of this prompt"  # each take once
            # The PC's row cannot say which take is kept: its history is read, and agrees.
            qtbot.waitUntil(lambda: ids[0] in studio._histories, timeout=10000)
            studio._client_changed()
            assert studio.coverageText == "0 / 34"
            assert studio.rowsModel.rows[0]["statusTitle"] == "Missing"
            assert studio.takesText == "3 takes of this prompt"
    finally:
        model.shutdown()


def test_restore_asks_when_what_came_before_is_not_known(
    qtbot: Any, data_dir: Path, tmp_path: Path, pc: PC, library: Any
) -> None:
    """A take recorded elsewhere and seen here only rejected: Restore asks, Restore as kept
    or as draft; Cancel changes nothing. Once its decision before is known here, Restore
    uses it without asking."""
    (rejected,) = decided_elsewhere(pc, tmp_path, ["rejected"], seed=95)
    ids = prompt_ids(pc)
    model = studio_app(data_dir, tmp_path, pc, StudioRecorder())
    try:
        with open_studio(qtbot, model, tmp_path) as window:
            ready(qtbot, model)
            studio, takes = model.studio, model.studio.takes
            client = studio.client
            studio.selectPrompt(ids[0])
            qtbot.waitUntil(lambda: len(takes.history) == 1, timeout=10000)
            takes.selectTake(rejected)
            qtbot.waitUntil(lambda: takes.canRestore, timeout=5000)
            dialog = window.find("studioRestoreAsk")
            click(window, "studioRestoreTake")
            qtbot.waitUntil(
                lambda: takes.restoreAskOpen and dialog.property("visible"), timeout=5000
            )
            assert window.find("studioScreen").property("menuOpen")  # keys off meanwhile
            window.items("studioRestoreCancel")[0].clicked.emit()
            qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=5000)
            assert not takes.restoreAskOpen
            assert not any(take.remote for take in client.takes())  # nothing noted
            click(window, "studioRestoreTake")
            qtbot.waitUntil(lambda: dialog.property("visible"), timeout=5000)
            window.items("studioRestoreAsDraft")[0].clicked.emit()
            qtbot.waitUntil(lambda: not dialog.property("visible"), timeout=5000)
            noted = client.spool.take(rejected)
            assert noted.remote and noted.sync.decision.decision == "draft"
            qtbot.waitUntil(lambda: takes.history[0]["decision"] == "draft", timeout=5000)
            # Rejected again here: its decision before (draft) is known, so no question.
            client.decide(rejected, "rejected", "again")
            studio._client_changed()
            qtbot.waitUntil(lambda: takes.canRestore, timeout=5000)
            click(window, "studioRestoreTake")
            assert not takes.restoreAskOpen and not dialog.property("visible")
            assert client.spool.take(rejected).sync.decision.decision == "draft"
    finally:
        model.shutdown()
