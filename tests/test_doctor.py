from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import cole_voice_lab.doctor as doctor
from cole_voice_lab.doctor import DoctorCheck, disk_check, run_doctor
from cole_voice_lab.paths import AssetLayout


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(("tool",), returncode, stdout, stderr)


def _stub_external_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_tool_version",
        lambda binary, _arguments: DoctorCheck(binary, "pass", "stub", {}),
    )
    monkeypatch.setattr(
        doctor,
        "_rsync_features",
        lambda: DoctorCheck("rsync", "pass", "stub", {}),
    )
    monkeypatch.setattr(
        doctor,
        "_rrsync_receiver",
        lambda: DoctorCheck("rrsync", "review", "stub", {}),
    )


def test_disk_check_reports_structured_evidence(tmp_path: Path) -> None:
    check = disk_check(tmp_path, minimum_free_gib=0.000001)
    assert check.status == "pass"
    assert int(check.details["free_bytes"]) > 0


def test_doctor_check_serializes_as_json_values() -> None:
    check = DoctorCheck("example", "pass", "ready", {"count": 1})
    assert check.to_json() == {
        "check_id": "example",
        "status": "pass",
        "message": "ready",
        "details": {"count": 1},
    }


def test_tool_version_reports_missing_and_execution_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: None)
    assert doctor._tool_version("missing", ("--version",)).status == "fail"

    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: "/bin/tool")

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise OSError("not executable")

    monkeypatch.setattr(doctor.subprocess, "run", fail_run)
    check = doctor._tool_version("broken", ("--version",))
    assert check.status == "fail"
    assert "not executable" in check.message
    assert check.details["path"] == "/bin/tool"


@pytest.mark.parametrize(
    ("completed", "expected_status", "expected_message"),
    (
        (_completed(0, "tool 1.2\nmore\n"), "pass", "tool 1.2"),
        (_completed(1, stderr="unsupported flag\n"), "review", "unsupported flag"),
        (_completed(1), "review", "version output unavailable"),
    ),
)
def test_tool_version_uses_first_available_output_line(
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[str],
    expected_status: str,
    expected_message: str,
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: "/bin/tool")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *_args, **_kwargs: completed)

    check = doctor._tool_version("tool", ("--version",))
    assert (check.status, check.message) == (expected_status, expected_message)


def test_rsync_probe_reports_missing_and_execution_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: None)
    assert doctor._rsync_features().message == "rsync is not on PATH"

    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: "/bin/rsync")

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("rsync", 10)

    monkeypatch.setattr(doctor.subprocess, "run", fail_run)
    check = doctor._rsync_features()
    assert check.status == "fail"
    assert "cannot inspect rsync" in check.message


def test_rsync_probe_requires_exact_sender_features(monkeypatch: pytest.MonkeyPatch) -> None:
    required = " --partial-dir --delay-updates --checksum --chmod --timeout "
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: "/bin/rsync")

    def successful_run(
        command: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return _completed(0, "openrsync 2.6.9\n")
        return _completed(0, required)

    monkeypatch.setattr(doctor.subprocess, "run", successful_run)
    check = doctor._rsync_features()
    assert check.status == "pass"
    assert check.message == "openrsync 2.6.9"
    assert check.details["missing_options"] == []

    def missing_feature_run(
        command: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return _completed(0, stderr="rsync version from stderr\n")
        return _completed(0, required.replace("--chmod", ""))

    monkeypatch.setattr(doctor.subprocess, "run", missing_feature_run)
    check = doctor._rsync_features()
    assert check.status == "fail"
    assert check.details["missing_options"] == ["--chmod"]
    assert "--chmod" in check.message


@pytest.mark.parametrize(
    ("version_code", "help_code"),
    ((1, 0), (0, 1)),
)
def test_rsync_probe_rejects_failed_subprocesses_even_with_all_features(
    monkeypatch: pytest.MonkeyPatch, version_code: int, help_code: int
) -> None:
    required = " --partial-dir --delay-updates --checksum --chmod --timeout "
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: "/bin/rsync")

    def run_probe(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return _completed(version_code)
        return _completed(help_code, required)

    monkeypatch.setattr(doctor.subprocess, "run", run_probe)
    check = doctor._rsync_features()
    assert check.status == "fail"
    assert check.message.endswith("probe failed")


def test_rrsync_probe_distinguishes_missing_unsafe_and_reviewable_receivers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: None)
    assert doctor._rrsync_receiver().status == "fail"

    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: str(tmp_path))
    assert doctor._rrsync_receiver().message == "rrsync is not an executable file"

    receiver = tmp_path / "rrsync"
    receiver.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'usage: rrsync -wo -no-del DIR'\n",
        encoding="utf-8",
    )
    receiver.chmod(0o600)
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: str(receiver))
    assert doctor._rrsync_receiver().status == "fail"

    receiver.chmod(0o700)
    check = doctor._rrsync_receiver()
    assert check.status == "review"
    assert check.details["path"] == str(receiver)
    assert check.details["resolved_path"] == str(receiver)
    assert check.details["missing_options"] == []
    assert len(str(check.details["sha256"])) == 64


def test_rrsync_probe_rejects_missing_restrictions_and_execution_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receiver = tmp_path / "rrsync"
    receiver.write_text("#!/bin/sh\nprintf 'usage: rrsync -wo DIR\\n'\n", encoding="utf-8")
    receiver.chmod(0o700)
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: str(receiver))
    check = doctor._rrsync_receiver()
    assert check.status == "fail"
    assert check.details["missing_options"] == ["-no-del"]

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("rrsync", 10)

    monkeypatch.setattr(doctor.subprocess, "run", fail_run)
    check = doctor._rrsync_receiver()
    assert check.status == "fail"
    assert "cannot inspect" in check.message


def test_disk_check_walks_to_existing_parent_and_reports_low_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[Path] = []

    def fake_disk_usage(path: Path) -> SimpleNamespace:
        observed.append(path)
        return SimpleNamespace(free=1024**3, total=20 * 1024**3)

    monkeypatch.setattr(doctor.shutil, "disk_usage", fake_disk_usage)
    check = disk_check(tmp_path / "missing" / "child", minimum_free_gib=10)

    assert check.status == "fail"
    assert observed == [tmp_path]
    assert check.details["path"] == str(tmp_path)


def test_doctor_does_not_require_asset_root() -> None:
    checks = {check.check_id: check for check in run_doctor()}
    assert checks["python"].status == "pass"
    assert checks["asset-root"].status == "review"
    assert "rsync" in checks
    assert "rrsync" in checks


def test_doctor_reports_valid_asset_layout_and_non_target_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_external_checks(monkeypatch)
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")

    checks = {check.check_id: check for check in run_doctor(layout.root)}
    assert checks["asset-root"].status == "pass"
    assert checks["asset-root"].details["path"] == str(layout.root)
    assert checks["host"].status == "review"


def test_doctor_reports_invalid_asset_layout_and_old_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_external_checks(monkeypatch)
    monkeypatch.setattr(doctor.sys, "version_info", (3, 11))

    checks = {check.check_id: check for check in run_doctor(tmp_path / "absent" / "assets")}
    assert checks["python"].status == "fail"
    assert checks["asset-root"].status == "fail"
    assert checks["asset-root"].details["path"] == str(tmp_path / "absent" / "assets")
