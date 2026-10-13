"""The sync engine's rules at their edges (D-052, step 5): a PC without the take gets its
upload again, once; a request the PC lost goes again under its id; an upload that cannot
start leaves the take queued and says why; a lost sign-in, a busy server and a failed try
leave every outbox as it was."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from corpus_rig import PC, bind_library, client, record
from voice_lab_desktop.api import ApiError, ApiRefused, ApiUnavailable
from voice_lab_desktop.auth import SignInRequired
from voice_lab_desktop.config import ConfigError
from voice_lab_desktop.corpus.client import next_delay
from voice_lab_desktop.corpus.records import DecisionOp, UploadOp
from voice_lab_desktop.corpus.sync import PassResult

from cole_voice_lab.api.corpus_contracts import CorpusRequestStatus
from cole_voice_lab.corpus_requests import (
    CorpusRequestOutcome,
    DecisionChange,
    queued_requests,
)
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.home_host import HomeHostLedger
from cole_voice_lab.intake_client import UploadError


class Scripted:
    """The app's API client with some of its methods replaced."""

    def __init__(self, api: Any, **methods: Any) -> None:
        self._api = api
        self._methods = methods

    def __getattr__(self, name: str) -> Any:
        if name in self._methods:
            return self._methods[name]
        return getattr(self._api, name)


def uploaded_take(corpus: Any, pc: PC, seed: int) -> Any:
    corpus.run_pass()
    take = record(corpus, pc.catalog[1].suite.prompts[seed % 5].prompt_id, seed=seed)
    assert corpus.run_pass().uploaded == 1
    return take


def test_a_pc_without_the_take_gets_its_upload_again_once(tmp_path: Path, data_dir: Path) -> None:
    library = bind_library(data_dir)
    first = PC.create(tmp_path / "first").start()
    corpus = client(data_dir, first)
    take = uploaded_take(corpus, first, seed=40)
    first.stop()
    # The PC was rebuilt (the same catalog, seeded again) and never saw the take.
    rebuilt = PC.create(tmp_path / "rebuilt").start()
    try:
        assert rebuilt.catalog[0] == first.catalog[0]
        moved = client(data_dir, rebuilt)
        moved.decide(take.take_id, "kept")
        (op,) = moved.spool.take(take.take_id).sync.outbox
        result = moved.run_pass()
        assert (result.uploaded, result.applied, result.refused) == (1, 1, 0)
        sync = moved.spool.take(take.take_id).sync
        assert sync.outbox == () and sync.decision.revision == 1
        assert sync.journal[-1].request_id == op.request_id  # the same id: never applied
        assert len(rebuilt.raw_packages()) == 1
        events = [entry.event for entry, _ in HomeHostLedger(library).entries()]
        assert events == ["upload-started", "upload-completed"] * 2

        # Still no record after its upload was checked again: the change is given up,
        # never retried in a loop.
        moved.decide(take.take_id, "removed")
        refuse = ApiRefused("no record (take_not_found)", status=404, code="take_not_found")
        attempts: list[str] = []

        def submit(_take_id: str, request: Any) -> Any:
            attempts.append(request.request_id)
            raise refuse

        real = moved.engine.api
        moved.engine.api = lambda: Scripted(real(), submit_decision=submit)
        result = moved.run_pass()
        assert (result.uploaded, result.refused, result.waiting) == (1, 1, 0)
        assert len(attempts) == 2 and len(set(attempts)) == 1
        sync = moved.spool.take(take.take_id).sync
        assert sync.outbox == () and sync.journal[-1].code == "take_not_found"
        (notice,) = moved.notices()
        assert notice.kind == "change-refused" and "the PC has no record of this take" in (
            notice.text
        )
    finally:
        rebuilt.stop()


