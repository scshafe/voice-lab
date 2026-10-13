"""The corpus client's sync (D-052, RECORDING-V1.md step 5: "an offline session syncs when
back online"): takes, decisions and trims made offline are durable here, survive a restart,
and reach the PC in order once it answers; the PC is canonical once it has them.

The PC is the backend's real ``/api/v1`` (intake staging and corpus routes) over a synthetic
lab with a seeded catalog; its intake publisher runs in-process between the client's polls.
The takes are generated tones with the worker's evidence, never a person's voice."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from corpus_rig import PC, bind_library, capture, client, record
from voice_lab_desktop.api import ApiUnavailable
from voice_lab_desktop.corpus.client import PendingTake
from voice_lab_desktop.corpus.records import DecisionOp, LocalTrim, TrimOp, UploadOp
from voice_lab_desktop.corpus.spool import SpooledTake, SpoolError

from cole_voice_lab.corpus_read_model import LibraryTake
from cole_voice_lab.corpus_requests import outcomes_directory, queued_requests, read_outcome
from cole_voice_lab.corpus_take_store import TakeTrim, read_take_state
from cole_voice_lab.corpus_takes import validate_take_package
from cole_voice_lab.home_host import HomeHostLedger


def pc_takes(pc: PC) -> dict[str, LibraryTake]:
    """Every take in the PC's library, by id."""
    found: dict[str, LibraryTake] = {}
    for row in pc.library().rows:
        if row.take_count:
            status, document = pc.send("GET", f"/api/v1/corpus/prompts/{row.prompt_id}/takes")
            assert status == 200, document
            for take in document["takes"]:
                parsed = LibraryTake.model_validate_json(json.dumps(take))
                found[parsed.take_id] = parsed
    return found


def assert_pc_shows(pc: PC, take: SpooledTake) -> None:
    """The PC's library shows exactly the state here, with its revisions."""
    sync = take.sync
    there = pc_takes(pc)[take.take_id]
    assert there.decision == sync.decision.decision
    assert there.decision_revision == sync.decision.revision
    assert there.trim == TakeTrim(
        revision=sync.trim.revision,
        start_frame=sync.trim.start_frame,
        end_frame=sync.trim.end_frame,
    )


def private(root: Path) -> None:
    """Every directory under ``root`` is 0700 and every file 0600 (the take package's own
    files are write-protected by its builder: 0400 at most)."""
    for directory, _dirs, files in os.walk(root):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700, directory
        for name in files:
            mode = stat.S_IMODE(os.stat(Path(directory) / name).st_mode)
            assert mode & 0o077 == 0, (directory, name, oct(mode))


