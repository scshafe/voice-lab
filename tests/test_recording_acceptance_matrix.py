"""The acceptance matrix must never report a closed gate it has no evidence for."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from recording_fixtures import REPOSITORY_ROOT

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from recording_acceptance_matrix import (  # noqa: E402
    PARITY_ROWS,
    PHYSICAL_GATE_ROWS,
    build_matrix,
    expected_state_snapshots,
    main,
    missing_state_snapshots,
    render_markdown,
)

PASSING_BENCHMARK = {
    "backend": "native",
    "profile": "acceptance",
    "created_at": "2026-09-12T00:00:00+00:00",
    "release_acceptance_passed": True,
    "host": {"swift_configuration": "release", "sanitizers": []},
    "acceptance_rows": {
        "stop_reaches_controller": {
            "doc_line": 562,
            "row": "Stop command reaches capture controller",
            "verdict": "pass",
            "observed_ms": {"p95": 0.1},
            "thresholds_ms": {"p95": 50.0},
        }
    },
}


def complete_root(tmp_path: Path) -> Path:
    """A verification root that satisfies every parity row, so gaps are the test's doing."""

    root = tmp_path / "verification"
    root.mkdir()
    keys = {
        row["evidence"]["key"]: True for row in PARITY_ROWS if row["evidence"]["kind"] == "identity"
    }
    keys.update(shell_geometry_cases=1, maximum_transport_region_delta_points=0.0)
    (root / "verification.json").write_text(json.dumps(keys), encoding="utf-8")
    for row in PARITY_ROWS:
        if row["evidence"]["kind"] != "artifact":
            continue
        path = root / row["evidence"]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    return root


def test_the_physical_gate_alone_keeps_acceptance_open(tmp_path: Path) -> None:
    """Every synthetic row can pass and the matrix must still not read as closed."""

    matrix = build_matrix(PASSING_BENCHMARK, complete_root(tmp_path))
    assert all(row["verdict"] == "verified" for row in matrix["parity"])
    assert all(row["verdict"] == "pass" for row in matrix["performance"])
    assert len(matrix["physical_gate"]) == len(PHYSICAL_GATE_ROWS)
    assert all(row["verdict"] == "not-measured" for row in matrix["physical_gate"])
    assert matrix["acceptance_closed"] is False
    assert len(matrix["outstanding"]) == len(PHYSICAL_GATE_ROWS)


def test_fine_tuning_is_cited_from_its_own_section_and_checked_physically(
    tmp_path: Path,
) -> None:
    import recording_acceptance_matrix as module

    matrix = build_matrix(PASSING_BENCHMARK, complete_root(tmp_path))
    row = next(row for row in matrix["parity"] if row["id"] == "finetune_from_recordings")
    assert row["section"] == "16" and row["doc_anchor"] == "16-fine-tuning-from-the-app"
    lines = module.DOCUMENT_PATH.read_text(encoding="utf-8").splitlines()
    assert lines[row["doc_line"] - 1] == "## 16. Fine-tuning from the app"
    assert row["evidence"] == "verification.json:finetune" and row["verdict"] == "verified"
    physical = [row["requirement"] for row in matrix["physical_gate"]]
    assert "A disposable full package through local intake" not in physical
    check = "Fine-tune model on real recordings: start, watch progress, cancel, finish, listen"
    assert check in physical
    text = render_markdown(matrix)
    assert "sections 4-13, 16 and 17" in text and "## Parity (sections 4-11, 13, 16, 17)" in text
    assert f"| [16:{row['doc_line']}]" in text and check in text


def test_the_speak_page_is_cited_from_its_own_section_and_checked_physically(
    tmp_path: Path,
) -> None:
    import recording_acceptance_matrix as module

    matrix = build_matrix(PASSING_BENCHMARK, complete_root(tmp_path))
    row = next(row for row in matrix["parity"] if row["id"] == "speak_page")
    assert row["section"] == "17" and row["doc_anchor"] == "17-speak-page"
    lines = module.DOCUMENT_PATH.read_text(encoding="utf-8").splitlines()
    assert lines[row["doc_line"] - 1] == "## 17. Speak page"
    assert row["evidence"] == "verification.json:speak" and row["verdict"] == "verified"
    check = (
        "Speak page: hear typed text from the latest fine-tune and from Base, Save As, then record"
    )
    physical = [row["requirement"] for row in matrix["physical_gate"]]
    assert physical[-1] == check
    text = render_markdown(matrix)
    assert f"| [17:{row['doc_line']}]" in text and check in text

    # Without the suite's evidence the row stays open.
    (tmp_path / "other").mkdir()
    root = complete_root(tmp_path / "other")
    identity = json.loads((root / "verification.json").read_text())
    del identity["speak"]
    (root / "verification.json").write_text(json.dumps(identity))
    unverified = build_matrix(PASSING_BENCHMARK, root)
    assert "speak_page" in unverified["outstanding"]


