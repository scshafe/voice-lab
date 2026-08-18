from __future__ import annotations

import importlib.metadata
import io
import math
import struct
import subprocess
import types
import wave
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.errors import ContractError, RuntimeUnavailableError, SecurityError
from cole_voice_lab.models import (
    ConsentRecord,
    EnvironmentEvidence,
    ExecutedCodeEvidence,
    ExperimentRequest,
    ReferenceEvidence,
    WorkerModelEvidence,
)
from cole_voice_lab.runtime import LocalSourceManifest
from cole_voice_lab.workers import common
from cole_voice_lab.workers import snapshot as snapshot_worker
from cole_voice_lab.workers.chatterbox_turbo import DEFINITION as TURBO
from cole_voice_lab.workers.chatterbox_v3 import DEFINITION as V3
from cole_voice_lab.workers.common import (
    WorkerContext,
    WorkerDefinition,
    bool_setting,
    execute_request,
    float_setting,
    integer_setting,
    local_huggingface_overrides,
    read_request,
)
from cole_voice_lab.workers.f5 import DEFINITION as F5
from cole_voice_lab.workers.f5 import _no_progress
from cole_voice_lab.workers.qwen import DEFINITION as QWEN
from cole_voice_lab.workers.seed_vc import DEFINITION as SEED
from cole_voice_lab.workers.seed_vc import _local_model_resolver


