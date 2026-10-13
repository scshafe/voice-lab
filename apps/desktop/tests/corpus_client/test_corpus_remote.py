"""Takes recorded elsewhere (``learn_remote``): a take another client uploaded, and a take
whose record the PC's importer wrote at revision 1 (step 9's imported Swift takes, D-053),
are noted here from the PC's history and decided and trimmed through the same outbox and
sync rules as this machine's takes: chained on the PC's revisions, folded while untried,
refused and adopted, queued offline and sent later. A remote entry never claims audio here
and is never uploaded; the PC's history is read again without ever going back to an older
state. Against the real API and intake publisher (``corpus_rig.PC``)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from corpus_rig import PC, Scripted, bind_library, client, raising, record
from voice_lab_desktop.api import ApiRefused
from voice_lab_desktop.corpus.client import CorpusClient
from voice_lab_desktop.corpus.records import DecisionOp, TrimOp
from voice_lab_desktop.corpus.spool import REMOTE_FILE, CorpusSpool, SpoolError

from cole_voice_lab.corpus_read_model import LibraryTake
from cole_voice_lab.corpus_take_store import read_take_state


def prompt_of(pc: PC, index: int = 0) -> str:
    return pc.catalog[1].suite.prompts[index].prompt_id


def recorded_elsewhere(pc: PC, tmp_path: Path, *, seed: int, index: int = 0) -> str:
    """A take another machine recorded and uploaded (undecided, revision 0). Its id."""
    other_dir = tmp_path / f"elsewhere-{seed}"
    other_dir.mkdir()
    bind_library(other_dir)
    other = client(other_dir, pc, enabled=True)
    other.run_pass()
    take = record(other, prompt_of(pc, index), seed=seed)
    assert other.run_pass().uploaded == 1
    return take.take_id


def pc_take(corpus: CorpusClient, pc: PC, take_id: str, index: int = 0) -> LibraryTake:
    history = corpus.prompt_history(prompt_of(pc, index))
    return next(t for t in history.takes if t.take_id == take_id)


def ops(corpus: CorpusClient, take_id: str) -> list[tuple[str, int]]:
    return [(op.kind, op.expected_revision) for op in corpus.spool.take(take_id).sync.outbox]


def test_a_take_another_client_uploaded_is_decided_and_trimmed_here(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=61)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    remote = corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    # Noted from the PC's history: its state, its revisions; no audio, nothing to upload.
    assert remote.remote and remote.sync.upload == "remote" and remote.sync.outbox == ()
    assert (remote.sync.decision.decision, remote.sync.decision.revision) == ("draft", 0)
    assert remote.frames == 12_000 and remote.prompt_id == prompt_of(pc)
    assert sorted(os.listdir(remote.path)) == [REMOTE_FILE, "state.json"]
    with pytest.raises(SpoolError, match="no audio here"):
        _ = remote.audio
    with pytest.raises(SpoolError):
        _ = remote.package
    assert corpus.status().takes == 0  # not a take of this machine
    assert corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc)) == remote  # once
    # Kept and trimmed here, sent like any take's changes.
    corpus.decide(take_id, "kept")
    corpus.set_trim(take_id, 480, 9_600)
    assert ops(corpus, take_id) == [("decision", 0), ("trim", 0)]
    result = corpus.run_pass()
    assert (result.applied, result.uploaded, result.refused) == (2, 0, 0)
    there = pc_take(corpus, pc, take_id)
    assert (there.decision, there.decision_revision) == ("kept", 1)
    assert (there.trim.start_frame, there.trim.end_frame, there.trim.revision) == (480, 9_600, 1)
    # Rejected with a reason, then restored to what it was before.
    corpus.decide(take_id, "rejected", "a cough")
    assert corpus.run_pass().applied == 1
    state = read_take_state(pc.layout, take_id)
    assert state is not None and state.decision.reason == "a cough"
    corpus.restore(take_id)
    assert corpus.run_pass().applied == 1
    there = pc_take(corpus, pc, take_id)
    assert (there.decision, there.decision_revision) == ("kept", 3)
    sync = corpus.spool.take(take_id).sync
    assert (sync.decision.revision, sync.trim.revision, sync.sent) == (3, 1, 4)
    assert corpus.status().takes == 0 and not (remote.path / "original.wav").exists()


def imported_from_the_mac(
    pc: PC,
    tmp_path: Path,
    *,
    index: int = 0,
    frames: tuple[int, int] | None = None,
    action: str | None = None,
) -> str:
    """A Swift take of the Mac, imported by step 9 (D-053): recorded by the Mac's own take
    store over a tone (a synthetic Mac library, ``corpus_import_fixtures``), kept (trimmed to
    ``frames``) and then ``action``ed (``reject``, with a reason, or ``remove``) when given,
    packaged by ``build_take_import_package`` and published by the PC's intake publisher. The
    PC records it with the Mac's decision and trim and ``origin: imported``. Its PC take id."""
    from corpus_import_fixtures import MacLibrary
    from corpus_take_fixtures import NOW, seed_catalog

    from cole_voice_lab.corpus_import import build_take_import_package
    from cole_voice_lab.paths import AssetLayout

    mac_layout = AssetLayout.initialize(tmp_path / f"mac-{index}-{action}" / "voice-lab" / "lab")
    assert seed_catalog(mac_layout) == pc.catalog  # the same version, every field
    mac = MacLibrary(mac_layout)
    take = mac.save(mac.sealed(mac.v2_group(pc.catalog), prompt_of(pc, index)), frames=frames)
    if action is not None:
        take = mac.decide(take, action, "Mouth click at the start")
    package = tmp_path / f"import-{take.reservation.take_id}"
    imported = build_take_import_package(
        package, layout=mac_layout, take=take, catalog=pc.catalog[0], clock=lambda: NOW
    )
    take_id = imported.manifest.take_id
    shutil.copytree(package, pc.layout.directory("incoming") / take_id)
    pc.publish()
    assert read_take_state(pc.layout, take_id) is not None  # published
    return take_id


