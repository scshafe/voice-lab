from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_fixtures import synthetic_python_runtime

from cole_voice_lab.canonical import canonical_sha256, write_canonical_json
from cole_voice_lab.errors import ContractError, RuntimeUnavailableError, SecurityError
from cole_voice_lab.models import ExperimentRequest, ExperimentResult, ReferenceEvidence
from cole_voice_lab.registry import (
    RuntimeLock,
    SourceFileLock,
    SourceLock,
    load_registry,
    runtime_artifact_versions,
)
from cole_voice_lab.runtime import (
    MINIMUM_FREE_BYTES,
    SOURCE_STAGING_MARGIN_BYTES,
    RuntimeInstallPlan,
    SourceVerification,
    WorkerInvocation,
    _immutable_tree_sha256,
    _sanitize_reviewed_runtime_files,
    assert_disk_floor,
    build_runtime_eviction_plan,
    build_runtime_install_plan,
    build_source_eviction_plan,
    build_source_provision_plan,
    exclusive_worker_slot,
    execute_provision_plan,
    execute_runtime_eviction,
    execute_runtime_install_plan,
    execute_source_eviction,
    plan_commands,
    run_worker,
    source_paths_by_repository,
    verify_local_source,
    verify_python_runtime,
    verify_reviewed_wheelhouse,
    verify_runtime_install_receipt,
    worker_environment,
    write_runtime_install_receipt,
    write_source_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
DISTUTILS_PTH = (
    b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') "
    b"== 'local'; enabled and __import__('_distutils_hack').add_shim(); \n"
)


def registry():
    return load_registry(ROOT / "config/candidates.toml", ROOT / "config/model-sources.lock.json")


def tiny_source(payload: bytes = b"locked model") -> SourceLock:
    return SourceLock(
        source_id="tiny-model",
        kind="huggingface",
        repository="example/tiny-model",
        revision="a" * 40,
        license="MIT",
        allow_patterns=("weights.bin",),
        files=(
            SourceFileLock(
                path="weights.bin",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )


def reviewed_lab_wheel(tmp_path: Path) -> tuple[Path, str]:
    wheel = tmp_path / "cole_voice_lab-0.1.0.dev0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "cole_voice_lab-0.1.0.dev0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: cole-voice-lab\nVersion: 0.1.0.dev0\n",
        )
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def sealed_runtime_fixture(tmp_path: Path, monkeypatch) -> tuple[RuntimeLock, Path, Path, str]:
    host = synthetic_python_runtime(tmp_path, monkeypatch)
    locked = host.registry.runtime("mlx-audio-0.5.0")
    environment = tmp_path / "environment"
    site_packages = host.create_environment(environment)
    artifact_versions = runtime_artifact_versions(locked, ROOT)
    installed_versions = {**artifact_versions, "cole-voice-lab": "0.1.0.dev0"}
    for name, version in installed_versions.items():
        metadata = site_packages / f"{name}-{version}.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text(
            f"Metadata-Version: 2.4\nName: {name.replace('_', '-')}\nVersion: {version}\n"
        )
    lab_wheel, lab_sha256 = reviewed_lab_wheel(tmp_path)
    plan = RuntimeInstallPlan(
        runtime=locked,
        repository_root=ROOT,
        environment_path=environment,
        reviewed_wheelhouse=None,
        lab_wheel=lab_wheel,
        lab_wheel_sha256=lab_sha256,
        lab_distribution_version="0.1.0.dev0",
        artifact_versions=artifact_versions,
        commands=(),
    )
    receipt_path, receipt_sha256 = write_runtime_install_receipt(plan)
    return locked, environment, receipt_path, receipt_sha256


def test_disk_floor_passes_and_fails_without_writing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "cole_voice_lab.runtime.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=MINIMUM_FREE_BYTES, total=1, used=0),
    )
    assert assert_disk_floor(tmp_path / "not-created") == MINIMUM_FREE_BYTES
    monkeypatch.setattr(
        "cole_voice_lab.runtime.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=MINIMUM_FREE_BYTES - 1, total=1, used=0),
    )
    with pytest.raises(RuntimeUnavailableError, match="disk floor"):
        assert_disk_floor(tmp_path)


