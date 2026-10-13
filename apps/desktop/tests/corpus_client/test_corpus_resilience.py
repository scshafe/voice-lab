"""Only the PC's explicit, take-level verdicts make the corpus client give up or adopt
(D-052, step 5, after review). Everything else (no answer, the door or a proxy, the sign-in,
a busy session, a server error, version skew, the tailnet check) is transient: the queue is
kept, the try is counted and backed off, and the status says so. Each test here failed
before its fix."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from corpus_rig import (
    PC,
    Scripted,
    capture,
    client,
    raising,
    record,
    uploaded_take,
)
from test_corpus_sync import assert_pc_shows
from voice_lab_desktop.api import ApiRefused, ApiUnavailable
from voice_lab_desktop.auth import AuthError
from voice_lab_desktop.corpus import spool as spool_module
from voice_lab_desktop.corpus import sync as sync_module
from voice_lab_desktop.corpus.records import DecisionOp, UploadOp
from voice_lab_desktop.corpus.spool import STATE_FILE, CorpusSpool

from cole_voice_lab.api.corpus_contracts import CorpusRequestStatus
from cole_voice_lab.corpus_requests import (
    CorpusRequestOutcome,
    DecisionChange,
    queued_requests,
)
from cole_voice_lab.intake_client import UploadRefused
from cole_voice_lab.models import utc_now
from cole_voice_lab.storage_ports import storage_error


def prompt(pc: PC, index: int) -> str:
    return pc.catalog[1].suite.prompts[index].prompt_id


def kinds(corpus: Any, take_id: str) -> list[str]:
    return [op.kind for op in corpus.spool.take(take_id).sync.outbox]


# -- blocker 1: only the intake's verdicts on the package stop a take ---------------------------


def test_only_the_intakes_verdicts_on_the_package_stop_a_take(
    pc: PC, data_dir: Path, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()
    take = record(corpus, prompt(pc, 0), seed=60)
    corpus.decide(take.take_id, "kept")
    # A busy session (a stalled PUT holds its lock), the door's bodiless 404, a proxy's 405:
    # transient. The take and its decision stay queued.
    for code in ("session_busy", "status-404", "method_not_allowed", "too_many_open_sessions"):
        monkeypatch.setattr(
            sync_module.TakeUploader, "upload", raising(UploadRefused(code, "not now"))
        )
        result = corpus.run_pass(force=True)
        assert (result.refused, result.uploaded, result.waiting) == (0, 0, 1), code
        assert code in result.error and not result.sign_in_required
        sync = corpus.spool.take(take.take_id).sync
        assert (sync.upload, kinds(corpus, take.take_id)) == ("queued", ["upload", "decision"])
    # The door's or the API's refusal of the sign-in, with a body or without: sign in.
    for code in ("status-401", "status-403", "unauthorized", "forbidden"):
        monkeypatch.setattr(
            sync_module.TakeUploader, "upload", raising(UploadRefused(code, "sign in"))
        )
        result = corpus.run_pass(force=True)
        assert result.sign_in_required and result.refused == 0, code
        assert kinds(corpus, take.take_id) == ["upload", "decision"]
    assert corpus.notices() == ()
    monkeypatch.undo()
    result = corpus.run_pass(force=True)
    assert (result.uploaded, result.applied, result.refused) == (1, 1, 0)
    # Another package under the take's id is a verdict on this one: it stops for good.
    other = record(corpus, prompt(pc, 1), seed=61)
    monkeypatch.setattr(
        sync_module.TakeUploader,
        "upload",
        raising(UploadRefused("session_conflict", "this session id was opened with other files")),
    )
    assert corpus.run_pass().refused == 1
    assert corpus.spool.take(other.take_id).sync.upload == "refused"
    (notice,) = corpus.notices()
    assert notice.kind == "take-refused" and "session_conflict" in notice.text


# -- blocker 2: only the PC's verdicts on a change drop it ----------------------------------------


def test_only_the_pcs_verdicts_on_a_change_drop_it(pc: PC, data_dir: Path, library: Any) -> None:
    corpus = client(data_dir, pc)
    take = uploaded_take(corpus, pc, seed=62)
    corpus.decide(take.take_id, "kept")
    corpus.set_trim(take.take_id, 480, 4_800)
    real = corpus.engine.api
    # The door, a missing route, a proxy: transient, never adopted.
    for error in (
        ApiRefused("the door refused (http_403)", status=403, code="http_403"),
        ApiRefused("no such route (not_found)", status=404, code="not_found"),
        ApiRefused("this route answers GET (method_not_allowed)", status=405,
                   code="method_not_allowed"),
    ):  # fmt: skip
        corpus.engine.api = lambda e=error: Scripted(real(), submit_decision=raising(e))
        result = corpus.run_pass(force=True)
        assert (result.refused, result.applied, result.waiting) == (0, 0, 1)
        assert str(error) in result.error
        assert kinds(corpus, take.take_id) == ["decision", "trim"]
    # Version skew: the API cannot read the body. Kept, and noticed once.
    skew = ApiRefused(
        "the body is not a valid TakeDecisionRequest (invalid_request)",
        status=422,
        code="invalid_request",
    )
    corpus.engine.api = lambda: Scripted(real(), submit_decision=raising(skew))
    for _ in range(2):
        assert corpus.run_pass(force=True).refused == 0
    (notice,) = corpus.notices()
    assert notice.kind == "change-blocked" and "disagree on its format" in notice.text
    assert kinds(corpus, take.take_id) == ["decision", "trim"]

    # The catalog refused (the door): nothing at all is sent in that pass.
    def never(*_args: Any) -> Any:
        raise AssertionError("nothing is sent after the catalog was refused")

    corpus.engine.api = lambda: Scripted(
        real(),
        catalog=raising(ApiRefused("the door refused (http_403)", status=403, code="http_403")),
        submit_decision=never,
        submit_trim=never,
    )
    result = corpus.run_pass(force=True)
    assert result.reached and result.error.startswith("the PC's catalog: ")
    assert result.waiting == 1 and kinds(corpus, take.take_id) == ["decision", "trim"]
    corpus.engine.api = real
    assert corpus.run_pass(force=True).applied == 2
    assert_pc_shows(pc, corpus.spool.take(take.take_id))

    # The PC's own verdict on a change (its id used for another change): adopted.
    corpus.decide(take.take_id, "removed")
    conflict = ApiRefused(
        "used for another change (request_conflict)", status=409, code="request_conflict"
    )
    corpus.engine.api = lambda: Scripted(real(), submit_decision=raising(conflict))
    assert corpus.run_pass().refused == 1
    sync = corpus.spool.take(take.take_id).sync
    assert sync.outbox == () and sync.refresh is None and sync.decision.decision == "kept"


def test_a_request_the_pc_set_aside_goes_again_under_a_new_id(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    take = uploaded_take(corpus, pc, seed=63)
    corpus.decide(take.take_id, "kept")
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    real = corpus.engine.api

    def set_aside(take_id: str, request: Any) -> CorpusRequestStatus:
        now = utc_now()
        outcome = CorpusRequestOutcome(
            request_id=request.request_id,
            take_id=take_id,
            change=DecisionChange(decision="kept", reason=None, expected_revision=0),
            received_at=now,
            status="refused",
            code="invalid_request",
            detail="the request file could not be read",
            revision=None,
            decided_at=now,
        )
        return CorpusRequestStatus(
            request_id=request.request_id,
            take_id=take_id,
            status="refused",
            received_at=now,
            outcome=outcome,
        )

    corpus.engine.api = lambda: Scripted(real(), submit_decision=set_aside)
    result = corpus.run_pass()
    assert (result.refused, result.waiting) == (0, 1)
    (again,) = corpus.spool.take(take.take_id).sync.outbox
    assert isinstance(again, DecisionOp) and again.request_id != op.request_id
    assert (again.expected_revision, again.attempts, again.submitted_at) == (0, 1, None)
    corpus.engine.api = real
    assert corpus.run_pass(force=True).applied == 1


# -- should-fix 3 and 4: the PC's state is adopted only when it is known to be current ----------


def removed_elsewhere(pc: PC, take_id: str) -> None:
    status, answer = pc.send(
        "POST",
        f"/api/v1/corpus/takes/{take_id}/decision",
        {"request_id": "req-" + "d" * 32, "decision": "removed", "expected_revision": 0},
    )
    assert status == 202, answer
    pc.publish()


def test_a_stale_library_is_never_installed(pc: PC, data_dir: Path, library: Any) -> None:
    corpus = client(data_dir, pc)
    take = uploaded_take(corpus, pc, seed=64)
    real = corpus.engine.api
    stale = real().prompt_takes(take.prompt_id)  # before the change made elsewhere
    removed_elsewhere(pc, take.take_id)
    corpus.decide(take.take_id, "kept")  # expects revision 0; the PC is at 1
    corpus.engine.api = lambda: Scripted(real(), prompt_takes=lambda _prompt_id: stale)
    result = corpus.run_pass()
    assert (result.refused, result.waiting) == (1, 1)
    sync = corpus.spool.take(take.take_id).sync
    # Nothing older than the refusal's revision is installed: the state here waits.
    assert sync.refresh is not None and sync.refresh.decision_at_least == 1
    assert (sync.decision.decision, sync.decision.revision, sync.outbox) == ("kept", 0, ())
    assert corpus.status().refreshing == 1
    (notice,) = corpus.notices()
    assert notice.kind == "changed-elsewhere" and "read again" in notice.text
    # Keeping it again is queued even though it equals the state here: that state is stale.
    corpus.decide(take.take_id, "kept")
    assert kinds(corpus, take.take_id) == ["decision"]
    assert corpus.run_pass().waiting == 1  # still stale: held, never sent
    assert queued_requests(pc.layout) == ()
    corpus.engine.api = real
    result = corpus.run_pass()
    assert (result.applied, result.waiting) == (1, 0)
    sync = corpus.spool.take(take.take_id).sync
    assert sync.refresh is None and (sync.decision.decision, sync.decision.revision) == ("kept", 2)
    assert_pc_shows(pc, corpus.spool.take(take.take_id))


def test_without_the_pcs_state_nothing_here_changes_and_the_take_waits(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    take = uploaded_take(corpus, pc, seed=65)
    removed_elsewhere(pc, take.take_id)
    corpus.decide(take.take_id, "kept")
    corpus.set_trim(take.take_id, 960, 6_000)
    real = corpus.engine.api
    unreadable = ApiRefused(
        "no such prompt (prompt_not_found)", status=404, code="prompt_not_found"
    )
    corpus.engine.api = lambda: Scripted(real(), prompt_takes=raising(unreadable))
    result = corpus.run_pass()
    assert (result.refused, result.waiting) == (1, 1)
    sync = corpus.spool.take(take.take_id).sync
    assert (sync.decision.decision, sync.decision.revision) == ("kept", 0)
    assert (sync.trim.start_frame, sync.trim.end_frame, sync.trim.revision) == (960, 6_000, 0)
    assert sync.refresh is not None and (sync.outbox, sync.dropped) == ((), 1)
    # Neither change is a no-op while the PC's state is unknown.
    corpus.decide(take.take_id, "kept")
    corpus.set_trim(take.take_id, 960, 6_000)
    assert kinds(corpus, take.take_id) == ["decision", "trim"]
    assert corpus.run_pass().waiting == 1 and queued_requests(pc.layout) == ()
    corpus.engine.api = real
    result = corpus.run_pass()
    assert (result.applied, result.waiting) == (2, 0)
    sync = corpus.spool.take(take.take_id).sync
    assert (sync.decision.revision, sync.trim.revision, sync.refresh) == (2, 1, None)
    assert_pc_shows(pc, corpus.spool.take(take.take_id))


# -- should-fix 5: failures outside the API never escape a pass -----------------------------------


def test_failures_outside_the_api_are_tried_again_and_the_pass_goes_on(
    pc: PC, data_dir: Path, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = client(data_dir, pc)
    first = uploaded_take(corpus, pc, seed=66)
    second = uploaded_take(corpus, pc, seed=67)
    corpus.decide(first.take_id, "kept")
    corpus.decide(second.take_id, "kept")
    real = corpus.engine.api

    def flaky(take_id: str, request: Any) -> Any:
        if take_id == first.take_id:
            # Session.bearer() refreshing the token at Pocket ID, outside _request's try.
            raise httpx.ConnectError("Pocket ID did not answer")
        return real().submit_decision(take_id, request)

    corpus.engine.api = lambda: Scripted(real(), submit_decision=flaky)
    result = corpus.run_pass()
    assert result.offline and (result.applied, result.waiting) == (1, 1)
    (op,) = corpus.spool.take(first.take_id).sync.outbox
    assert op.attempts == 1 and op.not_before is not None and op.last_error is not None
    status = corpus.status()
    assert status.offline and first.take_id in status.last_error
    # The sign-in's provider failing (not a lost sign-in), and the tailnet check: the same.
    discovery = AuthError("the issuer's discovery document answered HTTP 502")
    corpus.engine.api = lambda: Scripted(real(), submit_decision=raising(discovery))
    result = corpus.run_pass(force=True)
    assert result.offline and not result.sign_in_required
    assert corpus.spool.take(first.take_id).sync.outbox[0].attempts == 2
    third = record(corpus, prompt(pc, 3), seed=68)
    tailnet = storage_error("STORAGE.UNAVAILABLE", "tailscale status did not answer")
    monkeypatch.setattr(sync_module.TakeUploader, "upload", raising(tailnet))
    corpus.engine.api = real
    result = corpus.run_pass(force=True)
    assert result.offline and result.applied == 1  # the first take's decision went
    upload = corpus.spool.take(third.take_id).sync.outbox[0]
    assert isinstance(upload, UploadOp) and upload.attempts == 1
    # Before any operation: the catalog's request could not be made at all.
    corpus.engine.api = lambda: Scripted(
        real(), catalog=raising(httpx.ConnectError("Pocket ID did not answer"))
    )
    result = corpus.run_pass()
    assert result.offline and not result.reached and "Pocket ID" in result.error


# -- nits -----------------------------------------------------------------------------------


def test_a_request_the_pc_lost_goes_again_even_after_network_failures(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    take = uploaded_take(corpus, pc, seed=69)
    corpus.decide(take.take_id, "kept")
    real = corpus.engine.api
    busy = ApiUnavailable("busy (request_queue_full)", status=503, code="request_queue_full")
    corpus.engine.api = lambda: Scripted(real(), submit_decision=raising(busy))
    for _ in range(3):
        corpus.run_pass(force=True)
    idle = client(data_dir, pc, publish_while_waiting=False, request_wait=0.0)
    assert idle.run_pass(force=True).waiting == 1
    (op,) = idle.spool.take(take.take_id).sync.outbox
    assert op.attempts == 3 and op.submitted_at is not None
    (queued,) = queued_requests(pc.layout)
    os.unlink(pc.layout.directory("incoming") / queued.name)
    assert client(data_dir, pc).run_pass().applied == 1


def test_turning_sync_off_stops_before_the_next_operation(
    pc: PC, data_dir: Path, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch = {"on": True}
    corpus = client(data_dir, pc, enabled=lambda: switch["on"])
    corpus.run_pass()
    first = record(corpus, prompt(pc, 0), seed=70)
    second = record(corpus, prompt(pc, 1), seed=71)
    upload = sync_module.TakeUploader.upload

    def then_off(self: Any, *args: Any) -> Any:
        try:
            return upload(self, *args)
        finally:
            switch["on"] = False  # the owner turns sync off meanwhile

    monkeypatch.setattr(sync_module.TakeUploader, "upload", then_off)
    result = corpus.run_pass()
    assert (result.uploaded, result.gated) == (1, True)
    assert corpus.spool.take(first.take_id).sync.upload == "accepted"
    assert corpus.spool.take(second.take_id).sync.outbox[0].attempts == 0


def test_a_take_a_crash_left_unsaved_is_saved_at_the_next_start(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()
    finished = corpus.begin_take(prompt(pc, 2))
    captured = capture(finished.directory, seed=72)  # the worker finished; Save never ran
    interrupted = corpus.begin_take(prompt(pc, 3))
    (interrupted.directory / "original.partial.wav").write_bytes(b"RIFF")  # mid-take
    again = client(data_dir, pc)
    (take,) = again.takes()
    assert take.take_id == f"take-{captured.sha256}"
    assert (take.record.prompt, take.record.catalog) == (finished.prompt, finished.catalog)
    assert kinds(again, take.take_id) == ["upload"]
    assert not finished.directory.exists()
    assert again.status().unsaved_recordings == (interrupted.directory.name,)
    assert again.run_pass().uploaded == 1
    again.discard_take(interrupted)
    assert again.status().unsaved_recordings == ()


def test_a_listing_during_a_save_never_takes_it_over(
    pc: PC, data_dir: Path, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client(data_dir, pc).run_pass()  # the catalog, cached
    corpus = client(data_dir, pc)  # a new run: its spool has not listed its takes yet
    pending = corpus.begin_take(prompt(pc, 0))
    captured = capture(pending.directory, seed=73)
    write = spool_module.write_canonical_json
    listed: list[list[str]] = []

    def and_list(path: Path, value: Any, **options: Any) -> str:
        digest = write(path, value, **options)
        if path.name == STATE_FILE and path.parent == pending.directory:
            # Another thread lists the takes for the first time between the records and
            # the rename: it must neither take the recording over nor see half of it.
            other = threading.Thread(
                target=lambda: listed.append([t.take_id for t in corpus.spool.takes()])
            )
            other.start()
            other.join(timeout=0.5)
        return digest

    monkeypatch.setattr(spool_module, "write_canonical_json", and_list)
    take = corpus.save_take(pending, captured)
    monkeypatch.undo()
    assert take.path.parent == corpus.spool.takes_root
    for _ in range(50):
        if listed:
            break
        threading.Event().wait(0.05)
    assert listed == [[take.take_id]]


# -- should-fix 7: the outbox stays short --------------------------------------------------------


def test_the_outbox_stays_short_however_long_the_pc_is_away(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()
    pc.stop()
    take = record(corpus, prompt(pc, 0), seed=74)
    frames = take.record.frames
    for index in range(1, 301):
        corpus.decide(take.take_id, "kept" if index % 2 else "removed")
        corpus.set_trim(take.take_id, index, frames - index)
    assert kinds(corpus, take.take_id) == ["upload", "decision", "trim"]
    assert (take.path / STATE_FILE).stat().st_size < 16 * 1024
    # A restart lists it; a take whose records cannot be read is reported, never hidden.
    damaged = corpus.spool.take_dir("take-" + "2" * 64)
    damaged.mkdir(mode=0o700)
    (damaged / "take.json").write_text("{}")
    again = client(data_dir, pc)
    assert [t.take_id for t in again.takes()] == [take.take_id]
    assert again.status().damaged == (damaged.name,)
    assert (
        CorpusSpool(data_dir / "corpus").takes()[0].sync.outbox
        == again.spool.take(take.take_id).sync.outbox
    )