def test_an_imported_take_record_is_decided_and_trimmed_on_its_revisions(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = imported_from_the_mac(pc, tmp_path, index=1, frames=(2_400, 9_600))
    corpus = client(data_dir, pc)
    corpus.run_pass()
    there = pc_take(corpus, pc, take_id, 1)
    assert there.origin is not None and there.origin.kind == "imported"
    remote = corpus.remote_take(there, prompt_of(pc, 1))
    assert remote.record.origin == there.origin  # type: ignore[union-attr]
    sync = remote.sync
    assert (sync.decision.decision, sync.decision.revision) == ("kept", 1)
    assert (sync.trim.start_frame, sync.trim.end_frame, sync.trim.revision) == (2_400, 9_600, 1)
    # The whole take again, then rejected: each change expects the importer's revision.
    corpus.set_trim(take_id, None, None)
    corpus.decide(take_id, "rejected", "a stumble")
    assert ops(corpus, take_id) == [("trim", 1), ("decision", 1)]
    assert corpus.run_pass().applied == 2
    there = pc_take(corpus, pc, take_id, 1)
    assert (there.decision, there.decision_revision) == ("rejected", 2)
    assert (there.trim.start_frame, there.trim.end_frame, there.trim.revision) == (None, None, 2)
    # Restore goes back to the importer's kept.
    corpus.restore(take_id)
    assert corpus.run_pass().applied == 1
    assert pc_take(corpus, pc, take_id, 1).decision == "kept"


def test_the_pcs_origin_is_kept_with_the_take(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    """The PC's ``origin`` (D-053), as its contracts carry it: an imported take's names the
    Mac's own take; another client's upload has none."""
    imported = imported_from_the_mac(pc, tmp_path)
    uploaded = recorded_elsewhere(pc, tmp_path, seed=63, index=1)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    there = pc_take(corpus, pc, imported)
    assert there.origin is not None and there.origin.kind == "imported"
    assert there.origin.source_take_id != imported  # the Swift take's own id
    remote = corpus.remote_take(there, prompt_of(pc))
    again = CorpusSpool(data_dir / "corpus").take(imported)
    assert again.record == remote.record and again.record.origin == there.origin  # type: ignore[union-attr]
    other = corpus.remote_take(pc_take(corpus, pc, uploaded, 1), prompt_of(pc, 1))
    assert other.record.origin is None  # type: ignore[union-attr]


def test_a_conflict_adopts_the_pcs_state(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=64)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    # Another client removes it on the PC; here it is kept meanwhile.
    status, answer = pc.send(
        "POST",
        f"/api/v1/corpus/takes/{take_id}/decision",
        {"request_id": "req-" + "a" * 32, "decision": "removed", "expected_revision": 0},
    )
    assert status == 202, answer
    pc.publish()
    corpus.decide(take_id, "kept")
    result = corpus.run_pass()
    assert (result.applied, result.refused) == (0, 1)
    sync = corpus.spool.take(take_id).sync
    assert (sync.decision.decision, sync.decision.revision, sync.outbox) == ("removed", 1, ())
    assert sync.history[-1].source == "pc"
    (notice,) = corpus.notices()
    assert (notice.kind, notice.take_id) == ("changed-elsewhere", take_id)
    # A change after the adoption chains on the PC's revision (as draft: what it was
    # before the removal is not known here, so the operator says).
    corpus.restore(take_id, "draft")
    assert ops(corpus, take_id) == [("decision", 1)]
    assert corpus.run_pass().applied == 1
    assert pc_take(corpus, pc, take_id).decision == "draft"


def test_changes_made_offline_queue_and_send_later(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=65)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    pc.stop()
    corpus.decide(take_id, "kept")
    corpus.set_trim(take_id, 1_200, 10_800)
    corpus.decide(take_id, "rejected", "too fast")  # folded into the untried keep
    queued = corpus.spool.take(take_id).sync.outbox
    assert [type(op) for op in queued] == [DecisionOp, TrimOp]
    assert isinstance(queued[0], DecisionOp) and queued[0].decision == "rejected"
    assert corpus.run_pass().offline and len(corpus.spool.take(take_id).sync.outbox) == 2
    # The app restarts and the PC comes back: the queue goes, in order.
    pc.start()
    later = client(data_dir, pc)
    assert later.spool.take(take_id).remote
    assert later.run_pass().applied == 2
    there = pc_take(later, pc, take_id)
    assert (there.decision, there.decision_revision) == ("rejected", 1)
    assert (there.trim.start_frame, there.trim.end_frame) == (1_200, 10_800)


def test_the_pcs_history_is_read_again_never_backwards(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=66)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    stale = corpus.prompt_history(prompt_of(pc))
    corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    # Another client keeps it and trims it: the next read installs the PC's newer state.
    for body in ({"request_id": "req-" + "b" * 32, "decision": "kept", "expected_revision": 0},):
        assert pc.send("POST", f"/api/v1/corpus/takes/{take_id}/decision", body)[0] == 202
    trim = {"request_id": "req-" + "c" * 32, "start_frame": 0, "end_frame": 6_000}
    assert (
        pc.send("PUT", f"/api/v1/corpus/takes/{take_id}/trim", {**trim, "expected_revision": 0})[0]
        == 202
    )
    pc.publish()
    fresh = corpus.prompt_history(prompt_of(pc))
    assert corpus.observe_history(fresh) == 1
    sync = corpus.spool.take(take_id).sync
    assert (sync.decision.decision, sync.decision.revision) == ("kept", 1)
    assert (sync.trim.start_frame, sync.trim.end_frame, sync.trim.revision) == (0, 6_000, 1)
    assert sync.history[-1].source == "pc"
    # An older read never takes it back; a change queued here is left to its outcome.
    assert corpus.observe_history(stale) == 0
    assert corpus.spool.take(take_id).sync.decision.revision == 1
    corpus.decide(take_id, "removed")
    assert corpus.observe_history(fresh) == 0
    assert ops(corpus, take_id) == [("decision", 1)]


def test_a_remote_take_interrupted_while_noted_is_finished(
    pc: PC, data_dir: Path, tmp_path: Path
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=67)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    remote = corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    # A crash between writing it beside and moving it in.
    staged = data_dir / "corpus" / "recording" / "rem-0123456789abcdef"
    shutil.move(remote.path, staged)
    again = CorpusSpool(data_dir / "corpus")
    assert [take.take_id for take in again.takes()] == [take_id]
    assert again.unsaved == [] and not staged.exists()


def test_restore_asks_when_what_came_before_is_not_known(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=68)
    # Another client rejected it before this machine ever saw it.
    body = {"request_id": "req-" + "d" * 32, "decision": "rejected", "reason": "a cough"}
    assert (
        pc.send(
            "POST", f"/api/v1/corpus/takes/{take_id}/decision", {**body, "expected_revision": 0}
        )[0]
        == 202
    )
    pc.publish()
    corpus = client(data_dir, pc)
    corpus.run_pass()
    corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    assert corpus.restore_target(take_id) is None
    with pytest.raises(SpoolError, match="restore it as kept or as draft"):
        corpus.restore(take_id)  # no guess: the operator says
    with pytest.raises(SpoolError, match="kept or draft"):
        corpus.restore(take_id, "removed")  # type: ignore[arg-type]
    corpus.restore(take_id, "kept")
    assert corpus.run_pass().applied == 1
    assert pc_take(corpus, pc, take_id).decision == "kept"
    # Once known (here, or from an import's record), Restore uses it without asking.
    corpus.decide(take_id, "removed")
    assert corpus.restore_target(take_id) == "kept"


def test_restore_asks_for_an_import_rejected_on_the_mac(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    """A take the Mac rejected before it was imported: the PC's records say rejected, not
    what it was before (the Mac's answer stays in the raw package), so Restore asks."""
    take_id = imported_from_the_mac(pc, tmp_path, action="reject")
    corpus = client(data_dir, pc)
    corpus.run_pass()
    there = pc_take(corpus, pc, take_id)
    assert (there.decision, there.origin is not None) == ("rejected", True)
    corpus.remote_take(there, prompt_of(pc))
    assert corpus.restore_target(take_id) is None
    with pytest.raises(SpoolError, match="not known here"):
        corpus.restore(take_id)
    corpus.restore(take_id, "kept")
    assert ops(corpus, take_id) == [("decision", there.decision_revision)]
    assert corpus.run_pass().applied == 1
    assert pc_take(corpus, pc, take_id).decision == "kept"


def test_a_change_is_never_folded_into_one_being_sent(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's interleaving, made deterministic: the engine reads the take, and before
    it sends, the operator restores. The removal must go as it was queued, and the restore
    after it, so the PC and this machine end the same."""
    take_id = recorded_elsewhere(pc, tmp_path, seed=70)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    corpus.decide(take_id, "removed")
    spool = corpus.spool
    real_take = CorpusSpool.take
    armed = {"on": False}

    def take(self: CorpusSpool, wanted: str) -> Any:
        found = real_take(self, wanted)
        if armed["on"] and wanted == take_id:
            armed["on"] = False
            corpus.restore(take_id)  # the operator, between the engine's read and its send
        return found

    monkeypatch.setattr(CorpusSpool, "take", take)
    armed["on"] = True
    corpus.run_pass()
    corpus.run_pass()
    monkeypatch.setattr(CorpusSpool, "take", real_take)
    here = spool.take(take_id).sync
    there = pc_take(corpus, pc, take_id)
    assert here.outbox == ()
    assert (here.decision.decision, here.decision.revision) == (
        there.decision,
        there.decision_revision,
    )
    assert there.decision == "draft"  # removed, then restored: both reached the PC, in order


def test_a_half_written_note_is_removed_not_counted(data_dir: Path) -> None:
    recording = data_dir / "corpus" / "recording"
    recording.mkdir(parents=True)
    (recording / "rem-0000000000000000").mkdir()
    partial = recording / "rem-1111111111111111"
    partial.mkdir()
    (partial / REMOTE_FILE).write_text("{}")
    spool = CorpusSpool(data_dir / "corpus")
    assert spool.takes() == [] and spool.unsaved == []
    assert sorted(os.listdir(recording)) == []


def test_a_remote_take_the_pc_no_longer_has_is_gone(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=71)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    corpus.remote_take(pc_take(corpus, pc, take_id), prompt_of(pc))
    # The PC answers that it has no such take (404 take_not_found).
    real = corpus.engine.api
    lost = ApiRefused("the PC has no such take (take_not_found)", status=404, code="take_not_found")
    corpus.engine.api = lambda: Scripted(real(), submit_decision=raising(lost))
    corpus.decide(take_id, "kept")
    assert corpus.run_pass().refused == 1
    corpus.engine.api = real
    sync = corpus.spool.take(take_id).sync
    assert (sync.upload, sync.outbox) == ("gone", ())
    corpus.decide(take_id, "rejected", "late")  # still recorded here, never sent again
    assert corpus.spool.take(take_id).sync.outbox == ()
    again = CorpusSpool(data_dir / "corpus").take(take_id)
    assert again.remote and again.sync.upload == "gone"


def test_recovery_at_start_never_holds_the_spool(
    pc: PC, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from corpus_rig import capture

    corpus = client(data_dir, pc)
    corpus.run_pass()
    pending = corpus.begin_take(prompt_of(pc))
    capture(pending.directory, seed=72)  # finished by the worker, never saved: a crash
    started, release = threading.Event(), threading.Event()
    real = CorpusSpool._save_left_over

    def slow(self: CorpusSpool, path: Path) -> bool:
        started.set()
        release.wait(10)
        return real(self, path)

    monkeypatch.setattr(CorpusSpool, "_save_left_over", slow)
    later = client(data_dir, pc)
    worker = threading.Thread(target=later.prepare)
    worker.start()
    try:
        assert started.wait(10)
        # Meanwhile the GUI thread reads without waiting on the recovery (it copies and
        # hashes a WAV): the status says it is not ready, the takes listed so far are none.
        begun = time.monotonic()
        status = later.status(wait=False)
        notices = later.notices()
        assert time.monotonic() - begun < 1.0
        assert not status.ready and not later.ready and later.spool.peek() == []
        assert notices == ()
    finally:
        release.set()
        worker.join(10)
    assert later.ready and later.status(wait=False).ready
    (saved,) = later.spool.peek()
    assert saved.prompt_id == prompt_of(pc) and later.library.loaded