def write_tone(path: Path, *, sample_rate: int = 24_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(
            b"".join(
                struct.pack("<h", int(3_000 * math.sin(2 * math.pi * 220 * i / sample_rate)))
                for i in range(2_400)
            )
        )


def request_fixture(
    tmp_path: Path,
    *,
    timing_mode: str = "cold",
    execution_boundaries: tuple[Literal["capture-host", "tailnet-local-host"], ...] = (
        "tailnet-local-host",
    ),
) -> ExperimentRequest:
    reference_path = (tmp_path / "reference.wav").resolve()
    if not reference_path.exists():
        write_tone(reference_path)
    reference_digest, _ = sha256_file(reference_path)
    consent = ConsentRecord(
        consent_id="cole-evaluation-v1",
        subject_id="cole-self",
        approved=True,
        attestation="self-recorded-and-self-authorized",
        allowed_purposes=("private-zero-shot-evaluation",),
        allowed_execution_boundaries=execution_boundaries,
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    consent_path = (tmp_path / "consent.json").resolve()
    if not consent_path.exists():
        consent_digest = write_canonical_json(consent_path, consent)
    else:
        consent_digest, _ = sha256_file(consent_path)
    return ExperimentRequest(
        experiment_id="experiment-1",
        trial_id=f"trial-{timing_mode}",
        candidate_id="fake-worker",
        track="direct-tts",
        text="A held-out synthetic test sentence.",
        output_path=str((tmp_path / f"{timing_mode}.wav").resolve()),
        reference=ReferenceEvidence(
            path=str(reference_path),
            sha256=reference_digest,
            transcript="A reference sentence.",
            subject_id="cole-self",
            consent_path=str(consent_path),
            consent_sha256=consent_digest,
        ),
        seed=17,
        model_id="example/model",
        model_revision="a" * 40,
        code_revision="b" * 40,
        settings={
            "timing_mode": timing_mode,
            "timing_semantics": "test semantics",
            "profile": "smoke",
        },
    )


def fake_definition() -> WorkerDefinition:
    return WorkerDefinition(
        candidate_id="fake-worker",
        track="direct-tts",
        code_revision="b" * 40,
        model_revisions={"example/model": "a" * 40},
        auxiliary_revisions={},
        allowed_settings=frozenset(),
        package_name="cole-voice-lab",
        package_version="0.1.0.dev0",
        watermarked=False,
    )


def fake_context(tmp_path: Path) -> WorkerContext:
    return WorkerContext(
        model_path=tmp_path / "model",
        auxiliary_paths={},
        code_path=None,
        model_manifest_sha256="c" * 64,
        auxiliary_manifest_sha256={},
    )


def test_execute_request_binds_consent_reference_and_portable_evidence(tmp_path: Path) -> None:
    request = request_fixture(tmp_path)
    calls: list[Path | None] = []

    def load(_request, _context):
        return object()

    def synthesize(_model, active_request, _context, output_path):
        calls.append(output_path)
        if output_path is not None:
            write_tone(output_path)
        return active_request.seed

    result = execute_request(
        fake_definition(),
        request,
        load=load,
        synthesize=synthesize,
        context=fake_context(tmp_path),
        synchronize=lambda: None,
    )
    assert result.ok
    assert calls == [Path(request.output_path)]
    assert result.output_sha256 == sha256_file(Path(request.output_path))[0]
    assert result.environment is not None
    assert result.environment.dependency_evidence_path == "evidence/trial-cold.pip-freeze.txt"
    assert result.metadata["model_evidence_path"] == ("evidence/trial-cold.model-evidence.json")
    assert result.metadata["executed_code_evidence_path"] == (
        "evidence/trial-cold.executed-code.json"
    )
    assert result.metadata["watermarked"] is False
    assert result.metadata["worker_lifecycle"] == "one-request-then-exit"
    assert (tmp_path / result.environment.dependency_evidence_path).is_file()
    assert (tmp_path / str(result.metadata["model_evidence_path"])).is_file()
    executed_path = tmp_path / str(result.metadata["executed_code_evidence_path"])
    assert sha256_file(executed_path)[0] == result.metadata["executed_code_evidence_sha256"]
    ExecutedCodeEvidence.model_validate_json(executed_path.read_bytes())
    WorkerModelEvidence.model_validate_json(
        (tmp_path / str(result.metadata["model_evidence_path"])).read_bytes()
    )


def test_execute_request_rejects_executable_byte_drift(tmp_path: Path, monkeypatch) -> None:
    request = request_fixture(tmp_path)
    snapshots = iter(
        (
            {"schema_version": "test", "integrity_sha256": "a" * 64},
            {"schema_version": "test", "integrity_sha256": "b" * 64},
        )
    )
    monkeypatch.setattr(common, "_executed_code_evidence", lambda _definition: next(snapshots))

    with pytest.raises(SecurityError, match="package bytes changed"):
        execute_request(
            fake_definition(),
            request,
            load=lambda _request, _context: object(),
            synthesize=lambda _model, active_request, _context, output: (
                write_tone(output) or active_request.seed
            ),
            context=fake_context(tmp_path),
            synchronize=lambda: None,
        )
    assert not Path(request.output_path).exists()


def test_executed_code_evidence_includes_every_installed_distribution(monkeypatch) -> None:
    class FakeDistribution:
        def __init__(self, name: str) -> None:
            self.metadata = {"Name": name}

    monkeypatch.setattr(
        common.importlib.metadata,
        "distributions",
        lambda: (
            FakeDistribution("cole_voice_lab"),
            FakeDistribution("mlx_audio"),
            FakeDistribution("Transformers"),
        ),
    )
    monkeypatch.setattr(
        common,
        "_worker_package_tree_evidence",
        lambda: {
            "root_name": "cole_voice_lab",
            "files": {"workers/common.py": {"sha256": "a" * 64, "size_bytes": 1}},
        },
    )
    observed: list[str] = []

    def fake_distribution(name: str) -> dict[str, object]:
        observed.append(name)
        return {
            "version": "1.0",
            "files": {f"{name}/module.py": {"sha256": "b" * 64, "size_bytes": 1}},
        }

    monkeypatch.setattr(common, "_distribution_file_evidence", fake_distribution)
    evidence = common._executed_code_evidence(  # type: ignore[attr-defined]
        WorkerDefinition(
            candidate_id="fake-worker",
            track="direct-tts",
            code_revision="b" * 40,
            model_revisions={"example/model": "a" * 40},
            auxiliary_revisions={},
            allowed_settings=frozenset(),
            package_name="mlx-audio",
            package_version="0.5.0",
        )
    )
    assert observed == ["cole-voice-lab", "mlx-audio", "transformers"]
    assert set(evidence["distributions"]) == {  # type: ignore[arg-type]
        "cole-voice-lab",
        "mlx-audio",
        "transformers",
    }


def test_warm_semantics_use_one_same_request_warmup_then_timed_output(tmp_path: Path) -> None:
    request = request_fixture(tmp_path, timing_mode="warm")
    calls: list[Path | None] = []
    synchronizations: list[None] = []

    def synthesize(_model, active_request, _context, output_path):
        calls.append(output_path)
        if output_path is not None:
            write_tone(output_path)
        return active_request.seed

    result = execute_request(
        fake_definition(),
        request,
        load=lambda _request, _context: object(),
        synthesize=synthesize,
        context=fake_context(tmp_path),
        synchronize=lambda: synchronizations.append(None),
    )
    assert result.ok
    assert calls == [None, Path(request.output_path)]
    assert len(synchronizations) == 3
    assert float(result.metadata["warmup_seconds"]) >= 0.0
    assert result.metadata["timing_mode"] == "warm"


def test_worker_rejects_tampered_reference_and_unknown_settings(tmp_path: Path) -> None:
    request = request_fixture(tmp_path)
    Path(request.reference.path).write_bytes(b"tampered")
    with pytest.raises(SecurityError, match="reference audio digest"):
        execute_request(
            fake_definition(),
            request,
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, _output: 0,
            context=fake_context(tmp_path),
        )

    unknown = request.model_copy(update={"settings": {"unknown": True}})
    with pytest.raises(ContractError, match="unsupported worker settings"):
        execute_request(
            fake_definition(),
            unknown,
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, _output: 0,
            context=fake_context(tmp_path),
        )


def test_worker_requires_tailnet_local_host_consent_boundary(tmp_path: Path) -> None:
    request = request_fixture(tmp_path, execution_boundaries=("capture-host",))

    with pytest.raises(SecurityError, match="execution boundary tailnet-local-host"):
        execute_request(
            fake_definition(),
            request,
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, _output: 0,
            context=fake_context(tmp_path),
        )


def test_voice_conversion_source_is_bound_to_request_digest_and_size(tmp_path: Path) -> None:
    direct = request_fixture(tmp_path)
    source_path = (tmp_path / "performed-source.wav").resolve()
    write_tone(source_path, sample_rate=22_050)
    source_digest, source_size = sha256_file(source_path)
    request = ExperimentRequest.model_validate(
        direct.model_dump(mode="python")
        | {
            "track": "voice-conversion",
            "source_audio_path": str(source_path),
            "source_audio_sha256": source_digest,
            "source_audio_size_bytes": source_size,
        }
    )
    definition = replace(fake_definition(), track="voice-conversion")

    result = execute_request(
        definition,
        request,
        load=lambda _request, _context: object(),
        synthesize=lambda _model, active_request, _context, output: (
            write_tone(output) or active_request.seed
        ),
        context=fake_context(tmp_path),
        synchronize=lambda: None,
    )
    assert result.metadata["source_audio_sha256"] == source_digest
    assert result.metadata["source_audio_size_bytes"] == source_size

    tampered_request = request.model_copy(update={"source_audio_sha256": "f" * 64})
    Path(request.output_path).unlink()
    with pytest.raises(SecurityError, match="digest or size"):
        execute_request(
            definition,
            tampered_request,
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, _output: 0,
            context=fake_context(tmp_path),
        )


def test_request_reader_is_bounded_and_strict(tmp_path: Path) -> None:
    request = request_fixture(tmp_path)
    assert read_request(io.StringIO(request.model_dump_json())) == request
    with pytest.raises(ContractError, match="size limit"):
        read_request(io.StringIO("x" * (2 * 1024 * 1024 + 1)))
    with pytest.raises(ContractError, match="UTF-8 JSON text"):
        read_request(io.BytesIO(b"{}"))  # type: ignore[arg-type]


def test_setting_readers_are_strict(tmp_path: Path) -> None:
    request = request_fixture(tmp_path).model_copy(
        update={"settings": {"count": 3, "ratio": 0.5, "enabled": True}}
    )
    assert integer_setting(request, "count", 1, minimum=1) == 3
    assert float_setting(request, "ratio", 1.0) == 0.5
    assert bool_setting(request, "enabled", False)
    assert integer_setting(request, "missing", 4) == 4
    with pytest.raises(ContractError, match="integer"):
        integer_setting(request, "enabled", 1)
    with pytest.raises(ContractError, match="at least"):
        integer_setting(
            request.model_copy(update={"settings": {"count": 0}}), "count", 1, minimum=1
        )
    with pytest.raises(ContractError, match="numeric"):
        float_setting(request, "enabled", 1.0)
    with pytest.raises(ContractError, match="boolean"):
        bool_setting(request, "count", False)


def test_context_from_environment_binds_all_local_manifests(tmp_path: Path, monkeypatch) -> None:
    request = request_fixture(tmp_path)
    model = tmp_path / "model"
    auxiliary = tmp_path / "auxiliary"
    model.mkdir()
    auxiliary.mkdir()
    write_canonical_json(
        model / ".cole-voice-lab-source.json",
        LocalSourceManifest(
            source_id="model",
            kind="huggingface",
            repository=request.model_id,
            revision=request.model_revision,
        ),
    )
    write_canonical_json(
        auxiliary / ".cole-voice-lab-source.json",
        LocalSourceManifest(
            source_id="aux",
            kind="huggingface",
            repository="example/aux",
            revision="d" * 40,
        ),
    )
    definition = fake_definition()
    definition = WorkerDefinition(
        candidate_id=definition.candidate_id,
        track=definition.track,
        code_revision=definition.code_revision,
        model_revisions=definition.model_revisions,
        auxiliary_revisions={"example/aux": "d" * 40},
        allowed_settings=definition.allowed_settings,
        package_name=None,
        package_version=None,
    )
    monkeypatch.setenv("COLE_VOICE_LAB_MODEL_PATH", str(model))
    monkeypatch.setenv("COLE_VOICE_LAB_AUXILIARY_PATHS", f'{{"example/aux": "{auxiliary}"}}')
    context = WorkerContext.from_environment(definition, request)
    assert context.model_path == model
    assert context.auxiliary_paths["example/aux"] == auxiliary
    assert len(context.model_manifest_sha256) == 64

    monkeypatch.setenv("COLE_VOICE_LAB_AUXILIARY_PATHS", "not-json")
    with pytest.raises(ContractError, match="invalid auxiliary"):
        WorkerContext.from_environment(definition, request)
    monkeypatch.delenv("COLE_VOICE_LAB_MODEL_PATH")
    with pytest.raises(RuntimeUnavailableError, match="local model path"):
        WorkerContext.from_environment(definition, request)


def test_context_environment_rejects_unbound_maps_and_code_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    request = request_fixture(tmp_path)
    model = tmp_path / "model"
    model.mkdir()
    write_canonical_json(
        model / ".cole-voice-lab-source.json",
        LocalSourceManifest(
            source_id="model",
            kind="huggingface",
            repository=request.model_id,
            revision=request.model_revision,
        ),
    )
    definition = replace(fake_definition(), package_name=None, package_version=None)
    monkeypatch.setenv("COLE_VOICE_LAB_MODEL_PATH", str(model))

    monkeypatch.setenv("COLE_VOICE_LAB_AUXILIARY_PATHS", "[]")
    with pytest.raises(ContractError, match="string paths"):
        WorkerContext.from_environment(definition, request)

    monkeypatch.setenv("COLE_VOICE_LAB_AUXILIARY_PATHS", '{"unexpected": "/tmp"}')
    with pytest.raises(ContractError, match="path set"):
        WorkerContext.from_environment(definition, request)

    monkeypatch.setenv("COLE_VOICE_LAB_AUXILIARY_PATHS", "{}")
    monkeypatch.setenv("COLE_VOICE_LAB_CODE_INTEGRITY_SHA256", "a" * 64)
    with pytest.raises(ContractError, match="without a checkout"):
        WorkerContext.from_environment(definition, request)

    code = tmp_path / "code"
    code.mkdir()
    monkeypatch.setenv("COLE_VOICE_LAB_CODE_PATH", str(code))
    monkeypatch.setenv("COLE_VOICE_LAB_CODE_INTEGRITY_SHA256", "invalid")
    with pytest.raises(ContractError, match="parent-verified"):
        WorkerContext.from_environment(definition, request)

    monkeypatch.setenv("COLE_VOICE_LAB_CODE_INTEGRITY_SHA256", "a" * 64)
    monkeypatch.setattr(
        common,
        "verify_git_checkout_integrity",
        lambda _path, _revision: ("b" * 64, {}),
    )
    with pytest.raises(SecurityError, match="differs from parent"):
        WorkerContext.from_environment(definition, request)

    monkeypatch.setattr(
        common,
        "verify_git_checkout_integrity",
        lambda _path, _revision: ("a" * 64, {}),
    )
    context = WorkerContext.from_environment(definition, request)
    assert context.code_path == code
    assert context.code_checkout_integrity_sha256 == "a" * 64


def test_source_manifest_and_request_definition_must_match(tmp_path: Path) -> None:
    request = request_fixture(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    write_canonical_json(
        source / ".cole-voice-lab-source.json",
        LocalSourceManifest(
            source_id="wrong",
            kind="huggingface",
            repository="wrong/repository",
            revision="a" * 40,
        ),
    )
    with pytest.raises(ContractError, match="does not bind"):
        common._verify_source_manifest(
            source, repository=request.model_id, revision=request.model_revision
        )

    definition = fake_definition()
    with pytest.raises(ContractError, match="candidate/track"):
        common._validate_request(
            definition, request.model_copy(update={"candidate_id": "other-worker"})
        )
    with pytest.raises(ContractError, match="immutable revision"):
        common._validate_request(
            definition, request.model_copy(update={"model_revision": "f" * 40})
        )
    with pytest.raises(ContractError, match="code revision"):
        common._validate_request(definition, request.model_copy(update={"code_revision": "f" * 40}))
    with pytest.raises(ContractError, match="timing_mode"):
        common._validate_request(
            definition,
            request.model_copy(update={"settings": {"timing_mode": "sometimes"}}),
        )
    voice_conversion = request.model_copy(
        update={"track": "voice-conversion", "settings": {"timing_mode": "warm"}}
    )
    with pytest.raises(ContractError, match="cold-only"):
        common._validate_request(replace(definition, track="voice-conversion"), voice_conversion)


def test_environment_evidence_fails_closed_and_canonicalizes_empty_freeze(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "output.wav"
    monkeypatch.setattr(
        common.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ("pip", "freeze"), 1, "", "freeze failed"
        ),
    )
    with pytest.raises(RuntimeUnavailableError, match="freeze failed"):
        common._environment_evidence(output, "trial-failed")

    monkeypatch.setattr(
        common.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(("pip", "freeze"), 0, "", ""),
    )
    evidence = common._environment_evidence(output, "trial-empty")
    assert (tmp_path / evidence.dependency_evidence_path).read_bytes() == b""


def test_model_state_failure_removes_unbound_output(tmp_path: Path) -> None:
    request = request_fixture(tmp_path)

    def tampering_synthesis(_model, active_request, _context, output_path):
        assert output_path is not None
        write_tone(output_path)
        Path(active_request.reference.path).write_bytes(b"changed during inference")
        return active_request.seed

    with pytest.raises(SecurityError, match="reference audio digest"):
        execute_request(
            fake_definition(),
            request,
            load=lambda _request, _context: object(),
            synthesize=tampering_synthesis,
            context=fake_context(tmp_path),
            synchronize=lambda: None,
        )
    assert not Path(request.output_path).exists()


def test_execute_request_rejects_existing_output_before_loading(
    tmp_path: Path, monkeypatch
) -> None:
    request = request_fixture(tmp_path)
    Path(request.output_path).write_bytes(b"existing")
    monkeypatch.setattr(common, "_installed_package_version", lambda _definition: None)
    monkeypatch.setattr(
        common, "_executed_code_evidence", lambda _definition: {"integrity_sha256": "a" * 64}
    )
    monkeypatch.setattr(common, "_verify_human_bindings", lambda _request: ("a" * 64, None, None))

    with pytest.raises(ContractError, match="overwrite worker output"):
        execute_request(
            replace(fake_definition(), package_name=None, package_version=None),
            request,
            load=lambda _request, _context: pytest.fail("model must not load"),
            synthesize=lambda _model, _request, _context, _output: 0,
            context=fake_context(tmp_path),
        )


def test_execute_request_detects_binding_change_and_cleans_output(
    tmp_path: Path, monkeypatch
) -> None:
    request = request_fixture(tmp_path)
    bindings = iter((("a" * 64, None, None), ("b" * 64, None, None)))
    monkeypatch.setattr(common, "_installed_package_version", lambda _definition: None)
    monkeypatch.setattr(
        common, "_executed_code_evidence", lambda _definition: {"integrity_sha256": "a" * 64}
    )
    monkeypatch.setattr(common, "_verify_human_bindings", lambda _request: next(bindings))

    with pytest.raises(SecurityError, match="changed during inference"):
        execute_request(
            replace(fake_definition(), package_name=None, package_version=None),
            request,
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, output: write_tone(output) or 0,
            context=fake_context(tmp_path),
            synchronize=lambda: None,
        )
    assert not Path(request.output_path).exists()


def test_execute_request_omits_inapplicable_watermark_and_protects_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    request = request_fixture(tmp_path)
    definition = replace(
        fake_definition(), package_name=None, package_version=None, watermarked=None
    )
    code_evidence = {"schema_version": "fixture", "integrity_sha256": "a" * 64}
    monkeypatch.setattr(common, "_installed_package_version", lambda _definition: None)
    monkeypatch.setattr(common, "_executed_code_evidence", lambda _definition: code_evidence)
    monkeypatch.setattr(common, "_verify_human_bindings", lambda _request: ("a" * 64, None, None))
    monkeypatch.setattr(
        common,
        "_environment_evidence",
        lambda _output, trial_id: EnvironmentEvidence(
            python_version="3.12",
            platform="macOS",
            machine="arm64",
            dependency_fingerprint_sha256="c" * 64,
            dependency_evidence_path=f"evidence/{trial_id}.pip-freeze.txt",
        ),
    )

    result = execute_request(
        definition,
        request,
        load=lambda _request, _context: object(),
        synthesize=lambda _model, _request, _context, output: write_tone(output) or 0,
        context=fake_context(tmp_path),
        synchronize=lambda: None,
    )
    assert "watermarked" not in result.metadata

    Path(request.output_path).unlink()
    model_evidence = tmp_path / "evidence" / f"{request.trial_id}.model-evidence.json"
    model_evidence.write_text("existing", encoding="utf-8")
    with pytest.raises(ContractError, match="overwrite model evidence"):
        execute_request(
            definition,
            request,
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, output: write_tone(output) or 0,
            context=fake_context(tmp_path),
            synchronize=lambda: None,
        )


def test_worker_main_always_emits_one_result_for_a_valid_request(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    request = request_fixture(tmp_path)
    failed = common._failure_result(request, ContractError("fixture failure"))
    monkeypatch.setattr(common, "read_request", lambda: request)
    monkeypatch.setattr(common, "execute_request", lambda *args, **kwargs: failed)
    assert (
        common.worker_main(
            fake_definition(),
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, _output: 0,
        )
        == 0
    )
    assert "fixture failure" in capsys.readouterr().out

    monkeypatch.setattr(common, "read_request", lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert (
        common.worker_main(
            fake_definition(),
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, _output: 0,
        )
        == 2
    )
    assert "invalid worker request" in capsys.readouterr().err

    monkeypatch.setattr(common, "read_request", lambda: request)
    monkeypatch.setattr(
        common,
        "execute_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(ContractError("execution failed")),
    )
    assert (
        common.worker_main(
            fake_definition(),
            load=lambda _request, _context: object(),
            synthesize=lambda _model, _request, _context, _output: 0,
        )
        == 0
    )
    assert "execution failed" in capsys.readouterr().out


def test_internal_hub_calls_are_redirected_only_to_curated_paths(
    tmp_path: Path, monkeypatch
) -> None:
    auxiliary = tmp_path / "s3"
    auxiliary.mkdir()
    (auxiliary / "model.safetensors").write_bytes(b"fixture")
    fake_hub = types.SimpleNamespace(
        snapshot_download=lambda *args, **kwargs: "network",
        hf_hub_download=lambda *args, **kwargs: "network",
    )
    monkeypatch.setattr(
        "cole_voice_lab.workers.common.importlib.import_module", lambda _name: fake_hub
    )
    with local_huggingface_overrides({"example/aux": auxiliary}):
        assert fake_hub.snapshot_download("example/aux") == str(auxiliary)
        assert fake_hub.hf_hub_download("example/aux", "model.safetensors") == str(
            auxiliary / "model.safetensors"
        )
        with pytest.raises(RuntimeUnavailableError, match="unprovisioned"):
            fake_hub.snapshot_download("mutable/unknown")
        with pytest.raises(RuntimeUnavailableError, match="unprovisioned"):
            fake_hub.hf_hub_download("mutable/unknown", "weights.bin")
        with pytest.raises(RuntimeUnavailableError, match="missing"):
            fake_hub.hf_hub_download("example/aux", "missing.bin")
    assert fake_hub.snapshot_download() == "network"


def test_worker_definitions_match_locked_runtime_boundaries() -> None:
    assert QWEN.package_version == "0.5.0"
    assert len(QWEN.model_revisions) == 2
    assert TURBO.package_version == V3.package_version == "0.5.0"
    assert TURBO.watermarked is False
    assert V3.watermarked is False
    assert F5.package_version == "1.1.22"
    assert SEED.track == "voice-conversion"
    assert SEED.code_revision == "51383efd921027683c89e5348211d93ff12ac2a8"
    assert tuple(_no_progress((1, 2), ignored=True)) == (1, 2)
    assert snapshot_worker._REVISION.fullmatch("a" * 40)


def test_installed_package_evidence_detects_missing_and_drift() -> None:
    actual = importlib.metadata.version("pydantic")
    definition = WorkerDefinition(
        candidate_id="package-test",
        track="direct-tts",
        code_revision="a" * 40,
        model_revisions={"model": "b" * 40},
        auxiliary_revisions={},
        allowed_settings=frozenset(),
        package_name="pydantic",
        package_version=actual,
    )
    assert common._installed_package_version(definition) == actual
    with pytest.raises(RuntimeUnavailableError, match="drift"):
        common._installed_package_version(
            WorkerDefinition(
                candidate_id=definition.candidate_id,
                track=definition.track,
                code_revision=definition.code_revision,
                model_revisions=definition.model_revisions,
                auxiliary_revisions={},
                allowed_settings=frozenset(),
                package_name="pydantic",
                package_version="0.0.0",
            )
        )
    with pytest.raises(RuntimeUnavailableError, match="missing"):
        common._installed_package_version(
            WorkerDefinition(
                candidate_id=definition.candidate_id,
                track=definition.track,
                code_revision=definition.code_revision,
                model_revisions=definition.model_revisions,
                auxiliary_revisions={},
                allowed_settings=frozenset(),
                package_name="definitely-not-installed-voice-lab-package",
                package_version="1.0.0",
            )
        )

    assert (
        common._installed_package_version(
            replace(definition, package_name=None, package_version=None)
        )
        is None
    )


def test_distribution_and_runtime_graph_evidence_fail_closed(tmp_path: Path, monkeypatch) -> None:
    def missing_distribution(_name: str) -> None:
        raise importlib.metadata.PackageNotFoundError("missing")

    monkeypatch.setattr(common.importlib.metadata, "distribution", missing_distribution)
    with pytest.raises(RuntimeUnavailableError, match="distribution is missing"):
        common._distribution_file_evidence("missing")

    no_files = types.SimpleNamespace(files=None, version="1.0")
    monkeypatch.setattr(common.importlib.metadata, "distribution", lambda _name: no_files)
    with pytest.raises(RuntimeUnavailableError, match="no installed-file manifest"):
        common._distribution_file_evidence("empty")

    outside = tmp_path / "outside.py"
    outside.write_text("fixture", encoding="utf-8")
    escaping = types.SimpleNamespace(
        files=("outside.py",),
        version="1.0",
        locate_file=lambda _entry: outside,
    )
    monkeypatch.setattr(common.importlib.metadata, "distribution", lambda _name: escaping)
    with pytest.raises(SecurityError, match="escapes the isolated runtime"):
        common._distribution_file_evidence("escape")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    unsafe = runtime / "unsafe"
    unsafe.mkdir()
    nonregular = types.SimpleNamespace(
        files=("unsafe",),
        version="1.0",
        locate_file=lambda _entry: unsafe,
    )
    monkeypatch.setattr(common.sys, "prefix", str(runtime))
    monkeypatch.setattr(common.importlib.metadata, "distribution", lambda _name: nonregular)
    with pytest.raises(SecurityError, match="non-regular file"):
        common._distribution_file_evidence("unsafe")

    monkeypatch.setattr(common.importlib.metadata, "distributions", lambda: ())
    with pytest.raises(RuntimeUnavailableError, match="no attestable distributions"):
        common._installed_distribution_names()

    unnamed = types.SimpleNamespace(metadata={})
    monkeypatch.setattr(common.importlib.metadata, "distributions", lambda: (unnamed,))
    with pytest.raises(RuntimeUnavailableError, match="canonical name"):
        common._installed_distribution_names()


def test_executed_code_evidence_requires_worker_and_runtime_distributions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(common, "_installed_distribution_names", lambda: ("cole-voice-lab",))
    with pytest.raises(RuntimeUnavailableError, match="omits required"):
        common._executed_code_evidence(
            replace(fake_definition(), package_name="mlx_audio", package_version="0.5.0")
        )

    monkeypatch.setattr(
        common,
        "_worker_package_tree_evidence",
        lambda: {"root_name": "cole_voice_lab", "files": {"workers/common.py": {}}},
    )
    monkeypatch.setattr(
        common,
        "_distribution_file_evidence",
        lambda _name: {"version": "1.0", "files": {"package.py": {}}},
    )
    evidence = common._executed_code_evidence(
        replace(fake_definition(), package_name=None, package_version=None)
    )
    assert evidence["integrity_sha256"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SecurityError("x"), "security_error"),
        (ContractError("x"), "contract_error"),
        (RuntimeUnavailableError("x"), "runtime_unavailable"),
        (ValueError("x"), "inference_failed"),
    ],
)
def test_failure_results_are_structured(tmp_path: Path, error: Exception, code: str) -> None:
    result = common._failure_result(request_fixture(tmp_path), error)
    assert result.error_code == code


def test_failure_result_uses_exception_class_for_empty_message(tmp_path: Path) -> None:
    result = common._failure_result(request_fixture(tmp_path), ValueError())
    assert result.error_message == "ValueError"


def test_seed_auxiliary_resolver_never_falls_back_to_network(tmp_path: Path) -> None:
    camp = tmp_path / "camp"
    camp.mkdir()
    (camp / "campplus.bin").write_bytes(b"fixture")
    context = WorkerContext(
        model_path=tmp_path,
        auxiliary_paths={"funasr/campplus": camp},
        code_path=None,
        model_manifest_sha256="c" * 64,
        auxiliary_manifest_sha256={},
    )
    assert _local_model_resolver(context, "funasr/campplus", "campplus.bin") == str(
        camp / "campplus.bin"
    )
    with pytest.raises(RuntimeUnavailableError, match="unprovisioned"):
        _local_model_resolver(context, "unknown/model", "weights.bin")
    with pytest.raises(RuntimeUnavailableError, match="missing"):
        _local_model_resolver(context, "funasr/campplus", "missing.bin")
