"""The resident F5-TTS backend (speech/backend.py, speech/resident.py, resources/f5_resident.py).

The runtime is the stub of ``finetune_stub_runtime`` (its pinned files and probe), and the
resident worker is a stand-in that speaks ``f5_resident.py``'s protocol and writes a tone
shaped by each segment's seed, run by this interpreter: no torch, no weights, no GPU. The real
``f5_resident.py`` is checked statically.
"""

from __future__ import annotations

import ast
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from finetune_stub_runtime import stub_finetune_runtime
from speech_fixtures import NOW, Lab, OnePass, build_lab, finished_run

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.finetune import FineTuneDigestCache, FineTunePretrainedPins
from cole_voice_lab.resources import resource_path
from cole_voice_lab.speech.backend import SpeechBackendError
from cole_voice_lab.speech.config import F5SpeechRuntime
from cole_voice_lab.speech.contracts import SpeechRenderRecord, SpeechRequest
from cole_voice_lab.speech.f5 import F5ResidentBackend
from cole_voice_lab.speech.release import current_release, verify_release
from cole_voice_lab.speech.runner import check_wav, ledger_path
from cole_voice_lab.speech.store import SpeechStore

STAND_IN = r"""
import json, math, os, struct, sys, time, wave
from pathlib import Path

MODE = json.loads(Path(sys.argv[0]).with_name("mode.json").read_text())
protocol = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)
load = json.loads(sys.stdin.readline())
Path(sys.argv[0]).with_name("load.json").write_text(json.dumps(
    {"load": load, "env": {k: os.environ.get(k) for k in
     ("HIP_VISIBLE_DEVICES", "MIOPEN_USER_DB_PATH", "HF_HUB_OFFLINE", "XDG_CACHE_HOME")}}))
work = Path(load["work_directory"]); work.mkdir(mode=0o700)
(work / "reference-copy.wav").write_bytes(b"converted reference")
if MODE.get("fail_load"):
    protocol.write(json.dumps({"event": "failed", "error": "no GPU"}) + "\n"); protocol.flush()
    sys.exit(1)
if MODE.get("exit_on_load"):
    sys.exit(1)
protocol.write(json.dumps({"event": "ready", "f5_tts_version": "1.1.22-standin",
    "torch_version": "2.14.0+rocm7.2", "model_dtype": "torch.float16"}) + "\n"); protocol.flush()
print("upstream chatter on what used to be stdout")
for line in sys.stdin:
    request = json.loads(line)
    if request["op"] == "quit":
        break
    if MODE.get("sleep"):
        time.sleep(MODE["sleep"])
    if MODE.get("garbage"):
        protocol.write("not json\n"); protocol.flush(); continue
    if request["text"] == MODE.get("fail_text"):
        protocol.write(json.dumps({"event": "failed", "id": request["id"], "error": "X"}) + "\n")
        protocol.flush(); continue
    frames = 240 + len(request["text"])
    period = 20 + request["seed"] % 40
    with wave.open(request["output_path"], "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
        handle.writeframes(b"".join(struct.pack("<h", round(8000 * math.sin(i * 6.283 / period)))
                                    for i in range(frames)))
    os.chmod(request["output_path"], 0o600)
    protocol.write(json.dumps({"event": "rendered", "id": request["id"], "frames": frames}) + "\n")
    protocol.flush()
"""


