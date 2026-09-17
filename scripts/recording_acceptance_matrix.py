"""Build the parity, failure and performance matrix for the native recording acceptance.

Every row names the document requirement it stands for and the durable artifact that
answers it. A row with no artifact is reported as unverified rather than assumed, and a
row that only a person with real hardware can answer is reported as exactly that. This
generator never re-derives a verdict from the thing it is judging: performance verdicts
come from the benchmark's own acceptance rows, which are computed and never hand-set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

DOCUMENT = "docs/NATIVE_RECORDING_UX.md"
DOCUMENT_PATH = Path(__file__).resolve().parent.parent / DOCUMENT

# Section 4 through 11 and 13, each row tied to the durable evidence that answers it.
# "artifact" rows cite a file under the native verification root; "identity" rows cite a
# key in its verification.json; "benchmark" rows defer to the benchmark's own verdict.
PARITY_ROWS: tuple[dict[str, Any], ...] = (
    {
        "id": "fixed_workspace_geometry",
        "section": "4",
        "requirement": "Fixed workspace: reserved regions never move with content or state",
        "evidence": {"kind": "identity", "key": "shell_geometry_cases"},
    },
    {
        "id": "transport_region_stability",
        "section": "4",
        "requirement": "Named region bounds differ by at most one point across transport",
        "evidence": {"kind": "identity", "key": "maximum_transport_region_delta_points"},
    },
    {
        "id": "keyboard_safety",
        "section": "4",
        "doc_heading": "Keyboard behavior",
        "requirement": "Keyboard behavior stays safe while a text field owns the keyboard",
        "evidence": {"kind": "artifact", "path": "transport/native/transport-result.json"},
    },
    {
        "id": "state_and_availability",
        "section": "5",
        "requirement": "Every control's availability follows the published state, not hope",
        "evidence": {"kind": "artifact", "path": "transport/native/transport-transitions.json"},
    },
    {
        "id": "prompts_and_sessions",
        "section": "6",
        "requirement": "Prompt navigation, room tone and Save and Next resolve one target",
        "evidence": {"kind": "artifact", "path": "transport/native/transport-result.json"},
    },
    {
        "id": "three_process_ownership",
        "section": "7",
        "requirement": "Client, recorder helper and service own their own lifetimes",
        "evidence": {"kind": "identity", "key": "bridge"},
    },
    {
        "id": "local_protocol_failures",
        "section": "7",
        "doc_heading": "Local protocols",
        "requirement": "Every local protocol failure is explicit and fails closed",
        "evidence": {"kind": "identity", "key": "bridge_failures"},
    },
    {
        "id": "native_capture_and_waveform",
        "section": "7",
        "doc_heading": "Native capture and waveform path",
        "requirement": "Native capture and waveform extrema come from the real written file",
        "evidence": {"kind": "identity", "key": "waveform"},
    },
    {
        "id": "capture_variants",
        "section": "7",
        "doc_heading": "Native capture and waveform path",
        "requirement": "Capture continuity, interruption and process control are exercised",
        "evidence": {"kind": "identity", "key": "capture_variants"},
    },
    {
        "id": "durable_save_ordering",
        "section": "8",
        "doc_heading": "Durable save ordering",
        "requirement": "Durable save ordering survives injected faults without losing a take",
        "evidence": {"kind": "artifact", "path": "review/native/review-report.json"},
    },
    {
        "id": "edits_and_derivatives",
        "section": "8",
        "requirement": "Edits are recipes over an accepted raw original, never in place",
        "evidence": {"kind": "artifact", "path": "review/native/review-report.json"},
    },
    {
        "id": "existing_recordings",
        "section": "9",
        "requirement": "Existing recordings stay readable and reviewable in the native app",
        "evidence": {"kind": "artifact", "path": "review-legacy/native/legacy-review-report.json"},
    },
    {
        "id": "finetune_from_recordings",
        "section": "9",
        "requirement": "Fine-tune model trains on the latest kept takes, shows progress, cancels",
        "evidence": {"kind": "identity", "key": "finetune"},
    },
    {
        "id": "recovery_after_crash",
        "section": "10",
        "requirement": "A crashed capture is recovered or explained, never silently lost",
        "evidence": {"kind": "identity", "key": "recovery_crash"},
    },
    {
        "id": "recovery_after_restart",
        "section": "10",
        "requirement": "A restarted session opens its own context and recovers partials",
        "evidence": {"kind": "identity", "key": "recovery_restart"},
    },
    {
        "id": "trust_boundaries",
        "section": "11",
        "requirement": "Private roots, confined paths and fail-closed validation hold",
        "evidence": {"kind": "identity", "key": "executables"},
    },
    {
        "id": "rollback_release",
        "section": "13",
        "requirement": "An independent frozen previous release exists as a rollback artifact",
        "evidence": {"kind": "identity", "key": "packaged_app_rollback_release_present"},
    },
)

# Stable metric IDs resolve against the current document table. A moved or
# amended table cannot silently leave generated citations pointing elsewhere.
PERFORMANCE_PREFIXES = {
    "visible_command_acknowledgment": "Click/key to visible acknowledged state",
    "record_to_first_confirmed_frames": "Warm Record to first confirmed frames",
    "cold_device_start": "Cold device start after permission already granted",
    "stop_reaches_controller": "Stop command reaches capture controller",
    "stop_to_sealed_review": "Stop to sealed review for",
    "save_next_durable_acknowledgment": "Save & Next to durable acknowledgment",
    "captured_sample_visible_age": "Captured-sample to visible waveform age",
    "waveform_render_work": "Foreground waveform drawing",
    "warm_launch_to_useful": "Warm indexed launch to useful library/recorder",
    "metadata_snapshot_at_1000": "Metadata snapshot and next-missing lookup at 1,000 takes",
    "recorder_memory_growth": "Added recorder/waveform working memory over warmed idle",
    "history_memory_growth": "Added indexed-history/cache memory at 1,000 takes",
    "historical_audio_inspections": "Historical audio inspections requested",
    "timer_frame_agreement": "Timer and final audio duration",
    "stable_layout": "Stable layout during transport/background processing",
}


def document_reference(
    lines: list[str], section: str, *, heading: str | None = None, metric_id: str | None = None
) -> dict[str, object]:
    """Resolve a unique heading/table row; keep its stable Markdown anchor with its line."""
    headings = [
        (number, line.removeprefix("### ").removeprefix("## "))
        for number, line in enumerate(lines, start=1)
        if (line == f"### {heading}" if heading is not None else line.startswith(f"## {section}. "))
    ]
    if len(headings) != 1:
        raise ValueError(
            f"Acceptance document heading is absent or ambiguous: {heading or section}"
        )
    line_number, title = headings[0]
    if metric_id is not None:
        prefix = PERFORMANCE_PREFIXES[metric_id]
        matches = [
            number for number, line in enumerate(lines, start=1) if line.startswith(f"| {prefix}")
        ]
        if len(matches) != 1:
            raise ValueError(f"Acceptance document metric is absent or ambiguous: {metric_id}")
        line_number = matches[0]
    return {
        "doc_line": line_number,
        "doc_anchor": re.sub(r"[^\w -]", "", title.casefold()).replace(" ", "-"),
    }


# Section 13's physical acceptance list. No synthetic run can answer any of these, and
# none of them is reported here as anything other than outstanding.
PHYSICAL_GATE_ROWS: tuple[str, ...] = (
    "Microphone permission granted and denied, with correct visible states",
    "The configured UMIK-1 is the device actually opened",
    "Startup and Stop on real hardware",
    "Quiet and spoken waveforms differ as expected",
    "Continuous capture without dropouts",
    "Headphone playback comparison",
    "Rapid record, stop and save cycles on real hardware",
    "Device unplug, reconnect and sleep or wake recovery",
    "Safe reopen and recovery after an interrupted session",
    "A disposable full package through local intake",
)


# The canonical configuration whose every state is rendered for review. Anything else in
# the shell matrix exists to prove geometry, not to be looked at.
CANONICAL_SNAPSHOT = {
    "width": 1060,
    "height": 700,
    "scale": 2,
    "scriptSize": 24,
    "focusMode": False,
    "roomTone": False,
}


def expected_state_snapshots(shell_directory: Path) -> list[str]:
    """Every state the harness recorded at the canonical configuration, by filename.

    Derived from the harness's own geometry record, so a state added to the shell can
    never be quietly absent from the inventory this gate checks.
    """

    records = json.loads((shell_directory / "geometry.json").read_text(encoding="utf-8"))
    canonical = [
        record
        for record in records
        if all(record.get(key) == value for key, value in CANONICAL_SNAPSHOT.items())
    ]
    if not canonical:
        raise RuntimeError("The shell suite recorded no canonical-configuration state")
    return sorted(
        {
            f"shell-{record['width']}x{record['height']}-{record['scale']}x-"
            f"{record['appearance']}-{record['phase']}-reading.png"
            for record in canonical
        }
    )


def missing_state_snapshots(shell_directory: Path) -> list[str]:
    """Names of canonical states with no rendered file. Absence is never assumed away."""

    return [
        name
        for name in expected_state_snapshots(shell_directory)
        if not (shell_directory / name).is_file()
    ]


def evidence_verdict(row: dict[str, Any], root: Path, identity: dict[str, Any]) -> dict[str, Any]:
    """Answer one parity row from durable evidence, defaulting to unverified."""

    evidence = row["evidence"]
    if evidence["kind"] == "identity":
        key = evidence["key"]
        present = key in identity
        value = identity.get(key)
        valid = present
        if key == "packaged_app_rollback_release_present":
            valid = value is True
        elif key == "shell_geometry_cases":
            valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
        elif key == "maximum_transport_region_delta_points":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 0 <= value <= 1
            )
        return {
            "verdict": "verified" if valid else "not-verified",
            "evidence": f"verification.json:{key}",
            "detail": "present and valid"
            if valid
            else "absent from the verification identity"
            if not present
            else "verification identity does not satisfy the requirement",
        }
    path = root / evidence["path"]
    if not path.is_file():
        return {
            "verdict": "not-verified",
            "evidence": evidence["path"],
            "detail": "no durable artifact at this path",
        }
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {
            "verdict": "not-verified",
            "evidence": evidence["path"],
            "detail": f"artifact is unreadable: {type(error).__name__}",
        }
    return {"verdict": "verified", "evidence": evidence["path"], "detail": "artifact present"}


def build_matrix(benchmark: dict[str, Any], root: Path) -> dict[str, Any]:
    """Combine parity evidence, the benchmark's own verdicts and the physical gate."""

    identity_path = root / "verification.json"
    identity = (
        json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.is_file() else {}
    )
    document = DOCUMENT_PATH.read_bytes()
    lines = document.decode("utf-8").splitlines()
    parity = [
        row
        | document_reference(lines, row["section"], heading=row.get("doc_heading"))
        | evidence_verdict(row, root, identity)
        for row in PARITY_ROWS
    ]
    performance = [
        {
            "id": row_id,
            "section": "12",
            **document_reference(lines, "12", metric_id=row_id),
            "benchmark_doc_line": entry.get("doc_line"),
            "requirement": entry["row"],
            "verdict": entry["verdict"],
            "owner": entry.get("owner", "benchmark"),
            "observed": entry.get("observed_ms") or entry.get("observed") or {},
            "thresholds": entry.get("thresholds_ms") or entry.get("thresholds") or {},
            "reason": entry.get("reason", ""),
            "substitute_metric_name": entry.get("substitute_metric_name", ""),
        }
        for row_id, entry in benchmark.get("acceptance_rows", {}).items()
    ]
    physical = [
        {
            "id": f"physical_{index}",
            "section": "13",
            **document_reference(lines, "13"),
            "requirement": requirement,
            "verdict": "not-measured",
            "owner": "operator with real hardware",
        }
        for index, requirement in enumerate(PHYSICAL_GATE_ROWS)
    ]
    outstanding = [
        row["id"]
        for row in (*parity, *performance, *physical)
        if row["verdict"] not in {"verified", "pass"}
    ]
    return {
        "schema_version": "cole-voice-lab.recording-acceptance-matrix/v1",
        "document": DOCUMENT,
        "document_path": str(DOCUMENT_PATH.resolve()),
        "document_sha256": hashlib.sha256(document).hexdigest(),
        "benchmark_report": {
            "backend": benchmark.get("backend"),
            "profile": benchmark.get("profile"),
            "created_at": benchmark.get("created_at"),
            "release_acceptance_passed": benchmark.get("release_acceptance_passed", False),
            "host": {
                key: benchmark.get("host", {}).get(key)
                for key in ("swift_configuration", "sanitizers", "git_revision", "macos_build")
            },
        },
        "verification_root": str(root),
        "parity": parity,
        "performance": performance,
        "physical_gate": physical,
        "outstanding": outstanding,
        # One number the reader cannot misread: acceptance is closed only when nothing
        # is outstanding, and the physical rows alone keep that impossible here.
        "acceptance_closed": not outstanding,
    }


