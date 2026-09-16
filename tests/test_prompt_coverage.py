"""TD1-TD2: the read-only coverage report and its parity with RecordingIndex (design §7.2)."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args

import pytest
from recording_fixtures import synthetic_pilot_v2_catalog
from test_pilot_v2_coverage import (
    ROOM_TONES,
    SUBJECT,
    NativeCoverage,
    add_foreign_package,
    build_legacy_root,
    pilot_v1,
    prepare_pilot_v2_package,
    publish,
    spoken,
)
from test_pilot_v2_coverage import analyzer_build as analyzer_build
from test_pilot_v2_coverage import foreign_pilot_v2_package as foreign_pilot_v2_package
from test_pilot_v2_coverage import native_coverage as native_coverage
from test_recording_takes import DEVICE, Rig

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.cli import main
from cole_voice_lab.models import PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.prompt_coverage import PromptCoverageProjectionError, prompt_coverage_report
from cole_voice_lab.recording_contracts import AnalysisState, DecisionState
from cole_voice_lab.recording_index import _RETAINED, RecordingIndex, is_retained
from cole_voice_lab.recording_takes import RecordingTakeStore


def _tree(root: Path) -> dict[str, str | None]:
    return {
        path.relative_to(root).as_posix(): None
        if path.is_dir()
        else hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
    }


def test_report_explains_the_pilot_v2_room_tone_losses(
    tmp_path: Path, foreign_pilot_v2_package: tuple[Path, str]
) -> None:
    """TD1: only the changed room tones are lost; a foreign package stays a baseline Unknown."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout, pilot, pilot_v2 = evidence.layout, pilot_v1(), synthetic_pilot_v2_catalog()
    report = prompt_coverage_report(layout, SUBJECT, pilot_v2, pilot)
    assert report.catalog_sha256 == canonical_sha256(pilot_v2)
    assert report.baseline_catalog_sha256 == canonical_sha256(pilot)
    assert report.changed_prompt_ids == ROOM_TONES
    assert report.lost_prompt_ids == ROOM_TONES
    assert report.missing_prompt_ids == ROOM_TONES
    assert (report.covered_prompt_count, report.total_prompt_count) == (34, 36)
    assert report.unknown_record_ids == report.baseline_unknown_record_ids == ()
    assert report.exit_status == 0
    rows = {row.prompt_id: row for row in report.prompts}
    assert rows[spoken(1)].retained_take_count == 3
    assert rows[spoken(1)].changed is False and rows[spoken(1)].covered_in_baseline is True
    assert rows["room-tone-start"].changed is True
    assert rows["room-tone-start"].covered_in_baseline is True
    payload = report.to_json()
    assert payload["exit_status"] == 0 and len(payload["prompts"]) == 36  # type: ignore[arg-type]

    foreign = add_foreign_package(evidence, foreign_pilot_v2_package)
    report = prompt_coverage_report(layout, SUBJECT, pilot_v2, pilot)
    assert report.unknown_record_ids == report.baseline_unknown_record_ids == (f"legacy:{foreign}",)
    assert report.lost_prompt_ids == ROOM_TONES and report.exit_status == 0
    without_baseline = prompt_coverage_report(layout, SUBJECT, pilot_v2)
    assert without_baseline.exit_status == 1
    assert "changed" not in without_baseline.to_json()["prompts"][0]  # type: ignore[index,operator]