@dataclass
class Rig:
    lab: Lab
    runtime: Path
    pin: FineTunePretrainedPins
    script: Path

    def mode(self, **mode: Any) -> None:
        self.script.with_name("mode.json").write_text(json.dumps(mode))

    def backend(self, **options: Any) -> F5ResidentBackend:
        options.setdefault("pretrained_pin", self.pin)
        options.setdefault("python", sys.executable)
        options.setdefault("resident_script", self.script)
        return F5ResidentBackend(
            self.lab.config.runtime,
            state_root=Path(self.lab.config.state_root),
            asset_root=self.lab.layout.root,
            **options,
        )

    def verified(self) -> Any:
        release, digest = current_release(self.lab.layout)
        return verify_release(self.lab.layout, release, digest, digests=FineTuneDigestCache())

    @property
    def loaded(self) -> dict[str, Any]:
        return json.loads(self.script.with_name("load.json").read_text())


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    runtime, pin = stub_finetune_runtime(tmp_path.resolve() / "runtime")
    lab = build_lab(
        tmp_path,
        runtime=F5SpeechRuntime(
            runtime_root=str(runtime), device="cpu", nfe_steps=16, visible_devices="0"
        ),
    )
    script = tmp_path.resolve() / "stand-in" / "resident.py"
    script.parent.mkdir()
    script.write_text(STAND_IN)
    made = Rig(lab, runtime, pin, script)
    made.mode()
    yield made


def failure(code: str, call: Any) -> SpeechBackendError:
    with pytest.raises(SpeechBackendError) as raised:
        call()
    assert raised.value.code == code, raised.value
    return raised.value


def test_the_model_is_loaded_once_and_renders_each_segment(rig: Rig) -> None:
    backend = rig.backend()
    verified = rig.verified()
    fingerprint = backend.prepare(verified)
    assert backend.loaded
    release = verified.release
    assert {item.role: item.sha256 for item in fingerprint.files} == {
        "checkpoint": release.file("checkpoint").sha256,
        "reference": release.file("reference").sha256,
    }
    assert fingerprint.runtime["model_dtype"] == "torch.float16"
    assert (fingerprint.backend, fingerprint.device, fingerprint.settings.nfe_steps) == (
        "f5-tts-resident",
        "cpu",
        16,
    )
    assert (fingerprint.model_family, fingerprint.native_sample_rate_hz, fingerprint.seeded) == (
        "f5-tts",
        24_000,
        True,
    )
    load = rig.loaded["load"]
    root = rig.lab.layout.root
    assert load["checkpoint_path"] == str(root / release.file("checkpoint").path)
    assert load["reference_audio_path"] == str(root / release.file("reference").path)
    assert load["vocoder_path"].endswith("pretrained/vocos-mel-24khz") and load["use_ema"] is True
    assert load["reference_text"] == verified.family_state.reference.text
    env = rig.loaded["env"]
    assert env["HIP_VISIBLE_DEVICES"] == "0" and env["HF_HUB_OFFLINE"] == "1"
    assert env["MIOPEN_USER_DB_PATH"].startswith(rig.lab.config.state_root)
    scratch = Path(rig.lab.config.state_root) / "resident"
    assert (scratch / "reference-copy.wav").exists()
    output = rig.lab.root / "one.wav"
    backend.render_segment("Hello there.", seed=7, output=output, deadline_seconds=10)
    assert check_wav(output).frames == 252
    again = rig.lab.root / "again.wav"
    backend.render_segment("Hello there.", seed=7, output=again, deadline_seconds=10)
    assert sha256_file(again)[0] == sha256_file(output)[0]
    process = backend._process
    assert backend.prepare(verified) == fingerprint and backend._process is process
    backend.unload()
    assert not backend.loaded and not scratch.exists()
    backend.unload()


def test_without_a_runtime_nothing_starts(rig: Rig, tmp_path: Path) -> None:
    verified = rig.verified()
    state = Path(rig.lab.config.state_root)
    failure(
        "runtime_unavailable",
        lambda: F5ResidentBackend(None, state_root=state, asset_root=rig.lab.layout.root).prepare(
            verified
        ),
    )
    missing = F5SpeechRuntime(runtime_root=str(tmp_path / "no-runtime"))
    failure(
        "runtime_unavailable",
        lambda: F5ResidentBackend(
            missing, state_root=state, asset_root=rig.lab.layout.root
        ).prepare(verified),
    )
    failure("runtime_unavailable", lambda: rig.backend(pretrained_pin=None).prepare(verified))
    failure(
        "runtime_unavailable",
        lambda: rig.backend(python=str(tmp_path / "no-python")).prepare(verified),
    )
    failure(
        "render_failed",
        lambda: rig.backend().render_segment(
            "x", seed=1, output=tmp_path / "x.wav", deadline_seconds=1
        ),
    )