def test_curated_snapshot_manifest_detects_extra_and_tampered_files(tmp_path: Path) -> None:
    source = tiny_source()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "weights.bin").write_bytes(b"locked model")
    manifest_digest = write_source_manifest(source, snapshot)
    verification = verify_local_source(source, snapshot)
    assert verification.manifest_sha256 == manifest_digest
    assert verification.file_sha256["weights.bin"] == source.files[0].sha256

    (snapshot / "unexpected.bin").write_bytes(b"not allow-listed")
    with pytest.raises(ContractError, match="non-allow-listed"):
        verify_local_source(source, snapshot)
    (snapshot / "unexpected.bin").unlink()
    (snapshot / "weights.bin").write_bytes(b"tampered!!!!!")
    with pytest.raises(ContractError, match="digest mismatch|size mismatch"):
        verify_local_source(source, snapshot)


def test_source_verification_rejects_root_links_extra_links_and_hardlinks(tmp_path: Path) -> None:
    source = tiny_source()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    artifact = snapshot / "weights.bin"
    artifact.write_bytes(b"locked model")
    write_source_manifest(source, snapshot)

    alias = tmp_path / "snapshot-alias"
    alias.symlink_to(snapshot, target_is_directory=True)
    with pytest.raises(SecurityError, match="symlink component"):
        verify_local_source(source, alias)

    extra_target = tmp_path / "extra-target"
    extra_target.write_bytes(b"unreviewed")
    extra = snapshot / "extra-link"
    extra.symlink_to(extra_target)
    with pytest.raises(SecurityError, match="forbidden symlink"):
        verify_local_source(source, snapshot)
    extra.unlink()

    hardlink = tmp_path / "artifact-hardlink"
    os.link(artifact, hardlink)
    with pytest.raises(SecurityError, match="exactly one hard link"):
        verify_local_source(source, snapshot)


def test_worker_lock_rejects_final_symlink_without_touching_target(tmp_path: Path) -> None:
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir(mode=0o700)
    outside = tmp_path / "outside-lock-target"
    outside.write_text("must remain unchanged\n", encoding="utf-8")
    (runtimes / "worker.lock").symlink_to(outside)

    with pytest.raises(SecurityError, match="safely open"):
        with exclusive_worker_slot(runtimes / "worker.lock", boundary=runtimes):
            pass
    assert outside.read_text(encoding="utf-8") == "must remain unchanged\n"


def test_runtime_tree_rejects_external_symlink_bridge_that_reenters_root(tmp_path: Path) -> None:
    root = tmp_path / "reviewed-keg"
    outside = tmp_path / "mutable-bridge"
    root.mkdir()
    outside.mkdir()
    (root / "a").write_bytes(b"locked-a")
    (root / "b").write_bytes(b"locked-b")
    (outside / "bridge").symlink_to(root / "a")
    (root / "reenter").symlink_to(outside / "bridge")

    with pytest.raises(SecurityError, match="escapes its reviewed root"):
        _immutable_tree_sha256(root, label="fixture keg")
    (outside / "bridge").unlink()
    (outside / "bridge").symlink_to(root / "b")
    with pytest.raises(SecurityError, match="escapes its reviewed root"):
        _immutable_tree_sha256(root, label="fixture keg")

    (root / "reenter").unlink()
    (root / "internal").symlink_to("a")
    assert len(_immutable_tree_sha256(root, label="fixture keg")) == 64


