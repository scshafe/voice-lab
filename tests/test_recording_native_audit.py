"""Startup-role and fail-closed evidence tests; empty synthetic marker files only."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import REPOSITORY_ROOT
from recording_native_audit import (
    AUDIT_SCHEMA,
    SERVICE_IDENTITY_PREFLIGHT_COMMAND,
    _startup_role,
    audit_site,
    read_audit,
)


@pytest.fixture
def audit_root(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    root.chmod(0o700)
    (root / "assets").mkdir(mode=0o700)
    return root


def _run(root: Path, arguments: list[str]) -> tuple[subprocess.CompletedProcess[str], Path]:
    log = root / "wav-opens.jsonl"
    site = audit_site(root / "site", root / "assets", log)
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(site), str(root), str(REPOSITORY_ROOT / "src"))),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Error in sitecustomize" not in result.stderr
    return result, log


def test_normal_module_service_startup_is_service_and_opens_control(audit_root: Path) -> None:
    result, log = _run(
        audit_root, ["-m", "cole_voice_lab.recording_service", "--native-workspace", "--help"]
    )
    assert "usage:" in result.stdout
    rows = read_audit(log)
    assert len(rows) == 1
    control = rows[0]
    assert control["control"] is True and control["role"] == "service"
    assert control["argv"][0] == "-m"
    assert "--native-workspace" in control["argv"]
    assert control["module"] == "cole_voice_lab.recording_service"
    assert control["control_completed_uptime_ns"] >= control["uptime_ns"]
    assert Path(control["path"]).stat().st_size == 0


def test_exact_fixture_identity_preflight_is_foreground_service(audit_root: Path) -> None:
    result, log = _run(audit_root, ["-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND])
    identity = json.loads(result.stdout)
    assert isinstance(identity, dict) and identity
    rows = read_audit(log)
    assert len(rows) == 1
    control = rows[0]
    assert control["role"] == "service" and control["control"] is True
    assert control["argv"] == ["-c"] and control["module"] is None
    assert control["orig_argv"][1:] == ["-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND]
    assert control["control_completed_uptime_ns"] >= control["uptime_ns"]


@pytest.mark.parametrize(
    ("argv", "original"),
    [
        (["-c"], ["python", "-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND + " "]),
        (["-c"], ["python", "-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND + "; pass"]),
        (["-c"], ["python", "-u", "-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND]),
        (["-c", "--worker"], ["python", "-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND, "--worker"]),
        (["-c"], ["python", "-c", "print(" + repr(SERVICE_IDENTITY_PREFLIGHT_COMMAND) + ")"]),
    ],
)
def test_near_match_identity_commands_remain_unknown(argv: list[str], original: list[str]) -> None:
    assert _startup_role(argv, original) == ("unknown", None)


def test_actual_near_match_identity_preflight_fails_closed(audit_root: Path) -> None:
    _result, log = _run(audit_root, ["-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND + "; pass"])
    with pytest.raises(ValueError, match="BENCHMARK_AUDIT_UNKNOWN_ROLE"):
        read_audit(log)


@pytest.mark.parametrize(
    ("module", "arguments"),
    [
        ("cole_voice_lab.library_replication_service", ["--worker"]),
        ("cole_voice_lab.recording_analysis", ["--worker"]),
        ("cole_voice_lab.recording_edit_jobs", ["--worker"]),
        ("cole_voice_lab.recording_finetune_jobs", ["--worker"]),
        (
            "cole_voice_lab.recording_discovery",
            [
                "--asset-root",
                "/private/tmp/unused-synthetic-audit-root",
                "--group-id",
                "synthetic",
                "--inventory-fd",
                "3",
                "--spool-fd",
                "4",
                "--parent-fd",
                "5",
            ],
        ),
    ],
)
def test_known_worker_startup_has_separate_real_control(
    audit_root: Path, module: str, arguments: list[str]
) -> None:
    _result, log = _run(audit_root, ["-m", module, *arguments, "--help"])
    controls = read_audit(log)
    assert len(controls) == 1
    assert controls[0]["role"] == "worker" and controls[0]["control"] is True
    assert controls[0]["module"] == module
    assert controls[0]["argv"][0] == "-m"
    assert controls[0]["control_completed_uptime_ns"] >= controls[0]["uptime_ns"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["-c", "pass"],
        ["-c", "pass", "--worker"],
        ["-m", "cole_voice_lab.recording_analysis", "--help"],
        ["-m", "cole_voice_lab.recording_discovery", "--help"],
    ],
)
def test_unknown_startup_cannot_inherit_worker_exemption(
    audit_root: Path, arguments: list[str]
) -> None:
    _result, log = _run(audit_root, arguments)
    with pytest.raises(ValueError, match="BENCHMARK_AUDIT_UNKNOWN_ROLE"):
        read_audit(log)


def test_real_open_hook_separates_control_and_asset_files_from_sibling_prefix(
    audit_root: Path,
) -> None:
    source = audit_root / "assets" / "synthetic marker.wav"
    source.touch(mode=0o600)
    sibling = audit_root / "assets-sibling"
    sibling.mkdir(mode=0o700)
    other = sibling / "not-inside-assets.wav"
    other.touch(mode=0o600)
    probe = audit_root / "audit_empty_open_probe.py"
    probe.write_text(
        "import os, sys\nfor path in sys.argv[2:]:\n"
        "    fd = os.open(path, os.O_RDONLY)\n    os.close(fd)\n"
    )
    probe.chmod(0o600)
    _result, log = _run(
        audit_root, ["-m", "audit_empty_open_probe", "--native-workspace", str(source), str(other)]
    )
    rows = read_audit(log)
    controls = [row for row in rows if row["control"]]
    sources = [row for row in rows if not row["control"]]
    assert len(controls) == 1 and len(sources) == 1
    assert sources[0]["path"] == str(source)
    assert sources[0]["pid"] == controls[0]["pid"]
    assert sources[0]["role"] == "service"
    assert sources[0]["startup_id"] == controls[0]["startup_id"]


def _records(root: Path) -> list[dict[str, Any]]:
    control = str(root / "audit-positive-control.wav")
    return [
        {
            "kind": "startup",
            "schema_version": AUDIT_SCHEMA,
            "uptime_ns": 10,
            "pid": 123,
            "role": "service",
            "startup_id": "123-10",
            "argv": ["-m", "--native-workspace"],
            "orig_argv": ["python", "-m", "cole_voice_lab.recording_service", "--native-workspace"],
            "module": "cole_voice_lab.recording_service",
            "control_path": control,
            "asset_root": str(root / "assets"),
        },
        {
            "kind": "open",
            "uptime_ns": 11,
            "pid": 123,
            "role": "service",
            "startup_id": "123-10",
            "path": control,
            "control": True,
        },
        {
            "kind": "control_complete",
            "uptime_ns": 12,
            "pid": 123,
            "role": "service",
            "startup_id": "123-10",
            "path": control,
        },
        {
            "kind": "open",
            "uptime_ns": 13,
            "pid": 123,
            "role": "service",
            "startup_id": "123-10",
            "path": str(root / "assets" / "synthetic.wav"),
            "control": False,
        },
    ]


def _write_records(root: Path, records: list[dict[str, Any]]) -> Path:
    log = root / "audit.jsonl"
    log.write_text("".join(json.dumps(record) + "\n" for record in records))
    log.chmod(0o600)
    return log


def test_previously_unknown_preflight_evidence_is_not_reclassified(audit_root: Path) -> None:
    records = _records(audit_root)[:3]
    for record in records:
        record["role"] = "unknown"
    records[0].update(
        argv=["-c"],
        orig_argv=["python", "-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND],
        module=None,
    )
    log = _write_records(audit_root, records)
    original = log.read_bytes()
    with pytest.raises(ValueError, match="startup role differs"):
        read_audit(log)
    assert log.read_bytes() == original


@pytest.mark.parametrize(
    "fault",
    [
        "missing_startup",
        "missing_control",
        "missing_completion",
        "duplicate_control",
        "wrong_role",
        "wrong_pid",
        "boolean_pid",
        "wrong_control_flag",
        "outside_assets",
        "extra_field",
        "wrong_schema",
        "wrong_module",
        "early_completion",
    ],
)
def test_malformed_or_unbound_evidence_is_rejected(audit_root: Path, fault: str) -> None:
    records = copy.deepcopy(_records(audit_root))
    if fault == "missing_startup":
        records.pop(0)
    elif fault == "missing_control":
        records.pop(1)
    elif fault == "missing_completion":
        records.pop(2)
    elif fault == "duplicate_control":
        records.append(records[1])
    elif fault == "wrong_role":
        records[0]["role"] = "worker"
    elif fault == "wrong_pid":
        records[-1]["pid"] = 124
    elif fault == "boolean_pid":
        records[-1]["pid"] = True
    elif fault == "wrong_control_flag":
        records[-1]["control"] = True
    elif fault == "outside_assets":
        records[-1]["path"] = str(audit_root / "assets-sibling" / "synthetic.wav")
    elif fault == "extra_field":
        records[-1]["unexpected"] = 1
    elif fault == "wrong_schema":
        records[0]["schema_version"] = "unknown"
    elif fault == "wrong_module":
        records[0]["module"] = "cole_voice_lab.recording_analysis"
    elif fault == "early_completion":
        records[2]["uptime_ns"] = 10
    with pytest.raises(ValueError, match="BENCHMARK_AUDIT_INVALID"):
        read_audit(_write_records(audit_root, records))


@pytest.mark.parametrize("payload", ["", "{}", "broken\n", '{"pid":1,"pid":2}\n'])
def test_empty_truncated_and_duplicate_json_evidence_is_rejected(
    audit_root: Path, payload: str
) -> None:
    log = audit_root / "invalid.jsonl"
    log.write_text(payload)
    log.chmod(0o600)
    with pytest.raises(ValueError, match="BENCHMARK_AUDIT_INVALID"):
        read_audit(log)


def test_multiple_startups_keep_distinct_process_controls(audit_root: Path) -> None:
    records = _records(audit_root)
    second = copy.deepcopy(records[:3])
    for record in second:
        record.update(
            pid=456, startup_id="456-20", uptime_ns=record["uptime_ns"] + 10, role="worker"
        )
    second[0].update(
        argv=["-m", "--worker"],
        orig_argv=["python", "-m", "cole_voice_lab.recording_analysis", "--worker"],
        module="cole_voice_lab.recording_analysis",
    )
    rows = read_audit(_write_records(audit_root, records + second))
    assert {(row["pid"], row["role"]) for row in rows if row["control"]} == {
        (123, "service"),
        (456, "worker"),
    }


def test_generated_hook_rejects_nonempty_or_symlink_control(audit_root: Path) -> None:
    control = audit_root / "audit-positive-control.wav"
    control.write_bytes(b"synthetic-marker")
    control.chmod(0o600)
    with pytest.raises(ValueError, match="BENCHMARK_AUDIT_UNSAFE_FILE"):
        audit_site(audit_root / "site", audit_root / "assets", audit_root / "audit.jsonl")
    control.unlink()
    control.symlink_to(audit_root / "assets")
    with pytest.raises(ValueError, match="BENCHMARK_AUDIT_UNSAFE_FILE"):
        audit_site(audit_root / "site", audit_root / "assets", audit_root / "audit.jsonl")