@pytest.mark.parametrize("mode", [{"fail_load": True}, {"exit_on_load": True}])
def test_a_worker_that_cannot_load_is_runtime_unavailable(rig: Rig, mode: dict[str, Any]) -> None:
    rig.mode(**mode)
    backend = rig.backend()
    failure("runtime_unavailable", lambda: backend.prepare(rig.verified()))
    assert not backend.loaded
    assert not (Path(rig.lab.config.state_root) / "resident").exists()


def test_a_slow_segment_is_killed_and_unloads_the_model(rig: Rig) -> None:
    rig.mode(sleep=30)
    backend = rig.backend()
    backend.prepare(rig.verified())
    started = time.monotonic()
    failure(
        "render_timeout",
        lambda: backend.render_segment(
            "Slow.", seed=1, output=rig.lab.root / "slow.wav", deadline_seconds=0.5
        ),
    )
    assert time.monotonic() - started < 10 and not backend.loaded


def test_a_failed_segment_keeps_the_model_and_nonsense_unloads_it(rig: Rig) -> None:
    rig.mode(fail_text="Bad sentence.")
    backend = rig.backend()
    backend.prepare(rig.verified())
    failure(
        "render_failed",
        lambda: backend.render_segment(
            "Bad sentence.", seed=1, output=rig.lab.root / "bad.wav", deadline_seconds=10
        ),
    )
    assert backend.loaded
    backend.unload()
    rig.mode(garbage=True)
    backend.prepare(rig.verified())
    failure(
        "render_failed",
        lambda: backend.render_segment(
            "Any.", seed=1, output=rig.lab.root / "any.wav", deadline_seconds=10
        ),
    )
    assert not backend.loaded


def test_a_new_release_restarts_the_worker(rig: Rig) -> None:
    backend = rig.backend()
    first = backend.prepare(rig.verified())
    process = backend._process
    rig.lab.designate()  # the same run again: the same files, so no restart
    assert backend.prepare(rig.verified()) == first and backend._process is process
    finished_run(rig.lab.layout, "finetune-b")
    rig.lab.designate(run_id="finetune-b")
    third = backend.prepare(rig.verified())
    assert backend._process is not process and third.files != first.files
    backend.unload()


def test_a_job_runs_end_to_end_through_the_resident_worker(rig: Rig) -> None:
    lab = rig.lab
    store = SpeechStore(lab.config)
    store.ensure()
    text = "The first sentence is here. A second one follows it!"
    job, _ = store.submit(lab.config.callers[0], SpeechRequest(text=text), now=NOW)
    report = OnePass(lab.config, rig.backend(), clock=lambda: NOW).run_pending()
    assert report.outcomes == {job.record.job_id: "succeeded"}
    ended = store.load(job.record.job_id)
    assert ended is not None and ended.state.segment_count == 2
    ledger = SpeechRenderRecord.model_validate_json(
        ledger_path(lab.layout, job.record.job_id).read_bytes()
    )
    assert ledger.fingerprint is not None and ledger.fingerprint.settings.nfe_steps == 16
    assert ledger.fingerprint.runtime["torch_version"] == "2.14.0+rocm7.2"


def test_the_real_resident_worker_renders_sequentially_and_offline() -> None:
    source = resource_path("f5_resident.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "infer_process" not in calls  # upstream's thread pool shares the RNG between chunks
    assert {"infer_batch_process", "seed_everything", "load_vocoder"} <= calls
    batch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "infer_batch_process"
    )
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in batch.keywords}
    assert keywords["streaming"] == "True"
    assert ast.unparse(batch.args[2]) == "[request['text']]"  # one segment per pass
    vocoder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "load_vocoder"
    )
    vocoder_keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in vocoder.keywords}
    assert vocoder_keywords["is_local"] == "True"
    assert "HF_HUB_OFFLINE" in source and "attn" not in source.lower()
