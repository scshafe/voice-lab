from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import cole_voice_lab.cli as cli_module
from cole_voice_lab.capture import AvfoundationDevice
from cole_voice_lab.cli import MAX_CLI_DATASET_JSON_BYTES, build_parser, main
from cole_voice_lab.datasets import DatasetSelection, PilotGateApproval
from cole_voice_lab.errors import ContractError
from cole_voice_lab.resources import load_prompt_suite


class _Dumpable:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return self.value


def _initialize_assets(root: Path, capsys: object) -> None:
    assert main(["assets", "init", "--asset-root", str(root)]) == 0
    capsys.readouterr()  # type: ignore[attr-defined]


def _write_dataset_inputs(tmp_path: Path) -> tuple[Path, Path, str]:
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "schema_version": "cole-voice-lab.dataset-selection/v1",
                "selections": [
                    {
                        "raw_manifest_sha256": "a" * 64,
                        "prompt_id": "neutral-01",
                        "split": "train",
                        "headphone_review_path": "provenance/headphone-reviews/review.json",
                        "headphone_review_sha256": "b" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    suite = load_prompt_suite("pilot")
    suite_path = tmp_path / "pilot-suite.json"
    suite_path.write_text(suite.model_dump_json(), encoding="utf-8")
    return selection_path, suite_path, suite.suite_id


def test_prompt_cli_prints_suite(capsys: object) -> None:
    assert main(["prompts", "show", "evaluation", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["purpose"] == "held-out-evaluation"


def test_assets_and_pending_consent_flow(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "private" / "voice-assets"
    assert main(["assets", "init", "--asset-root", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert result["asset_root"] == str(root)

    assert main(["consent", "status", "--asset-root", str(root)]) == 0
    status = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert status["record"]["approved"] is False


def test_consent_cli_cannot_infer_attestation(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "private" / "voice-assets"
    assert main(["assets", "init", "--asset-root", str(root)]) == 0
    capsys.readouterr()  # type: ignore[attr-defined]

    scope_args = [
        "--purpose",
        "private-zero-shot-evaluation",
        "--execution-boundary",
        "tailnet-local-host",
        "--expires-at",
        "2026-09-17T00:00:00Z",
    ]
    assert main(["consent", "preview", *scope_args]) == 0
    preview = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert preview["approved"] is False
    assert (
        main(
            [
                "consent",
                "approve-self",
                "--asset-root",
                str(root),
                *scope_args,
                "--reviewed-scope-sha256",
                preview["scope_sha256"],
            ]
        )
        == 2
    )
    assert "explicit" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_consent_cli_confines_record_ids(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "private" / "voice-assets"
    assert main(["assets", "init", "--asset-root", str(root)]) == 0
    capsys.readouterr()  # type: ignore[attr-defined]

    assert (
        main(
            [
                "consent",
                "status",
                "--asset-root",
                str(root),
                "--consent-id",
                "../../../outside",
            ]
        )
        == 2
    )
    assert "validation" in capsys.readouterr().err.lower()  # type: ignore[attr-defined]


def test_missing_asset_root_is_reported(capsys: object, monkeypatch: object) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)  # type: ignore[attr-defined]
    assert main(["assets", "status"]) == 2
    assert "asset-root" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_capture_device_cli_reports_umik_without_opening_audio(
    capsys: object, monkeypatch: object
) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "list_avfoundation_audio_devices",
        lambda **_kwargs: (AvfoundationDevice(2, "miniDSP UMIK-1"),),
    )
    assert main(["capture", "devices"]) == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["devices"] == [
        {"device_id": "2", "display_name": "miniDSP UMIK-1", "is_umik1": True}
    ]


def test_intake_scan_is_bounded_and_empty_is_success(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "private" / "voice-assets"
    assert main(["assets", "init", "--asset-root", str(root)]) == 0
    capsys.readouterr()  # type: ignore[attr-defined]
    assert main(["intake", "scan", "--asset-root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == {"accepted": [], "errors": []}  # type: ignore[attr-defined]


def test_models_cli_lists_and_verifies_pinned_registry(capsys: object) -> None:
    assert main(["models", "list", "--track", "direct-tts"]) == 0
    listing = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert {candidate["candidate_id"] for candidate in listing["candidates"]} == {
        "qwen3-tts-base",
        "chatterbox-turbo",
        "chatterbox-v3",
        "f5-tts",
    }
    assert main(["models", "verify-registry"]) == 0
    verification = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert verification["status"] == "pass"
    assert verification["network_used"] is False


def test_models_prepare_uses_the_same_paths_as_audition_runtime(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)

    assert (
        main(
            [
                "models",
                "prepare",
                "seed-vc",
                "--asset-root",
                str(root),
                "--profile",
                "smoke",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    source_destinations = {item["source_id"]: item["destination"] for item in output["sources"]}
    assert source_destinations["seed-vc-code"] == str(
        root / "runtimes/source-checkouts/seed-vc-code"
    )
    assert source_destinations["seed-vc-model"] == str(root / "models/sources/seed-vc-model")
    assert output["execution_requested"] is False
    assert output["runtime"]["install_ready"] is False
    assert output["runtime"]["estimated_install_bytes"] == 8 * 1024**3
    assert output["runtime"]["commands"][0][1:6] == ["-I", "-P", "-S", "-m", "venv"]
    assert all(
        source["commands"][0][1:5] == ["-I", "-P", "-m", "cole_voice_lab.workers.snapshot"]
        if source["source_id"] != "seed-vc-code"
        else True
        for source in output["sources"]
    )

    assert (
        main(
            [
                "models",
                "prepare",
                "qwen3-tts-base",
                "--asset-root",
                str(root),
                "--profile",
                "smoke",
            ]
        )
        == 0
    )
    direct_output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    direct_source_ids = {item["source_id"] for item in direct_output["sources"]}
    assert direct_source_ids == {"qwen-base-8bit"}
    assert "qwen3-tts-upstream-code" not in direct_source_ids
    assert "mlx-audio-code" not in direct_source_ids
    assert direct_output["runtime"]["install_ready"] is False
    assert "REVIEWED-COLE-VOICE-LAB-WHEEL-REQUIRED.whl" in json.dumps(
        direct_output["runtime"]["commands"]
    )


def test_models_prepare_reuse_requires_and_reports_exact_runtime_receipt(
    tmp_path: Path,
    capsys: object,
    monkeypatch: object,
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    runtime = root / "runtimes/mlx-audio-0.5.0"
    runtime_python = runtime / "bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("fixture")
    base = [
        "models",
        "prepare",
        "qwen3-tts-base",
        "--asset-root",
        str(root),
        "--profile",
        "smoke",
        "--base-python",
        str(Path(sys.executable)),
        "--execute",
        "--allow-downloads",
    ]
    assert main(base) == 2
    assert "expected-runtime-receipt-sha256" in capsys.readouterr().err  # type: ignore[attr-defined]

    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "verify_runtime_install_receipt",
        lambda *_args, **_kwargs: (None, "a" * 64),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "execute_provision_plan",
        lambda *_args, **_kwargs: None,
    )
    assert main([*base, "--expected-runtime-receipt-sha256", "a" * 64]) == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["runtime"]["receipt_sha256"] == "a" * 64
    assert output["runtime"]["receipt_path"].endswith(
        "/mlx-audio-0.5.0/.cole-voice-lab-runtime-install.json"
    )


def test_audition_plan_wires_reviewed_references_and_bound_sources(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    reference_path = root / "references/ref-a/reference.wav"
    reference_path.parent.mkdir(mode=0o700)
    reference_path.write_bytes(b"reference")
    consent_path = root / "provenance/consents/cole-self-v1.json"
    consent_path.write_text("{}", encoding="utf-8")
    source_path = root / "derived/performed/eval-short-neutral.wav"
    source_path.parent.mkdir(mode=0o700)
    source_path.write_bytes(b"source")
    manifest = SimpleNamespace(
        output=SimpleNamespace(path="references/ref-a/reference.wav", sha256="a" * 64),
        source=SimpleNamespace(transcript="Exact reference words.", subject_id="cole-self"),
        consent=SimpleNamespace(
            file=SimpleNamespace(
                path="provenance/consents/cole-self-v1.json",
                sha256="b" * 64,
            )
        ),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "validate_reference_publication",
        lambda *_args, **_kwargs: SimpleNamespace(manifest=manifest),
    )
    captured: dict[str, Any] = {}
    fake_plan = SimpleNamespace(
        experiment_id="smoke-a",
        profile="smoke",
        trials=(
            SimpleNamespace(candidate_id="qwen3-tts-base"),
            SimpleNamespace(candidate_id="seed-vc"),
        ),
    )

    def fake_build(*_args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_plan

    monkeypatch.setattr(cli_module, "build_audition_plan", fake_build)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "persist_audition_plan",
        lambda _layout, _plan: (root / "experiments/smoke-a/plan.json", "c" * 64),
    )

    assert (
        main(
            [
                "audition",
                "plan",
                "--asset-root",
                str(root),
                "--experiment-id",
                "smoke-a",
                "--prompt-id",
                "eval-short-neutral",
                "--reference-id",
                "ref-a",
                "--seed",
                "17",
                "--source-audio",
                f"eval-short-neutral={source_path}",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["trial_count"] == 2
    assert output["trials_by_candidate"] == {"qwen3-tts-base": 1, "seed-vc": 1}
    assert captured["seeds"] == (17,)
    assert captured["source_audio_by_prompt"] == {"eval-short-neutral": source_path}
    references = captured["references"]
    assert references[0].path == str(reference_path)
    assert references[0].consent_path == str(consent_path)


def test_audition_plan_rejects_performed_sources_outside_asset_root(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"source")

    with pytest.raises(cli_module.VoiceLabError, match="beneath the asset root"):
        cli_module._audition_sources(  # type: ignore[attr-defined]
            cli_module.AssetLayout.validate(root),
            [f"eval-neutral-01={outside}"],
            {"eval-neutral-01"},
        )


def test_audition_run_is_preview_only_until_execute(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    plan = SimpleNamespace(
        experiment_id="smoke-a",
        trials=(
            SimpleNamespace(
                candidate_id="qwen3-tts-base",
                trial_id="qwen-trial",
                runtime_id="mlx-audio-0.5.0",
            ),
            SimpleNamespace(
                candidate_id="seed-vc",
                trial_id="seed-trial",
                runtime_id="seed-vc-51383efd-mps",
            ),
        ),
    )
    monkeypatch.setattr(cli_module, "_load_audition_plan", lambda *_args: plan)  # type: ignore[attr-defined]
    executed: list[dict[str, object]] = []

    def fake_run(*_args: object, **kwargs: object) -> object:
        executed.append(kwargs)
        return SimpleNamespace(experiment_id="smoke-a", status="complete", results=(1, 2))

    monkeypatch.setattr(cli_module, "run_offline_audition", fake_run)  # type: ignore[attr-defined]
    base = [
        "audition",
        "run",
        "--asset-root",
        str(root),
        "--experiment-id",
        "smoke-a",
        "--candidate",
        "qwen3-tts-base",
    ]
    assert main(base) == 0
    preview = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert preview["execution_requested"] is False
    assert executed == []

    assert main([*base, "--execute"]) == 2
    assert "approved --runtime-receipt" in capsys.readouterr().err  # type: ignore[attr-defined]

    assert (
        main(
            [
                *base,
                "--runtime-receipt",
                f"mlx-audio-0.5.0={'a' * 64}",
                "--execute",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert result["status"] == "complete"
    assert executed[0]["direct_candidate_ids"] == ["qwen3-tts-base"]
    assert executed[0]["voice_conversion_candidate_ids"] == []
    assert executed[0]["expected_runtime_receipt_sha256"] == {"mlx-audio-0.5.0": "a" * 64}


def test_dataset_build_reads_strict_selection_and_explicit_suite_mapping(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    selection_path, suite_path, suite_id = _write_dataset_inputs(tmp_path)
    captured: dict[str, Any] = {}
    manifest = _Dumpable({"dataset_id": "pilot-dataset"})

    def fake_build(_layout: object, **kwargs: object) -> _Dumpable:
        captured.update(kwargs)
        return manifest

    monkeypatch.setattr(cli_module, "build_dataset_manifest", fake_build)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "publish_dataset_manifest",
        lambda _layout, _manifest: SimpleNamespace(
            directory=root / "derived/datasets/pilot-dataset",
            manifest=manifest,
            manifest_sha256="c" * 64,
        ),
    )

    assert (
        main(
            [
                "dataset",
                "build",
                "--asset-root",
                str(root),
                "--dataset-id",
                "pilot-dataset",
                "--corpus-stage",
                "pilot",
                "--selection-file",
                str(selection_path),
                "--prompt-suite",
                f"{suite_id}={suite_path}",
                "--consent-path",
                "provenance/consents/cole-self-v1.json",
                "--consent-sha256",
                "d" * 64,
                "--minimum-holdout-seconds",
                "12.5",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["manifest_sha256"] == "c" * 64
    assert captured["dataset_id"] == "pilot-dataset"
    assert captured["minimum_holdout_seconds"] == 12.5
    selections = captured["selections"]
    assert isinstance(selections, tuple)
    assert isinstance(selections[0], DatasetSelection)
    suites = captured["prompt_suites"]
    assert isinstance(suites, dict)
    assert suites[suite_id].suite_id == suite_id


def test_dataset_build_rejects_non_strict_or_oversized_selection_json(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    selection_path, suite_path, suite_id = _write_dataset_inputs(tmp_path)
    invalid = json.loads(selection_path.read_text(encoding="utf-8"))
    invalid["unexpected"] = True
    selection_path.write_text(json.dumps(invalid), encoding="utf-8")
    base_args = [
        "dataset",
        "build",
        "--asset-root",
        str(root),
        "--dataset-id",
        "pilot-dataset",
        "--corpus-stage",
        "pilot",
        "--selection-file",
        str(selection_path),
        "--prompt-suite",
        f"{suite_id}={suite_path}",
        "--consent-path",
        "provenance/consents/cole-self-v1.json",
        "--consent-sha256",
        "d" * 64,
    ]
    assert main(base_args) == 2
    assert "unexpected" in capsys.readouterr().err  # type: ignore[attr-defined]

    with selection_path.open("wb") as stream:
        stream.truncate(MAX_CLI_DATASET_JSON_BYTES + 1)
    assert main(base_args) == 2
    assert "exceeds" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_dataset_build_rejects_prompt_suite_mapping_mismatch(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    selection_path, suite_path, _suite_id = _write_dataset_inputs(tmp_path)
    assert (
        main(
            [
                "dataset",
                "build",
                "--asset-root",
                str(root),
                "--dataset-id",
                "pilot-dataset",
                "--corpus-stage",
                "pilot",
                "--selection-file",
                str(selection_path),
                "--prompt-suite",
                f"wrong-suite={suite_path}",
                "--consent-path",
                "provenance/consents/cole-self-v1.json",
                "--consent-sha256",
                "d" * 64,
            ]
        )
        == 2
    )
    assert "does not match" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_dataset_pilot_gate_requires_explicit_attestations_and_revalidates_capture(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    manifest_sha256 = "e" * 64
    base_args = [
        "dataset",
        "pilot-gate",
        "--asset-root",
        str(root),
        "--gate-id",
        "pilot-approved-v1",
        "--pilot-manifest-sha256",
        manifest_sha256,
        "--statement",
        "Pilot capture passed headphone review; full-corpus work is approved.",
    ]
    assert main(base_args) == 2
    assert "requires --approve" in capsys.readouterr().err  # type: ignore[attr-defined]

    validation_calls: list[Path] = []

    def fake_validate(path: Path, *, allow_intake_receipt: bool = False) -> object:
        assert allow_intake_receipt is True
        validation_calls.append(path)
        return SimpleNamespace(
            manifest_sha256=manifest_sha256,
            qc_report=SimpleNamespace(status="pass"),
        )

    def fake_load_gate(_layout: object, *, relative_path: str, expected_sha256: str) -> object:
        assert len(expected_sha256) == 64
        record = PilotGateApproval.model_validate_json((root / relative_path).read_bytes())
        return SimpleNamespace(record=record)

    monkeypatch.setattr(cli_module, "validate_capture_package", fake_validate)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "load_pilot_gate", fake_load_gate)  # type: ignore[attr-defined]
    assert (
        main(
            [
                *base_args,
                "--approve",
                "--headphone-review-completed",
                "--allow-full-corpus",
                "--approved-at",
                "2026-08-17T12:00:00-07:00",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["record"]["approved"] is True
    assert output["record"]["full_corpus_capture_allowed"] is True
    assert validation_calls == [root / "raw" / manifest_sha256]
    gate_path = root / output["path"]
    assert stat.S_IMODE(gate_path.stat().st_mode) == 0o400


def test_dataset_validate_revalidates_published_evidence(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    manifest = _Dumpable({"dataset_id": "dataset-v1"})
    requested: list[Path] = []

    def fake_validate(_layout: object, directory: Path) -> object:
        requested.append(directory)
        return SimpleNamespace(
            directory=directory,
            manifest=manifest,
            manifest_sha256="f" * 64,
        )

    monkeypatch.setattr(cli_module, "validate_published_dataset", fake_validate)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "dataset",
                "validate",
                "--asset-root",
                str(root),
                "--dataset-id",
                "dataset-v1",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["manifest_sha256"] == "f" * 64
    assert requested == [root / "derived/datasets/dataset-v1"]


def test_dataset_transfer_plan_defaults_to_prohibited_and_never_executes_network(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private" / "voice-assets"
    _initialize_assets(root, capsys)
    calls: list[dict[str, object]] = []

    def fake_build(_layout: object, **kwargs: object) -> _Dumpable:
        calls.append(kwargs)
        status = "approved-not-executed" if kwargs["destination"] else "prohibited"
        return _Dumpable(
            {
                "plan_id": kwargs["plan_id"],
                "hosted_transfer_status": status,
                "execution_performed": False,
            }
        )

    def fake_publish(_layout: object, plan: _Dumpable) -> object:
        return SimpleNamespace(
            directory=root / "derived/transfer-plans/plan-v1",
            plan=plan,
            plan_sha256="1" * 64,
        )

    monkeypatch.setattr(cli_module, "build_transfer_plan", fake_build)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "publish_transfer_plan", fake_publish)  # type: ignore[attr-defined]
    base_args = [
        "dataset",
        "transfer-plan",
        "--asset-root",
        str(root),
        "--plan-id",
        "plan-v1",
        "--dataset-id",
        "dataset-v1",
    ]
    assert main(base_args) == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["plan"]["hosted_transfer_status"] == "prohibited"
    assert output["network_executed"] is False
    assert calls[-1]["destination"] is None
    assert calls[-1]["approval_path"] is None
    assert calls[-1]["approval_sha256"] is None

    assert (
        main(
            [
                *base_args,
                "--hosted-destination",
                "approved-provider:private-project",
                "--hosted-approval-path",
                "provenance/transfer-approvals/plan-v1.json",
                "--hosted-approval-sha256",
                "2" * 64,
            ]
        )
        == 0
    )
    approved_output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert approved_output["plan"]["hosted_transfer_status"] == "approved-not-executed"
    assert approved_output["network_executed"] is False
    assert calls[-1]["destination"] == "approved-provider:private-project"
    assert calls[-1]["approval_path"] == "provenance/transfer-approvals/plan-v1.json"
    assert calls[-1]["approval_sha256"] == "2" * 64


def test_dataset_transfer_parser_exposes_no_execution_or_approval_authoring() -> None:
    options = build_parser().parse_args(
        [
            "dataset",
            "transfer-plan",
            "--asset-root",
            "/private/voice-assets",
            "--plan-id",
            "plan-v1",
            "--dataset-id",
            "dataset-v1",
        ]
    )
    assert not hasattr(options, "execute")
    assert not hasattr(options, "allow_network")
    assert not hasattr(options, "approve_hosted_transfer")


def test_datetime_prompt_and_source_selection_helpers_reject_ambiguous_input(
    tmp_path: Path,
) -> None:
    with pytest.raises(cli_module.argparse.ArgumentTypeError, match="invalid ISO-8601"):
        cli_module._aware_datetime("not-a-time")  # type: ignore[attr-defined]
    with pytest.raises(cli_module.argparse.ArgumentTypeError, match="explicit UTC offset"):
        cli_module._aware_datetime("2026-08-17T12:00:00")  # type: ignore[attr-defined]

    suite = load_prompt_suite("evaluation")
    with pytest.raises(cli_module.VoiceLabError, match="must be unique"):
        cli_module._selected_prompts(  # type: ignore[attr-defined]
            suite,
            [suite.prompts[0].prompt_id, suite.prompts[0].prompt_id],
        )
    assert cli_module._selected_prompts(suite, []) == suite.prompts  # type: ignore[attr-defined]
    with pytest.raises(cli_module.VoiceLabError, match="unknown audition prompt"):
        cli_module._selected_prompts(suite, ["unknown-prompt"])  # type: ignore[attr-defined]

    layout = cli_module.AssetLayout.initialize(tmp_path / "assets")
    source = layout.directory("derived") / "source.wav"
    source.write_bytes(b"source")
    with pytest.raises(cli_module.VoiceLabError, match="exact PROMPT_ID=PATH"):
        cli_module._audition_sources(layout, [str(source)], {"prompt-a"})  # type: ignore[attr-defined]
    with pytest.raises(cli_module.VoiceLabError, match="duplicate performed source"):
        cli_module._audition_sources(  # type: ignore[attr-defined]
            layout,
            [f"prompt-a={source}", f"prompt-a={source}"],
            {"prompt-a"},
        )
    with pytest.raises(cli_module.VoiceLabError, match="unselected prompt"):
        cli_module._audition_sources(  # type: ignore[attr-defined]
            layout,
            [f"prompt-b={source}"],
            {"prompt-a"},
        )


def test_prompt_suite_mapping_rejects_bad_and_duplicate_mappings(tmp_path: Path) -> None:
    suite = load_prompt_suite("pilot")
    path = tmp_path / "suite.json"
    path.write_text(suite.model_dump_json(), encoding="utf-8")
    with pytest.raises(cli_module.VoiceLabError, match="exact SUITE_ID=PATH"):
        cli_module._prompt_suite_mappings([str(path)])  # type: ignore[attr-defined]
    mapping = f"{suite.suite_id}={path}"
    with pytest.raises(cli_module.VoiceLabError, match="duplicate"):
        cli_module._prompt_suite_mappings([mapping, mapping])  # type: ignore[attr-defined]


def test_doctor_assets_status_and_human_prompt_output(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    checks = (
        SimpleNamespace(
            status="pass",
            check_id="ffmpeg",
            message="available",
            to_json=lambda: {"status": "pass", "check_id": "ffmpeg"},
        ),
        SimpleNamespace(
            status="fail",
            check_id="rrsync",
            message="missing",
            to_json=lambda: {"status": "fail", "check_id": "rrsync"},
        ),
    )
    monkeypatch.setattr(cli_module, "run_doctor", lambda _root: checks)  # type: ignore[attr-defined]
    assert main(["doctor", "--asset-root", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["checks"][1]["check_id"] == "rrsync"  # type: ignore[attr-defined]
    assert main(["doctor"]) == 1
    assert "FAIL   rrsync" in capsys.readouterr().out  # type: ignore[attr-defined]

    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    (root / "tmp" / "marker").write_text("x", encoding="utf-8")
    assert main(["assets", "status", "--asset-root", str(root)]) == 0
    status_output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert status_output["directories"]["tmp"]["entries"] == 1

    assert main(["prompts", "show", "pilot"]) == 0
    human_output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "expected minutes" in human_output
    assert "[reference]" in human_output


def test_capture_run_is_fully_wired_without_opening_audio(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "list_avfoundation_audio_devices",
        lambda **_kwargs: (AvfoundationDevice(3, "miniDSP UMIK-1"),),
    )
    captured: dict[str, Any] = {}

    def fake_capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            session_directory=root / "capture/ready/session-a",
            manifest_sha256="a" * 64,
            qc_report_sha256="b" * 64,
            qc_report=SimpleNamespace(status="pass"),
            ready=_Dumpable({"schema_version": "ready/v1", "session_id": "session-a"}),
        )

    monkeypatch.setattr(cli_module, "capture_prompt_suite", fake_capture)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "capture",
                "run",
                "--asset-root",
                str(root),
                "--session-id",
                "session-a",
                "--device",
                "3",
                "--microphone-serial",
                "UMIK-SERIAL",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["qc_status"] == "pass"
    assert output["ready"]["session_id"] == "session-a"
    device = captured["device"]
    assert device.device_id == "3"
    assert device.microphone_serial == "UMIK-SERIAL"
    assert captured["session_id"] == "session-a"


def test_capture_console_routes_shared_inputs_resume_and_open_without_hardware(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    import cole_voice_lab.capture_console as capture_console_module

    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    discovered_ffmpeg: list[str] = []

    def fake_devices(*, ffmpeg: str) -> tuple[AvfoundationDevice, ...]:
        discovered_ffmpeg.append(ffmpeg)
        return (AvfoundationDevice(3, "miniDSP UMIK-1"),)

    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "list_avfoundation_audio_devices",
        fake_devices,
    )
    engine_inputs: dict[str, Any] = {}
    engine = object()

    def fake_engine(**kwargs: object) -> object:
        engine_inputs.update(kwargs)
        return engine

    served: list[tuple[object, bool]] = []

    def fake_serve(selected_engine: object, *, open_browser: bool) -> None:
        served.append((selected_engine, open_browser))
        print("http://127.0.0.1:49152/#test-token")

    monkeypatch.setattr(cli_module, "TakeEngine", fake_engine)  # type: ignore[attr-defined]
    monkeypatch.setattr(
        capture_console_module,
        "serve_capture_console",
        fake_serve,
    )

    assert (
        main(
            [
                "capture",
                "console",
                "--asset-root",
                str(root),
                "--session-id",
                "session-console",
                "--subject-id",
                "cole-subject",
                "--suite",
                "evaluation-v1",
                "--device",
                "3",
                "--microphone-serial",
                "UMIK-SERIAL",
                "--ffmpeg",
                "/test/ffmpeg",
                "--resume",
                "--open",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "http://127.0.0.1:49152/#test-token\n"  # type: ignore[attr-defined]
    assert discovered_ffmpeg == ["/test/ffmpeg"]
    assert served == [(engine, True)]
    assert engine_inputs["layout"].root == root
    assert engine_inputs["prompt_suite"] == load_prompt_suite("evaluation-v1")
    assert engine_inputs["subject_id"] == "cole-subject"
    assert engine_inputs["session_id"] == "session-console"
    assert engine_inputs["ffmpeg"] == "/test/ffmpeg"
    assert engine_inputs["resume"] is True
    device = engine_inputs["device"]
    assert device.device_id == "3"
    assert device.display_name == "miniDSP UMIK-1"
    assert device.microphone_serial == "UMIK-SERIAL"


def test_capture_console_has_no_bind_host_option() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "capture",
                "console",
                "--session-id",
                "session-console",
                "--bind-host",
                "0.0.0.0",
            ]
        )


def test_intake_validate_accept_and_scan_report_each_outcome(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    package = SimpleNamespace(
        manifest=SimpleNamespace(session_id="session-a"),
        manifest_sha256="a" * 64,
        qc_report_sha256="b" * 64,
        qc_report=SimpleNamespace(status="pass"),
    )
    monkeypatch.setattr(cli_module, "validate_capture_package", lambda _path: package)  # type: ignore[attr-defined]
    assert main(["intake", "validate", str(tmp_path / "package")]) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "session-a"  # type: ignore[attr-defined]

    accepted_sources: list[Path] = []

    def fake_accept(_layout: object, source: Path) -> _Dumpable:
        accepted_sources.append(source)
        if source.name == "bad-session":
            raise cli_module.VoiceLabError("checksum mismatch")
        return _Dumpable({"session_id": source.name, "status": "accepted"})

    monkeypatch.setattr(cli_module, "accept_capture_session", fake_accept)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "intake",
                "accept",
                "--asset-root",
                str(root),
                "--session-id",
                "one-session",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"  # type: ignore[attr-defined]
    incoming = root / "incoming"
    (incoming / ".partial").mkdir()
    (incoming / "bad-session").mkdir()
    (incoming / "good-session").mkdir()
    accepted_sources.clear()
    assert main(["intake", "scan", "--asset-root", str(root)]) == 1
    scan = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert scan["accepted"] == [{"session_id": "good-session", "status": "accepted"}]
    assert scan["errors"] == [{"error": "checksum mismatch", "session": "bad-session"}]
    assert {path.name for path in accepted_sources} == {"bad-session", "good-session"}


def test_sync_plan_previews_and_executes_only_through_explicit_handler(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    built: list[dict[str, object]] = []
    executed: list[tuple[object, bool, str | None]] = []
    transfer_pass = SimpleNamespace(name="payload", argv=("rsync", "--dry-run"))
    approved_digest = "a" * 64
    package_snapshot_digest = "b" * 64
    package_snapshot_payload = {
        "schema_version": "cole-voice-lab.sync-package-snapshot/v1",
        "session_id": "session-a",
    }
    plan = SimpleNamespace(
        session_id="session-a",
        source_directory=tmp_path / "session-a",
        target="100.123.230.91",
        remote_directory="incoming/session-a",
        dry_run=False,
        package_snapshot=SimpleNamespace(
            sha256=lambda: package_snapshot_digest,
            approval_payload=lambda: package_snapshot_payload,
        ),
        passes=(transfer_pass,),
        approval_sha256=lambda: approved_digest,
        shell_display=lambda: "rsync --dry-run",
    )

    def fake_build(_session: Path, **kwargs: object) -> object:
        built.append(kwargs)
        plan.dry_run = kwargs["dry_run"]
        return plan

    monkeypatch.setattr(cli_module, "build_sync_plan", fake_build)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "execute_sync_plan",
        lambda selected, *, allow_network, approved_plan_sha256: executed.append(
            (selected, allow_network, approved_plan_sha256)
        ),
    )
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    base = [
        "sync",
        "plan",
        str(tmp_path / "session-a"),
        "--identity-file",
        str(identity),
        "--known-hosts-file",
        str(known_hosts),
    ]
    assert (
        main(
            [
                *base,
                "--json",
                "--live-plan",
                "--execute",
                "--allow-network",
                "--approved-plan-sha256",
                approved_digest,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["dry_run"] is False
    assert output["plan_sha256"] == approved_digest
    assert output["package_snapshot_sha256"] == package_snapshot_digest
    assert output["package_snapshot"] == package_snapshot_payload
    assert output["passes"][0]["argv"] == ["rsync", "--dry-run"]
    assert built[-1]["identity_file"] == identity
    assert built[-1]["known_hosts_file"] == known_hosts
    assert executed == [(plan, True, approved_digest)]

    assert main(base) == 0
    assert capsys.readouterr().out.strip() == "rsync --dry-run"  # type: ignore[attr-defined]
    assert built[-1]["dry_run"] is True


def test_reference_review_publish_and_validate_operator_paths(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    base_review = [
        "reference",
        "review",
        "--asset-root",
        str(root),
        "--review-id",
        "review-a",
        "--raw-manifest-sha256",
        "a" * 64,
        "--prompt-id",
        "pilot-neutral-01",
    ]
    assert main(base_review) == 2
    assert "explicit headphone" in capsys.readouterr().err  # type: ignore[attr-defined]

    source = SimpleNamespace(
        binding=SimpleNamespace(
            session_id="session-a",
            prompt_id="pilot-neutral-01",
            raw_audio_sha256="b" * 64,
        )
    )
    monkeypatch.setattr(cli_module, "load_accepted_utterance", lambda *_a, **_k: source)  # type: ignore[attr-defined]
    attestations = [
        "--headphones-used",
        "--transcript-matches",
        "--audio-quality-passed",
        "--finding",
        "clean",
    ]
    assert main([*base_review, *attestations]) == 0
    review = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert review["record"]["findings"] == ["clean"]
    assert (root / review["path"]).is_file()
    assert main([*base_review, *attestations]) == 2
    assert "refusing to replace" in capsys.readouterr().err  # type: ignore[attr-defined]

    manifest = SimpleNamespace(
        model_dump=lambda *, mode: {"reference_id": "reference-a"},
        output=SimpleNamespace(sha256="c" * 64),
        source=SimpleNamespace(transcript="Reference words."),
    )
    published = SimpleNamespace(
        directory=root / "references/reference-a",
        manifest_sha256="d" * 64,
        manifest=manifest,
    )
    publish_kwargs: dict[str, object] = {}

    def fake_publish(_layout: object, **kwargs: object) -> object:
        publish_kwargs.update(kwargs)
        return published

    monkeypatch.setattr(cli_module, "publish_reference", fake_publish)  # type: ignore[attr-defined]
    publish_args = [
        "reference",
        "publish",
        "--asset-root",
        str(root),
        "--reference-id",
        "reference-a",
        "--raw-manifest-sha256",
        "a" * 64,
        "--prompt-id",
        "pilot-neutral-01",
        "--consent-path",
        "provenance/consents/cole-self-v1.json",
        "--consent-sha256",
        "e" * 64,
        "--headphone-review-path",
        review["path"],
        "--headphone-review-sha256",
        review["sha256"],
        "--provenance-statement",
        "Self-recorded reference reviewed on headphones.",
    ]
    assert main(publish_args) == 0
    publish_output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert publish_output["manifest"]["reference_id"] == "reference-a"
    assert publish_kwargs["headphone_review_sha256"] == review["sha256"]

    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "validate_reference_publication",
        lambda *_args: published,
    )
    assert (
        main(
            [
                "reference",
                "validate",
                "--asset-root",
                str(root),
                "--reference-id",
                "reference-a",
            ]
        )
        == 0
    )
    validation = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert validation["reference_sha256"] == "c" * 64
    assert validation["transcript"] == "Reference words."


def test_consent_full_cli_lifecycle_is_digest_and_scope_bound(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    assert main(["consent", "install-pending", "--asset-root", str(root)]) == 0
    pending = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert pending["approved"] is False

    scope_args = [
        "--purpose",
        "private-zero-shot-evaluation",
        "--execution-boundary",
        "tailnet-local-host",
        "--expires-at",
        "2030-01-01T00:00:00Z",
    ]
    assert main(["consent", "preview", *scope_args]) == 0
    preview = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    bad_approve = [
        "consent",
        "approve-self",
        "--asset-root",
        str(root),
        *scope_args,
        "--expected-current-sha256",
        pending["sha256"],
        "--reviewed-scope-sha256",
        "0" * 64,
        "--i-am-the-subject-and-authorize",
    ]
    assert main(bad_approve) == 2
    assert "exact --reviewed-scope-sha256" in capsys.readouterr().err  # type: ignore[attr-defined]

    good_approve = bad_approve.copy()
    good_approve[good_approve.index("0" * 64)] = preview["scope_sha256"]
    assert main(good_approve) == 0
    approved = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert approved["approved"] is True
    assert approved["scope_sha256"] == preview["scope_sha256"]
    assert main(["consent", "status", "--asset-root", str(root)]) == 0
    status_output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert status_output["record"]["approved"] is True
    assert status_output["sha256"] == approved["sha256"]

    revoke = [
        "consent",
        "revoke",
        "--asset-root",
        str(root),
        "--expected-sha256",
        approved["sha256"],
    ]
    assert main(revoke) == 2
    assert "--confirm-revoke" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert main([*revoke, "--confirm-revoke"]) == 0
    revoked = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert revoked["approved"] is False
    assert revoked["revoked_at"] is not None


def test_consent_scope_rejects_duplicate_purpose(capsys: object) -> None:
    args = [
        "consent",
        "preview",
        "--purpose",
        "private-zero-shot-evaluation",
        "--purpose",
        "private-zero-shot-evaluation",
        "--execution-boundary",
        "capture-host",
        "--expires-at",
        "2030-01-01T00:00:00Z",
    ]
    assert main(args) == 2
    assert "must be unique" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_audition_status_reports_not_started_persisted_and_mismatched_runs(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    plan = SimpleNamespace(experiment_id="experiment-a", trials=(1, 2, 3))
    run_manifest_path = tmp_path / "run-manifest.json"
    paths = SimpleNamespace(run_manifest_path=run_manifest_path)
    monkeypatch.setattr(cli_module, "_load_audition_plan", lambda *_args: plan)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "audition_paths", lambda *_args: paths)  # type: ignore[attr-defined]
    base = [
        "audition",
        "status",
        "--asset-root",
        str(root),
        "--experiment-id",
        "experiment-a",
    ]
    assert main(base) == 0
    not_started = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert not_started == {
        "experiment_id": "experiment-a",
        "status": "not-started",
        "trial_count": 3,
    }

    run_manifest_path.write_text("{}", encoding="utf-8")
    matching = SimpleNamespace(
        experiment_id="experiment-a",
        model_dump=lambda *, mode: {"experiment_id": "experiment-a", "status": "complete"},
    )
    monkeypatch.setattr(
        cli_module,
        "load_validated_audition_run",
        lambda *_args, **_kwargs: matching,
    )  # type: ignore[attr-defined]
    assert main(base) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"  # type: ignore[attr-defined]

    monkeypatch.setattr(
        cli_module,
        "load_validated_audition_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContractError("run manifest experiment ID does not match the audition plan")
        ),
    )  # type: ignore[attr-defined]
    assert main(base) == 2
    assert "does not match the audition plan" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_audition_run_rejects_ambiguous_or_empty_selections_and_routes_vc(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    trials = (
        SimpleNamespace(
            candidate_id="qwen3-tts-base",
            trial_id="direct-trial",
            runtime_id="mlx-audio-0.5.0",
        ),
        SimpleNamespace(
            candidate_id="seed-vc",
            trial_id="vc-trial",
            runtime_id="seed-vc-51383efd-mps",
        ),
    )
    plan = SimpleNamespace(experiment_id="experiment-a", trials=trials)
    monkeypatch.setattr(cli_module, "_load_audition_plan", lambda *_args: plan)  # type: ignore[attr-defined]
    base = [
        "audition",
        "run",
        "--asset-root",
        str(root),
        "--experiment-id",
        "experiment-a",
    ]
    assert main([*base, "--candidate", "qwen3-tts-base", "--candidate", "qwen3-tts-base"]) == 2
    assert "must be unique" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert main([*base, "--candidate", "f5-tts"]) == 2
    assert "not present" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert (
        main(
            [
                *base,
                "--candidate",
                "qwen3-tts-base",
                "--trial-id",
                "nonexistent-trial",
            ]
        )
        == 2
    )
    assert "contains no trials" in capsys.readouterr().err  # type: ignore[attr-defined]

    calls: list[dict[str, object]] = []

    def fake_run(*_args: object, **kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(experiment_id="experiment-a", status="complete", results=(1,))

    monkeypatch.setattr(cli_module, "run_offline_audition", fake_run)  # type: ignore[attr-defined]
    assert (
        main(
            [
                *base,
                "--candidate",
                "seed-vc",
                "--runtime-receipt",
                f"seed-vc-51383efd-mps={'b' * 64}",
                "--execute",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["result_count"] == 1
    assert calls[0]["direct_candidate_ids"] == []
    assert calls[0]["voice_conversion_candidate_ids"] == ["seed-vc"]


def test_model_eviction_is_previewed_and_digest_confirmed(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    plan = SimpleNamespace(
        source_id="qwen-base-8bit",
        path=root / "models/sources/qwen-base-8bit",
        manifest_sha256="a" * 64,
        estimated_bytes=1234,
    )
    monkeypatch.setattr(cli_module, "build_source_eviction_plan", lambda *_a, **_k: plan)  # type: ignore[attr-defined]
    removals: list[dict[str, object]] = []

    def fake_evict(*_args: object, **kwargs: object) -> int:
        removals.append(kwargs)
        return 1234

    monkeypatch.setattr(cli_module, "execute_source_eviction", fake_evict)  # type: ignore[attr-defined]
    base = [
        "models",
        "evict",
        "--asset-root",
        str(root),
        "qwen-base-8bit",
    ]
    assert main(base) == 0
    preview = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert preview["removed"] is False
    assert preview["manifest_sha256"] == "a" * 64
    assert removals == []

    assert main([*base, "--execute"]) == 2
    assert "--confirm-evict" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert (
        main(
            [
                *base,
                "--execute",
                "--confirm-evict",
                "--expected-manifest-sha256",
                "a" * 64,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert result["removed"] is True
    assert result["removed_bytes"] == 1234
    assert removals[0]["expected_manifest_sha256"] == "a" * 64
    assert removals[0]["confirmed"] is True


def test_runtime_eviction_is_previewed_and_receipt_confirmed(
    tmp_path: Path,
    capsys: object,
    monkeypatch: object,
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    plan = SimpleNamespace(
        runtime_id="mlx-audio-0.5.0",
        path=root / "runtimes/mlx-audio-0.5.0",
        receipt_sha256="a" * 64,
        estimated_bytes=4321,
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "build_runtime_eviction_plan",
        lambda *_args, **_kwargs: plan,
    )
    removals: list[dict[str, object]] = []

    def fake_evict(*_args: object, **kwargs: object) -> int:
        removals.append(kwargs)
        return 4321

    monkeypatch.setattr(cli_module, "execute_runtime_eviction", fake_evict)  # type: ignore[attr-defined]
    base = [
        "models",
        "evict-runtime",
        "--asset-root",
        str(root),
        "mlx-audio-0.5.0",
    ]
    assert main(base) == 0
    preview = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert preview["removed"] is False
    assert preview["receipt_sha256"] == "a" * 64
    assert main([*base, "--execute"]) == 2
    assert "--confirm-evict" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert (
        main(
            [
                *base,
                "--execute",
                "--confirm-evict",
                "--expected-receipt-sha256",
                "a" * 64,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert result["removed_bytes"] == 4321
    assert removals[0]["orchestration_lock_path"] == root / "runtimes/audition-orchestrator.lock"


def test_blind_build_verify_and_aggregate_commands(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    root = tmp_path / "private-assets"
    _initialize_assets(root, capsys)
    package_root = root / "evaluations" / "review-a"
    package = SimpleNamespace(
        root=package_root,
        index=SimpleNamespace(review_id="review-a", samples=(1, 2)),
        review_index_sha256="a" * 64,
        public_manifest_sha256="b" * 64,
        package_manifest_sha256="c" * 64,
    )
    input_calls: list[dict[str, object]] = []

    def fake_inputs(*_args: object, **kwargs: object) -> tuple[object, ...]:
        input_calls.append(kwargs)
        return (object(),)

    monkeypatch.setattr(cli_module, "blind_inputs_from_audition", fake_inputs)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "build_review_package", lambda *_a, **_k: package)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "verify_review_package", lambda *_a, **_k: package)  # type: ignore[attr-defined]

    build = [
        "blind",
        "build",
        "--asset-root",
        str(root),
        "--experiment-id",
        "experiment-a",
        "--review-id",
        "review-a",
        "--track",
        "direct-tts",
    ]
    assert main(build) == 0
    built = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert built["candidate_labels_exposed"] is False
    assert built["public_root"] == str(package_root / "public")
    assert built["sample_count"] == 2
    assert input_calls == [{"experiment_id": "experiment-a", "tracks": ("direct-tts",)}]

    assert (
        main(
            [
                "blind",
                "verify",
                "--asset-root",
                str(root),
                "--review-id",
                "review-a",
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert verified["verified"] is True
    assert verified["public_manifest_sha256"] == "b" * 64

    score_sheet = tmp_path / "scores.json"
    score_sheet.write_text(
        json.dumps(
            {
                "schema_version": "cole-voice-lab.listening-scores/v2",
                "review_id": "review-a",
                "review_index_sha256": "a" * 64,
                "public_manifest_sha256": "b" * 64,
                "reviewer_id": "cole",
                "scored_at": "2026-08-17T20:00:00Z",
                "headphones_used": True,
                "scores": [
                    {
                        "blind_id": "sample-0123456789abcdef",
                        "likeness": 4,
                        "naturalness": 4,
                        "prosody": 4,
                        "intelligibility": 5,
                        "artifact_freedom": 4,
                        "notes": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sheets: list[object] = []

    def fake_aggregate(_layout: object, sheet: object) -> dict[str, object]:
        sheets.append(sheet)
        return {"review_id": "review-a", "winner": "candidate-a"}

    monkeypatch.setattr(cli_module, "aggregate_scores", fake_aggregate)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "blind",
                "aggregate",
                "--asset-root",
                str(root),
                "--score-sheet",
                str(score_sheet),
            ]
        )
        == 0
    )
    aggregated = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert aggregated["winner"] == "candidate-a"
    assert sheets[0].review_id == "review-a"  # type: ignore[attr-defined]