def test_a_missing_or_unreadable_artifact_is_unverified_not_assumed(tmp_path: Path) -> None:
    root = complete_root(tmp_path)
    artifact = next(row for row in PARITY_ROWS if row["evidence"]["kind"] == "artifact")
    (root / artifact["evidence"]["path"]).unlink()
    matrix = build_matrix(PASSING_BENCHMARK, root)
    missing = next(row for row in matrix["parity"] if row["id"] == artifact["id"])
    assert missing["verdict"] == "not-verified"
    assert "no durable artifact" in missing["detail"]

    (root / artifact["evidence"]["path"]).write_text("{not json", encoding="utf-8")
    unreadable = next(
        row
        for row in build_matrix(PASSING_BENCHMARK, root)["parity"]
        if row["id"] == artifact["id"]
    )
    assert unreadable["verdict"] == "not-verified"
    assert "unreadable" in unreadable["detail"]


def test_an_absent_verification_identity_verifies_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    matrix = build_matrix(PASSING_BENCHMARK, empty)
    assert all(row["verdict"] == "not-verified" for row in matrix["parity"])


def test_performance_verdicts_are_taken_from_the_benchmark_never_recomputed(
    tmp_path: Path,
) -> None:
    """The matrix reports what the benchmark judged; it cannot upgrade a row itself."""

    failing = PASSING_BENCHMARK | {
        "acceptance_rows": {
            "recorder_memory_growth": {
                "doc_line": 569,
                "row": "Added recorder and waveform working memory over warmed idle",
                "verdict": "fail",
                "observed": {"value_bytes": 400_000_000.0},
                "thresholds": {"maximum": 67_108_864.0},
            },
            "warm_launch_to_useful": {
                "doc_line": 567,
                "row": "Warm indexed launch to useful library and recorder",
                "verdict": "not-measured",
                "owner": "physical gate",
                "reason": "No frame is ever presented.",
                "substitute_metric_name": "release_spawn_to_useful_model_ms",
            },
        }
    }
    matrix = build_matrix(failing, complete_root(tmp_path))
    verdicts = {row["id"]: row for row in matrix["performance"]}
    assert verdicts["recorder_memory_growth"]["verdict"] == "fail"
    assert verdicts["recorder_memory_growth"]["observed"] == {"value_bytes": 400_000_000.0}
    assert verdicts["warm_launch_to_useful"]["owner"] == "physical gate"
    assert {"recorder_memory_growth", "warm_launch_to_useful"} <= set(matrix["outstanding"])


def test_the_rendered_document_names_every_row_and_its_verdict(tmp_path: Path) -> None:
    text = render_markdown(build_matrix(PASSING_BENCHMARK, complete_root(tmp_path)))
    for row in PARITY_ROWS:
        assert row["requirement"] in text
    for requirement in PHYSICAL_GATE_ROWS:
        assert requirement in text
    assert "Acceptance closed: **False**" in text


