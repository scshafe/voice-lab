"""Speech jobs on disk and the job runner (speech/store.py, speech/runner.py, speech/gpu.py).

The backend is the fake of ``speech_fixtures`` (a generated tone); nothing here starts a model.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from speech_fixtures import NOW, FakeBackend, Lab, build_lab, caller, write_tone

from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.consent import revoke_consent
from cole_voice_lab.speech.backend import SpeechBackendError
from cole_voice_lab.speech.contracts import (
    GpuSlotHolder,
    SpeechJobState,
    SpeechRenderRecord,
    SpeechRequest,
    WebhookOutboxEntry,
)
from cole_voice_lab.speech.gpu import (
    GpuSlotTimeout,
    busy_with_long_job,
    current_holder,
    hold_slot,
    speech_waiting,
)
from cole_voice_lab.speech.release import consent_path, current_release
from cole_voice_lab.speech.runner import SpeechRunner, check_wav, ledger_path
from cole_voice_lab.speech.store import Job, SpeechRefusal, SpeechStore, open_audio, to_status


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
    return SpeechRunner(
        lab.config, backend or FakeBackend(), clock=lambda: at, slot_poll_seconds=0.01
    )


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


def test_a_job_renders_writes_its_ledger_and_queues_its_webhook(lab: Lab) -> None:
    hook = f"{lab.config.callers[0].webhook_urls[0]}/turn-7"
    job = submit(lab, "Hello there.", callback_url=hook, client_ref="turn-7")
    job_id = job.record.job_id
    backend = FakeBackend()
    report = runner(lab, backend).run_pending()
    assert report.outcomes == {job_id: "succeeded"}
    store = store_for(lab)
    ended = store.load(job_id)
    assert ended is not None and ended.state.state == "succeeded"
    audio = store.audio_path(job_id)
    assert stat.S_IMODE(audio.stat().st_mode) == 0o600
    assert ended.state.audio is not None
    assert ended.state.audio.sha256 == sha256_file(audio)[0]
    assert ended.state.audio_expires_at == NOW + timedelta(days=7)
    assert not (store.job_directory(job_id) / "input.json").exists()
    assert not (store.queue_root / job_id).exists()
    release, release_digest = current_release(lab.layout)
    assert backend.calls[0].release.release == release
    ledger = SpeechRenderRecord.model_validate_json(ledger_path(lab.layout, job_id).read_bytes())
    assert stat.S_IMODE(ledger_path(lab.layout, job_id).stat().st_mode) == 0o400
    assert (ledger.outcome, ledger.caller_id, ledger.text_characters) == (
        "succeeded",
        "agent-server",
        12,
    )
    assert (
        ledger.release.release_id == "vr-0001" and ledger.release.release_sha256 == release_digest
    )
    assert ledger.consent_sha256 == lab.consent_sha256
    assert ledger.output == ended.state.audio and ledger.audio_ms == 100
    assert "Hello there" not in ledger_path(lab.layout, job_id).read_text()
    entry = WebhookOutboxEntry.model_validate_json(
        (store.outbox_root / f"msg_{job_id}.json").read_bytes()
    )
    assert (entry.url, entry.event.type, entry.event.data.client_ref) == (
        hook,
        "speech.succeeded",
        "turn-7",
    )
    assert entry.event.data.audio is not None
    assert "Hello there" not in (store.outbox_root / f"msg_{job_id}.json").read_text()
    stream, size = open_audio(store, ended)
    with stream:
        assert size == ended.state.audio.bytes


@pytest.mark.parametrize(
    ("fail", "outcome", "code"),
    [
        ("runtime_unavailable", "failed", "runtime_unavailable"),
        ("render_timeout", "failed", "render_timeout"),
        ("render_failed", "failed", "render_failed"),
        ("cancelled", "cancelled", None),
    ],
)
def test_a_render_that_fails_is_ended_with_its_code_and_still_in_the_ledger(
    lab: Lab, fail: str, outcome: str, code: str | None
) -> None:
    job = submit(lab, callback_url=lab.config.callers[0].webhook_urls[0])
    report = runner(lab, FakeBackend(fail=fail)).run_pending()
    job_id = job.record.job_id
    assert report.outcomes == {job_id: outcome}
    store = store_for(lab)
    ended = store.load(job_id)
    assert ended is not None
    assert (ended.state.error.code if ended.state.error else None) == code
    assert not store.audio_path(job_id).exists()
    ledger = SpeechRenderRecord.model_validate_json(ledger_path(lab.layout, job_id).read_bytes())
    assert (ledger.outcome, ledger.failure, ledger.output) == (outcome, code, None)
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


def test_cancel_ends_a_queued_job_at_once_and_a_running_one_through_the_runner(lab: Lab) -> None:
    store = store_for(lab)
    registered = lab.config.callers[0]
    queued = submit(lab, callback_url=registered.webhook_urls[0])
    cancelled = store.cancel(registered, queued.record.job_id, now=NOW)
    assert cancelled.state.state == "cancelled"
    assert (store.outbox_root / f"msg_{queued.record.job_id}.json").exists()
    assert runner(lab).run_pending().outcomes == {}  # the runner cannot claim it
    refused("already_finished", lambda: store.cancel(registered, queued.record.job_id, now=NOW))

    running = submit(lab, "Second.")
    backend = FakeBackend(wait_for_cancel=True)
    pass_thread = threading.Thread(target=runner(lab, backend).run_pending)
    pass_thread.start()
    for _ in range(500):
        state = store.load(running.record.job_id)
        if state is not None and state.state.state == "running":
            break
        threading.Event().wait(0.01)
    during = store.cancel(registered, running.record.job_id, now=NOW)
    assert during.state.state == "running"  # the runner owns it; the cancel is a marker
    assert store.cancel(registered, running.record.job_id, now=NOW).state.state in {
        "running",
        "cancelled",
    }
    pass_thread.join(10)
    ended = store.load(running.record.job_id)
    assert ended is not None and ended.state.state == "cancelled"


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
        (lab.layout.root / release.checkpoint_path).write_bytes(b"retrained")
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


def test_a_stale_job_times_out_and_an_orphan_fails(lab: Lab) -> None:
    stale = submit(lab)
    report = runner(lab, at=NOW + timedelta(minutes=11)).run_pending()
    assert report.outcomes == {stale.record.job_id: "failed"}
    orphan = submit(lab, "Orphan.")
    store = store_for(lab)
    store.claim(orphan.record.job_id, "runner")
    store.mark_running(orphan, now=NOW)
    report = runner(lab).run_pending()
    ended = store.load(orphan.record.job_id)
    assert report.outcomes == {orphan.record.job_id: "failed"}
    assert ended is not None and ended.state.error is not None
    assert "stopped while it rendered" in ended.state.error.detail


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