def test_model_snapshot_eviction_is_exact_digest_gated_and_confined(tmp_path: Path) -> None:
    source = tiny_source()
    boundary = tmp_path / "models/sources"
    snapshot = boundary / source.source_id
    snapshot.mkdir(parents=True)
    (snapshot / "weights.bin").write_bytes(b"locked model")
    manifest_digest = write_source_manifest(source, snapshot)

    plan = build_source_eviction_plan(source, snapshot, boundary=boundary)
    assert plan.manifest_sha256 == manifest_digest
    assert plan.estimated_bytes >= len(b"locked model")
    with pytest.raises(RuntimeUnavailableError, match="explicit confirmation"):
        execute_source_eviction(
            source,
            plan,
            boundary=boundary,
            expected_manifest_sha256=manifest_digest,
        )
    with pytest.raises(ContractError, match="does not match"):
        execute_source_eviction(
            source,
            plan,
            boundary=boundary,
            expected_manifest_sha256="f" * 64,
            confirmed=True,
        )
    removed = execute_source_eviction(
        source,
        plan,
        boundary=boundary,
        expected_manifest_sha256=manifest_digest,
        confirmed=True,
    )
    assert removed == plan.estimated_bytes
    assert not snapshot.exists()


def test_model_snapshot_eviction_rejects_wrong_boundary_and_git(tmp_path: Path) -> None:
    source = tiny_source()
    boundary = tmp_path / "models/sources"
    boundary.mkdir(parents=True)
    snapshot = tmp_path / source.source_id
    snapshot.mkdir()
    (snapshot / "weights.bin").write_bytes(b"locked model")
    write_source_manifest(source, snapshot)
    with pytest.raises(SecurityError, match="direct"):
        build_source_eviction_plan(source, snapshot, boundary=boundary)

    git_source = SourceLock(
        source_id="git-source",
        kind="git",
        repository="https://example.invalid/repo.git",
        revision="a" * 40,
        license="MIT",
    )
    with pytest.raises(ContractError, match="Hugging Face"):
        build_source_eviction_plan(git_source, snapshot, boundary=tmp_path)


def test_download_and_runtime_plans_are_dry_until_explicitly_allowed(
    tmp_path: Path, monkeypatch
) -> None:
    source = tiny_source()
    plan = build_source_provision_plan(
        source,
        tmp_path / "model",
        runtime_python=Path("/runtime/bin/python"),
    )
    command = plan_commands(plan)[0]
    assert command[:5] == (
        "/runtime/bin/python",
        "-I",
        "-P",
        "-m",
        "cole_voice_lab.workers.snapshot",
    )
    assert plan.estimated_download_bytes == len(b"locked model")
    assert plan.required_free_bytes == (
        MINIMUM_FREE_BYTES + len(b"locked model") + SOURCE_STAGING_MARGIN_BYTES
    )
    assert command[command.index("--revision") + 1] == "a" * 40
    assert command[-2:] == ("--allow-file", "weights.bin")
    with pytest.raises(RuntimeUnavailableError, match="explicit allow_downloads"):
        execute_provision_plan(plan)
    assert not plan.destination.exists()

    host = synthetic_python_runtime(tmp_path, monkeypatch)
    runtime = host.registry.runtime("mlx-audio-0.5.0")
    install = build_runtime_install_plan(
        runtime,
        tmp_path / "runtime",
        base_python=host.python,
        repository_root=ROOT,
    )
    assert install.commands[0][1:6] == ("-I", "-P", "-S", "-m", "venv")
    assert "--require-hashes" in install.commands[1]
    assert install.commands[1][-1].endswith("requirements/runtimes/mlx-audio-0.5.0.artifacts.txt")
    assert "REVIEWED-COLE-VOICE-LAB-WHEEL-REQUIRED.whl" in install.commands[2][-1]
    assert install.commands[3][-2:] == ("pip", "check")


def test_source_path_mapping_requires_every_auxiliary(tmp_path: Path) -> None:
    sources = registry().resolve("chatterbox-turbo", "smoke").auxiliary_sources
    mapped = source_paths_by_repository(sources, {sources[0].source_id: tmp_path})
    assert mapped == {"mlx-community/S3TokenizerV2": tmp_path}
    with pytest.raises(ContractError, match="missing local path"):
        source_paths_by_repository(sources, {})