def test_the_generator_writes_both_artifacts_privately(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps(PASSING_BENCHMARK), encoding="utf-8")
    output = tmp_path / "matrix"
    assert (
        main(
            [
                "--benchmark-report",
                str(report),
                "--verification-root",
                str(complete_root(tmp_path)),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    written = json.loads((output / "acceptance-matrix.json").read_text(encoding="utf-8"))
    assert written["acceptance_closed"] is False
    assert (output / "acceptance-matrix.md").read_text(encoding="utf-8").startswith("# Native")


def shell_root(tmp_path: Path, *, phases: tuple[str, ...], rendered: tuple[str, ...]) -> Path:
    """A shell artifact directory recording the canonical configuration and some renders."""

    root = tmp_path / "shell"
    root.mkdir()
    records = [
        {
            "width": 1060,
            "height": 700,
            "scale": 2,
            "appearance": appearance,
            "phase": phase,
            "scriptSize": 24,
            "focusMode": False,
            "roomTone": False,
            "regions": {},
        }
        for appearance in ("light", "dark")
        for phase in phases
    ] + [
        # Other configurations exist in the same record and must not be expected as files.
        {
            "width": 1440,
            "height": 980,
            "scale": 1,
            "appearance": "light",
            "phase": phases[0],
            "scriptSize": 32,
            "focusMode": True,
            "roomTone": True,
            "regions": {},
        }
    ]
    (root / "geometry.json").write_text(json.dumps(records), encoding="utf-8")
    for name in rendered:
        (root / name).write_bytes(b"\x89PNG")
    return root


def canonical_name(appearance: str, phase: str) -> str:
    return f"shell-1060x700-2x-{appearance}-{phase}-reading.png"


def test_the_state_inventory_comes_from_the_harness_record_not_a_kept_list(
    tmp_path: Path,
) -> None:
    """A state the shell starts drawing is expected without anyone updating this gate."""

    phases = ("ready", "recording", "aNewPhase")
    names = tuple(canonical_name(a, p) for a in ("light", "dark") for p in phases)
    root = shell_root(tmp_path, phases=phases, rendered=names)
    assert expected_state_snapshots(root) == sorted(names)
    assert missing_state_snapshots(root) == []


def test_a_state_with_no_rendered_file_is_reported_missing(tmp_path: Path) -> None:
    phases = ("ready", "recording", "saving")
    names = tuple(canonical_name(a, p) for a in ("light", "dark") for p in phases)
    root = shell_root(tmp_path, phases=phases, rendered=names[:-1])
    assert missing_state_snapshots(root) == [names[-1]]


def test_a_shell_record_without_the_canonical_configuration_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "shell"
    root.mkdir()
    (root / "geometry.json").write_text(
        json.dumps(
            [
                {
                    "width": 1440,
                    "height": 980,
                    "scale": 1,
                    "appearance": "light",
                    "phase": "ready",
                    "scriptSize": 32,
                    "focusMode": True,
                    "roomTone": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="canonical-configuration"):
        expected_state_snapshots(root)


@pytest.mark.parametrize(
    ("key", "value", "row_id"),
    [
        ("packaged_app_rollback_release_present", False, "rollback_release"),
        ("shell_geometry_cases", 0, "fixed_workspace_geometry"),
        ("shell_geometry_cases", True, "fixed_workspace_geometry"),
        ("maximum_transport_region_delta_points", 1.1, "transport_region_stability"),
        ("maximum_transport_region_delta_points", float("nan"), "transport_region_stability"),
    ],
)
def test_scalar_evidence_must_satisfy_its_actual_requirement(
    tmp_path: Path, key: str, value: object, row_id: str
) -> None:
    root = complete_root(tmp_path)
    path = root / "verification.json"
    identity = json.loads(path.read_text())
    identity[key] = value
    path.write_text(json.dumps(identity))
    matrix = build_matrix(PASSING_BENCHMARK, root)
    row = next(row for row in matrix["parity"] if row["id"] == row_id)
    assert row["verdict"] == "not-verified" and row_id in matrix["outstanding"]


def test_document_lines_follow_amendments_and_retain_stable_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import recording_acceptance_matrix as module

    root = complete_root(tmp_path)
    before = build_matrix(PASSING_BENCHMARK, root)
    amended = tmp_path / "amended.md"
    amended.write_bytes(
        b"<!-- A visible preceding amendment. -->\n\n" + module.DOCUMENT_PATH.read_bytes()
    )
    monkeypatch.setattr(module, "DOCUMENT_PATH", amended)
    after = build_matrix(PASSING_BENCHMARK, root)
    for category in ("parity", "performance", "physical_gate"):
        for original, current in zip(before[category], after[category], strict=True):
            assert current["doc_line"] == original["doc_line"] + 2
            assert current["doc_anchor"] == original["doc_anchor"]
    assert after["document_sha256"] != before["document_sha256"]
    row = after["performance"][0]
    assert (
        amended.read_text()
        .splitlines()[row["doc_line"] - 1]
        .startswith("| Stop command reaches capture controller")
    )
    assert row["benchmark_doc_line"] == 562


def test_missing_document_requirement_cannot_leave_a_stale_line_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import recording_acceptance_matrix as module

    amended = tmp_path / "amended.md"
    amended.write_text(
        module.DOCUMENT_PATH.read_text().replace("| Stop command reaches", "| Changed")
    )
    monkeypatch.setattr(module, "DOCUMENT_PATH", amended)
    with pytest.raises(ValueError, match="metric is absent"):
        build_matrix(PASSING_BENCHMARK, complete_root(tmp_path))
