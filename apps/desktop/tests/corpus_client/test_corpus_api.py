"""The corpus on the app's ``/api/v1`` client (``api.py``): every answer read through the
backend's strict models, refusals with their codes, a take's audio whole or as a byte range,
and a request's status."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from corpus_rig import PC, capture, client, record, signed_in
from voice_lab_desktop.api import ApiClient, ApiError, ApiRefused
from voice_lab_desktop.corpus.client import CorpusClient
from voice_lab_desktop.corpus.records import DecisionOp
from voice_lab_desktop.tasks import Tasks

from cole_voice_lab.api.corpus_contracts import TakeDecisionRequest


def api_for(pc: PC, data_dir: Path) -> ApiClient:
    session = signed_in(pc.url, data_dir)
    return ApiClient(pc.url, session, session.http)


def test_the_corpus_reads_and_requests(pc: PC, data_dir: Path, library: Any) -> None:
    corpus = client(data_dir, pc)
    corpus.run_pass()
    prompt_id = pc.catalog[1].suite.prompts[0].prompt_id
    take = record(corpus, prompt_id, seed=50)
    corpus.decide(take.take_id, "kept")
    (_upload, op) = corpus.spool.take(take.take_id).sync.outbox
    assert isinstance(op, DecisionOp)
    corpus.run_pass()
    api = api_for(pc, data_dir)

    library_view = api.library()
    assert library_view.catalog == pc.catalog[0] and library_view.coverage.kept == 1
    history = api.prompt_takes(prompt_id)
    assert [t.take_id for t in history.takes] == [take.take_id] and history.row.status == "kept"
    with pytest.raises(ApiRefused) as refused:
        api.prompt_takes("no-such-prompt")
    assert (refused.value.status, refused.value.code) == (404, "prompt_not_found")

    audio = take.audio.read_bytes()
    assert api.take_audio(take.take_id) == audio
    assert api.take_audio(take.take_id, (0, 43)) == audio[:44]
    assert api.take_audio(take.take_id, (100, 100)) == audio[100:101]
    with pytest.raises(ValueError, match="not a byte range"):
        api.take_audio(take.take_id, (10, 9))
    with pytest.raises(ApiRefused) as missing:
        api.take_audio("take-" + "0" * 64)
    assert missing.value.code == "take_not_found"

    status = api.request_status(op.request_id)
    assert (status.status, status.take_id) == ("applied", take.take_id)
    assert status.outcome is not None and status.outcome.revision == 1
    # A retry of the same request answers as before; another change under its id is refused.
    again = api.submit_decision(take.take_id, op.body())
    assert again.status == "applied" and again.outcome == status.outcome
    other = TakeDecisionRequest(
        request_id=op.request_id, decision="removed", reason=None, expected_revision=1
    )
    with pytest.raises(ApiRefused) as conflict:
        api.submit_decision(take.take_id, other)
    assert (conflict.value.status, conflict.value.code) == (409, "request_conflict")
    with pytest.raises(ApiRefused) as unknown:
        api.request_status("req-" + "0" * 32)
    assert unknown.value.code == "request_not_found"


def test_answers_that_do_not_fit_are_errors(data_dir: Path) -> None:
    replies = iter(
        [
            httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"RIFF"),
            httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=b"RIFF...."),
            httpx.Response(206, headers={"Content-Type": "audio/wav"}, content=b"RIFF"),
            httpx.Response(
                200,
                headers={"Content-Type": "audio/wav", "Content-Length": str(2**40)},
                content=b"",
            ),
            httpx.Response(
                202,
                json={
                    "schema_version": "cole-voice-lab.api.corpus-request-status/v1",
                    "request_id": "req-" + "1" * 32,
                    "take_id": None,
                    "status": "queued",
                    "received_at": None,
                    "outcome": None,
                },
            ),
        ]
    )
    http = httpx.Client(transport=httpx.MockTransport(lambda _request: next(replies)))
    api = ApiClient("http://127.0.0.1:9", signed_in("http://127.0.0.1:9", data_dir), http)
    take_id = "take-" + "2" * 64
    with pytest.raises(ApiError, match="not audio"):
        api.take_audio(take_id)
    assert api.take_audio(take_id, (1, 3)) == b"IFF"  # the whole file: the range is cut here
    with pytest.raises(ApiError, match="answered 206 for a take's audio"):
        api.take_audio(take_id)
    with pytest.raises(ApiError, match="too large"):
        api.take_audio(take_id)
    with pytest.raises(ApiError, match="for another request"):
        api.request_status("req-" + "0" * 32)


def test_the_scheduler_syncs_off_the_gui_thread(
    qtbot: Any, pc: PC, data_dir: Path, library: Any
) -> None:
    import threading

    tasks = Tasks()
    corpus = client(data_dir, pc, tasks=tasks)
    threads: list[str] = []
    run_pass = corpus.engine.run_pass

    def recorded(*, force: bool = False) -> Any:
        threads.append(threading.current_thread().name)
        return run_pass(force=force)

    corpus.engine.run_pass = recorded  # type: ignore[method-assign]
    heard: list[int] = []
    listener = lambda: heard.append(corpus.status().sent)  # noqa: E731
    corpus.add_listener(listener)
    try:
        corpus.start()
        qtbot.waitUntil(lambda: corpus.status().can_record, timeout=10_000)
        take = record(corpus, pc.catalog[1].suite.prompts[0].prompt_id, seed=51)
        qtbot.waitUntil(lambda: corpus.status().sent == 1, timeout=15_000)
        corpus.decide(take.take_id, "kept")
        corpus.sync_now()
        qtbot.waitUntil(
            lambda: corpus.status().sent == 2 and not corpus.status().busy, timeout=15_000
        )
        assert threads and all(name.startswith("vld") for name in threads)
        assert threading.current_thread().name not in threads
        assert heard and heard[-1] == 2
        assert corpus.status().last_sync_at is not None
        # A pass asked for while one runs follows it.
        assert corpus.scheduler is not None
        corpus.scheduler.busy = True
        corpus.scheduler.request(0.0)
        assert corpus.scheduler._again
        corpus.scheduler.busy = False
        corpus.remove_listener(listener)
        count = len(heard)
        corpus.dismiss_notice("notice-" + "0" * 16)
        assert len(heard) == count
    finally:
        corpus.shutdown()
        tasks.close()
    assert corpus.scheduler.stopped
    corpus.sync_now()  # nothing runs after shutdown


def test_a_failed_pass_is_retried_and_its_error_raised(qtbot: Any, data_dir: Path, pc: PC) -> None:
    tasks = Tasks()
    corpus = client(data_dir, pc, tasks=tasks)

    def broken(*, force: bool = False) -> Any:
        raise RuntimeError("a bug")

    corpus.engine.run_pass = broken  # type: ignore[method-assign]
    assert corpus.scheduler is not None
    seen: list[BaseException | None] = []
    done = corpus.scheduler._done

    def capture(result: Any, error: BaseException | None) -> None:
        seen.append(error)
        try:
            done(result, error)
        except RuntimeError:
            pass

    corpus.scheduler._done = capture
    try:
        corpus.start()
        qtbot.waitUntil(lambda: bool(seen), timeout=10_000)
        assert isinstance(seen[0], RuntimeError) and corpus.scheduler.failures == 1
        assert corpus.scheduler._timer.isActive()
    finally:
        corpus.shutdown()
        tasks.close()


def test_the_client_runs_without_a_task_pool(pc: PC, data_dir: Path) -> None:
    corpus = CorpusClient(
        data_dir / "corpus",
        api=lambda: api_for(pc, data_dir),
        uploader=lambda: None,  # type: ignore[arg-type,return-value]
        enabled=lambda: False,
    )
    corpus.start()
    corpus.sync_now()
    corpus.shutdown()
    assert corpus.run_pass().gated
    assert corpus.status().busy is False


def test_a_take_saved_off_the_gui_thread_still_syncs(
    qtbot: Any, pc: PC, data_dir: Path, library: Any
) -> None:
    """The studio saves a take on a worker (the save hashes the WAV): the scheduler's timer
    and the listeners stay on the GUI thread (a timer started elsewhere is a Qt warning, and
    the pass it asked for never comes)."""
    import threading

    tasks = Tasks()
    corpus = client(data_dir, pc, tasks=tasks)
    gui = threading.current_thread()
    heard: list[bool] = []
    corpus.add_listener(lambda: heard.append(threading.current_thread() is gui))
    try:
        corpus.start()
        qtbot.waitUntil(lambda: corpus.status().can_record, timeout=10_000)
        pending = corpus.begin_take(pc.catalog[1].suite.prompts[0].prompt_id)
        captured = capture(pending.directory, seed=75)
        worker = threading.Thread(target=lambda: corpus.save_take(pending, captured))
        worker.start()
        worker.join(timeout=30)
        qtbot.waitUntil(lambda: corpus.status().sent == 1, timeout=15_000)
        take_id = f"take-{captured.sha256}"
        worker = threading.Thread(target=lambda: corpus.decide(take_id, "kept"))
        worker.start()
        worker.join(timeout=30)
        threading.Thread(target=corpus.sync_now).start()
        qtbot.waitUntil(lambda: corpus.status().sent == 2, timeout=15_000)
        assert heard and all(heard)
    finally:
        corpus.shutdown()
        tasks.close()
