"""``voice-lab voice release …``, ``voice-lab speech …`` and ``serve --speech-config``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from speech_fixtures import NOW, Lab, build_lab

from cole_voice_lab.api import server as server_module
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.cli import main
from cole_voice_lab.speech.contracts import SpeechRequest
from cole_voice_lab.speech.routes import PREFIX
from cole_voice_lab.speech.store import SpeechStore


def write_config(lab: Lab) -> Path:
    path = lab.root / "speech.json"
    path.write_bytes(canonical_json_bytes(lab.config))
    return path


def test_designation_previews_then_writes_and_shows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = build_lab(tmp_path, release=False)
    root = str(lab.layout.root)
    base = ["voice", "release", "designate", "--asset-root", root, "--run", "finetune-a"]
    assert main([*base, "--designated-by", "Cole"]) == 0
    preview = capsys.readouterr().out
    assert '"written": false' in preview and "preview only" in preview
    assert main(["voice", "release", "show", "--asset-root", root]) == 2
    assert "no voice release" in capsys.readouterr().err
    assert main([*base, "--designated-by", "Cole", "--i-designate-this-run"]) == 0
    assert '"written": true' in capsys.readouterr().out
    assert main(["voice", "release", "show", "--asset-root", root]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["release"]["release_id"] == "vr-0001"
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
    assert main(["speech", "run", "--config", str(config)]) == 0
    report = json.loads(capsys.readouterr().out)
    # No runtime on this host: the job fails runtime_unavailable (or queue_timeout, since the
    # fixture's clock is not this one), and the runner does not crash.
    assert report["outcomes"][job.record.job_id] == "failed"
    ended = store.load(job.record.job_id)
    assert ended is not None and ended.state.error is not None
    assert ended.state.error.code in {"runtime_unavailable", "queue_timeout"}
    assert main(["speech", "deliver", "--config", str(config)]) == 0
    delivered = json.loads(capsys.readouterr().out)
    assert delivered[f"msg_{job.record.job_id}"] == "refused"  # no secret file on this host


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