def render_markdown(matrix: dict[str, Any]) -> str:
    """Render the matrix as a document a person can act on."""

    passed = matrix["benchmark_report"]["release_acceptance_passed"]
    lines = [
        "# Native recording acceptance matrix",
        "",
        f"Document: `{matrix['document']}` sections 4-13.",
        f"Benchmark: {matrix['benchmark_report']['backend']} backend, "
        f"{matrix['benchmark_report']['profile']} profile, "
        f"{matrix['benchmark_report']['created_at']}.",
        f"Release acceptance passed: **{passed}**. "
        f"Acceptance closed: **{matrix['acceptance_closed']}**. "
        f"Outstanding rows: **{len(matrix['outstanding'])}**.",
        "",
        "## Parity (sections 4-11, 13)",
        "",
        "| Section | Requirement | Verdict | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| [{row['section']}:{row['doc_line']}]({matrix['document_path']}:{row['doc_line']}) "
        f"| {row['requirement']} | {row['verdict']} "
        f"| `{row['evidence']}` |"
        for row in matrix["parity"]
    ]
    lines += [
        "",
        "## Performance (section 12)",
        "",
        "| Line | Requirement | Verdict | Observed | Target | Owner |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| [{row['doc_line']}]({matrix['document_path']}:{row['doc_line']}) "
        f"| {row['requirement']} | {row['verdict']} "
        f"| {row['observed'] or '-'} | {row['thresholds'] or '-'} | {row['owner']} |"
        for row in matrix["performance"]
    ]
    lines += [
        "",
        "## Physical gate (section 13)",
        "",
        "Not one of these can be answered by a synthetic run. Each needs an explicitly",
        "initiated, bounded diagnostic on the recording Mac with the real microphone.",
        "",
        "| Requirement | Verdict | Owner |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {row['requirement']} | {row['verdict']} | {row['owner']} |"
        for row in matrix["physical_gate"]
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-report", type=Path, required=True)
    parser.add_argument("--verification-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(argv)
    benchmark = json.loads(options.benchmark_report.read_text(encoding="utf-8"))
    matrix = build_matrix(benchmark, options.verification_root.resolve())
    options.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    (options.output / "acceptance-matrix.json").write_text(
        json.dumps(matrix, indent=2) + "\n", encoding="utf-8"
    )
    (options.output / "acceptance-matrix.md").write_text(render_markdown(matrix), encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "matrix_written",
                "output": str(options.output),
                "outstanding": len(matrix["outstanding"]),
                "acceptance_closed": matrix["acceptance_closed"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
