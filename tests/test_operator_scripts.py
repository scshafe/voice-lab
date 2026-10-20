from __future__ import annotations

import ast
import base64
import csv
import functools
import hashlib
import io
import json
import os
import platform
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import zipfile
from collections.abc import Mapping
from pathlib import Path

import macos_only
import pytest

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name", ("stage-vault-image", "verify-vault-container", "check-vault-deployment")
)
def test_vault_operator_wrappers_are_isolated_and_explain_their_scope(name: str) -> None:
    script = REPOSITORY / "scripts" / name
    parsed = _run("/bin/sh", "-n", str(script))
    assert parsed.returncode == 0, parsed.stderr
    help_result = _run(str(script), "--help")
    assert help_result.returncode == 0 and help_result.stdout.startswith("usage:")
    assert "-I -B" in script.read_text()
    assert "PYTHONDONTWRITEBYTECODE=1" in script.read_text()


def _run(
    *arguments: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _verify_bench(tmp_path: Path) -> Path:
    """``scripts/verify`` beside stub tools: its Python logs each command to
    ``$VERIFY_COMMAND_LOG`` and exits ``$VERIFY_FAIL_CODE`` for one containing
    ``$VERIFY_FAIL_ON`` (0 otherwise)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    entry = scripts / "verify"
    entry.write_bytes((REPOSITORY / "scripts/verify").read_bytes())
    entry.chmod(0o700)
    binaries = tmp_path / ".venv/bin"
    binaries.mkdir(parents=True)
    for command in (binaries / "ruff", binaries / "mypy", scripts / "verify-native-recording"):
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o700)
    python = binaries / "python"
    python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$PYTHONDONTWRITEBYTECODE:$*" >> "$VERIFY_COMMAND_LOG"\n'
        'if [ -n "${VERIFY_FAIL_ON:-}" ]; then\n'
        '  case "$*" in *"$VERIFY_FAIL_ON"*) exit "$VERIFY_FAIL_CODE" ;; esac\n'
        "fi\n"
    )
    python.chmod(0o700)
    return entry


def test_full_verification_launches_host_python_isolated_without_real_tools(tmp_path: Path) -> None:
    entry = _verify_bench(tmp_path)
    log = tmp_path / "commands.txt"
    result = _run(str(entry), env={**os.environ, "VERIFY_COMMAND_LOG": str(log)})
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "1:-I -B -m pytest -n auto -m not serial"
        " --cov=cole_voice_lab --cov-branch --cov-report= --cov-fail-under=0",
        "1:-I -B -m pytest -m serial"
        " --cov=cole_voice_lab --cov-branch --cov-append --cov-report=term-missing",
        "1:-I -B -m pytest -n auto -m not serial -c apps/desktop/pyproject.toml"
        " apps/desktop/tests --cov=apps/desktop/src/voice_lab_desktop"
        " --cov-config=apps/desktop/pyproject.toml --cov-branch --cov-report= --cov-fail-under=0",
        "1:-I -B -m pytest -m serial -c apps/desktop/pyproject.toml apps/desktop/tests"
        " --cov=apps/desktop/src/voice_lab_desktop --cov-config=apps/desktop/pyproject.toml"
        " --cov-branch --cov-append --cov-report=term-missing",
        "1:-I -B -m build --no-isolation",
        "1:-I -B -m build --no-isolation --outdir dist apps/desktop",
        "1:-I -B -m cole_voice_lab.schema --check",
        "1:-I -B -m cole_voice_lab.contract_bundle check",
        "1:-I -B scripts/guard-private-assets",
    ]


@pytest.mark.parametrize(
    ("fails_on", "code", "passes"),
    [
        ("-m serial -c apps/desktop/pyproject.toml", 5, True),  # no desktop test is serial
        ("-m serial -c apps/desktop/pyproject.toml", 1, False),  # a failure or low coverage
        ("-m serial --cov=cole_voice_lab", 5, False),  # the backend's serial tests vanished
        ("-m not serial -c apps/desktop/pyproject.toml", 5, False),
    ],
)
def test_only_the_desktop_serial_pass_may_find_no_test(
    tmp_path: Path, fails_on: str, code: int, passes: bool
) -> None:
    entry = _verify_bench(tmp_path)
    log = tmp_path / "commands.txt"
    env = {
        **os.environ,
        "VERIFY_COMMAND_LOG": str(log),
        "VERIFY_FAIL_ON": fails_on,
        "VERIFY_FAIL_CODE": str(code),
    }
    result = _run(str(entry), env=env)
    assert (result.returncode == 0) is passes, result.stderr
    # Verification stops at a failed pass: nothing is built after it.
    built = any("-m build" in line for line in log.read_text().splitlines())
    assert built is passes


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mini_release(tmp_path: Path) -> tuple[Path, str, Path, Path, Path]:
    wheel = tmp_path / "bundle" / "cole_voice_lab-0.1.0.dev0-py3-none-any.whl"
    wheel.parent.mkdir(mode=0o700, parents=True)
    package_payloads = {
        "cole_voice_lab/__init__.py": b'"""Synthetic installed package."""\n',
        "cole_voice_lab/cli.py": b"def main():\n    return 0\n",
        "cole_voice_lab/intake.py": b"INTAKE_CONTRACT = 'sealed'\n",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in package_payloads.items():
            archive.writestr(name, payload)
        archive.writestr(
            "cole_voice_lab-0.1.0.dev0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: cole-voice-lab\nVersion: 0.1.0.dev0\n",
        )
        archive.writestr(
            "cole_voice_lab-0.1.0.dev0.dist-info/entry_points.txt",
            "[console_scripts]\nvoice-lab = cole_voice_lab.cli:main\n",
        )
    wheel.chmod(0o600)
    wheel_sha256 = _sha256(wheel)
    release = tmp_path / "mini" / "releases" / wheel_sha256
    executable = release / "bin" / "voice-lab"
    executable.parent.mkdir(mode=0o700, parents=True)
    runtime = executable.parent / "python3.12"
    runtime.symlink_to(Path(sys.executable).resolve())
    executable.write_text(
        f"#!{runtime}\n"
        "import sys\n"
        "from cole_voice_lab.cli import main\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    executable.chmod(0o700)
    site_packages = release / "lib/python3.12/site-packages"
    package = site_packages / "cole_voice_lab"
    package.mkdir(mode=0o700, parents=True)
    for name, payload in package_payloads.items():
        relative = Path(name).relative_to("cole_voice_lab")
        destination = package / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(payload)
    metadata = site_packages / "cole_voice_lab-0.1.0.dev0.dist-info"
    metadata.mkdir(mode=0o700)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: cole-voice-lab\nVersion: 0.1.0.dev0\n"
    )
    (metadata / "entry_points.txt").write_text(
        "[console_scripts]\nvoice-lab = cole_voice_lab.cli:main\n"
    )
    sealed = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "seal-mini-install"),
        "--release",
        str(release),
        "--wheel",
        str(wheel),
        "--expected-wheel-sha256",
        wheel_sha256,
    )
    assert sealed.returncode == 0, sealed.stderr
    receipt_path, receipt_sha256 = sealed.stdout.splitlines()
    return (
        Path(receipt_path),
        receipt_sha256,
        executable,
        package / "intake.py",
        site_packages,
    )


def test_shell_scripts_parse_and_capture_builder_is_offline_by_default(tmp_path: Path) -> None:
    for name in ("build-capture-kit", "macbook-bootstrap"):
        parsed = _run("/bin/sh", "-n", str(REPOSITORY / "scripts" / name))
        assert parsed.returncode == 0, parsed.stderr

    output = tmp_path / "capture-kit"
    result = _run(
        str(REPOSITORY / "scripts" / "build-capture-kit"),
        "--output",
        str(output),
    )
    assert result.returncode == 64
    assert "offline mode requires --wheelhouse" in result.stderr
    assert not output.exists()

    builder = (REPOSITORY / "scripts/build-capture-kit").read_text()
    capture_docs = (REPOSITORY / "docs/CAPTURE_KIT.md").read_text()
    runbook = (REPOSITORY / "docs/RUNBOOK.md").read_text()
    for payload in (builder, capture_docs, runbook):
        assert "/usr/bin/printf" in payload
        assert "/usr/bin/shasum -a 256 -c -" in payload
        assert "/usr/bin/shasum -a 256 -c SHA256SUMS" in payload
    assert builder.index("/usr/bin/shasum -a 256 -c -") < builder.index(
        "Only after both checks pass"
    )
    assert r"%%s  %%s\\\\n" in builder


def test_capture_kit_builder_installs_its_build_tools_hash_locked_and_offline() -> None:
    builder = (REPOSITORY / "scripts/build-capture-kit").read_text()
    assert "capture-kit-build.artifacts.txt" in builder
    assert "capture-kit-runtime.artifacts.txt" in builder
    assert builder.count("--require-hashes") >= 3
    assert 'toolchain_dir="$work_dir/build-tools"' in builder
    assert "$repo_dir/.venv/bin/python" not in builder
    assert "git_bin=/usr/bin/git" in builder
    assert "archive --format=tar" in builder
    assert 'rev-parse --verify "$source_commit^{tree}"' in builder
    assert "rev-parse --verify 'HEAD^{tree}'" not in builder
    assert 'SOURCE_DATE_EPOCH="$source_date_epoch"' in builder
    assert '"source": {' in builder


@macos_only.LAUNCHD_PLUTIL
def test_launch_agent_renderer_enforces_types_modes_and_plutil(tmp_path: Path) -> None:
    receipt, receipt_sha256, executable, _, _ = _mini_release(tmp_path)
    receipt_record = json.loads(receipt.read_bytes())
    assert receipt_record["schema_version"] == "cole-voice-lab.mini-install-receipt/v2"
    assert receipt_record["install_tree"]["entry_count"] > 3
    assert len(receipt_record["install_tree"]["sha256"]) == 64
    assert receipt_record["python_runtime"]["launcher_path"].startswith(str(receipt.parent / "bin"))
    asset_root = tmp_path / "assets"
    log_directory = tmp_path / "logs"
    output_directory = tmp_path / "launch-agents"
    for directory in (asset_root, log_directory, output_directory):
        directory.mkdir(mode=0o700)
    output = output_directory / "com.scshafe.cole-voice-lab.intake.plist"

    command = (
        sys.executable,
        str(REPOSITORY / "scripts" / "render-launch-agent"),
        "--install-receipt",
        str(receipt),
        "--install-receipt-sha256",
        receipt_sha256,
        "--asset-root",
        str(asset_root),
        "--log-directory",
        str(log_directory),
        "--output",
        str(output),
    )
    rendered = _run(*command)
    assert rendered.returncode == 0, rendered.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    launcher = output.with_name(f"{output.stem}.launcher.py")
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o500
    document = plistlib.loads(output.read_bytes())
    assert document["ProgramArguments"] == [
        "/usr/bin/python3",
        str(launcher),
        "--receipt",
        str(receipt),
        "--expected-receipt-sha256",
        receipt_sha256,
        "--asset-root",
        str(asset_root),
    ]
    assert document["StandardOutPath"] == str(log_directory / "intake.stdout.log")
    assert document["StandardErrorPath"] == str(log_directory / "intake.stderr.log")
    lint = _run("/usr/bin/plutil", "-lint", "--", str(output))
    assert lint.returncode == 0, lint.stderr
    verified = _run(
        "/usr/bin/python3",
        str(launcher),
        "--receipt",
        str(receipt),
        "--expected-receipt-sha256",
        receipt_sha256,
        "--asset-root",
        str(asset_root),
        "--verify-only",
    )
    assert verified.returncode == 0, verified.stderr

    asset_root.chmod(0o755)
    rejected = _run(*command)
    assert rejected.returncode == 2
    assert "asset root must have mode 0700" in rejected.stderr

    asset_root.chmod(0o700)
    executable.write_text("#!/usr/bin/false\n")
    executable.chmod(0o700)
    stale_release = _run(*command)
    assert stale_release.returncode == 2
    assert "installed voice-lab executable does not match its receipt" in stale_release.stderr


def test_durable_launcher_rejects_application_and_python_path_injection(
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "assets"
    asset_root.mkdir(mode=0o700)
    launcher = REPOSITORY / "deploy/macos/run-installed-intake.py"

    module_receipt, module_digest, _, application_module, _ = _mini_release(
        tmp_path / "module-tamper"
    )
    baseline = _run(
        "/usr/bin/python3",
        str(launcher),
        "--receipt",
        str(module_receipt),
        "--expected-receipt-sha256",
        module_digest,
        "--asset-root",
        str(asset_root),
        "--verify-only",
    )
    assert baseline.returncode == 0, baseline.stderr
    application_module.write_text("INTAKE_CONTRACT = 'tampered'\n")
    changed_module = _run(
        "/usr/bin/python3",
        str(launcher),
        "--receipt",
        str(module_receipt),
        "--expected-receipt-sha256",
        module_digest,
        "--asset-root",
        str(asset_root),
        "--verify-only",
    )
    assert changed_module.returncode != 0
    assert "installed release tree does not match its receipt" in changed_module.stderr
    reseal = _run(
        sys.executable,
        str(REPOSITORY / "scripts/seal-mini-install"),
        "--release",
        str(module_receipt.parent),
        "--wheel",
        str(tmp_path / "module-tamper/bundle/cole_voice_lab-0.1.0.dev0-py3-none-any.whl"),
        "--expected-wheel-sha256",
        module_receipt.parent.name,
    )
    assert reseal.returncode == 2
    assert "package bytes do not match the application wheel" in reseal.stderr

    path_receipt, path_digest, _, _, site_packages = _mini_release(tmp_path / "path-tamper")
    (site_packages / "unsealed-path-injection.pth").write_text(
        "import sys; sys.path.insert(0, '/tmp/unsealed-code')\n"
    )
    injected_path = _run(
        "/usr/bin/python3",
        str(launcher),
        "--receipt",
        str(path_receipt),
        "--expected-receipt-sha256",
        path_digest,
        "--asset-root",
        str(asset_root),
        "--verify-only",
    )
    assert injected_path.returncode != 0
    assert "installed release tree does not match its receipt" in injected_path.stderr


def test_sealer_and_launcher_reject_external_python_path_symlink(tmp_path: Path) -> None:
    receipt, receipt_sha256, _, _, site_packages = _mini_release(tmp_path)
    external_path_hook = tmp_path / "external-path-hook.pth"
    external_path_hook.write_text("# benign while the install is sealed\n")
    (site_packages / "sealed-link.pth").symlink_to(external_path_hook)
    asset_root = tmp_path / "assets"
    asset_root.mkdir(mode=0o700)

    verified = _run(
        "/usr/bin/python3",
        str(REPOSITORY / "deploy/macos/run-installed-intake.py"),
        "--receipt",
        str(receipt),
        "--expected-receipt-sha256",
        receipt_sha256,
        "--asset-root",
        str(asset_root),
        "--verify-only",
    )
    assert verified.returncode != 0
    assert "symlink is forbidden outside the Python launcher set" in verified.stderr

    receipt.unlink()
    result = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "seal-mini-install"),
        "--release",
        str(receipt.parent),
        "--wheel",
        str(tmp_path / "bundle/cole_voice_lab-0.1.0.dev0-py3-none-any.whl"),
        "--expected-wheel-sha256",
        receipt.parent.name,
    )

    assert result.returncode == 2
    assert "symlink is forbidden outside the Python launcher set" in result.stderr


def test_durable_launcher_rejects_extra_executable_receipt_fields(tmp_path: Path) -> None:
    receipt, _, _, _, _ = _mini_release(tmp_path)
    record = json.loads(receipt.read_bytes())
    record["executable"]["python_path"] = "/tmp/unsealed-python"
    receipt.write_text(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    changed_digest = _sha256(receipt)
    asset_root = tmp_path / "assets"
    asset_root.mkdir(mode=0o700)

    result = _run(
        "/usr/bin/python3",
        str(REPOSITORY / "deploy/macos/run-installed-intake.py"),
        "--receipt",
        str(receipt),
        "--expected-receipt-sha256",
        changed_digest,
        "--asset-root",
        str(asset_root),
        "--verify-only",
    )

    assert result.returncode != 0
    assert "executable evidence is invalid" in result.stderr


def test_private_asset_guard_sniffs_renamed_audio_and_private_json(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "renamed.dat").write_bytes(
        b"RIFF" + (40).to_bytes(4, "little") + b"WAVEfmt " + b"\0" * 24
    )
    (root / "evidence.json").write_text(
        json.dumps(
            {
                "schema_version": "cole-voice-lab.reference/v1",
                "raw_audio_sha256": "a" * 64,
            }
        )
    )

    result = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "guard-private-assets"),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "WAVE audio file signature" in result.stderr
    assert "private evidence JSON schema" in result.stderr
    assert "private evidence fields in JSON" in result.stderr


def test_private_asset_guard_rejects_approved_consent_and_schema_spoofing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "approved.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "schema_version": "cole-voice-lab.consent/v1",
                "consent_id": "cole-self-v1",
                "subject_id": "cole-self",
                "approved": True,
                "allowed_purposes": ["private-zero-shot-evaluation"],
            }
        )
    )
    (root / "looks-like-a-schema.json").write_text(
        json.dumps(
            {
                "properties": {},
                "required": [],
                "schema_version": "cole-voice-lab.human-reference-provenance/v1",
                "provenance_id": "private-provenance",
                "subject_id": "cole-self",
                "reference_sha256": "a" * 64,
                "consent_evidence_sha256": "b" * 64,
            }
        )
    )

    result = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "guard-private-assets"),
        "--root",
        str(root),
    )

    assert result.returncode == 1
    assert "approved consent record in JSON: approved.json" in result.stderr
    assert "private evidence JSON schema: approved.json" in result.stderr
    assert "human-reference provenance record in JSON: looks-like-a-schema.json" in result.stderr
    assert "private evidence JSON schema: looks-like-a-schema.json" in result.stderr


def test_private_asset_guard_takes_only_a_bundles_listed_examples_as_public(
    tmp_path: Path,
) -> None:
    # The contract bundle's examples are records by design (D-056); the guard takes one as
    # public only when the bundle's manifest lists its exact bytes.
    root = tmp_path / "repository"
    bundle = root / "contracts" / "corpus-client.v1"
    shutil.copytree(REPOSITORY / "contracts" / "corpus-client.v1", bundle)
    guard = (sys.executable, str(REPOSITORY / "scripts" / "guard-private-assets"), "--root")
    assert _run(*guard, str(root)).returncode == 0

    qc = bundle / "examples" / "take" / "qc.json"
    listed = qc.read_bytes()
    qc.write_bytes(listed.replace(b'"status":"pass"', b'"status":"review"'))
    result = _run(*guard, str(root))
    assert result.returncode == 1
    assert "private evidence JSON schema: contracts/corpus-client.v1/examples/take/qc.json" in (
        result.stderr
    )
    qc.write_bytes(listed)

    manifest = bundle / "manifest.json"
    original = manifest.read_bytes()
    manifest.write_bytes(original.replace(b'"corpus-client.v1"', b'"another.v1"', 1))
    result = _run(*guard, str(root))
    assert result.returncode == 1
    assert "private evidence filename: contracts/corpus-client.v1/manifest.json" in result.stderr
    assert "private evidence JSON schema: contracts/corpus-client.v1/examples/take/READY" in (
        result.stderr
    )
    manifest.write_bytes(original)

    # A record elsewhere in the repository is not the bundle's, whatever it matches.
    elsewhere = root / "examples" / "qc.json"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(listed)
    result = _run(*guard, str(root))
    assert result.returncode == 1
    assert "private evidence JSON schema: examples/qc.json" in result.stderr


def test_private_asset_guard_allows_catalog_json_schemas_and_no_longer_a_consent_template(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    schema_directory = root / "schemas"
    schema_directory.mkdir(parents=True)
    (schema_directory / "consent.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "consent_sha256": {"type": "string"},
                    "schema_version": {"const": "cole-voice-lab.consent/v1"},
                },
                "required": ["schema_version", "consent_sha256"],
            }
        )
    )
    guard = (sys.executable, str(REPOSITORY / "scripts" / "guard-private-assets"), "--root")
    assert _run(*guard, str(root)).returncode == 0
    # Consent records are retired (D-050) and the pending template is gone: a file at its old
    # path is private evidence like any other consent record.
    resource_directory = root / "src/cole_voice_lab/resources"
    resource_directory.mkdir(parents=True)
    (resource_directory / "consent.pending.json").write_text(
        json.dumps(
            {
                "schema_version": "cole-voice-lab.consent/v1",
                "consent_id": "cole-self-v1",
                "subject_id": "cole-self",
                "approved": False,
                "attestation": "pending-subject-attestation",
                "allowed_purposes": ["private-zero-shot-evaluation"],
                "effective_at": None,
                "revoked_at": None,
            }
        )
    )

    result = _run(*guard, str(root))

    assert result.returncode == 1
    assert (
        "private evidence JSON schema: src/cole_voice_lab/resources/consent.pending.json"
        in result.stderr
    )


@pytest.mark.parametrize(
    "asset_path",
    [
        "/Users/synthetic/.local/share/voice-lab/raw/take.wav",
        "/Users/synthetic/.local/share/cole-voice-lab/raw/take.wav",
        "/home/synthetic/.local/share/voice-lab/raw/take.wav",
        "/home/synthetic/.local/share/cole-voice-lab/raw/take.wav",
    ],
)
def test_private_asset_guard_rejects_new_and_pre_rename_asset_roots(
    tmp_path: Path, asset_path: str
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "notes.json").write_text(json.dumps({"source": asset_path}))

    result = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "guard-private-assets"),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "private asset path in JSON" in result.stderr


def test_private_asset_guard_reads_staged_blob_and_wheel_members(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    initialized = _run("git", "init", "--quiet", str(root))
    assert initialized.returncode == 0, initialized.stderr
    wheel = root / "apparently_safe-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("apparently_safe/reference.wav", b"RIFF\0\0\0\0WAVE")
    added = _run("git", "-C", str(root), "add", wheel.name)
    assert added.returncode == 0, added.stderr
    wheel.unlink()

    result = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "guard-private-assets"),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "git-index: built wheel must not be tracked or staged" in result.stderr
    assert "private/model artifact inside wheel" in result.stderr


def _wheel_record_hash(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _tree_sha256(root: Path) -> str:
    entries: list[dict[str, object]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            path = parent / name
            metadata = path.lstat()
            item: dict[str, object] = {
                "path": path.relative_to(root).as_posix(),
                "mode": stat.S_IMODE(metadata.st_mode),
                "size_bytes": metadata.st_size,
            }
            if stat.S_ISDIR(metadata.st_mode):
                item["kind"] = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                item.update({"kind": "file", "sha256": _sha256(path)})
            else:
                assert stat.S_ISLNK(metadata.st_mode)
                item.update({"kind": "symlink", "link_target": os.readlink(path)})
            entries.append(item)
    return _canonical_sha256(entries)


def _macho_references(roots: tuple[Path, ...]) -> list[str]:
    magics = {b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}
    allowed = tuple(path.resolve() for path in roots)
    references: set[str] = set()
    for root in allowed:
        for directory, _directory_names, file_names in os.walk(root, followlinks=False):
            for name in file_names:
                path = Path(directory) / name
                if not path.is_file() or path.is_symlink():
                    continue
                with path.open("rb") as stream:
                    if stream.read(4) not in magics:
                        continue
                completed = _run("/usr/bin/otool", "-L", str(path))
                assert completed.returncode == 0, completed.stderr
                for line in completed.stdout.splitlines()[1:]:
                    dependency = line.strip().split(" ", 1)[0]
                    if dependency.startswith(("/usr/lib/", "/System/Library/")):
                        references.add(dependency)
                    elif dependency:
                        resolved = Path(dependency).resolve()
                        assert any(
                            resolved == allowed_root or resolved.is_relative_to(allowed_root)
                            for allowed_root in allowed
                        )
    return sorted(references)


@functools.lru_cache(maxsize=1)
def _python_runtime_closure() -> dict[str, object]:
    python_alias = Path("/opt/homebrew/opt/python@3.12")
    python_prefix = python_alias.resolve(strict=True)
    resolved_python = Path(sys.executable).resolve(strict=True)
    dependency_evidence: list[dict[str, str]] = []
    dependency_roots: list[Path] = []
    for formula in ("mpdecimal", "openssl@3", "readline", "sqlite", "xz"):
        alias = Path("/opt/homebrew/opt") / formula
        prefix = alias.resolve(strict=True)
        dependency_roots.append(prefix)
        dependency_evidence.append(
            {
                "formula": formula,
                "prefix_path": str(prefix),
                "opt_alias_path": str(alias),
                "opt_alias_target": os.readlink(alias),
                "tree_sha256": _tree_sha256(prefix),
            }
        )
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    (ensurepip_wheel,) = sorted((stdlib / "ensurepip/_bundled").glob("pip-*.whl"))
    product = _run("/usr/bin/sw_vers", "-productVersion")
    build = _run("/usr/bin/sw_vers", "-buildVersion")
    assert product.returncode == 0 and build.returncode == 0
    assert Path(sys.base_prefix).resolve().is_relative_to(python_prefix)
    assert stdlib.is_relative_to(python_prefix)
    return {
        "schema_version": "cole-voice-lab.python-runtime-closure/v1",
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "build": list(platform.python_build()),
        "machine": platform.machine(),
        "macos_product_version": product.stdout.strip(),
        "macos_build_version": build.stdout.strip(),
        "prefix_path": str(python_prefix),
        "prefix_tree_sha256": _tree_sha256(python_prefix),
        "opt_alias_path": str(python_alias),
        "opt_alias_target": os.readlink(python_alias),
        "executable_relative_path": resolved_python.relative_to(python_prefix).as_posix(),
        "executable_sha256": _sha256(resolved_python),
        "ensurepip_wheel_relative_path": ensurepip_wheel.relative_to(python_prefix).as_posix(),
        "ensurepip_wheel_sha256": _sha256(ensurepip_wheel),
        "dependency_prefixes": dependency_evidence,
        "system_library_references": _macho_references((python_prefix, *dependency_roots)),
    }


def _refresh_test_bundle(bundle: Path) -> tuple[Path, str, str]:
    (wheel,) = sorted(bundle.glob("cole_voice_lab-*.whl"))
    lines = [
        f"{_sha256(path)}  {path.name}\n"
        for path in sorted(bundle.iterdir(), key=lambda item: item.name)
        if path.name != "SHA256SUMS"
    ]
    (bundle / "SHA256SUMS").write_text("".join(lines))
    return wheel, _sha256(wheel), _sha256(bundle / "SHA256SUMS")


def _reseal_test_install_tree(root: Path) -> None:
    entries: list[dict[str, object]] = []

    def visit(directory: Path) -> None:
        for item in sorted(directory.iterdir(), key=lambda path: path.name):
            relative = item.relative_to(root).as_posix()
            if relative in {"READY", ".INSTALLING"}:
                continue
            metadata = item.lstat()
            evidence: dict[str, object] = {
                "path": relative,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
            if stat.S_ISDIR(metadata.st_mode):
                evidence["type"] = "directory"
                entries.append(evidence)
                visit(item)
            elif stat.S_ISREG(metadata.st_mode):
                evidence.update(
                    {
                        "type": "file",
                        "size_bytes": metadata.st_size,
                        "sha256": _sha256(item),
                    }
                )
                entries.append(evidence)
            else:
                assert stat.S_ISLNK(metadata.st_mode)
                resolved = item.resolve(strict=True)
                resolved_metadata = resolved.stat()
                evidence.update(
                    {
                        "type": "symlink",
                        "target": os.readlink(item),
                        "resolved_path": str(resolved),
                        "resolved_sha256": _sha256(resolved),
                        "resolved_size_bytes": resolved_metadata.st_size,
                    }
                )
                entries.append(evidence)

    visit(root)
    receipt = json.loads((root / "READY").read_bytes())
    receipt["install_tree"] = {
        "entry_count": len(entries),
        "sha256": _canonical_sha256(entries),
    }
    (root / "READY").write_text(json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n")


def _write_test_bundle(
    bundle: Path,
    *,
    requires_dist: tuple[str, ...] = (),
    extra_wheel_members: Mapping[str, bytes] | None = None,
    wheel_member_modes: Mapping[str, int] | None = None,
    portable_bootstrap: bool = False,
    stock_validation_python: bool = False,
) -> tuple[Path, str, str]:
    bundle.mkdir(mode=0o700)
    bootstrap = bundle / "macbook-bootstrap"
    bootstrap_payload = (REPOSITORY / "scripts" / "macbook-bootstrap").read_text()
    if portable_bootstrap:
        bootstrap_payload = bootstrap_payload.replace(
            "python_bin=/opt/homebrew/bin/python3.12",
            f"python_bin={shlex.quote(sys.executable)}",
        ).replace("ffmpeg_bin=/opt/homebrew/bin/ffmpeg", "ffmpeg_bin=/usr/bin/true")
    elif stock_validation_python:
        bootstrap_payload = bootstrap_payload.replace(
            "python_bin=/opt/homebrew/bin/python3.12",
            "python_bin=/nonexistent/cole-voice-lab-python3.12",
        )
    bootstrap.write_text(bootstrap_payload)
    bootstrap.chmod(0o700)
    (bundle / "site.example.toml").write_text("# documentation only\n")
    wheel = bundle / "cole_voice_lab-0.1.0.dev0-py3-none-any.whl"
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: cole-voice-lab\n"
        "Version: 0.1.0.dev0\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires_dist)
        + "\n"
    ).encode()
    cli = (
        b"import os\n"
        b"import sys\n"
        b"def main():\n"
        b"    if sys.argv[1:] == ['--help']:\n"
        b"        marker = os.environ.get('VOICE_LAB_TEST_HELP_MARKER')\n"
        b"        if marker:\n"
        b"            with open(marker, 'a', encoding='utf-8') as stream:\n"
        b"                stream.write(sys.prefix + '\\n')\n"
        b"        print('usage: voice-lab')\n"
        b"        return 0\n"
        b"    if sys.argv[1:3] == ['assets', 'init']:\n"
        b"        root = sys.argv[sys.argv.index('--asset-root') + 1]\n"
        b"        os.mkdir(root, 0o700)\n"
        b"    return 0\n"
        b"if __name__ == '__main__':\n"
        b"    raise SystemExit(main())\n"
    )
    wheel_members: dict[str, bytes] = {
        "cole_voice_lab/__init__.py": b'"""Synthetic capture-kit package."""\n',
        "cole_voice_lab/cli.py": cli,
        "cole_voice_lab-0.1.0.dev0.dist-info/METADATA": metadata,
        "cole_voice_lab-0.1.0.dev0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: operator-test\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "cole_voice_lab-0.1.0.dev0.dist-info/entry_points.txt": (
            b"[console_scripts]\nvoice-lab = cole_voice_lab.cli:main\n"
        ),
    }
    wheel_members.update(extra_wheel_members or {})
    record_name = "cole_voice_lab-0.1.0.dev0.dist-info/RECORD"
    record = (
        "".join(
            f"{name},{_wheel_record_hash(payload)},{len(payload)}\n"
            for name, payload in sorted(wheel_members.items())
        )
        + f"{record_name},,\n"
    )
    wheel_members[record_name] = record.encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in wheel_members.items():
            if wheel_member_modes and name in wheel_member_modes:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = wheel_member_modes[name] << 16
                archive.writestr(info, payload)
            else:
                archive.writestr(name, payload)
    wheel.chmod(0o600)
    wheel_digest = _sha256(wheel)
    (bundle / "capture-kit-requirements.txt").write_text(
        f"cole-voice-lab==0.1.0.dev0 --hash=sha256:{wheel_digest}\n"
    )
    (bundle / "BUNDLE.json").write_text(
        json.dumps(
            {
                "application": {
                    "filename": wheel.name,
                    "name": "cole-voice-lab",
                    "sha256": wheel_digest,
                    "version": "0.1.0.dev0",
                },
                "application_requires_dist": list(requires_dist),
                "build": {
                    "python": {
                        "resolved_path": str(Path(sys.executable).resolve(strict=True)),
                        "sha256": _sha256(Path(sys.executable).resolve(strict=True)),
                        "version": platform.python_version(),
                        "runtime_closure": _python_runtime_closure(),
                    }
                },
                "dependency_wheels": [],
                "manifest": {"must_be_supplied_out_of_band": True},
                "source": {
                    "commit": "1" * 40,
                    "source_date_epoch": 1_700_000_000,
                    "tree": "2" * 40,
                },
                "schema_version": "cole-voice-lab.capture-kit/v1",
            },
            sort_keys=True,
        )
        + "\n"
    )
    members = sorted(bundle.iterdir(), key=lambda path: path.name)
    (bundle / "SHA256SUMS").write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in members)
    )
    return wheel, wheel_digest, _sha256(bundle / "SHA256SUMS")


@macos_only.HOMEBREW_PYTHON
def test_bootstrap_requires_independent_manifest_digest_and_exact_membership(
    tmp_path: Path,
) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(tmp_path / "capture-kit")
    bootstrap = wheel.parent / "macbook-bootstrap"
    verified = _run(
        str(bootstrap),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
    )
    assert verified.returncode == 0, verified.stderr
    assert "verified capture-kit bundle" in verified.stdout

    (wheel.parent / "unmanifested-file").write_text("stale")
    rejected = _run(
        str(bootstrap),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
    )
    assert rejected.returncode != 0
    assert "bundle membership mismatch" in rejected.stderr


@macos_only.HOMEBREW_PYTHON
def test_capture_bundle_trust_starts_with_system_hashes_and_isolated_python(
    tmp_path: Path,
) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(tmp_path / "capture-kit")
    bundle = wheel.parent
    manifest_line = f"{manifest_digest}  {bundle / 'SHA256SUMS'}\n"
    manifest_check = subprocess.run(
        ("/usr/bin/shasum", "-a", "256", "-c", "-"),
        input=manifest_line,
        check=False,
        capture_output=True,
        text=True,
    )
    assert manifest_check.returncode == 0, manifest_check.stderr
    entries_check = _run(
        "/usr/bin/shasum",
        "-a",
        "256",
        "-c",
        "SHA256SUMS",
        cwd=bundle,
    )
    assert entries_check.returncode == 0, entries_check.stderr

    hostile = tmp_path / "hostile-import-root"
    hostile.mkdir()
    marker = tmp_path / "untrusted-import-executed"
    (hostile / "hashlib.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    )
    verified = _run(
        str(bundle / "macbook-bootstrap"),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
        cwd=hostile,
        env={**os.environ, "PYTHONPATH": str(hostile)},
    )
    assert verified.returncode == 0, verified.stderr
    assert not marker.exists()

    bootstrap = bundle / "macbook-bootstrap"
    bootstrap.unlink()
    bootstrap.symlink_to("/usr/bin/true")
    tamper_check = _run(
        "/usr/bin/shasum",
        "-a",
        "256",
        "-c",
        "SHA256SUMS",
        cwd=bundle,
    )
    assert tamper_check.returncode != 0
    assert "macbook-bootstrap: FAILED" in tamper_check.stdout

    bootstrap_source = (REPOSITORY / "scripts/macbook-bootstrap").read_text()
    assert 'isolated_python "$validation_python" -' in bootstrap_source
    assert "/usr/bin/env -i" in bootstrap_source
    assert '"$isolated_python_executable" -I -B "$@"' in bootstrap_source
    assert "command -v python3" not in bootstrap_source


@macos_only.HOMEBREW_PYTHON
def test_verify_only_stock_macos_python_fallback_is_isolated(tmp_path: Path) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit", stock_validation_python=True
    )
    hostile = tmp_path / "hostile-import-root"
    hostile.mkdir()
    marker = tmp_path / "untrusted-import-executed"
    (hostile / "hashlib.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    )

    verified = _run(
        str(wheel.parent / "macbook-bootstrap"),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
        cwd=hostile,
        env={**os.environ, "PYTHONPATH": str(hostile)},
    )

    assert verified.returncode == 0, verified.stderr
    assert "verified capture-kit bundle" in verified.stdout
    assert not marker.exists()


@macos_only.HOMEBREW_PYTHON
@pytest.mark.parametrize(
    "member",
    ("../outside.py", "/absolute.py", "cole_voice_lab/../../outside.py"),
)
def test_bootstrap_rejects_unsafe_wheel_member_names(tmp_path: Path, member: str) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit", extra_wheel_members={member: b"malicious"}
    )

    result = _run(
        str(wheel.parent / "macbook-bootstrap"),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
    )

    assert result.returncode != 0
    assert "unsafe" in result.stderr
    assert "member name" in result.stderr


@macos_only.HOMEBREW_PYTHON
def test_bootstrap_rejects_unreviewed_application_dependency_metadata(tmp_path: Path) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit", requires_dist=("unexpected-package>=1",)
    )

    result = _run(
        str(wheel.parent / "macbook-bootstrap"),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
    )

    assert result.returncode != 0
    assert "Requires-Dist names are not in the reviewed bundled dependency set" in result.stderr


@macos_only.HOMEBREW_PYTHON
def test_bootstrap_keeps_the_vault_extra_out_of_a_capture_kit(tmp_path: Path) -> None:
    # As setuptools writes the optional vault encryption (pyproject.toml) into the metadata.
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit",
        requires_dist=(
            'cryptography==50.0.1; extra == "vault"',
            'cryptography==50.0.1; extra == "dev"',
        ),
    )

    result = _run(
        str(wheel.parent / "macbook-bootstrap"),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
    )

    assert result.returncode == 0, result.stderr
    assert "verified capture-kit bundle" in result.stdout


@macos_only.HOMEBREW_PYTHON
@pytest.mark.parametrize(
    ("member_type", "expected"),
    ((stat.S_IFLNK | 0o777, "symlink"), (stat.S_IFCHR | 0o600, "device")),
)
def test_bootstrap_rejects_nonregular_wheel_members(
    tmp_path: Path, member_type: int, expected: str
) -> None:
    member = "cole_voice_lab/unsafe"
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit",
        extra_wheel_members={member: b"target"},
        wheel_member_modes={member: member_type},
    )

    result = _run(
        str(wheel.parent / "macbook-bootstrap"),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
    )

    assert result.returncode != 0
    assert f"contains a {expected} member" in result.stderr


@macos_only.HOMEBREW_PYTHON
def test_bootstrap_quarantines_partial_state_seals_fresh_install_and_is_idempotent(
    tmp_path: Path,
) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit", portable_bootstrap=True
    )
    home = tmp_path / "home"
    parent = home / "private" / "capture-roots"
    parent.mkdir(mode=0o700, parents=True)
    target = parent / "capture-host"
    stale = parent / ".capture-host.INSTALLING"
    stale.mkdir(mode=0o700)
    (stale / "do-not-delete").write_text("partial runtime evidence\n")
    help_marker = tmp_path / "published-help-prefixes"
    environment = {
        **os.environ,
        "HOME": str(home),
        "VOICE_LAB_TEST_HELP_MARKER": str(help_marker),
    }
    command = (
        str(wheel.parent / "macbook-bootstrap"),
        str(target),
        str(wheel),
        wheel_digest,
        manifest_digest,
    )

    installed = _run(*command, env=environment)

    assert installed.returncode == 0, installed.stderr
    assert "capture runtime installed" in installed.stdout
    assert help_marker.read_text().splitlines() == [str(target / "runtime")]
    recoveries = list(parent.glob(".capture-host.RECOVERY.*"))
    assert len(recoveries) == 1
    assert (recoveries[0] / "partial" / "do-not-delete").read_text() == (
        "partial runtime evidence\n"
    )
    receipt = json.loads((target / "READY").read_bytes())
    assert receipt["schema_version"] == "cole-voice-lab.install-receipt/v2"
    assert receipt["install_tree"]["entry_count"] > 5
    assert len(receipt["install_tree"]["sha256"]) == 64
    console = target / "runtime" / "bin" / "voice-lab"
    assert console.is_file()
    assert console.stat().st_nlink == 1
    assert console.read_bytes().startswith(b"#!/bin/sh\nset -eu\n")
    final_help = _run(str(console), "--help", env=environment)
    assert final_help.returncode == 0, final_help.stderr
    assert final_help.stdout.startswith("usage: voice-lab")

    repeated = _run(*command, env=environment)

    assert repeated.returncode == 0, repeated.stderr
    assert "already installed and verified" in repeated.stdout
    assert list(parent.glob(".capture-host.RECOVERY.*")) == recoveries


@macos_only.HOMEBREW_PYTHON
def test_bootstrap_preserves_invalid_ready_stage_and_builds_clean_runtime(tmp_path: Path) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit", portable_bootstrap=True
    )
    home = tmp_path / "home"
    parent = home / "private" / "capture-roots"
    parent.mkdir(mode=0o700, parents=True)
    target = parent / "capture-host"
    environment = {**os.environ, "HOME": str(home)}
    command = (
        str(wheel.parent / "macbook-bootstrap"),
        str(target),
        str(wheel),
        wheel_digest,
        manifest_digest,
    )
    initial = _run(*command, env=environment)
    assert initial.returncode == 0, initial.stderr
    staging = parent / ".capture-host.INSTALLING"
    target.rename(staging)
    console = staging / "runtime" / "bin" / "voice-lab"
    console.write_text("#!/bin/sh\nexit 99\n")
    console.chmod(0o700)

    recovered = _run(*command, env=environment)

    assert recovered.returncode == 0, recovered.stderr
    assert "failed validation; preserving it and rebuilding" in recovered.stderr
    preserved = list(parent.glob(".capture-host.RECOVERY.*/partial"))
    assert len(preserved) == 1
    assert (preserved[0] / "runtime" / "bin" / "voice-lab").read_text() == ("#!/bin/sh\nexit 99\n")
    assert target.is_dir()
    assert (target / "runtime" / "bin" / "voice-lab").read_text() != "#!/bin/sh\nexit 99\n"


@macos_only.HOMEBREW_PYTHON
def test_bootstrap_rejects_host_python_evidence_that_does_not_match_bytes(tmp_path: Path) -> None:
    wheel, _, _ = _write_test_bundle(tmp_path / "capture-kit")
    metadata_path = wheel.parent / "BUNDLE.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["build"]["python"]["sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata, separators=(",", ":"), sort_keys=True) + "\n")
    wheel, wheel_digest, manifest_digest = _refresh_test_bundle(wheel.parent)

    result = _run(
        str(wheel.parent / "macbook-bootstrap"),
        "--verify-bundle",
        str(wheel),
        wheel_digest,
        manifest_digest,
    )

    assert result.returncode != 0
    assert "host Python executable bytes differ from reviewed BUNDLE evidence" in result.stderr


@pytest.mark.parametrize("script_name", ("macbook-bootstrap", "build-capture-kit"))
def test_python_runtime_closure_rejects_escape_and_reentry_symlink_hops(
    tmp_path: Path,
    script_name: str,
) -> None:
    script = (REPOSITORY / "scripts" / script_name).read_text()
    payload = next(
        block.split("\nPY\n", 1)[0]
        for block in script.split("<<'PY'\n")[1:]
        if "def immutable_tree_sha256" in block
    )
    module = ast.parse(payload)
    selected = {
        "canonical_sha256",
        "confined_symlink_resolution",
        "digest",
        "immutable_tree_sha256",
        "sha256",
    }
    functions = [
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in selected
    ]
    namespace = {
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "Path": Path,
        "stat": stat,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), script_name, "exec"), namespace)
    tree_sha256 = namespace["immutable_tree_sha256"]
    keg = tmp_path / "reviewed-keg"
    keg.mkdir()
    first_payload = keg / "first-payload"
    second_payload = keg / "second-payload"
    first_payload.write_text("first reviewed payload\n")
    second_payload.write_text("second reviewed payload\n")
    launcher = keg / "launcher"
    launcher.symlink_to(first_payload.name)

    assert len(tree_sha256(keg, "test keg", (keg,))) == 64

    outside_bridge = tmp_path / "mutable-external-bridge"
    outside_bridge.symlink_to(first_payload)
    launcher.unlink()
    launcher.symlink_to(outside_bridge)
    assert launcher.resolve(strict=True) == first_payload
    with pytest.raises(SystemExit, match="symlink escapes its reviewed keg root"):
        tree_sha256(keg, "test keg", (keg,))

    outside_bridge.unlink()
    outside_bridge.symlink_to(second_payload)
    assert launcher.resolve(strict=True) == second_payload
    with pytest.raises(SystemExit, match="symlink escapes its reviewed keg root"):
        tree_sha256(keg, "test keg", (keg,))


@macos_only.HOMEBREW_PYTHON
def test_bootstrap_rejects_retargeted_ready_python_launcher(tmp_path: Path) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit", portable_bootstrap=True
    )
    home = tmp_path / "home"
    parent = home / "private" / "capture-roots"
    parent.mkdir(mode=0o700, parents=True)
    target = parent / "capture-host"
    environment = {**os.environ, "HOME": str(home)}
    command = (
        str(wheel.parent / "macbook-bootstrap"),
        str(target),
        str(wheel),
        wheel_digest,
        manifest_digest,
    )
    initial = _run(*command, env=environment)
    assert initial.returncode == 0, initial.stderr
    staging = parent / ".capture-host.INSTALLING"
    target.rename(staging)
    launcher = (
        staging / "runtime" / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    )
    launcher.unlink()
    launcher.symlink_to("/usr/bin/true")
    _reseal_test_install_tree(staging)

    recovered = _run(*command, env=environment)

    assert recovered.returncode == 0, recovered.stderr
    assert "failed validation; preserving it and rebuilding" in recovered.stderr
    preserved = list(parent.glob(".capture-host.RECOVERY.*/partial"))
    assert len(preserved) == 1
    assert os.readlink(preserved[0] / "runtime" / "bin" / launcher.name) == "/usr/bin/true"
    assert (target / "runtime" / "bin" / launcher.name).resolve() == Path(sys.executable).resolve()


@macos_only.HOMEBREW_PYTHON
def test_bundle_evidence_not_recomputed_ready_controls_runtime_trust(tmp_path: Path) -> None:
    wheel, wheel_digest, manifest_digest = _write_test_bundle(
        tmp_path / "capture-kit", portable_bootstrap=True
    )
    home = tmp_path / "home"
    parent = home / "private" / "capture-roots"
    parent.mkdir(mode=0o700, parents=True)
    target = parent / "capture-host"
    environment = {**os.environ, "HOME": str(home)}
    command = (
        str(wheel.parent / "macbook-bootstrap"),
        str(target),
        str(wheel),
        wheel_digest,
        manifest_digest,
    )
    initial = _run(*command, env=environment)
    assert initial.returncode == 0, initial.stderr
    site_packages = (
        target
        / "runtime"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    injected = site_packages / "unrecorded_injection.py"
    injected.write_text("raise RuntimeError('unreviewed')\n")
    _reseal_test_install_tree(target)

    extra_rejected = _run(*command, env=environment)

    assert extra_rejected.returncode != 0
    assert "site-packages membership is not exactly derived from reviewed wheels" in (
        extra_rejected.stderr
    )
    injected.unlink()
    (record_path,) = sorted(site_packages.glob("cole_voice_lab-*.dist-info/RECORD"))
    canonical_record = record_path.read_bytes()
    record_rows = list(csv.reader(io.StringIO(canonical_record.decode(), newline="")))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerows(reversed(record_rows))
    record_path.write_bytes(output.getvalue().encode())
    _reseal_test_install_tree(target)

    record_rewrite = _run(*command, env=environment)

    assert record_rewrite.returncode != 0
    assert "installed RECORD bytes are not canonical and wheel-derived" in (record_rewrite.stderr)
    record_path.write_bytes(canonical_record)
    wrapper = target / "runtime" / "bin" / "voice-lab"
    malicious_wrapper = b"#!/bin/sh\nexec /usr/bin/true\n"
    wrapper.write_bytes(malicious_wrapper)
    wrapper.chmod(0o700)
    rows = list(csv.reader(io.StringIO(record_path.read_text())))
    relative_wrapper = os.path.relpath(wrapper, site_packages)
    encoded = base64.urlsafe_b64encode(hashlib.sha256(malicious_wrapper).digest()).rstrip(b"=")
    for row in rows:
        if row[0] == relative_wrapper:
            row[1] = f"sha256={encoded.decode()}"
            row[2] = str(len(malicious_wrapper))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record_path.write_text(output.getvalue())
    _reseal_test_install_tree(target)

    coherent_rewrite = _run(*command, env=environment)

    assert coherent_rewrite.returncode != 0
    assert "wrapper differs from manifest-bound canonical bytes" in coherent_rewrite.stderr


def test_bootstrap_install_is_exact_no_deps_and_help_uses_stdout() -> None:
    bootstrap = (REPOSITORY / "scripts/macbook-bootstrap").read_text()
    pip_install = bootstrap.split('"$staging_root/runtime/bin/python" -P -m pip install', 1)[1]
    pip_install = pip_install.split('"$staging_root/runtime/bin/python" -P -m pip check', 1)[0]
    assert "--no-index" in pip_install
    assert "--no-deps" in pip_install
    assert "--no-compile" in pip_install
    assert "--require-hashes" in pip_install
    assert '"$bundle_directory/capture-kit-requirements.txt"' in pip_install
    assert "venv --without-pip" in bootstrap
    assert 'PYTHONPATH="$ensurepip_wheel"' in bootstrap

    for script_name in ("macbook-bootstrap", "build-capture-kit"):
        result = _run(str(REPOSITORY / "scripts" / script_name), "--help")
        assert result.returncode == 0
        assert result.stdout.startswith("usage:")
        assert result.stderr == ""


@pytest.mark.parametrize(
    ("relative", "payload", "message"),
    [
        (
            "notes/index.json",
            {"schemaVersion": "voice-journey.recording-index.v1", "recordings": []},
            "voice-journey corpus manifest JSON",
        ),
        (
            "notes/mirror.json",
            {"schema": "voice-journey-mirror-manifest/1", "files": {}},
            "voice-journey corpus manifest JSON",
        ),
        ("manifests/local-stt-transcripts.json", {}, "voice-journey corpus manifest filename"),
        ("phone/_mirror/latest.json", {}, "voice-journey corpus manifest filename"),
    ],
)
def test_private_asset_guard_rejects_voice_journey_corpus_manifests(
    tmp_path: Path, relative: str, payload: dict[str, object], message: str
) -> None:
    root = tmp_path / "repository"
    target = root / relative
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(payload))

    result = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "guard-private-assets"),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert message in result.stderr


def test_private_asset_guard_rejects_voice_memos_database_and_allows_chain_registry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "CloudRecordings.db-wal").write_bytes(b"\0" * 8)
    (root / "chains.json").write_text(
        json.dumps({"schemaVersion": "voice-journey.chains.v1", "chains": []})
    )

    result = _run(
        sys.executable,
        str(REPOSITORY / "scripts" / "guard-private-assets"),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "Voice Memos database: CloudRecordings.db-wal" in result.stderr
    assert "chains.json" not in result.stderr
