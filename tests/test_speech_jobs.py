"""Speech jobs on disk and the job runner (speech/store.py, speech/runner.py, speech/gpu.py).

The backend is the fake of ``speech_fixtures`` (a generated tone); nothing here starts a model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from speech_fixtures import (
    NOW,
    FakeBackend,
    Lab,
    OnePass,
    build_lab,
    caller,
    designate_another_subject,
    write_tone,
)

import cole_voice_lab
from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.consent import revoke_consent
from cole_voice_lab.speech.backend import SpeechBackendError
from cole_voice_lab.speech.contracts import (
    FAILURE_DETAILS,
    GpuSlotHolder,
    SpeechJobState,
    SpeechRenderRecord,
    SpeechRequest,
    WebhookOutboxEntry,
)
from cole_voice_lab.speech.gpu import (
    GpuSlotLease,
    GpuSlotTimeout,
    busy_with_long_job,
    current_holder,
    hold_slot,
    slot_wanted,
    speech_waiting,
    want_slot,
    withdraw_want,
)
from cole_voice_lab.speech.release import consent_path, current_release
from cole_voice_lab.speech.runner import (
    CodeProblem,
    RunnerBusy,
    RunnerMisconfigured,
    RunnerOutdated,
    SpeechRunner,
    check_wav,
    ledger_path,
    release_code_check,
)
from cole_voice_lab.speech.segments import job_seed, segment_seed
from cole_voice_lab.speech.store import (
    TEXT_KEY_NAME,
    Job,
    SpeechRefusal,
    SpeechStore,
    TextKeyError,
    open_audio,
    open_segment,
    to_status,
)


def store_for(lab: Lab) -> SpeechStore:
    store = SpeechStore(lab.config)
    store.ensure()
    return store


def submit(lab: Lab, text: str = "Hello there.", **fields: Any) -> Job:
    job, created = store_for(lab).submit(
        lab.config.callers[0], SpeechRequest(text=text, **fields), now=NOW
    )
    assert created
    return job


def runner(lab: Lab, backend: Any | None = None, *, at: Any = NOW) -> SpeechRunner:
    return OnePass(lab.config, backend or FakeBackend(), clock=lambda: at, slot_poll_seconds=0.01)


def refused(code: str, call: Any) -> SpeechRefusal:
    with pytest.raises(SpeechRefusal) as failure:
        call()
    assert failure.value.code == code, failure.value
    return failure.value


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return build_lab(tmp_path)


# -- submitting ------------------------------------------------------------------------------


def test_a_submitted_job_keeps_its_text_apart_and_is_queued(lab: Lab) -> None:
    job = submit(lab, "Line one.\nLine two.", client_ref="turn-7")
    store = store_for(lab)
    directory = store.job_directory(job.record.job_id)
    assert sorted(path.name for path in directory.iterdir()) == [
        "input.json",
        "request.json",
        "status.json",
    ]
    assert "Line one" not in (directory / "request.json").read_text()
    assert store.read_input(job.record.job_id).text == "Line one.\nLine two."
    assert (store.queue_root / job.record.job_id).exists()
    for path in (store.root, directory, directory / "input.json"):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    status = to_status(job)
    assert (status.state, status.text_characters, status.client_ref) == ("queued", 19, "turn-7")
    assert status.links.audio == f"/api/v1/speech/{job.record.job_id}/audio"
    assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
    assert speech_waiting(store.queue_root)


def test_an_idempotent_replay_returns_the_job_and_a_changed_body_conflicts(lab: Lab) -> None:
    store = store_for(lab)
    registered = lab.config.callers[0]
    request = SpeechRequest(text="Hello.", idempotency_key="reply-1")
    first, created = store.submit(registered, request, now=NOW)
    again, created_again = store.submit(registered, request, now=NOW)
    assert created and not created_again and again.record == first.record
    assert store.replay(registered, request) == again
    refused(
        "idempotency_conflict",
        lambda: store.submit(
            registered, SpeechRequest(text="Other.", idempotency_key="reply-1"), now=NOW
        ),
    )
    assert store.replay(registered, SpeechRequest(text="Hello.")) is None


def test_a_caller_is_held_to_its_limits_and_the_queue_to_its_size(tmp_path: Path) -> None:
    other = caller(
        caller_id="other",
        subject="client-other",
        webhook_urls=(),
        webhook_secret_file=None,
    )
    lab = build_lab(
        tmp_path,
        callers=(caller(max_queued_jobs=2, daily_character_budget=20), other),
        queue_limit=2,
    )
    store = store_for(lab)
    registered = lab.config.callers[0]
    store.submit(registered, SpeechRequest(text="0123456789"), now=NOW)
    store.submit(registered, SpeechRequest(text="012345678"), now=NOW)
    refused("too_many_jobs", lambda: store.submit(registered, SpeechRequest(text="x"), now=NOW))
    runner(lab).run_pending()
    refused(
        "daily_budget_exhausted",
        lambda: store.submit(registered, SpeechRequest(text="xy"), now=NOW),
    )
    store.submit(registered, SpeechRequest(text="x"), now=NOW)
    store.submit(other, SpeechRequest(text="y"), now=NOW)
    full = refused("queue_full", lambda: store.submit(other, SpeechRequest(text="z"), now=NOW))
    assert full.retry_after == 60
    tomorrow = NOW + timedelta(days=1)
    runner(lab, at=tomorrow - timedelta(minutes=9)).run_pending()
    store.submit(registered, SpeechRequest(text="0123456789"), now=tomorrow)


# -- rendering -------------------------------------------------------------------------------


THREE = "The first sentence is here. A second one follows it! And is there a third?"


def test_a_job_renders_sentence_by_sentence_and_joins_them(lab: Lab) -> None:
    hook = f"{lab.config.callers[0].webhook_urls[0]}/turn-7"
    job = submit(lab, THREE, callback_url=hook, client_ref="turn-7")
    job_id = job.record.job_id
    backend = FakeBackend()
    report = runner(lab, backend).run_pending()
    assert report.outcomes == {job_id: "succeeded"}
    assert [text for text, _ in backend.calls] == [
        "The first sentence is here.",
        "A second one follows it!",
        "And is there a third?",
    ]
    store = store_for(lab)
    ended = store.load(job_id)
    assert ended is not None and ended.state.state == "succeeded"
    assert ended.state.segment_count == 3 and [s.index for s in ended.state.segments] == [1, 2, 3]
    for segment in ended.state.segments:
        path = store.segment_path(job_id, segment.index)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sha256_file(path)[0] == segment.sha256
    audio = store.audio_path(job_id)
    assert stat.S_IMODE(audio.stat().st_mode) == 0o600
    assert ended.state.audio is not None and ended.state.audio.frames == 3 * 2400
    assert ended.state.audio.sha256 == sha256_file(audio)[0]
    assert ended.state.audio_expires_at == NOW + timedelta(days=7)
    assert not (store.job_directory(job_id) / "input.json").exists()
    assert not (store.queue_root / job_id).exists()
    release, release_digest = current_release(lab.layout)
    assert backend.prepared[0].release == release
    ledger = SpeechRenderRecord.model_validate_json(ledger_path(lab.layout, job_id).read_bytes())
    assert stat.S_IMODE(ledger_path(lab.layout, job_id).stat().st_mode) == 0o400
    assert (ledger.outcome, ledger.caller_id, ledger.text_characters) == (
        "succeeded",
        "agent-server",
        len(THREE),
    )
    assert (
        ledger.release.release_id == "vr-0001" and ledger.release.release_sha256 == release_digest
    )
    assert ledger.consent_sha256 == lab.consent_sha256
    assert ledger.output == ended.state.audio and ledger.audio_ms == 300
    assert ledger.segment_count == 3
    assert ledger.segment_sha256 == tuple(segment.sha256 for segment in ended.state.segments)
    assert ledger.fingerprint is not None and ledger.fingerprint.settings.nfe_steps == 32
    assert ledger.model_family == "f5-tts" == ledger.fingerprint.model_family
    settings = canonical_sha256(ledger.fingerprint.settings)
    assert ledger.seed == job_seed(job.record.text_hmac_sha256, release_digest, settings)
    assert [seed for _, seed in backend.calls] == [
        segment_seed(ledger.seed, index) for index in (1, 2, 3)
    ]
    assert "first sentence" not in ledger_path(lab.layout, job_id).read_text()
    entry = WebhookOutboxEntry.model_validate_json(
        (store.outbox_root / f"msg_{job_id}.json").read_bytes()
    )
    assert (entry.url, entry.event.type, entry.event.data.client_ref) == (
        hook,
        "speech.succeeded",
        "turn-7",
    )
    assert entry.event.data.audio is not None and entry.event.data.segment_count == 3
    assert "first sentence" not in (store.outbox_root / f"msg_{job_id}.json").read_text()
    stream, size = open_audio(store, ended)
    with stream:
        assert size == ended.state.audio.bytes
    stream, size, segment = open_segment(store, ended, 2)
    with stream:
        assert segment.index == 2
    refused("not_found", lambda: open_segment(store, ended, 4))
    status = to_status(ended)
    assert status.segments[0].audio == f"/api/v1/speech/{job_id}/segments/1/audio"


def test_a_rerun_of_the_same_text_is_byte_identical(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    first = submit(lab, THREE)
    runner(lab).run_pending()
    second = submit(lab, THREE)
    runner(lab).run_pending()
    store = store_for(lab)
    one, two = store.load(first.record.job_id), store.load(second.record.job_id)
    assert one is not None and two is not None
    assert one.state.audio == two.state.audio
    assert [s.sha256 for s in one.state.segments] == [s.sha256 for s in two.state.segments]
    other = submit(lab, THREE + " Plus one more sentence.")
    runner(lab).run_pending()
    three = store.load(other.record.job_id)
    assert three is not None and three.state.segments[0].sha256 != one.state.segments[0].sha256


@pytest.mark.parametrize(
    ("fail", "outcome", "code", "ledger"),
    [
        ("runtime_unavailable", "failed", "runtime_unavailable", False),
        ("render_timeout", "failed", "render_timeout", True),
        ("render_failed", "failed", "render_failed", True),
        ("cancelled", "cancelled", None, True),
    ],
)
def test_a_render_that_fails_is_ended_with_its_code(
    lab: Lab, fail: str, outcome: str, code: str | None, ledger: bool
) -> None:
    job = submit(lab, THREE, callback_url=lab.config.callers[0].webhook_urls[0])
    backend = FakeBackend(fail=fail, fail_at=2)
    report = runner(lab, backend).run_pending()
    job_id = job.record.job_id
    assert report.outcomes == {job_id: outcome}
    store = store_for(lab)
    ended = store.load(job_id)
    assert ended is not None
    assert (ended.state.error.code if ended.state.error else None) == code
    assert not store.audio_path(job_id).exists()
    assert ledger_path(lab.layout, job_id).exists() == ledger
    if ledger:
        record = SpeechRenderRecord.model_validate_json(
            ledger_path(lab.layout, job_id).read_bytes()
        )
        assert (record.outcome, record.failure, record.output) == (outcome, code, None)
        assert len(record.segment_sha256) == 1  # the sentence published before the failure
        assert [s.index for s in ended.state.segments] == [1]
    else:
        assert backend.prepared == [] and not backend.loaded
    event = json.loads((store.outbox_root / f"msg_{job_id}.json").read_text())
    assert event["event"]["type"] == f"speech.{outcome}"


@pytest.mark.parametrize(
    "shape", [{"rate": 48_000}, {"channels": 2}, {"frames": 0}, "not-a-wav", "nothing"]
)
def test_a_render_that_is_not_24_khz_mono_pcm_fails(lab: Lab, shape: Any) -> None:
    def write(path: Path) -> None:
        if shape == "not-a-wav":
            path.write_bytes(b"RIFF....")
        elif shape != "nothing":
            write_tone(path, **shape)

    job = submit(lab)
    runner(lab, FakeBackend(write=write)).run_pending()
    ended = store_for(lab).load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == "render_failed"


def test_check_wav_names_the_shape_it_found(tmp_path: Path) -> None:
    path = tmp_path / "x.wav"
    write_tone(path, rate=16_000)
    with pytest.raises(SpeechBackendError, match="16000 Hz"):
        check_wav(path)


def test_cancel_ends_a_queued_job_at_once_and_a_running_one_between_sentences(
    lab: Lab,
) -> None:
    store = store_for(lab)
    registered = lab.config.callers[0]
    queued = submit(lab, callback_url=registered.webhook_urls[0])
    cancelled = store.cancel(registered, queued.record.job_id, now=NOW)
    assert cancelled.state.state == "cancelled"
    assert (store.outbox_root / f"msg_{queued.record.job_id}.json").exists()
    assert runner(lab).run_pending().outcomes == {}  # the runner cannot claim it
    refused("already_finished", lambda: store.cancel(registered, queued.record.job_id, now=NOW))

    running = submit(lab, THREE)
    seen: list[str] = []

    def cancel_during_the_first_sentence(index: int) -> None:
        if index == 1:
            seen.append(store.cancel(registered, running.record.job_id, now=NOW).state.state)

    backend = FakeBackend(on_segment=cancel_during_the_first_sentence)
    report = runner(lab, backend).run_pending()
    assert seen == ["running"]  # the runner owned it: the cancel was a marker
    assert report.outcomes == {running.record.job_id: "cancelled"}
    ended = store.load(running.record.job_id)
    assert ended is not None and [s.index for s in ended.state.segments] == [1]
    # The cancel did not unload the model: only the one-pass runner's end did.
    assert len(backend.calls) == 1 and backend.unloads == 1


def test_a_cancel_that_arrives_before_the_claim_is_honoured(lab: Lab) -> None:
    store = store_for(lab)
    job = submit(lab)
    (store.job_directory(job.record.job_id) / "cancel").write_bytes(b"")
    assert runner(lab).run_pending().outcomes == {job.record.job_id: "cancelled"}


def test_another_callers_job_is_not_found(lab: Lab) -> None:
    store = store_for(lab)
    job = submit(lab)
    stranger = caller(
        caller_id="stranger", subject="client-stranger", webhook_urls=(), webhook_secret_file=None
    )
    refused("speech_not_found", lambda: store.for_caller(stranger, job.record.job_id))
    refused("speech_not_found", lambda: store.for_caller(stranger, "../etc"))
    assert store.load("sp-" + "0" * 24) is None


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        ("release", "voice_release_changed"),
        ("consent", "consent_changed"),
        ("none", "voice_release_changed"),
    ],
)
def test_the_runner_rechecks_the_release_and_the_consent(
    tmp_path: Path, damage: str, code: str
) -> None:
    lab = build_lab(tmp_path, release=damage != "none")
    job = submit(lab)
    if damage == "release":
        release, _ = current_release(lab.layout)
        (lab.layout.root / release.file("checkpoint").path).write_bytes(b"retrained")
    elif damage == "consent":
        release, _ = current_release(lab.layout)
        revoke_consent(
            consent_path(lab.layout, release.consent_id),
            expected_sha256=release.consent_sha256,
            at=NOW - timedelta(hours=1),
        )
    backend = FakeBackend()
    runner(lab, backend).run_pending()
    ended = store_for(lab).load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == code
    assert backend.calls == [] and not ledger_path(lab.layout, job.record.job_id).exists()


def test_a_stale_job_times_out_and_an_orphan_fails(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = submit(lab)
    report = runner(lab, at=NOW + timedelta(minutes=11)).run_pending()
    assert report.outcomes == {stale.record.job_id: "failed"}
    orphan = submit(lab, "Orphan.")
    store = store_for(lab)
    store.claim(orphan.record.job_id, "runner")
    store.mark_running(orphan, now=NOW, segment_count=1)
    report = runner(lab).run_pending()
    ended = store.load(orphan.record.job_id)
    assert report.outcomes == {orphan.record.job_id: "failed"}
    assert ended is not None and ended.state.error is not None
    # The caller reads a code and a fixed sentence; the particulars are the runner's log.
    assert ended.state.error.model_dump() == {
        "code": "internal_error",
        "detail": "the job runner failed",
    }
    assert "stopped while it rendered" in capsys.readouterr().err
    assert not (store.job_directory(orphan.record.job_id) / "input.json").exists()


# -- the GPU slot -----------------------------------------------------------------------------


def test_the_slot_names_its_holder_only_while_it_is_held(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    assert current_holder(root) is None
    holder = GpuSlotHolder(
        kind="finetune",
        job_id="finetune-1",
        started_at=NOW,
        expected_end_at=NOW + timedelta(hours=2),
    )
    with hold_slot(root, holder, timeout_seconds=1):
        assert current_holder(root) == holder
        assert busy_with_long_job(root, now=NOW, default_seconds=600) == 7201
        assert busy_with_long_job(root, now=NOW + timedelta(hours=3), default_seconds=600) == 600
        with pytest.raises(GpuSlotTimeout):
            with hold_slot(root, holder, timeout_seconds=0.05, poll_seconds=0.01):
                pass  # pragma: no cover
    assert (
        current_holder(root) is None
        and busy_with_long_job(root, now=NOW, default_seconds=1) is None
    )
    write_canonical_json(root / "gpu-slot.json", holder)  # a stale record: nobody holds the lock
    assert current_holder(root) is None
    speech = holder.model_copy(update={"kind": "speech"})
    with hold_slot(root, speech, timeout_seconds=1):
        assert busy_with_long_job(root, now=NOW, default_seconds=1) is None
    assert not speech_waiting(tmp_path / "missing")


def test_speech_waits_for_the_slot_only_until_its_queue_limit(lab: Lab) -> None:
    job = submit(lab)
    holder = GpuSlotHolder(kind="finetune", job_id="finetune-1", started_at=NOW)
    with hold_slot(Path(lab.config.gpu_slot_root), holder, timeout_seconds=1):
        report = runner(lab, at=NOW + timedelta(seconds=599, microseconds=950_000)).run_pending()
    assert report.outcomes == {job.record.job_id: "failed"}
    ended = store_for(lab).load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == "queue_timeout"


def test_the_resident_model_stays_loaded_until_idle_or_a_long_job_wants_the_slot(
    lab: Lab,
) -> None:
    clock = [0.0]
    backend = FakeBackend()
    resident = SpeechRunner(
        lab.config, backend, clock=lambda: NOW, monotonic=lambda: clock[0], slot_poll_seconds=0.01
    )
    root = Path(lab.config.gpu_slot_root)
    submit(lab)
    resident.run_pending()
    assert backend.loaded and resident.lease.held
    assert current_holder(root) is not None and current_holder(root).kind == "speech"  # type: ignore[union-attr]
    assert busy_with_long_job(root, now=NOW, default_seconds=1) is None
    clock[0] = lab.config.idle_unload_seconds - 1
    assert not resident.yield_if_idle() and backend.loaded
    clock[0] = lab.config.idle_unload_seconds
    assert resident.yield_if_idle()
    assert not backend.loaded and not resident.lease.held and current_holder(root) is None
    assert not resident.yield_if_idle()

    submit(lab, "Load it again.")
    resident.run_pending()
    assert backend.loaded
    long_job = GpuSlotHolder(kind="finetune", job_id="finetune-1", started_at=NOW)
    want_slot(root, long_job)
    queued = submit(lab, "Queued speech goes first.")
    assert not resident.yield_if_idle()  # speech is queued
    resident.run_pending()
    assert store_for(lab).load(queued.record.job_id).state.state == "succeeded"  # type: ignore[union-attr]
    assert resident.yield_if_idle()  # wanted and the queue is empty
    assert not backend.loaded and not resident.lease.held
    with hold_slot(root, long_job, timeout_seconds=1):
        withdraw_want(root)
        assert not slot_wanted(root)
    resident.unload()


def test_a_lease_is_taken_once_and_times_out_while_another_holds_the_slot(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    speech = GpuSlotHolder(kind="speech", job_id="sp-1", started_at=NOW)
    lease = GpuSlotLease(root)
    lease.acquire(speech, timeout_seconds=0)
    lease.acquire(speech, timeout_seconds=0)
    assert lease.held and current_holder(root) == speech
    other = GpuSlotLease(root)
    with pytest.raises(GpuSlotTimeout):
        other.acquire(speech, timeout_seconds=0.02, poll_seconds=0.01, sleep=lambda _: None)
    lease.release()
    lease.release()
    assert not lease.held and current_holder(root) is None


def test_serve_runs_until_stopped_and_unloads(lab: Lab) -> None:
    submit(lab)
    backend = FakeBackend()
    resident = SpeechRunner(lab.config, backend, clock=lambda: NOW)
    resident.serve(stop=lambda: bool(backend.calls), poll_seconds=0.0)  # once it has spoken
    assert backend.calls and not backend.loaded and not resident.lease.held


def test_a_runtime_that_cannot_load_frees_the_slot(lab: Lab) -> None:
    submit(lab)
    resident = SpeechRunner(lab.config, FakeBackend(fail="runtime_unavailable"), clock=lambda: NOW)
    resident.run_pending()
    assert not resident.lease.held


# -- retention --------------------------------------------------------------------------------


def test_retention_deletes_audio_then_the_record_and_keeps_the_ledger(lab: Lab) -> None:
    job = submit(lab, idempotency_key="reply-1")
    job_id = job.record.job_id
    runner(lab).run_pending()
    store = store_for(lab)
    assert store.sweep(now=NOW + timedelta(days=6)) == (0, 0)
    assert store.sweep(now=NOW + timedelta(days=7)) == (1, 0)
    expired = store.load(job_id)
    assert expired is not None and expired.state.state == "expired"
    assert not store.audio_path(job_id).exists()
    refused("audio_expired", lambda: open_audio(store, expired))
    assert to_status(expired).audio is not None
    assert store.sweep(now=NOW + timedelta(days=30)) == (0, 1)
    assert store.load(job_id) is None
    assert not any(store.idempotency_root.rglob("*.json"))
    assert ledger_path(lab.layout, job_id).exists()


def test_a_submit_that_never_finished_is_cleared_after_an_hour(lab: Lab) -> None:
    store = store_for(lab)
    broken = store.jobs_root / ("sp-" + "1" * 24)
    broken.mkdir()
    (store.jobs_root / "not-a-job").mkdir()
    assert store.sweep(now=NOW) == (0, 0)
    os.utime(broken, (0, 0))
    assert store.sweep(now=NOW) == (0, 1)
    assert not broken.exists() and (store.jobs_root / "not-a-job").exists()


def test_audio_is_served_only_once_rendered(lab: Lab) -> None:
    job = submit(lab)
    refused("audio_not_ready", lambda: open_audio(store_for(lab), job))


def test_a_job_state_is_consistent() -> None:
    job_id = "sp-" + "a" * 24
    with pytest.raises(ValueError, match="end time"):
        SpeechJobState(job_id=job_id, state="succeeded")
    with pytest.raises(ValueError, match="names its failure"):
        SpeechJobState(job_id=job_id, state="failed", finished_at=NOW)
    with pytest.raises(ValueError, match="names its audio"):
        SpeechJobState(job_id=job_id, state="succeeded", finished_at=NOW)
    with pytest.raises(ValueError, match="start time"):
        SpeechJobState(job_id=job_id, state="running")


# -- review findings: nothing stays claimed, every path ends the job ------------------------


def claimed_but_never_started(lab: Lab, text: str = "Claimed then lost.") -> Job:
    """A job whose runner claimed it and was killed before it ran (in the slot wait, say)."""
    job = submit(lab, text)
    assert store_for(lab).claim(job.record.job_id, "runner")
    return job


def test_a_claimed_job_a_stopped_runner_left_is_queued_again_and_rendered(lab: Lab) -> None:
    job = claimed_but_never_started(lab)
    store = store_for(lab)
    assert not (store.queue_root / job.record.job_id).exists()
    report = runner(lab, at=NOW + timedelta(minutes=5)).run_pending()
    assert report.outcomes == {job.record.job_id: "succeeded"}
    assert not (store.job_directory(job.record.job_id) / "input.json").exists()


@pytest.mark.parametrize(("cancel", "outcome"), [(False, "failed"), (True, "cancelled")])
def test_a_claimed_job_past_its_queue_time_or_cancelled_is_ended(
    lab: Lab, cancel: bool, outcome: str
) -> None:
    job = claimed_but_never_started(lab)
    store = store_for(lab)
    if cancel:
        store.cancel(lab.config.callers[0], job.record.job_id, now=NOW)  # leaves the marker
    later = NOW + timedelta(seconds=lab.config.queue_timeout_seconds + (0 if cancel else 1))
    backend = FakeBackend()
    report = runner(lab, backend, at=later).run_pending()
    assert report.outcomes == {job.record.job_id: outcome}
    ended = store.load(job.record.job_id)
    assert ended is not None and backend.calls == []
    if not cancel:
        assert ended.state.error is not None and ended.state.error.code == "queue_timeout"
    assert not (store.job_directory(job.record.job_id) / "input.json").exists()
    # It no longer counts against the caller's limits or the queue.
    assert not any(queued.record.job_id == job.record.job_id for queued in store.queued())


def test_a_cancel_the_api_claimed_but_never_ended_is_ended_by_the_runner(lab: Lab) -> None:
    job = submit(lab)
    store = store_for(lab)
    assert store.claim(job.record.job_id, "cancel")  # and then the API stopped
    assert runner(lab).run_pending().outcomes == {}  # within the grace: the API may finish it
    claim = store.job_directory(job.record.job_id) / "claim"
    os.utime(claim, (0, 0))
    assert runner(lab).run_pending().outcomes == {job.record.job_id: "cancelled"}


def broken_release_directory(lab: Lab) -> None:
    (lab.layout.root / "provenance/voice-releases/0001.json~").write_text("an editor's copy")


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        ("stray-release-file", "voice_release_changed"),
        ("prepare-oserror", "runtime_unavailable"),
        ("prepare-unexpected", "internal_error"),
        ("slot-unopenable", "internal_error"),
        ("input-missing", "internal_error"),
    ],
)
def test_any_error_ends_the_job_instead_of_leaving_it_claimed(
    lab: Lab, damage: str, code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    job = submit(lab)
    store = store_for(lab)
    backend = FakeBackend()
    if damage == "stray-release-file":
        broken_release_directory(lab)
    elif damage == "prepare-oserror":

        def unreadable(release: Any) -> Any:
            raise PermissionError("the runtime cannot be read")

        backend.prepare = unreadable  # type: ignore[method-assign]
    elif damage == "prepare-unexpected":

        def surprise(release: Any) -> Any:
            raise RuntimeError("something nobody expected")

        backend.prepare = surprise  # type: ignore[method-assign]
    elif damage == "slot-unopenable":
        slot = Path(lab.config.gpu_slot_root)
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_text("a file where the slot's directory belongs")
    else:
        (store.job_directory(job.record.job_id) / "input.json").unlink()
    resident = runner(lab, backend)
    report = resident.run_pending()
    assert report.outcomes == {job.record.job_id: "failed"}
    ended = store.load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == code
    assert ended.state.error.detail == FAILURE_DETAILS[code]
    assert not (store.job_directory(job.record.job_id) / "input.json").exists()
    assert not resident.lease.held
    logged = capsys.readouterr().err
    assert job.record.job_id in logged and "Hello there" not in logged


def test_a_job_whose_ending_fails_too_is_recovered_by_the_next_pass(lab: Lab) -> None:
    job = submit(lab)
    broken_release_directory(lab)
    resident = runner(lab)
    original = resident.store.finish
    calls = []

    def finish_fails_twice(*args: Any, **kwargs: Any) -> Job:
        calls.append(args)
        if len(calls) <= 2:  # the job's own ending, then the internal_error ending
            raise OSError("disk full")
        return original(*args, **kwargs)

    resident.store.finish = finish_fails_twice  # type: ignore[method-assign]
    report = resident.run_pending()
    assert report.outcomes == {job.record.job_id: "queued"}  # still claimed, still queued
    later = NOW + timedelta(seconds=lab.config.queue_timeout_seconds)
    report = runner(lab, at=later).run_pending()
    assert report.outcomes == {job.record.job_id: "failed"}


def test_a_cancel_ends_the_wait_for_the_gpu_slot(lab: Lab) -> None:
    job = submit(lab)
    store = store_for(lab)
    holder = GpuSlotHolder(kind="finetune", job_id="finetune-1", started_at=NOW)
    resident = runner(lab)
    polls = []

    def cancel_while_waiting(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 2:
            store.cancel(lab.config.callers[0], job.record.job_id, now=NOW)

    resident.lease.acquire = (  # type: ignore[method-assign]
        lambda holder, **options: GpuSlotLease.acquire(
            resident.lease, holder, sleep=cancel_while_waiting, **options
        )
    )
    with hold_slot(Path(lab.config.gpu_slot_root), holder, timeout_seconds=1):
        report = resident.run_pending()
    assert report.outcomes == {job.record.job_id: "cancelled"}
    assert len(polls) == 2  # it stopped waiting at once, long before the queue limit


# -- review findings: only the pinned subject, and consent checked per sentence ------------


def test_a_release_of_another_subject_than_the_configuration_pins_never_renders(
    tmp_path: Path,
) -> None:
    lab = build_lab(tmp_path)
    job = submit(lab)
    designate_another_subject(lab)  # after the job was accepted
    backend = FakeBackend()
    runner(lab, backend).run_pending()
    ended = store_for(lab).load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == "voice_release_changed" and backend.calls == []


@pytest.mark.parametrize(
    ("change", "code"), [("revoke", "consent_changed"), ("redesignate", "voice_release_changed")]
)
def test_a_revocation_or_a_new_release_stops_a_running_render_at_its_next_sentence(
    lab: Lab, change: str, code: str
) -> None:
    job = submit(lab, THREE)
    release, digest = current_release(lab.layout)

    def change_after_the_first_sentence(index: int) -> None:
        if index != 1:
            return
        if change == "revoke":
            revoke_consent(
                consent_path(lab.layout, release.consent_id),
                expected_sha256=release.consent_sha256,
                at=NOW - timedelta(minutes=1),
            )
        else:
            lab.designate()

    backend = FakeBackend(on_segment=change_after_the_first_sentence)
    runner(lab, backend).run_pending()
    ended = store_for(lab).load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == code
    assert len(backend.calls) == 1 and [s.index for s in ended.state.segments] == [1]
    ledger = SpeechRenderRecord.model_validate_json(
        ledger_path(lab.layout, job.record.job_id).read_bytes()
    )
    assert (ledger.outcome, ledger.failure, len(ledger.segment_sha256)) == ("failed", code, 1)


# -- review findings: a deploy restarts the runner ---------------------------------------------


def test_an_outdated_runner_claims_nothing_and_says_so(lab: Lab) -> None:
    job = submit(lab)
    resident = runner(lab)
    resident.code_check = lambda: "the release moved"
    with pytest.raises(RunnerOutdated):
        resident.run_pending()
    store = store_for(lab)
    assert store.claim_owner(job.record.job_id) is None
    assert (store.queue_root / job.record.job_id).exists()
    with pytest.raises(RunnerOutdated):
        resident.serve(stop=iter([False, True]).__next__, poll_seconds=0)


def test_a_runner_outdated_just_before_its_model_loads_gives_the_job_back(lab: Lab) -> None:
    job = submit(lab)
    answers = iter([None, None, "the release moved"])  # the pass, the claim, the load
    resident = runner(lab)
    resident.code_check = lambda: next(answers)
    with pytest.raises(RunnerOutdated):
        resident.run_pending()
    store = store_for(lab)
    assert store.claim_owner(job.record.job_id) is None
    assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
    assert (store.queue_root / job.record.job_id).exists() and not resident.lease.held


def test_the_code_check_follows_the_release_link(tmp_path: Path) -> None:
    package = Path(cole_voice_lab.__file__).resolve().parent
    link = tmp_path / "current"
    link.symlink_to(package.parent)
    check = release_code_check(link)
    assert check() is None
    link.unlink()
    link.symlink_to(tmp_path)
    assert "not in the active release" in (check() or "")
    # Missing, dangling or not a directory: a restart cannot help.
    for broken in (tmp_path / "pruned", tmp_path / "a-file"):
        link.unlink()
        link.symlink_to(broken)
        problem = check()
        assert isinstance(problem, CodeProblem) and not problem.restartable
    (tmp_path / "a-file").write_text("x")
    assert "not a release directory" in check().reason  # type: ignore[union-attr]
    link.unlink()
    problem = check()
    assert isinstance(problem, CodeProblem) and "does not resolve" in problem.reason


# -- review findings: the text is named by a keyed digest ---------------------------------------


def test_the_text_is_named_by_a_keyed_digest_never_a_plain_hash(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    text = "Yes."
    job = submit(lab, text)
    runner(lab).run_pending()
    store = store_for(lab)
    key_path = store.root / TEXT_KEY_NAME
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600 and key_path.stat().st_size == 32
    key = key_path.read_bytes()
    plain = hashlib.sha256(text.encode()).hexdigest()
    keyed = hmac.new(key, text.encode(), hashlib.sha256).hexdigest()
    assert job.record.text_hmac_sha256 == keyed != plain
    request = (store.job_directory(job.record.job_id) / "request.json").read_text()
    ledger = ledger_path(lab.layout, job.record.job_id).read_text()
    for record in (request, ledger):
        assert plain not in record and keyed in record
    assert not str(key_path).startswith(str(lab.layout.root))  # never in the asset root
    # Another install, another key: the same text has another digest there.
    other = build_lab(tmp_path / "other")
    assert submit(other, text).record.text_hmac_sha256 != keyed
    # A key that is open to others, or of the wrong size, is refused.
    key_path.chmod(0o644)
    refused("internal_error", lambda: SpeechStore(lab.config).text_key())
    key_path.chmod(0o600)
    key_path.write_bytes(b"short")
    refused("internal_error", lambda: SpeechStore(lab.config).text_key())


def test_an_ending_interrupted_after_the_state_leaves_no_text(lab: Lab) -> None:
    job = submit(lab)
    store = store_for(lab)
    store.claim(job.record.job_id, "runner")
    ended = store.finish(job, "cancelled", now=NOW)
    text = store.job_directory(job.record.job_id) / "input.json"
    write_canonical_json(text, {"job_id": job.record.job_id, "text": "left behind"})
    assert ended.state.state == "cancelled"
    store.sweep(now=NOW)
    assert not text.exists()


# -- second review: a busy runner restarts after a deploy, between jobs -------------------------


def test_a_busy_runner_restarts_between_jobs_once_a_deploy_moved_its_code(lab: Lab) -> None:
    backend = FakeBackend()
    deployed = {"now": False}
    checks: list[bool] = []

    def check() -> str | None:
        checks.append(backend.loaded)
        return "the release moved" if deployed["now"] else None

    resident = SpeechRunner(
        lab.config, backend, clock=lambda: NOW, slot_poll_seconds=0.01, code_check=check
    )
    first = submit(lab, "One.")
    assert resident.run_pending(sweep=False).outcomes == {first.record.job_id: "succeeded"}
    assert backend.loaded and resident.lease.held
    # A deploy lands while the model is resident: the next pass checks anyway.
    deployed["now"] = True
    second = submit(lab, "Two.")
    with pytest.raises(RunnerOutdated):
        resident.run_pending(sweep=False)
    assert True in checks  # checked while a model was loaded
    assert not backend.loaded and not resident.lease.held and backend.unloads == 1
    store = store_for(lab)
    assert store.claim_owner(second.record.job_id) is None
    assert [queued.record.job_id for queued in store.queued()] == [second.record.job_id]


def test_a_deploy_mid_job_lets_the_job_finish_and_then_restarts(lab: Lab) -> None:
    deployed = {"now": False}

    def deploy_during_the_first_sentence(index: int) -> None:
        if index == 1:
            deployed["now"] = True

    backend = FakeBackend(on_segment=deploy_during_the_first_sentence)
    resident = SpeechRunner(
        lab.config,
        backend,
        clock=lambda: NOW,
        slot_poll_seconds=0.01,
        code_check=lambda: "the release moved" if deployed["now"] else None,
    )
    rendering = submit(lab, THREE)
    waiting, _ = store_for(lab).submit(
        lab.config.callers[0], SpeechRequest(text="Next."), now=NOW + timedelta(seconds=1)
    )
    with pytest.raises(RunnerOutdated):
        resident.run_pending(sweep=False)
    store = store_for(lab)
    ended = store.load(rendering.record.job_id)
    # Never mid-sentence, never mid-job: every sentence rendered and the job succeeded.
    assert ended is not None and ended.state.state == "succeeded"
    assert len(backend.calls) == 3 and ledger_path(lab.layout, rendering.record.job_id).exists()
    assert store.claim_owner(waiting.record.job_id) is None
    assert [queued.record.job_id for queued in store.queued()] == [waiting.record.job_id]
    assert not backend.loaded and not resident.lease.held


def test_serve_restarts_with_a_model_loaded(lab: Lab) -> None:
    backend = FakeBackend()
    deployed = {"now": False}
    resident = SpeechRunner(
        lab.config,
        backend,
        clock=lambda: NOW,
        slot_poll_seconds=0.01,
        code_check=lambda: "the release moved" if deployed["now"] else None,
    )
    job = submit(lab, "One.")

    def stop() -> bool:
        if backend.loaded:
            deployed["now"] = True  # deployed while the model is resident
        return False

    with pytest.raises(RunnerOutdated):
        resident.serve(stop=stop, poll_seconds=0)
    ended = store_for(lab).load(job.record.job_id)
    assert ended is not None and ended.state.state == "succeeded"
    assert not backend.loaded and not resident.lease.held
    # The instance lock went with it: a pass by hand (of the new code) may run now.
    assert SpeechRunner(lab.config, FakeBackend(), clock=lambda: NOW).run_once().outcomes == {}


# -- second review: a second runner instance never stalls the queue -----------------------------


def test_a_pass_by_hand_is_refused_while_the_runner_unit_runs(lab: Lab) -> None:
    job = submit(lab)
    service = SpeechRunner(lab.config, FakeBackend(), clock=lambda: NOW)
    held = service._instance_lock()  # the unit, between passes
    assert held is not None
    by_hand = SpeechRunner(lab.config, FakeBackend(), clock=lambda: NOW, slot_poll_seconds=0.01)
    try:
        with pytest.raises(RunnerBusy, match="another speech runner is running"):
            by_hand.run_once()
        store = store_for(lab)
        assert store.claim_owner(job.record.job_id) is None and not by_hand.lease.held
        # A unit that starts while a pass by hand runs waits for it (here: until stopped).
        stops = iter([False, True])
        late = SpeechRunner(lab.config, FakeBackend(), clock=lambda: NOW)
        late.serve(stop=lambda: next(stops), poll_seconds=0)
        assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
    finally:
        os.close(held)
    assert by_hand.run_once().outcomes == {job.record.job_id: "succeeded"}
    assert not by_hand.lease.held and not by_hand.backend.loaded


# -- second review: recovery honours a cancel ---------------------------------------------------


def test_a_running_job_with_a_cancel_recovers_as_cancelled(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    job = submit(lab)
    store = store_for(lab)
    assert store.claim(job.record.job_id, "runner")
    store.mark_running(job, now=NOW, segment_count=1)
    store.cancel(lab.config.callers[0], job.record.job_id, now=NOW)  # the marker only
    report = runner(lab).run_pending(sweep=False)
    ended = store.load(job.record.job_id)
    assert report.outcomes == {job.record.job_id: "cancelled"}
    assert ended is not None and ended.state.state == "cancelled" and ended.state.error is None
    assert "stopped while it rendered; cancelled" in capsys.readouterr().err


# -- second review: nothing is served without its ledger entry ----------------------------------


def test_a_ledger_failure_deletes_the_segments_too(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = submit(lab, THREE)
    resident = runner(lab)

    def no_ledger(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the ledger's directory is gone")

    monkeypatch.setattr(resident, "_ledger", no_ledger)
    assert resident.run_pending(sweep=False).outcomes == {job.record.job_id: "failed"}
    store = store_for(lab)
    ended = store.load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == "internal_error"
    assert ended.state.segments == () and ended.state.audio is None
    assert ended.state.audio_expires_at is None and ended.state.segment_count == 3
    directory = store.job_directory(job.record.job_id)
    assert (
        not (directory / "segments").exists() and not store.audio_path(job.record.job_id).exists()
    )
    refused("not_found", lambda: open_segment(store, ended, 1))
    refused("audio_not_ready", lambda: open_audio(store, ended))
    assert "the ledger entry could not be written" in capsys.readouterr().err


def test_withdrawing_audio_deletes_the_files_even_when_the_state_cannot_be_written(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = submit(lab, THREE)
    store = store_for(lab)
    runner(lab).run_pending(sweep=False)
    done = store.load(job.record.job_id)
    assert done is not None and done.state.segments

    def no_state(_state: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_state", no_state)
    with pytest.raises(OSError, match="disk full"):
        store.withdraw_audio(done)
    directory = store.job_directory(job.record.job_id)
    assert (
        not (directory / "segments").exists() and not store.audio_path(job.record.job_id).exists()
    )


# -- second and third review: the text key's failures are logged and repairable -----------------


def _key_logs(lab: Lab) -> tuple[SpeechStore, list[str]]:
    logged: list[str] = []
    return SpeechStore(lab.config, log=logged.append), logged


def test_an_unusable_text_key_is_logged_with_its_reason_and_left_for_the_operator(
    lab: Lab,
) -> None:
    submit(lab)  # a job exists
    store = store_for(lab)
    key = store.root / TEXT_KEY_NAME
    key.unlink()
    key.write_bytes(b"")
    key.chmod(0o600)
    fresh, logged = _key_logs(lab)
    fresh.ensure()
    fresh.ensure()  # the same problem is logged once
    assert len(logged) == 1 and "is 0 bytes, not 32 bytes" in logged[0]
    assert "deploy/speech/README.md" in logged[0]
    assert key.read_bytes() == b""  # never replaced here
    for _ in range(3):  # every request refuses; the log says it once per change
        refusal = refused(
            "internal_error",
            lambda: fresh.submit(lab.config.callers[0], SpeechRequest(text="Hi."), now=NOW),
        )
        assert refusal.status == 500 and "bytes" not in refusal.detail
    assert len(logged) == 1
    # Other problems, each named.
    key.write_bytes(b"x" * 40)
    assert "is longer, not 32 bytes" in _key_problem(lab)
    key.write_bytes(bytes(32))
    key.chmod(0o640)
    assert "open to group or others (mode 0640" in _key_problem(lab)
    key.unlink()
    key.symlink_to(store.root / "elsewhere")
    assert "not a regular file" in _key_problem(lab)
    key.unlink()
    key.mkdir()
    assert "not a regular file" in _key_problem(lab)
    key.rmdir()
    # Missing once a job was named by it: reported, not made again.
    assert "is missing and is not made again (1 job record(s) and 0 ledger" in _key_problem(lab)
    assert not key.exists()


def _key_problem(lab: Lab) -> str:
    store, logged = _key_logs(lab)
    store.ensure()
    assert len(logged) == 1
    return logged[0]


def test_a_missing_key_is_made_only_on_a_new_install(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    store, logged = _key_logs(lab)
    store.ensure()
    assert len(store.text_key()) == 32 and logged == []


def test_a_lost_key_is_not_made_again_while_the_ledger_remembers_it(lab: Lab) -> None:
    job = submit(lab)
    runner(lab).run_pending()
    store = store_for(lab)
    store.sweep(now=NOW + timedelta(days=60))  # the job record is gone; the ledger stays
    assert store.load(job.record.job_id) is None
    assert ledger_path(lab.layout, job.record.job_id).exists()
    (store.root / TEXT_KEY_NAME).unlink()
    assert "0 job record(s) and 1 ledger entr(ies)" in _key_problem(lab)
    assert not (store.root / TEXT_KEY_NAME).exists()
    fresh, logged = _key_logs(lab)
    refused("internal_error", fresh.text_key)
    assert "is missing and is not made again" in logged[0]


def test_the_operator_makes_a_new_key_by_hand(lab: Lab) -> None:
    submit(lab)
    store = store_for(lab)
    key = store.root / TEXT_KEY_NAME
    with pytest.raises(TextKeyError, match="exists: move it aside"):
        store.make_text_key()
    key.unlink()
    maker, logged = _key_logs(lab)
    assert maker.make_text_key() == (1, 0)
    assert len(key.read_bytes()) == 32 and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert "made by hand" in logged[0] and "1 job record(s)" in logged[0]


def test_the_api_notices_a_key_that_vanished_or_changed(lab: Lab) -> None:
    store, logged = _key_logs(lab)
    store.ensure()
    first = store.text_key()
    key = store.root / TEXT_KEY_NAME
    key.unlink()
    # Never signed with a key that is gone: refused, with the reason logged once.
    refused("internal_error", store.text_key)
    refused("internal_error", store.text_key)
    assert len(logged) == 1 and "is missing" in logged[0]
    # A replaced key is noticed and used from then on.
    key.write_bytes(bytes(range(32)))
    key.chmod(0o600)
    assert store.text_key() == bytes(range(32)) != first
    assert logged[1] == "the text key is usable again"
    # Swapped in place while cached: the API reads the new one, and says so.
    replacement = store.root / "replacement"
    replacement.write_bytes(bytes(range(1, 33)))
    replacement.chmod(0o600)
    os.replace(replacement, key)
    assert store.text_key() == bytes(range(1, 33))
    assert "the text key changed on disk" in logged[2] and "bytes" not in logged[2]


def test_an_idempotency_key_made_under_a_lost_text_key_answers_clearly(lab: Lab) -> None:
    first = submit(lab, "Same.", idempotency_key="k-1")
    store = store_for(lab)
    (store.root / TEXT_KEY_NAME).unlink()
    fresh = SpeechStore(lab.config, log=lambda _message: None)
    fresh.make_text_key()  # the operator's repair
    refusal = refused(
        "idempotency_key_unverifiable",
        lambda: fresh.submit(
            lab.config.callers[0], SpeechRequest(text="Same.", idempotency_key="k-1"), now=NOW
        ),
    )
    assert refusal.status == 409 and "new idempotency key" in refusal.detail
    # A new key is answered as usual, and the old job is untouched.
    again, created = fresh.submit(
        lab.config.callers[0], SpeechRequest(text="Same.", idempotency_key="k-2"), now=NOW
    )
    assert created and again.record.job_id != first.record.job_id
    assert fresh.load(first.record.job_id) is not None


def test_an_idempotency_entry_without_a_key_id_is_compared_as_before(lab: Lab) -> None:
    first = submit(lab, "Same.", idempotency_key="k-1")
    store = store_for(lab)
    entries = list((store.idempotency_root / "agent-server").iterdir())
    assert len(entries) == 1
    recorded = json.loads(entries[0].read_text())
    assert recorded["text_key_id"] == store.text_key_id() != store.text_key().hex()
    write_canonical_json(entries[0], {"job_id": first.record.job_id})  # an older entry
    replay, created = store.submit(
        lab.config.callers[0], SpeechRequest(text="Same.", idempotency_key="k-1"), now=NOW
    )
    assert not created and replay.record.job_id == first.record.job_id
    refused(
        "idempotency_conflict",
        lambda: store.submit(
            lab.config.callers[0], SpeechRequest(text="Other.", idempotency_key="k-1"), now=NOW
        ),
    )


# -- third review: a stopped runner withdraws unledgered audio; stop between sentences ----------


def test_a_job_left_running_without_a_ledger_entry_is_withdrawn_at_recovery(lab: Lab) -> None:
    job = submit(lab, THREE)
    killed = runner(lab)

    def killed_mid_render(index: int) -> None:
        if index == 3:
            raise SystemExit("killed")  # nothing after this runs: no ending, no ledger

    killed.backend = FakeBackend(on_segment=killed_mid_render)
    with pytest.raises(SystemExit):
        killed.run_pending(sweep=False)
    store = store_for(lab)
    left = store.load(job.record.job_id)
    assert left is not None and left.state.state == "running" and len(left.state.segments) == 2
    assert store.segment_path(job.record.job_id, 1).exists()  # served during the render
    report = runner(lab).run_pending(sweep=False)
    ended = store.load(job.record.job_id)
    assert report.outcomes == {job.record.job_id: "failed"}
    assert ended is not None and ended.state.segments == () and ended.state.audio is None
    assert not store.segment_path(job.record.job_id, 1).exists()
    refused("not_found", lambda: open_segment(store, ended, 1))


def test_a_job_left_running_with_its_ledger_entry_keeps_its_sentences(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = submit(lab, THREE)
    resident = runner(lab)
    store = resident.store

    def killed_after_the_ledger(*_args: Any, **_kwargs: Any) -> Job:
        raise SystemExit("killed")

    monkeypatch.setattr(store, "finish", killed_after_the_ledger)
    with pytest.raises(SystemExit):
        resident.run_pending(sweep=False)
    assert ledger_path(lab.layout, job.record.job_id).exists()
    runner(lab).run_pending(sweep=False)
    ended = store_for(lab).load(job.record.job_id)
    assert ended is not None and ended.state.state == "failed"
    assert [segment.index for segment in ended.state.segments] == [1, 2, 3]


def test_a_stop_ends_a_render_between_sentences_with_its_ledger_entry(lab: Lab) -> None:
    job = submit(lab, THREE)
    waiting, _ = store_for(lab).submit(
        lab.config.callers[0], SpeechRequest(text="Next."), now=NOW + timedelta(seconds=1)
    )
    stopping = {"now": False}

    def sigterm_during_the_first_sentence(index: int) -> None:
        if index == 1:
            stopping["now"] = True

    backend = FakeBackend(on_segment=sigterm_during_the_first_sentence)
    resident = SpeechRunner(lab.config, backend, clock=lambda: NOW, slot_poll_seconds=0.01)
    resident.serve(stop=lambda: stopping["now"], poll_seconds=0)
    store = store_for(lab)
    ended = store.load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == "internal_error" and len(backend.calls) == 1
    assert [segment.index for segment in ended.state.segments] == [1]
    ledger = SpeechRenderRecord.model_validate_json(
        ledger_path(lab.layout, job.record.job_id).read_bytes()
    )
    assert (ledger.outcome, len(ledger.segment_sha256)) == ("failed", 1)
    # The next job was never claimed, and nothing is held.
    assert [queued.record.job_id for queued in store.queued()] == [waiting.record.job_id]
    assert store.claim_owner(waiting.record.job_id) is None
    assert not resident.lease.held and not backend.loaded


def test_a_stop_while_a_job_waits_for_the_slot_gives_the_job_back(lab: Lab) -> None:
    job = submit(lab)
    stopping = {"now": False}
    resident = SpeechRunner(lab.config, FakeBackend(), clock=lambda: NOW, slot_poll_seconds=0.01)
    polls: list[int] = []

    def stop() -> bool:
        if resident.store.claim_owner(job.record.job_id) is not None:
            polls.append(1)
            stopping["now"] = len(polls) > 2
        return stopping["now"]

    holder = GpuSlotHolder(kind="finetune", job_id="ft-1", started_at=NOW)
    with hold_slot(Path(lab.config.gpu_slot_root), holder, timeout_seconds=1):
        resident.serve(stop=stop, poll_seconds=0)
    store = store_for(lab)
    assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
    assert store.claim_owner(job.record.job_id) is None and not resident.lease.held


def test_a_stop_right_after_the_slot_gives_the_job_back(lab: Lab) -> None:
    job = submit(lab)
    resident = SpeechRunner(lab.config, FakeBackend(), clock=lambda: NOW, slot_poll_seconds=0.01)
    resident._stopping = lambda: resident.lease.held
    resident.run_pending(sweep=False)
    store = store_for(lab)
    assert store.claim_owner(job.record.job_id) is None
    assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
    resident.unload()


# -- third review: a broken release link is not restarted every ten seconds ---------------------


def test_a_missing_release_link_stops_the_runner_without_a_restart(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    job = submit(lab)
    backend = FakeBackend()
    broken = {"now": False}
    resident = SpeechRunner(
        lab.config,
        backend,
        clock=lambda: NOW,
        slot_poll_seconds=0.01,
        code_check=lambda: (
            CodeProblem("current does not resolve", restartable=False) if broken["now"] else None
        ),
    )
    assert resident.run_pending(sweep=False).outcomes == {job.record.job_id: "succeeded"}
    broken["now"] = True
    later = submit(lab, "Later.")
    with pytest.raises(RunnerMisconfigured):
        resident.run_pending(sweep=False)
    assert "stopping, not restarting" in capsys.readouterr().err
    assert not backend.loaded and not resident.lease.held
    assert store_for(lab).claim_owner(later.record.job_id) is None


def test_serve_says_once_that_it_waits_for_a_pass_by_hand(lab: Lab) -> None:
    held = SpeechRunner(lab.config, FakeBackend())._instance_lock()
    assert held is not None
    logged: list[str] = []
    try:
        late = SpeechRunner(lab.config, FakeBackend(), clock=lambda: NOW, log=logged.append)
        stops = iter([False, False, False, True])
        late.serve(stop=lambda: next(stops), poll_seconds=0)
    finally:
        os.close(held)
    assert logged == ["waiting for a pass by hand (speech run --once) to end"]