def test_fake_curated_provision_executes_only_after_opt_in(tmp_path: Path, monkeypatch) -> None:
    source = tiny_source()
    destination = tmp_path / "model"
    plan = build_source_provision_plan(
        source,
        destination,
        runtime_python=Path("/runtime/bin/python"),
    )
    disk_checks: list[int | None] = []

    def fake_disk_floor(_path, *, minimum_free_bytes=None):
        disk_checks.append(minimum_free_bytes)
        return 123

    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", fake_disk_floor)

    def fake_download(command, **kwargs):
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["env"]["PYTHONNOUSERSITE"] == "1"
        assert command[:5] == (
            "/runtime/bin/python",
            "-I",
            "-P",
            "-m",
            "cole_voice_lab.workers.snapshot",
        )
        destination.mkdir()
        (destination / "weights.bin").write_bytes(b"locked model")
        return subprocess.CompletedProcess(command, 0, "", "")

    verification = execute_provision_plan(plan, allow_downloads=True, runner=fake_download)
    assert verification.source_id == "tiny-model"
    assert verification.manifest_sha256 is not None
    assert disk_checks[0] == plan.required_free_bytes
    assert disk_checks[1:] == [None, None]

    failing = build_source_provision_plan(
        source,
        tmp_path / "failed-model",
        runtime_python=Path("/runtime/bin/python"),
    )
    with pytest.raises(Exception, match="source provisioning failed"):
        execute_provision_plan(
            failing,
            allow_downloads=True,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 1, "", "network fixture failed"
            ),
        )