def test_a_request_the_pc_refused_for_a_missing_take_goes_again_under_a_new_id(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    take = uploaded_take(corpus, pc, seed=41)
    corpus.decide(take.take_id, "kept")
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    assert isinstance(op, DecisionOp)
    seen: list[str] = []
    real = corpus.engine.api

    def submit(take_id: str, request: Any) -> CorpusRequestStatus:
        seen.append(request.request_id)
        if len(seen) > 1:
            return real().submit_decision(take_id, request)
        outcome = CorpusRequestOutcome(
            request_id=request.request_id,
            take_id=take_id,
            change=DecisionChange(decision="kept", reason=None, expected_revision=0),
            received_at=pc_now(),
            status="refused",
            code="take_not_found",
            detail="the PC has no record of the take",
            revision=None,
            decided_at=pc_now(),
        )
        return CorpusRequestStatus(
            request_id=request.request_id,
            take_id=take_id,
            status="refused",
            received_at=pc_now(),
            outcome=outcome,
        )

    corpus.engine.api = lambda: Scripted(real(), submit_decision=submit)
    result = corpus.run_pass()
    assert (result.uploaded, result.applied) == (1, 1)
    assert seen[0] == op.request_id and seen[1] != op.request_id
    assert corpus.spool.take(take.take_id).sync.decision.revision == 1


def pc_now() -> Any:
    from cole_voice_lab.models import utc_now

    return utc_now()


def test_a_request_the_pc_lost_goes_again_under_its_id(
    pc: PC, data_dir: Path, library: Any
) -> None:
    take = uploaded_take(client(data_dir, pc), pc, seed=42)
    # The PC's writer does not run while this client waits.
    idle = client(data_dir, pc, publish_while_waiting=False, request_wait=0.0)
    idle.decide(take.take_id, "kept")
    assert idle.run_pass().waiting == 1
    (op,) = idle.spool.take(take.take_id).sync.outbox
    assert op.submitted_at is not None
    (queued,) = queued_requests(pc.layout)
    os.unlink(pc.layout.directory("incoming") / queued.name)  # lost before its writer ran
    again = client(data_dir, pc)
    result = again.run_pass()
    assert result.applied == 1
    sync = again.spool.take(take.take_id).sync
    assert sync.journal[-1].request_id == op.request_id and sync.decision.revision == 1


def test_an_upload_that_cannot_start_leaves_the_take_queued(pc: PC, data_dir: Path) -> None:
    corpus = client(data_dir, pc)  # no approval is bound in this machine's library
    corpus.run_pass()
    take = record(corpus, pc.catalog[1].suite.prompts[0].prompt_id, seed=43)
    result = corpus.run_pass()
    assert result.reached and result.waiting == 1 and result.uploaded == 0
    assert "no home-host approval is bound on this machine yet" in result.upload_blocked
    assert corpus.status().upload_blocked == result.upload_blocked
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    assert isinstance(op, UploadOp) and op.attempts == 0
    assert pc.raw_packages() == []


def test_failures_leave_every_outbox_as_it_was(
    pc: PC, data_dir: Path, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()
    take = record(corpus, pc.catalog[1].suite.prompts[0].prompt_id, seed=44)
    from voice_lab_desktop.corpus import sync as sync_module

    def failing(error: BaseException) -> Any:
        def upload(*_args: Any, **_kwargs: Any) -> Any:
            raise error

        return upload

    # An upload the PC answers with a server error: tried again after a backoff.
    monkeypatch.setattr(
        sync_module.TakeUploader, "upload", failing(UploadError("internal_error: boom"))
    )
    result = corpus.run_pass()
    assert (result.reached, result.offline, result.waiting) == (True, False, 1)
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    assert (op.attempts, op.last_error) == (1, "internal_error: boom")
    assert op.not_before is not None and op.last_attempt_at is not None
    assert timedelta(seconds=29) < op.not_before - op.last_attempt_at <= timedelta(seconds=31)
    assert corpus.run_pass().waiting == 1  # not due yet: not tried
    assert corpus.spool.take(take.take_id).sync.outbox[0].attempts == 1
    # The home host does not answer: the pass stops, offline.
    monkeypatch.setattr(
        sync_module.TakeUploader,
        "upload",
        failing(UploadError("the home host is unreachable (ConnectionRefusedError)")),
    )
    result = corpus.run_pass(force=True)
    assert result.offline and result.error.startswith("the home host is unreachable")
    # The local package fails its checks: nothing is sent; it says why.
    monkeypatch.setattr(
        sync_module.TakeUploader, "upload", failing(SecurityError("the package changed"))
    )
    result = corpus.run_pass(force=True)
    assert result.error == f"{take.take_id}: the package changed" and not result.offline
    # The sign-in is gone.
    monkeypatch.setattr(
        sync_module.TakeUploader, "upload", failing(SignInRequired("the sign-in is gone"))
    )
    result = corpus.run_pass(force=True)
    assert result.sign_in_required and corpus.status().sign_in_required
    assert next_delay(result, 0, 0) is None
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    assert (op.attempts, op.last_error) == (4, "sign-in required")
    monkeypatch.undo()

    # The server refuses the upload's token.
    def bad_bearer() -> Any:
        made = real_uploader()
        made.bearer = lambda: "not-a-token"
        return made

    real_uploader = corpus.engine.uploader
    corpus.engine.uploader = bad_bearer
    assert corpus.run_pass(force=True).sign_in_required
    corpus.engine.uploader = real_uploader
    assert corpus.run_pass(force=True).uploaded == 1

    # Changes: a busy server, an answer that is not a status, a lost sign-in.
    corpus.decide(take.take_id, "kept")
    real = corpus.engine.api
    busy = ApiUnavailable("busy (request_queue_full)", status=503, code="request_queue_full")

    def raising(error: BaseException) -> Any:
        def method(*_args: Any) -> Any:
            raise error

        return method

    for error, check in (
        (busy, lambda r: r.reached and not r.offline and r.error.startswith("busy")),
        (ApiError("the server answered with an invalid X"), lambda r: r.waiting == 1),
        (SignInRequired("gone"), lambda r: r.sign_in_required),
    ):
        corpus.engine.api = lambda error=error: Scripted(real(), submit_decision=raising(error))
        assert check(corpus.run_pass(force=True))
    (op,) = corpus.spool.take(take.take_id).sync.outbox
    assert op.attempts == 2 and op.submitted_at is None
    corpus.engine.api = real
    assert corpus.run_pass(force=True).applied == 1


def test_the_pass_says_why_it_could_not_start(pc: PC, data_dir: Path, library: Any) -> None:
    corpus = client(data_dir, pc)

    def no_server() -> Any:
        raise ConfigError("no server is set")

    real = corpus.engine.api
    corpus.engine.api = no_server
    result = corpus.run_pass()
    assert result.offline and result.error == "no server is set"
    assert corpus.status().last_error == "no server is set"

    def signed_out(_known: Any = None) -> Any:
        raise SignInRequired("not signed in")

    corpus.engine.api = lambda: Scripted(real(), catalog=signed_out)
    assert corpus.run_pass().sign_in_required

    def server_error(_known: Any = None) -> Any:
        raise ApiUnavailable("down (internal_error)", status=500, code="internal_error")

    corpus.engine.api = lambda: Scripted(real(), catalog=server_error)
    result = corpus.run_pass()
    assert not result.offline and not result.reached

    def invalid(_known: Any = None) -> Any:
        raise ApiError("the server answered with an invalid CatalogView")

    corpus.engine.api = lambda: Scripted(real(), catalog=invalid)
    result = corpus.run_pass()
    assert result.reached and result.error.startswith("the PC's catalog: ")


def test_the_next_pass_backs_off() -> None:
    started = pc_now()
    offline = PassResult(started_at=started, offline=True)
    assert [next_delay(offline, n, 0) for n in (1, 2, 3, 10)] == [5.0, 10.0, 20.0, 300.0]
    waiting = PassResult(started_at=started, reached=True, waiting=2)
    assert [next_delay(waiting, 0, n) for n in (1, 2, 6)] == [10.0, 20.0, 300.0]
    assert next_delay(PassResult(started_at=started, reached=True), 0, 0) == 600.0
    gated = PassResult(started_at=started, reached=True, gated=True, waiting=1)
    assert next_delay(gated, 0, 3) == 600.0
