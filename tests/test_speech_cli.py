"""``voice-lab voice release …``, ``voice-lab speech …`` and ``serve --speech-config``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from speech_fixtures import NOW, FakeBackend, Lab, build_lab

from cole_voice_lab.api import server as server_module
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.cli import main
from cole_voice_lab.speech.contracts import SpeechRequest
from cole_voice_lab.speech.routes import PREFIX
from cole_voice_lab.speech.runner import EXIT_MISCONFIGURED, EXIT_OUTDATED, SpeechRunner
from cole_voice_lab.speech.store import TEXT_KEY_NAME, SpeechStore


def write_config(lab: Lab) -> Path:
    path = lab.root / "speech.json"
    path.write_bytes(canonical_json_bytes(lab.config))
    return path


def test_designation_previews_then_writes_and_shows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = build_lab(tmp_path, release=False)
    root = str(lab.layout.root)
    base = [
        "voice",
        "release",
        "designate",
        "--asset-root",
        root,
        "--run",
        "finetune-a",
    ]
    # No consent record is named or needed (D-050): the option is gone.
    with pytest.raises(SystemExit) as refused:
        main([*base, "--designated-by", "Cole", "--consent-id", "cole-self-v1"])
    assert refused.value.code == 2 and "--consent-id" in capsys.readouterr().err
    assert main([*base, "--designated-by", "Cole"]) == 0
    preview = capsys.readouterr().out
    assert '"written": false' in preview and "preview only" in preview
    # Nothing released yet: the base voice speaks, and the finished run is next (D-055).
    assert main(["voice", "release", "show", "--asset-root", root]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["release"]["release_id"], shown["base"]) == ("base", True)
    assert shown["newer_runs"] == ["finetune-a"] and shown["unavailable"] is None
    # With the configuration, it says why the API would refuse (no runtime: no base files).
    config = write_config(lab)
    assert main(["voice", "release", "show", "--asset-root", root, "--config", str(config)]) == 0
    assert "no f5-tts runtime is configured" in json.loads(capsys.readouterr().out)["unavailable"]
    assert main([*base, "--designated-by", "Cole", "--i-designate-this-run"]) == 0
    assert '"written": true' in capsys.readouterr().out
    assert main(["voice", "release", "show", "--asset-root", root, "--config", str(config)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["release"]["release_id"], shown["release"]["designation"]) == ("vr-0001", "owner")
    assert (shown["base"], shown["newer_runs"], shown["unavailable"]) == (False, [], None)
    assert main(["voice", "release", "history", "--asset-root", root]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert main([*base, "--designated-by", "Cole", "--blind-review", "review-1"]) == 2
    assert "go together" in capsys.readouterr().err
    (lab.layout.root / "evaluations/blind-reviews/review-1").mkdir(parents=True)
    assert (
        main(
            [
                *base,
                "--designated-by",
                "Cole",
                "--blind-review",
                "review-1",
                "--score-sheet-sha256",
                "a" * 64,
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_designation_needs_an_asset_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)
    assert main(["voice", "release", "history"]) == 2
    assert "--asset-root" in capsys.readouterr().err


def test_the_host_passes_run_from_the_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = build_lab(tmp_path)
    config = write_config(lab)
    store = SpeechStore(lab.config)
    store.ensure()
    job, _ = store.submit(
        lab.config.callers[0],
        SpeechRequest(text="Hi.", callback_url=lab.config.callers[0].webhook_urls[0]),
        now=NOW,
    )
    assert main(["speech", "check-config", "--config", str(config)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["runtime_configured"] is False and checked["callers"][0]["caller_id"]
    assert "secret" not in json.dumps(checked)
    assert main(["speech", "run", "--config", str(config), "--once"]) == 0
    report = json.loads(capsys.readouterr().out)
    # No runtime on this host: the job fails runtime_unavailable (or queue_timeout, since the
    # fixture's clock is not this one), and the runner does not crash.
    assert report["outcomes"][job.record.job_id] == "failed"
    ended = store.load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code in {"runtime_unavailable", "queue_timeout"}
    assert main(["speech", "deliver", "--config", str(config)]) == 0
    delivered = json.loads(capsys.readouterr().out)
    # No secret file on this host: the attempt waits for it, as for any failed attempt.
    assert delivered[f"msg_{job.record.job_id}"] == "pending"


def test_a_runner_whose_release_link_moved_exits_for_systemd_to_restart_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = build_lab(tmp_path)
    config = write_config(lab)
    store = SpeechStore(lab.config)
    store.ensure()
    job, _ = store.submit(lab.config.callers[0], SpeechRequest(text="Hi."), now=NOW)
    link = tmp_path / "current"
    (tmp_path / "another-release").mkdir()
    link.symlink_to(tmp_path / "another-release")
    argv = ["speech", "run", "--config", str(config), "--release-link", str(link)]
    assert main([*argv, "--once"]) == EXIT_OUTDATED == 75
    assert "is not in the active release" in capsys.readouterr().err
    assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
    # A dangling or missing link is no deploy: 78, which the unit does not restart.
    link.unlink()
    link.symlink_to(tmp_path / "a-pruned-release")
    assert main([*argv, "--once"]) == EXIT_MISCONFIGURED == 78
    assert "stopping, not restarting" in capsys.readouterr().err
    link.unlink()
    assert main([*argv, "--once"]) == 78
    assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
    unit = Path(__file__).resolve().parents[1] / "deploy/host-manual/voice-lab-speech.service"
    assert "RestartPreventExitStatus=78" in unit.read_text()


def test_the_operator_makes_a_new_text_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = build_lab(tmp_path)
    config = write_config(lab)
    store = SpeechStore(lab.config)
    store.ensure()
    store.submit(lab.config.callers[0], SpeechRequest(text="Hi."), now=NOW)
    argv = ["speech", "new-text-key", "--config", str(config)]
    assert main(argv) == 2
    assert "--i-accept-the-lost-key" in capsys.readouterr().err
    assert main([*argv, "--i-accept-the-lost-key"]) == 2  # the key is there
    assert "move it aside" in capsys.readouterr().err
    (store.root / TEXT_KEY_NAME).unlink()
    assert main([*argv, "--i-accept-the-lost-key"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert made == {"made": True, "job_records": 1, "ledger_entries": 0}
    assert len((store.root / TEXT_KEY_NAME).read_bytes()) == 32


def test_serve_adds_the_speech_routes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = build_lab(tmp_path)
    config = write_config(lab)
    served: list[server_module.ApiServer] = []

    def serve_forever(self: server_module.ApiServer, poll_interval: float = 0.5) -> None:
        served.append(self)

    monkeypatch.setattr(server_module.ApiServer, "serve_forever", serve_forever)
    argv = [
        "serve",
        "--asset-root",
        str(lab.layout.root),
        "--jwks",
        str(tmp_path / "jwks.json"),
        "--issuer",
        "https://id.example.invalid",
        "--audience",
        "voice-lab-desktop",
        "--allowed-subject",
        "subject-cole",
        "--server-build",
        "voice-lab:test",
        "--port",
        "0",
        "--speech-config",
        str(config),
    ]
    assert server_module.main(argv) == 0
    assert served[0].self_authenticating == (PREFIX,)
    assert getattr(served[0].routes[0], "prefix", None) == PREFIX
    argv[argv.index("https://id.example.invalid")] = "https://other.example.invalid"
    assert server_module.main(argv) == 1
    assert "identity provider" in capsys.readouterr().err
    argv[-1] = str(tmp_path / "missing.json")
    assert server_module.main(argv) == 1


def test_a_pass_by_hand_is_refused_while_the_runner_unit_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = build_lab(tmp_path)
    config = write_config(lab)
    store = SpeechStore(lab.config)
    store.ensure()
    job, _ = store.submit(lab.config.callers[0], SpeechRequest(text="Hi."), now=NOW)
    held = SpeechRunner(lab.config, FakeBackend())._instance_lock()  # the unit's
    assert held is not None
    try:
        assert main(["speech", "run", "--config", str(config), "--once"]) == 2
        assert "another speech runner is running" in capsys.readouterr().err
        assert [queued.record.job_id for queued in store.queued()] == [job.record.job_id]
        assert store.claim_owner(job.record.job_id) is None
    finally:
        os.close(held)
