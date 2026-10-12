"""The F5-TTS backend and its worker subprocess (speech/backend.py, speech/worker.py).

The runtime is the stub of ``finetune_stub_runtime`` (a script that writes a generated clip),
so the real process chain runs (backend, worker, Speak seam, runtime) without torch, weights
or a GPU; its few-byte pretrained files are accepted through the ``pretrained_pin`` seam.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from finetune_stub_runtime import stub_finetune_runtime, write_stub_mode
from speech_fixtures import NOW, Lab, build_lab

from cole_voice_lab.finetune import FineTuneDigestCache, FineTuneError, FineTunePretrainedPins
from cole_voice_lab.speech import worker
from cole_voice_lab.speech.backend import (
    F5SpeechBackend,
    RenderRequest,
    SpeechBackendError,
    SpeechWorkerRequest,
)
from cole_voice_lab.speech.config import SpeechRuntime
from cole_voice_lab.speech.contracts import SpeechRequest
from cole_voice_lab.speech.release import current_release, verify_release
from cole_voice_lab.speech.runner import SpeechRunner, check_wav
from cole_voice_lab.speech.store import SpeechStore


@dataclass
class Stub:
    lab: Lab
    runtime: Path
    pin: FineTunePretrainedPins

    def backend(self, **options: Any) -> F5SpeechBackend:
        options.setdefault("pretrained_pin", self.pin)
        options.setdefault("poll_seconds", 0.02)
        return F5SpeechBackend(self.lab.config.runtime, **options)

    def request(self, text: str = "Hello there.", deadline: float = 30.0) -> RenderRequest:
        store = SpeechStore(self.lab.config)
        store.ensure()
        job, _ = store.submit(self.lab.config.callers[0], SpeechRequest(text=text), now=NOW)
        release, digest = current_release(self.lab.layout)
        verified = verify_release(self.lab.layout, release, digest, digests=FineTuneDigestCache())
        job_id = job.record.job_id
        return RenderRequest(
            job_id=job_id,
            job_directory=store.job_directory(job_id),
            asset_root=self.lab.layout.root,
            release=verified,
            output=store.partial_audio_path(job_id),
            deadline_seconds=deadline,
        )


@pytest.fixture
def stub(tmp_path: Path) -> Iterator[Stub]:
    runtime, pin = stub_finetune_runtime(tmp_path.resolve() / "runtime")
    lab = build_lab(
        tmp_path,
        runtime=SpeechRuntime(runtime_root=str(runtime), device="cpu", nfe_steps=8),
    )
    yield Stub(lab, runtime, pin)


def failure(code: str, call: Any) -> SpeechBackendError:
    with pytest.raises(SpeechBackendError) as raised:
        call()
    assert raised.value.code == code, raised.value
    return raised.value


def test_the_backend_renders_through_the_speak_seam_in_its_own_process(stub: Stub) -> None:
    trusted = stub.lab.root / "trusted.json"
    request = stub.request()
    fingerprint = stub.backend(trusted_digests=trusted).render(request, cancelled=lambda: False)
    audio = check_wav(request.output)
    assert audio.frames == 240
    release = request.release.release
    assert (fingerprint.checkpoint_sha256, fingerprint.reference_sha256) == (
        release.checkpoint_sha256,
        release.reference_sha256,
    )
    assert fingerprint.backend == "f5-tts" and fingerprint.device == "cpu"
    assert fingerprint.runtime["f5_tts_version"] == "1.1.22-stub"
    assert json.loads(trusted.read_text())["files"]
    directory = request.job_directory
    assert not (directory / "worker-request.json").exists()
    assert not (directory / "worker-result.json").exists()
    speech = stub.lab.layout.root / "derived/speech"
    assert not any(speech.iterdir())  # the Speak directory is removed after the copy
    again = stub.request("Again.")
    stub.backend(trusted_digests=trusted).render(again, cancelled=lambda: False)


def test_without_a_runtime_the_job_fails_runtime_unavailable(stub: Stub, tmp_path: Path) -> None:
    request = stub.request()
    failure(
        "runtime_unavailable",
        lambda: F5SpeechBackend(None).render(request, cancelled=lambda: False),
    )
    missing = SpeechRuntime(runtime_root=str(tmp_path / "no-runtime"))
    failure(
        "runtime_unavailable",
        lambda: F5SpeechBackend(missing).render(request, cancelled=lambda: False),
    )
    failure(
        "runtime_unavailable",
        lambda: stub.backend(python=str(tmp_path / "no-python")).render(
            request, cancelled=lambda: False
        ),
    )
    lab = stub.lab
    store = SpeechStore(lab.config)
    job, _ = store.submit(lab.config.callers[0], SpeechRequest(text="Hi."), now=NOW)
    SpeechRunner(lab.config, F5SpeechBackend(None), clock=lambda: NOW).run_pending()
    ended = store.load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code == "runtime_unavailable"


def test_cancel_and_the_deadline_kill_the_worker_group(stub: Stub) -> None:
    write_stub_mode(stub.runtime, {"synthesis_seconds": 30})
    started = time.monotonic()
    calls = iter([False, False, True])
    failure(
        "cancelled",
        lambda: stub.backend().render(stub.request(), cancelled=lambda: next(calls, True)),
    )
    failure(
        "render_timeout",
        lambda: stub.backend().render(stub.request(deadline=0.5), cancelled=lambda: False),
    )
    assert time.monotonic() - started < 20
    # The killed worker's Speak directory (its job file holds the text) is removed.
    assert not any((stub.lab.layout.root / "derived/speech").iterdir())


def test_a_failing_or_silent_worker_fails_the_render(stub: Stub, tmp_path: Path) -> None:
    write_stub_mode(stub.runtime, {"fail_synthesis": True})
    error = failure(
        "render_failed", lambda: stub.backend().render(stub.request(), cancelled=lambda: False)
    )
    assert "synthesis" in error.detail
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\nexit 0\n")
    silent.chmod(0o700)
    failure(
        "render_failed",
        lambda: stub.backend(python=str(silent)).render(stub.request(), cancelled=lambda: False),
    )


def worker_request(stub: Stub, **changes: Any) -> SpeechWorkerRequest:
    request = stub.request()
    release = request.release.release
    fields: dict[str, Any] = {
        "job_id": request.job_id,
        "asset_root": str(stub.lab.layout.root),
        "job_directory": str(request.job_directory),
        "output": str(request.output),
        "run_id": release.run_id,
        "reference_prompt_id": release.reference_prompt_id,
        "checkpoint_sha256": release.checkpoint_sha256,
        "reference_sha256": release.reference_sha256,
        "runtime_root": str(stub.runtime),
        "device": "cpu",
        "nfe_steps": 8,
        "pretrained_pin": stub.pin,
        **changes,
    }
    return SpeechWorkerRequest.model_validate(fields)


def test_the_worker_refuses_what_the_release_does_not_name(stub: Stub) -> None:
    base = worker_request(stub)

    def render(**changes: Any) -> Any:
        return worker.render(SpeechWorkerRequest.model_validate({**base.model_dump(), **changes}))

    assert (render(checkpoint_sha256="0" * 64).code, render(run_id="nowhere").code) == (
        "voice_release_changed",
        "voice_release_changed",
    )
    assert render(job_id="sp-" + "0" * 24).code == "internal_error"
    assert render(runtime_root=str(stub.lab.root / "nowhere")).code == "runtime_unavailable"
    ok = render()
    assert ok.ok and ok.fingerprint is not None


def test_the_worker_main_writes_its_result_beside_its_request(stub: Stub) -> None:
    request = worker_request(stub)
    path = Path(request.job_directory) / "worker-request.json"
    path.write_text(request.model_dump_json())
    assert worker.main([str(path)]) == 0
    result = json.loads((path.parent / "worker-result.json").read_text())
    assert result["ok"] is True
    assert worker.main([]) == 2


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("FINETUNE.SOURCE_CHANGED", "voice_release_changed"),
        ("FINETUNE.RUN_ARCHIVED", "voice_release_changed"),
        ("FINETUNE.RUNTIME_UNAVAILABLE", "runtime_unavailable"),
        ("FINETUNE.PRETRAINED_MISMATCH", "runtime_unavailable"),
        ("FINETUNE.RUNTIME_FAILED", "render_failed"),
        ("FINETUNE.UNSUPPORTED_TEXT", "render_failed"),
    ],
)
def test_speak_failures_map_to_stable_speech_codes(code: str, expected: str) -> None:
    assert worker.failure_code(FineTuneError(code, "x")) == expected
