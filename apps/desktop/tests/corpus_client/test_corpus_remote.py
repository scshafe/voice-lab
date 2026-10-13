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
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corpus_rig import PC, bind_library, client, record
from voice_lab_desktop.corpus.client import CorpusClient, remote_origin
from voice_lab_desktop.corpus.records import DecisionOp, RemoteOrigin, TrimOp
from voice_lab_desktop.corpus.spool import REMOTE_FILE, CorpusSpool, SpoolError

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.corpus_read_model import LibraryTake
from cole_voice_lab.corpus_take_store import TakeDecision, TakeTrim, read_take_state, state_path


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


def imported_style(pc: PC, take_id: str) -> None:
    """The record step 9's importer writes for a Swift take (D-053): the Mac's decision and
    trim at revision 1, no request ever made for them; the read model rebuilt."""
    state = read_take_state(pc.layout, take_id)
    assert state is not None
    moment = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    imported = state.model_copy(
        update={
            "decision": TakeDecision(decision="kept", revision=1, reason=None, decided_at=moment),
            "trim": TakeTrim(revision=1, start_frame=2_400, end_frame=9_600),
        }
    )
    path = state_path(pc.layout, take_id)
    mode = os.stat(path).st_mode & 0o777
    os.chmod(path, 0o600)
    write_canonical_json(path, imported, mode=0o600)
    os.chmod(path, mode)
    pc.publish()


def test_an_imported_take_record_is_decided_and_trimmed_on_its_revisions(
    pc: PC, data_dir: Path, tmp_path: Path, library: Any
) -> None:
    take_id = recorded_elsewhere(pc, tmp_path, seed=62, index=1)
    imported_style(pc, take_id)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    remote = corpus.remote_take(pc_take(corpus, pc, take_id, 1), prompt_of(pc, 1))
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
    # The PC's contracts carry ``origin`` from step 9 (D-053); read when present.
    imported = SimpleNamespace(origin=SimpleNamespace(kind="imported", source_take_id="T-42"))
    assert remote_origin(imported) == RemoteOrigin(kind="imported", source_take_id="T-42")  # type: ignore[arg-type]
    assert remote_origin(SimpleNamespace(origin=None)) is None  # type: ignore[arg-type]
    assert remote_origin(SimpleNamespace()) is None  # type: ignore[arg-type]
    take_id = recorded_elsewhere(pc, tmp_path, seed=63)
    corpus = client(data_dir, pc)
    corpus.run_pass()
    origin = RemoteOrigin(kind="imported", source_take_id="T-42")
    remote = corpus.spool.learn_remote(pc_take(corpus, pc, take_id), prompt_of(pc), origin=origin)
    again = CorpusSpool(data_dir / "corpus").take(take_id)
    assert again.record == remote.record and again.record.origin == origin  # type: ignore[union-attr]


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
    # A change after the adoption chains on the PC's revision.
    corpus.restore(take_id)
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