def test_an_offline_session_syncs_when_back_online(tmp_path: Path, data_dir: Path) -> None:
    pc = PC.create(tmp_path / "pc").start()
    library = bind_library(data_dir)
    prompts = [prompt.prompt_id for prompt in pc.catalog[1].suite.prompts[:3]]
    try:
        # The app's first run reaches the PC once: it keeps the catalog.
        first = client(data_dir, pc)
        assert not first.status().can_record
        assert first.run_pass().reached
        assert first.status().pinned_catalog == pc.catalog[0]
        pc.stop()

        # The next run is offline: the session pins the cached catalog and records.
        offline = client(data_dir, pc)
        status = offline.status()
        assert status.can_record and status.pinned_catalog == pc.catalog[0]
        kept = record(offline, prompts[0], seed=1)
        offline.decide(kept.take_id, "kept")
        rejected = record(offline, prompts[1], seed=2)
        offline.decide(rejected.take_id, "rejected", "a chair creaked")
        trimmed = record(offline, prompts[2], seed=3, seconds=0.5)
        offline.set_trim(trimmed.take_id, 2_400, 19_200)
        offline.decide(trimmed.take_id, "kept")
        result = offline.run_pass()
        assert result.offline and not result.reached
        status = offline.status()
        assert (status.offline, status.queued, status.waiting_takes, status.sent) == (True, 7, 3, 0)
        assert status.last_sync_at is not None and status.last_error.startswith(
            "the server did not answer"
        )
        # Durable here at once: the takes, their packages built once, their outboxes.
        private(data_dir / "corpus")
        outbox = offline.spool.take(trimmed.take_id).sync.outbox
        assert [op.kind for op in outbox] == ["upload", "trim", "decision"]
        assert isinstance(outbox[0], UploadOp) and outbox[0].attempts == 0  # never tried
        packages = {
            take.take_id: validate_take_package(take.package).manifest_sha256
            for take in (kept, rejected, trimmed)
        }
        assert all(take.record.package_manifest_sha256 == packages[take.take_id]
                   for take in (kept, rejected, trimmed))  # fmt: skip

        # The app restarts (new objects over the same data dir); the PC is back.
        pc.start()
        again = client(data_dir, pc)
        assert again.status().queued == 7
        assert [take.take_id for take in again.takes()] == [
            kept.take_id,
            rejected.take_id,
            trimmed.take_id,
        ]
        result = again.run_pass()
        assert (result.reached, result.offline, result.error) == (True, False, "")
        assert (result.uploaded, result.applied, result.refused, result.waiting) == (3, 4, 0, 0)
        status = again.status()
        assert (status.queued, status.sent, status.refused, status.dropped) == (0, 7, 0, 0)

        # The PC holds exactly the state here, with the revisions it confirmed.
        assert len(pc.raw_packages()) == 3
        final = {take.take_id: again.spool.take(take.take_id) for take in (kept, rejected, trimmed)}
        for take in final.values():
            assert_pc_shows(pc, take)
            assert take.sync.upload == "accepted" and take.sync.receipt is not None
            assert take.sync.receipt.manifest_sha256 == packages[take.take_id]
        assert final[kept.take_id].sync.decision.revision == 1
        assert final[trimmed.take_id].sync.trim == LocalTrim(
            start_frame=2_400, end_frame=19_200, revision=1
        )
        on_pc = read_take_state(pc.layout, rejected.take_id)
        assert on_pc is not None and on_pc.decision.reason == "a chair creaked"
        coverage = pc.library().coverage
        assert (coverage.kept, coverage.takes) == (2, 3)
        # Each upload under the standing approval, in this machine's ledger (D-021).
        events = [entry.event for entry, _ in HomeHostLedger(library).entries()]
        assert events == ["upload-started", "upload-completed"] * 3
        # A pass with nothing queued sends nothing more.
        assert again.run_pass().uploaded == 0 and len(pc.raw_packages()) == 3
    finally:
        pc.stop()