def test_git_source_verification_is_revision_exact(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(("git", "init", "-q", str(checkout)), check=True)
    subprocess.run(("git", "-C", str(checkout), "config", "user.name", "Test"), check=True)
    subprocess.run(
        ("git", "-C", str(checkout), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    (checkout / "README").write_text("fixture")
    subprocess.run(("git", "-C", str(checkout), "add", "README"), check=True)
    subprocess.run(("git", "-C", str(checkout), "commit", "-qm", "fixture"), check=True)
    revision = subprocess.run(
        ("git", "-C", str(checkout), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(("git", "-C", str(checkout), "checkout", "--detach", "-q"), check=True)
    source = SourceLock(
        source_id="git-fixture",
        kind="git",
        repository="https://example.invalid/repo.git",
        revision=revision,
        license="MIT",
    )
    assert verify_local_source(source, checkout).manifest_sha256 is None
    outside_hardlink = tmp_path / "tracked-hardlink"
    os.link(checkout / "README", outside_hardlink)
    with pytest.raises(SecurityError, match="exactly one hard link"):
        verify_local_source(source, checkout)
    outside_hardlink.unlink()
    (checkout / "ignored.py").write_text("raise RuntimeError('untrusted import')")
    (checkout / ".git/info/exclude").write_text("ignored.py\n")
    with pytest.raises(SecurityError, match="untracked, or ignored"):
        verify_local_source(source, checkout)
    (checkout / "ignored.py").unlink()
    (checkout / ".git/info/exclude").write_text("")
    (checkout / "README").write_text("tampered")
    with pytest.raises(SecurityError, match="tracked"):
        verify_local_source(source, checkout)
    (checkout / "README").write_text("fixture")
    wrong = source.model_copy(update={"revision": "f" * 40})
    with pytest.raises(RuntimeUnavailableError, match="not at"):
        verify_local_source(wrong, checkout)


def test_runtime_install_execution_is_explicit_and_fail_closed(tmp_path: Path, monkeypatch) -> None:
    host = synthetic_python_runtime(tmp_path, monkeypatch)
    locked = host.registry.runtime("mlx-audio-0.5.0")
    lab_wheel, lab_sha256 = reviewed_lab_wheel(tmp_path)
    plan = build_runtime_install_plan(
        locked,
        tmp_path / "environment",
        base_python=host.python,
        repository_root=ROOT,
        lab_wheel=lab_wheel,
        lab_wheel_sha256=lab_sha256,
    )
    with pytest.raises(RuntimeUnavailableError, match="explicit allow_downloads"):
        execute_runtime_install_plan(plan)
    disk_checks: list[int | None] = []

    def fake_disk_floor(_path, *, minimum_free_bytes=None):
        disk_checks.append(minimum_free_bytes)
        return 123

    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", fake_disk_floor)
    monkeypatch.setattr(
        "cole_voice_lab.runtime.write_runtime_install_receipt",
        lambda _plan: (tmp_path / "receipt.json", "a" * 64),
    )
    monkeypatch.setattr(
        "cole_voice_lab.runtime.verify_runtime_install_receipt",
        lambda *_args, **_kwargs: (None, "a" * 64),
    )
    calls: list[tuple[str, ...]] = []

    def success(command, **kwargs):
        calls.append(command)
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["env"]["PIP_CONFIG_FILE"] == "/dev/null"
        return subprocess.CompletedProcess(command, 0, "", "")

    execute_runtime_install_plan(plan, allow_downloads=True, runner=success)
    assert calls == list(plan.commands)
    assert disk_checks[0] == MINIMUM_FREE_BYTES + locked.estimated_install_bytes
    assert disk_checks[1:] == [None] * (len(plan.commands) + 1)
    failed_plan = build_runtime_install_plan(
        locked,
        tmp_path / "failed-environment",
        base_python=host.python,
        repository_root=ROOT,
        lab_wheel=lab_wheel,
        lab_wheel_sha256=lab_sha256,
    )
    with pytest.raises(Exception, match="runtime installation failed"):
        execute_runtime_install_plan(
            failed_plan,
            allow_downloads=True,
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 1, "", "pip fixture failed"
            ),
        )


def test_reviewed_wheelhouse_rejects_missing_extra_rebound_and_hardlinks(
    tmp_path: Path,
) -> None:
    locked = registry().runtime("f5-tts-1.1.22-mps")
    payload = b"reviewed local wheel fixture"
    evidence = locked.reviewed_built_wheels[0].model_copy(
        update={
            "wheel_filename": "fixture-1.0-py3-none-any.whl",
            "wheel_size_bytes": len(payload),
            "wheel_sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    fixture_runtime = locked.model_copy(update={"reviewed_built_wheels": (evidence,)})
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    with pytest.raises(SecurityError, match="membership mismatch"):
        verify_reviewed_wheelhouse(fixture_runtime, wheelhouse)

    wheel = wheelhouse / evidence.wheel_filename
    wheel.write_bytes(payload)
    assert verify_reviewed_wheelhouse(fixture_runtime, wheelhouse) == wheelhouse
    extra = wheelhouse / "extra.whl"
    extra.write_bytes(b"extra")
    with pytest.raises(SecurityError, match="membership mismatch"):
        verify_reviewed_wheelhouse(fixture_runtime, wheelhouse)
    extra.unlink()

    wheel.write_bytes(b"rebound local wheel fixture")
    with pytest.raises(SecurityError, match="bytes do not match"):
        verify_reviewed_wheelhouse(fixture_runtime, wheelhouse)
    wheel.write_bytes(payload)
    hardlink = tmp_path / "second-link.whl"
    os.link(wheel, hardlink)
    with pytest.raises(SecurityError, match="exactly one hard link"):
        verify_reviewed_wheelhouse(fixture_runtime, wheelhouse)


@pytest.mark.parametrize(
    "runtime_id",
    ("f5-tts-1.1.22-mps", "seed-vc-51383efd-mps"),
)
def test_reviewed_runtime_sanitizer_removes_exact_f5_and_seed_pth(
    tmp_path: Path,
    runtime_id: str,
) -> None:
    runtime = registry().runtime(runtime_id)
    environment = tmp_path / runtime_id
    target = environment / runtime.sanitized_files[0].path
    target.parent.mkdir(parents=True)
    target.write_bytes(DISTUTILS_PTH)
    plan = RuntimeInstallPlan(
        runtime=runtime,
        repository_root=ROOT,
        environment_path=environment,
        reviewed_wheelhouse=None,
        lab_wheel=None,
        lab_wheel_sha256=None,
        lab_distribution_version=None,
        artifact_versions={},
        commands=(),
    )
    _sanitize_reviewed_runtime_files(plan)
    assert not target.exists()


def test_reviewed_runtime_sanitizer_rejects_missing_and_rebound_pth(tmp_path: Path) -> None:
    runtime = registry().runtime("f5-tts-1.1.22-mps")
    environment = tmp_path / "f5-runtime"
    target = environment / runtime.sanitized_files[0].path
    target.parent.mkdir(parents=True)
    plan = RuntimeInstallPlan(
        runtime=runtime,
        repository_root=ROOT,
        environment_path=environment,
        reviewed_wheelhouse=None,
        lab_wheel=None,
        lab_wheel_sha256=None,
        lab_distribution_version=None,
        artifact_versions={},
        commands=(),
    )
    with pytest.raises(RuntimeUnavailableError, match="preimage is missing"):
        _sanitize_reviewed_runtime_files(plan)
    target.write_bytes(b"X" * len(DISTUTILS_PTH))
    with pytest.raises(SecurityError, match="preimage changed"):
        _sanitize_reviewed_runtime_files(plan)


def test_runtime_receipt_rejects_tree_pth_symlink_and_receipt_tamper(
    tmp_path: Path, monkeypatch
) -> None:
    locked, environment, receipt_path, receipt_sha256 = sealed_runtime_fixture(
        tmp_path, monkeypatch
    )
    receipt, observed = verify_runtime_install_receipt(
        locked,
        environment,
        repository_root=ROOT,
        expected_receipt_sha256=receipt_sha256,
    )
    assert observed == receipt_sha256
    assert receipt.installed_distributions == dict(
        sorted({**runtime_artifact_versions(locked, ROOT), "cole-voice-lab": "0.1.0.dev0"}.items())
    )

    site_packages = environment / "lib/python3.12/site-packages"
    injected = site_packages / "external-injection.pth"
    injected.write_text(str(tmp_path / "outside") + "\n")
    with pytest.raises(SecurityError, match="tree differs"):
        verify_runtime_install_receipt(
            locked,
            environment,
            repository_root=ROOT,
            expected_receipt_sha256=receipt_sha256,
        )
    injected.unlink()

    outside = tmp_path / "outside.py"
    outside.write_text("raise RuntimeError('unreviewed')\n")
    (site_packages / "injected.py").symlink_to(outside)
    with pytest.raises(SecurityError, match="forbidden symlink"):
        verify_runtime_install_receipt(
            locked,
            environment,
            repository_root=ROOT,
            expected_receipt_sha256=receipt_sha256,
        )
    (site_packages / "injected.py").unlink()

    receipt_alias = tmp_path / "receipt-alias.json"
    os.link(receipt_path, receipt_alias)
    with pytest.raises(SecurityError, match="link count"):
        verify_runtime_install_receipt(
            locked,
            environment,
            repository_root=ROOT,
            expected_receipt_sha256=receipt_sha256,
        )
    receipt_alias.unlink()

    receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
    with pytest.raises(SecurityError, match="digest differs"):
        verify_runtime_install_receipt(
            locked,
            environment,
            repository_root=ROOT,
            expected_receipt_sha256=receipt_sha256,
        )


def test_python_runtime_closure_rejects_wrong_binary_tree_and_pth(
    tmp_path: Path, monkeypatch
) -> None:
    locked, environment, _receipt_path, _receipt_sha256 = sealed_runtime_fixture(
        tmp_path, monkeypatch
    )
    runtime_lock = locked.python_runtime
    assert verify_python_runtime(
        runtime_lock,
        environment / "bin/python",
        environment_path=environment,
    ) == canonical_sha256(runtime_lock)

    wrong = tmp_path / "wrong-python"
    wrong.write_bytes(b"not the locked interpreter")
    with pytest.raises(SecurityError, match="entrypoint"):
        verify_python_runtime(runtime_lock, wrong)

    stale_tree = runtime_lock.model_copy(update={"prefix_tree_sha256": "f" * 64})
    with pytest.raises(SecurityError, match="prefix tree"):
        verify_python_runtime(stale_tree, environment / "bin/python")

    site_packages = environment / "lib/python3.12/site-packages"
    external = tmp_path / "external-imports"
    external.mkdir()
    (site_packages / "external.pth").write_text(str(external) + "\n", encoding="utf-8")
    with pytest.raises(SecurityError, match="import path escapes"):
        verify_python_runtime(
            runtime_lock,
            environment / "bin/python",
            environment_path=environment,
        )
    (site_packages / "external.pth").write_text(
        "import external_bootstrap\n",
        encoding="utf-8",
    )
    with pytest.raises(SecurityError, match="executable code"):
        verify_python_runtime(
            runtime_lock,
            environment / "bin/python",
            environment_path=environment,
        )


@pytest.mark.parametrize("changed_field", ("macos_product_version", "macos_build_version"))
def test_python_runtime_closure_rejects_changed_macos_tcb(
    tmp_path: Path, monkeypatch, changed_field: str
) -> None:
    host = synthetic_python_runtime(tmp_path, monkeypatch)
    setattr(host, changed_field, "99.9" if changed_field == "macos_product_version" else "99Z999")
    with pytest.raises(SecurityError, match="macOS runtime TCB differs"):
        verify_python_runtime(host.lock, host.python)


def test_repository_lock_rejects_a_self_consistent_forged_runtime_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    locked, environment, receipt_path, _receipt_sha256 = sealed_runtime_fixture(
        tmp_path, monkeypatch
    )
    receipt, _ = verify_runtime_install_receipt(
        locked,
        environment,
        repository_root=ROOT,
    )
    forged = receipt.model_copy(
        update={
            "installed_distributions": {
                "cole-voice-lab": "0.1.0.dev0",
                "attacker-runtime": "9.9.9",
            }
        }
    )
    forged_sha256 = write_canonical_json(receipt_path, forged)
    with pytest.raises(ContractError, match="stale installed_distributions"):
        verify_runtime_install_receipt(
            locked,
            environment,
            repository_root=ROOT,
            expected_receipt_sha256=forged_sha256,
        )


def test_failed_provision_and_runtime_install_remove_only_their_partial_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cole_voice_lab.runtime.assert_disk_floor",
        lambda *_args, **_kwargs: MINIMUM_FREE_BYTES,
    )
    source_plan = build_source_provision_plan(
        tiny_source(),
        tmp_path / "partial-source",
        runtime_python=Path("/runtime/bin/python"),
    )

    def failed_source(command, **_kwargs):
        source_plan.destination.mkdir()
        return subprocess.CompletedProcess(command, 1, "", "failed")

    with pytest.raises(Exception, match="source provisioning failed"):
        execute_provision_plan(source_plan, allow_downloads=True, runner=failed_source)
    assert not source_plan.destination.exists()

    lab_wheel, lab_sha256 = reviewed_lab_wheel(tmp_path)
    host = synthetic_python_runtime(tmp_path, monkeypatch)
    install_plan = build_runtime_install_plan(
        host.registry.runtime("mlx-audio-0.5.0"),
        tmp_path / "partial-runtime",
        base_python=host.python,
        repository_root=ROOT,
        lab_wheel=lab_wheel,
        lab_wheel_sha256=lab_sha256,
    )
    call_count = 0

    def failed_install(command, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            install_plan.environment_path.mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "failed")

    with pytest.raises(Exception, match="runtime installation failed"):
        execute_runtime_install_plan(
            install_plan,
            allow_downloads=True,
            runner=failed_install,
        )
    assert not install_plan.environment_path.exists()


def test_runtime_eviction_is_receipt_digest_confirmed_and_locked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locked = registry().runtime("mlx-audio-0.5.0")
    boundary = tmp_path / "runtimes"
    boundary.mkdir(mode=0o700)
    environment = boundary / locked.runtime_id
    environment.mkdir(mode=0o700)
    (environment / "payload").write_bytes(b"sealed runtime")
    monkeypatch.setattr(
        "cole_voice_lab.runtime.verify_runtime_install_receipt",
        lambda *_args, **_kwargs: (None, "a" * 64),
    )
    plan = build_runtime_eviction_plan(
        locked,
        environment,
        boundary=boundary,
        repository_root=ROOT,
    )
    assert plan.receipt_sha256 == "a" * 64
    assert plan.estimated_bytes >= len(b"sealed runtime")
    with pytest.raises(RuntimeUnavailableError, match="explicit confirmation"):
        execute_runtime_eviction(
            locked,
            plan,
            boundary=boundary,
            repository_root=ROOT,
            expected_receipt_sha256="a" * 64,
            orchestration_lock_path=boundary / "audition-orchestrator.lock",
        )
    with pytest.raises(ContractError, match="does not match"):
        execute_runtime_eviction(
            locked,
            plan,
            boundary=boundary,
            repository_root=ROOT,
            expected_receipt_sha256="b" * 64,
            orchestration_lock_path=boundary / "audition-orchestrator.lock",
            confirmed=True,
        )
    removed = execute_runtime_eviction(
        locked,
        plan,
        boundary=boundary,
        repository_root=ROOT,
        expected_receipt_sha256="a" * 64,
        orchestration_lock_path=boundary / "audition-orchestrator.lock",
        confirmed=True,
    )
    assert removed == plan.estimated_bytes
    assert not environment.exists()


def test_run_worker_uses_one_fresh_offline_subprocess(tmp_path: Path, monkeypatch) -> None:
    resolved = registry().resolve("qwen3-tts-base", "smoke")
    runtime_python = tmp_path / "runtime/bin/python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("stub")
    reference = ReferenceEvidence(
        path=str(tmp_path / "reference.wav"),
        sha256="a" * 64,
        transcript="Exact transcript.",
        subject_id="cole-self",
    )
    request = ExperimentRequest(
        experiment_id="bakeoff-1",
        trial_id="trial-1",
        candidate_id="qwen3-tts-base",
        track="direct-tts",
        text="Held-out line.",
        output_path=str(tmp_path / "out.wav"),
        reference=reference,
        model_id=resolved.model_id,
        model_revision=resolved.model_revision,
        code_revision=resolved.code_revision,
        settings={"timing_mode": "cold"},
    )
    invocation = WorkerInvocation(
        request=request,
        resolved=resolved,
        runtime_python=runtime_python,
        model_path=tmp_path / "model",
        auxiliary_paths={},
        worker_lock_path=tmp_path / "worker.lock",
        runtime_root=tmp_path,
    )
    monkeypatch.setattr("cole_voice_lab.runtime.assert_disk_floor", lambda _path: 123)
    source_verification = SourceVerification(
        resolved.model_source.source_id,
        tmp_path / "model",
        "a" * 64,
        {},
        "b" * 64,
    )
    monkeypatch.setattr(
        "cole_voice_lab.runtime.verify_local_source",
        lambda *_a, **_k: source_verification,
    )
    failed = ExperimentResult(
        trial_id="trial-1",
        candidate_id="qwen3-tts-base",
        ok=False,
        error_code="runtime_unavailable",
        error_message="dry-run fixture",
    )

    def fake_run(command, **kwargs):
        assert command[-2:] == ("-m", "cole_voice_lab.workers.qwen")
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        return subprocess.CompletedProcess(command, 0, failed.model_dump_json(), "worker log")

    monkeypatch.setattr("cole_voice_lab.runtime.subprocess.run", fake_run)
    result = run_worker(invocation)
    assert not result.ok
    assert result.metadata["worker_process_model"] == "fresh-subprocess-one-request"
    environment = worker_environment(invocation)
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert environment["COLE_VOICE_LAB_MODEL_PATH"] == str(tmp_path / "model")
    assert "PYTHONPATH" not in environment
    assert environment["PYTHONSAFEPATH"] == "1"