def test_report_exit_status_fails_on_projection_errors_and_catalog_only_unknowns(
    tmp_path: Path, analyzer_build: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """TD1: a projection failure and an Unknown that exists only under the catalog both exit 1."""
    local = prepare_pilot_v2_package(
        tmp_path.resolve() / "local", analyzer_build, ("room-tone-start", spoken(1))
    )[0]
    evaluation = load_builtin_prompt_suite("evaluation-v1.json")
    report = prompt_coverage_report(local, SUBJECT, evaluation, synthetic_pilot_v2_catalog())
    assert len(report.unknown_record_ids) == 1 and report.baseline_unknown_record_ids == ()
    assert report.exit_status == 1
    arguments = ["prompts", "coverage-report", "--asset-root", str(local.root)]
    arguments += ["--subject-id", SUBJECT, "--json"]
    assert main([*arguments, "--catalog", "pilot-v1"]) == 0
    capsys.readouterr()
    assert main([*arguments, "--catalog", "evaluation-v1", "--baseline", "pilot-v1"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["unknown_record_ids"] == list(report.unknown_record_ids)
    human = [argument for argument in arguments if argument != "--json"]
    assert main([*human, "--catalog", "evaluation-v1", "--baseline", "pilot-v1"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("0/12 prompts covered") and lines[3] == f"lost: {spoken(1)}"
    assert lines[4] == f"unknown: {report.unknown_record_ids[0]}"

    evidence = build_legacy_root(tmp_path.resolve() / "legacy")
    source = next((evidence.layout.directory("tmp") / "capture-legacy-pilot").rglob("*.wav"))
    metadata = source.stat()
    source.chmod(0o600)
    data = bytearray(source.read_bytes())
    data[-1] ^= 1
    source.write_bytes(data)
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    with pytest.raises(PromptCoverageProjectionError, match="projection failed"):
        prompt_coverage_report(evidence.layout, SUBJECT, pilot_v1())
    capsys.readouterr()
    legacy_arguments = ["prompts", "coverage-report", "--asset-root", str(evidence.layout.root)]
    assert main([*legacy_arguments, "--subject-id", SUBJECT, "--catalog", "pilot-v1"]) == 1
    assert "projection failed" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["pilot", "evaluation", "pilot-v1.json", "/tmp/pilot-v1.json"])
def test_coverage_report_accepts_built_in_versioned_names_only(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["prompts", "coverage-report", "--asset-root", "/private/tmp/unused-root"]
    for option in ("--catalog", "--baseline"):
        extra = [] if option == "--catalog" else ["--catalog", "pilot-v1"]
        with pytest.raises(SystemExit) as refused:
            main([*arguments, "--subject-id", SUBJECT, *extra, option, name])
        assert refused.value.code == 2 and "invalid choice" in capsys.readouterr().err


_AUDITED_DRIVER = r"""
import json
import os
import sys

ROOT = os.path.realpath(sys.argv[1])
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL
MUTATING = {
    "os.chflags", "os.chmod", "os.chown", "os.link", "os.mkdir", "os.mkfifo", "os.mknod",
    "os.remove", "os.rename", "os.rmdir", "os.symlink", "os.truncate", "os.utime",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move",
    "shutil.rmtree", "tempfile.mkdtemp", "tempfile.mkstemp",
}
VIOLATIONS = []


def _inside(value):
    if not isinstance(value, (str, bytes)):
        return False
    path = os.fsdecode(value)
    # Descriptor-relative names cannot be attributed, so any mutation through one counts.
    return not os.path.isabs(path) or path == ROOT or path.startswith(ROOT + os.sep)


def _guard(event, arguments):
    # Installed before cole_voice_lab is imported.
    if event == "sqlite3.connect":
        VIOLATIONS.append([event, repr(arguments[:1])])
    elif event == "open" and arguments:
        path, mode, flags = arguments[0], arguments[1], arguments[2]
        writes = (isinstance(flags, int) and flags & WRITE_FLAGS) or (
            isinstance(mode, str) and any(character in mode for character in "wax+")
        )
        if writes and _inside(path):
            VIOLATIONS.append([event, os.fsdecode(path)])
    elif event in MUTATING and any(_inside(argument) for argument in arguments):
        VIOLATIONS.append([event, repr(arguments)])


sys.addaudithook(_guard)

from pathlib import Path

from cole_voice_lab.cli import main
from cole_voice_lab.models import PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.prompt_coverage import prompt_coverage_report

catalog = PromptSuite.model_validate_json(Path(sys.argv[2]).read_bytes())
report = prompt_coverage_report(
    AssetLayout(Path(ROOT)), sys.argv[3], catalog, load_builtin_prompt_suite("pilot-v1.json")
)
status = main(
    ["prompts", "coverage-report", "--asset-root", ROOT, "--subject-id", sys.argv[3],
     "--catalog", "pilot-v1", "--baseline", "pilot-v1", "--json"]
)
print(json.dumps({"violations": VIOLATIONS, "report": report.to_json(), "cli_status": status}))
"""


def test_report_never_writes_creates_or_opens_sqlite_under_the_asset_root(
    tmp_path: Path,
) -> None:
    """TD1: an audit hook installed before import observes the report and its CLI."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout, pilot_v2 = evidence.layout, synthetic_pilot_v2_catalog()
    rig = Rig(layout, RecordingTakeStore(layout), "")
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    rig.group_id = rig.store.open_group(pilot_v1(), SUBJECT, DEVICE, "synthetic-host").group_id
    native = rig.store.save(rig.command(rig.seal(rig.reserve("reserve-native", spoken(5))))).take
    catalog_path = tmp_path / "pilot-v2.json"
    catalog_path.write_text(pilot_v2.model_dump_json(), encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(_AUDITED_DRIVER, encoding="utf-8")
    before = _tree(layout.root)

    completed = subprocess.run(
        (sys.executable, "-I", "-B", str(driver), str(layout.root), str(catalog_path), SUBJECT),
        cwd="/",
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["violations"] == []
    assert result["cli_status"] == 0
    rows = {row["prompt_id"]: row for row in result["report"]["prompts"]}
    assert rows[spoken(5)]["retained_take_count"] == 2  # sidecar-less legacy plus native
    assert native.decision == "kept"
    assert result["report"]["lost_prompt_ids"] == list(ROOM_TONES)
    assert result["report"]["exit_status"] == 0
    assert _tree(layout.root) == before


def test_is_retained_matches_the_index_predicate_for_every_state() -> None:
    """TD2."""
    decisions, analyses = get_args(DecisionState.__value__), get_args(AnalysisState.__value__)
    pairs = list(itertools.product(decisions, analyses))
    assert len(pairs) == 24
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE t (recording_take_decision, recording_take_analysis)")
        connection.executemany("INSERT INTO t VALUES (?, ?)", pairs)
        retained = set(connection.execute("SELECT * FROM t WHERE " + _RETAINED).fetchall())
    assert retained == {pair for pair in pairs if is_retained(*pair)}
    assert retained == {("kept", analysis) for analysis in analyses} - {("kept", "invalid")}


def _assert_parity(layout: AssetLayout, index: RecordingIndex, catalog: PromptSuite) -> None:
    report = prompt_coverage_report(layout, SUBJECT, catalog)
    page = index.history(canonical_sha256(catalog), page_size=200)
    assert not page.has_more_prompts
    indexed = {row.prompt_id: (row.kept_count > 0, row.kept_count) for row in page.prompts}
    assert {
        row.prompt_id: (row.covered, row.retained_take_count) for row in report.prompts
    } == indexed
    assert report.covered_prompt_count == page.covered_prompt_count


def test_report_coverage_equals_indexed_history_for_native_takes(
    native_coverage: NativeCoverage,
) -> None:
    """TD2 on the TC1 fixture."""
    for catalog in (native_coverage.pilot_v1, native_coverage.pilot_v2):
        _assert_parity(native_coverage.rig.layout, native_coverage.index, catalog)


def test_report_coverage_equals_indexed_history_for_legacy_takes(
    tmp_path: Path, foreign_pilot_v2_package: tuple[Path, str]
) -> None:
    """TD2 on the TC2 fixture, drafts, rejections, removals and the foreign package included."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    add_foreign_package(evidence, foreign_pilot_v2_package)
    with RecordingIndex(evidence.layout, subject_id=SUBJECT) as index:
        for catalog in (pilot_v1(), synthetic_pilot_v2_catalog()):
            publish(evidence.layout, index, catalog)
        for catalog in (pilot_v1(), synthetic_pilot_v2_catalog()):
            _assert_parity(evidence.layout, index, catalog)