def test_a_change_made_elsewhere_is_adopted_and_the_queued_ones_dropped(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()
    take = record(corpus, pc.catalog[1].suite.prompts[0].prompt_id, seed=4)
    assert corpus.run_pass().uploaded == 1
    # Another client removes the take on the PC.
    status, answer = pc.send(
        "POST",
        f"/api/v1/corpus/takes/{take.take_id}/decision",
        {"request_id": "req-" + "e" * 32, "decision": "removed", "expected_revision": 0},
    )
    assert status == 202, answer
    pc.publish()
    # Meanwhile, here: keep it, trim it, then reject it (folded into the unsent keep).
    corpus.decide(take.take_id, "kept")
    corpus.set_trim(take.take_id, 480, 9_600)
    corpus.decide(take.take_id, "rejected", "too fast")
    queued = corpus.spool.take(take.take_id).sync.outbox
    assert [(op.kind, op.expected_revision) for op in queued if not isinstance(op, UploadOp)] == [
        ("decision", 0),
        ("trim", 0),
    ]
    result = corpus.run_pass()
    assert (result.applied, result.refused, result.waiting) == (0, 1, 0)
    sync = corpus.spool.take(take.take_id).sync
    # The PC's state is adopted; what was queued behind the refusal is dropped, never sent.
    assert sync.outbox == ()
    assert (sync.decision.decision, sync.decision.revision, sync.decision.reason) == (
        "removed",
        1,
        None,
    )
    assert sync.trim == LocalTrim(revision=0)
    assert (sync.sent, sync.refused, sync.dropped, sync.refresh) == (1, 1, 1, None)
    assert [(entry.result, entry.code) for entry in sync.journal[-2:]] == [
        ("refused", "revision_conflict"),
        ("dropped", None),
    ]
    assert sync.history[-1].source == "pc" and sync.history[-1].decision == "removed"
    (notice,) = corpus.status().notices
    assert (notice.kind, notice.take_id) == ("changed-elsewhere", take.take_id)
    assert notice.text == (
        "Changed elsewhere: this take now shows the PC's state; 1 queued change(s) were dropped."
    )
    corpus.dismiss_notice(notice.notice_id)
    assert corpus.status().notices == ()
    # On the PC: the other client's change, and the refused one; nothing else arrived.
    outcomes = sorted(path.name for path in outcomes_directory(pc.layout).iterdir())
    assert len(outcomes) == 2 and queued_requests(pc.layout) == ()
    assert_pc_shows(pc, corpus.spool.take(take.take_id))
    # A change made after the adoption chains on the PC's revision and applies.
    # What it was before the other client's removal is not known here: Restore asks.
    with pytest.raises(SpoolError, match="not known here"):
        corpus.restore(take.take_id)
    corpus.restore(take.take_id, "draft")
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    assert isinstance(op, DecisionOp) and (op.decision, op.expected_revision) == ("draft", 1)
    assert corpus.run_pass().applied == 1
    assert_pc_shows(pc, corpus.spool.take(take.take_id))


class LosesAnswers:
    """The app's API client, whose answers to submitted changes are lost on the way back
    (the PC got the request; the client saw a network failure)."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.submitted: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.api, name)

    def submit_decision(self, take_id: str, request: Any) -> Any:
        self.api.submit_decision(take_id, request)
        self.submitted.append(request.request_id)
        raise ApiUnavailable("the server did not answer (ReadTimeout)")

    submit_trim = submit_decision


def test_retries_reuse_request_ids_and_never_apply_a_change_twice(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()  # the catalog
    take = record(corpus, pc.catalog[1].suite.prompts[1].prompt_id, seed=5)
    assert corpus.run_pass().uploaded == 1
    corpus.decide(take.take_id, "kept")
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    real = corpus.engine.api
    losing = LosesAnswers(real())
    corpus.engine.api = lambda: losing
    result = corpus.run_pass()
    assert result.offline and losing.submitted == [op.request_id]
    (tried,) = corpus.spool.take(take.take_id).sync.outbox
    assert (tried.request_id, tried.attempts, tried.submitted_at) == (op.request_id, 1, None)
    pc.publish()  # the PC applies it; the client does not know
    assert read_outcome(pc.layout, op.request_id) is not None

    # After a restart, the same request goes again, under the same id: the PC answers as
    # before, and the change is applied once.
    again = client(data_dir, pc)
    assert again.run_pass().applied == 1
    sync = again.spool.take(take.take_id).sync
    assert sync.outbox == () and sync.decision.revision == 1
    state = read_take_state(pc.layout, take.take_id)
    assert state is not None and (state.decision.decision, state.decision.revision) == ("kept", 1)

    # A change the PC's writer has not decided yet stays submitted, and is only polled.
    idle = client(data_dir, pc, publish_while_waiting=False, request_wait=0.0)
    idle.set_trim(take.take_id, 0, 4_800)
    assert idle.run_pass().waiting == 1
    (trim,) = idle.spool.take(take.take_id).sync.outbox
    assert isinstance(trim, TrimOp) and trim.submitted_at is not None
    assert [entry.request_id for entry in queued_requests(pc.layout)] == [trim.request_id]
    pc.publish()
    assert idle.run_pass().applied == 1
    assert len(list(outcomes_directory(pc.layout).iterdir())) == 2
    assert_pc_shows(pc, idle.spool.take(take.take_id))


def test_a_deferred_take_keeps_its_operations_queued_until_it_is_accepted(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc, upload_wait=0.0)
    corpus.run_pass()  # the catalog
    take = record(corpus, pc.catalog[1].suite.prompts[2].prompt_id, seed=6)
    corpus.decide(take.take_id, "kept")
    corpus.set_trim(take.take_id, 960, 6_000)
    # The catalog version the take names is not on the PC (its store is not there yet).
    store = pc.layout.root / "provenance" / "corpus" / "corpus.sqlite3"
    aside = pc.work / "corpus.sqlite3.aside"
    os.rename(store, aside)
    result = corpus.run_pass()
    assert (result.uploaded, result.waiting) == (0, 1)
    pc.publish()  # the PC defers it
    assert (pc.layout.directory("incoming") / take.take_id).is_dir()
    for _ in range(2):
        result = corpus.run_pass(force=True)
        assert (result.uploaded, result.applied, result.waiting) == (0, 0, 1)
        sync = corpus.spool.take(take.take_id).sync
        assert [op.kind for op in sync.outbox] == ["upload", "decision", "trim"]
        assert isinstance(sync.outbox[0], UploadOp) and sync.outbox[0].awaiting_receipt
        assert "not yet published" in (sync.outbox[0].last_error or "")
        assert corpus.status().awaiting_receipt == 1
        assert queued_requests(pc.layout) == ()  # its changes never left this machine
        pc.publish()
    # The version arrives on the PC: the take is accepted, then its changes apply in order.
    os.rename(aside, store)
    pc.publish()
    # Its backoff holds it until it is due (or forced).
    assert corpus.run_pass().uploaded == 0
    result = corpus.run_pass(force=True)
    assert (result.uploaded, result.applied, result.waiting) == (1, 2, 0)
    assert_pc_shows(pc, corpus.spool.take(take.take_id))


def test_a_take_the_pc_refuses_is_never_sent_again(pc: PC, data_dir: Path, library: Any) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()
    pinned = corpus.pinned
    assert pinned is not None
    # A prompt this catalog version does not count (the PC refuses the take).
    stranger = pinned.snapshot.suite.prompts[0].model_copy(
        update={"prompt_id": "not-in-this-catalog"}
    )
    pending = PendingTake(corpus.spool.new_recording_dir(), stranger, pinned.reference)
    take = corpus.save_take(pending, capture(pending.directory, seed=7))
    corpus.decide(take.take_id, "kept")
    result = corpus.run_pass()
    assert (result.uploaded, result.refused) == (0, 1)
    sync = corpus.spool.take(take.take_id).sync
    assert (sync.upload, sync.outbox, sync.refused, sync.dropped) == ("refused", (), 1, 1)
    (notice,) = corpus.status().notices
    assert notice.kind == "take-refused" and "take_prompt_not_in_catalog" in notice.text
    # Changes to it stay here.
    corpus.decide(take.take_id, "removed")
    assert corpus.spool.take(take.take_id).sync.outbox == ()
    assert corpus.run_pass().refused == 0


def test_nothing_is_sent_while_corpus_sync_is_off_and_simulated_takes_never_are(
    pc: PC, data_dir: Path, library: Any
) -> None:
    switch = {"on": False}
    corpus = client(data_dir, pc, enabled=lambda: switch["on"])
    corpus.run_pass()  # the catalog is read even so: nothing of the corpus is sent
    real = record(corpus, pc.catalog[1].suite.prompts[0].prompt_id, seed=8)
    corpus.decide(real.take_id, "kept")
    simulated = record(corpus, pc.catalog[1].suite.prompts[1].prompt_id, seed=9, simulated=True)
    corpus.decide(simulated.take_id, "kept")
    corpus.set_trim(simulated.take_id, 0, 4_800)
    sim = corpus.spool.take(simulated.take_id)
    assert (sim.sync.upload, sim.sync.outbox) == ("local-only", ())
    assert sim.record.simulated and sim.record.local_only and not sim.package.exists()
    status = corpus.status()
    assert not status.enabled and "until spike S-1 passes" in status.held
    assert (status.queued, status.local_only) == (2, 1)
    result = corpus.run_pass()
    assert result.reached and result.gated and result.uploaded == 0
    incoming = [p.name for p in pc.layout.directory("incoming").iterdir() if p.name[0] != "."]
    assert pc.raw_packages() == [] and incoming == [] and queued_requests(pc.layout) == ()
    uploads = pc.layout.directory("incoming") / ".uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []
    assert list(HomeHostLedger(library).entries()) == []
    # Turned on: the real take goes; the simulated one never does.
    switch["on"] = True
    result = corpus.run_pass()
    assert (result.uploaded, result.applied, result.waiting) == (1, 1, 0)
    assert len(pc.raw_packages()) == 1 and simulated.take_id not in pc_takes(pc)
    assert corpus.status().local_only == 1
