"""TD1-TD2: the read-only coverage report and its parity with RecordingIndex (design §7.2).

Published corpus-store catalog versions are read the same way: a keep-recordings alias keeps
earlier recordings of a prompt covered (the prompt still reads as changed), and a take whose
group references a snapshot is only counted once that snapshot resolves.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
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
    released_pilot_v2,
    spoken,
)
from test_pilot_v2_coverage import analyzer_build as analyzer_build
from test_pilot_v2_coverage import foreign_pilot_v2_package as foreign_pilot_v2_package
from test_pilot_v2_coverage import native_coverage as native_coverage
from test_recording_index_backfill import Published
from test_recording_index_backfill import published as published
from test_recording_takes import DEVICE, Rig, open_referenced, publish_catalog, store_suite

import cole_voice_lab.prompt_coverage as prompt_coverage
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.cli import main
from cole_voice_lab.corpus_store import CorpusSnapshotSecurityError, open_corpus_store
from cole_voice_lab.finetune import select_finetune_corpus
from cole_voice_lab.models import PROMPT_SUITE_V2, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite
from cole_voice_lab.prompt_coverage import (
    PromptCoverageProjectionError,
    coverage_catalog,
    prompt_coverage_report,
    prompt_recordings,
)
from cole_voice_lab.recording_contracts import (
    RECORDING_GROUP_V1,
    RECORDING_GROUP_V2,
    AnalysisState,
    CatalogSnapshot,
    DecisionState,
    PromptAlias,
    RecordingGroup,
    RecordingTake,
)
from cole_voice_lab.recording_index import (
    _RETAINED,
    RecordingIndex,
    history_entry_for_take,
    is_retained,
)
from cole_voice_lab.recording_takes import TAKE_PROVENANCE_ROOT, RecordingTakeStore


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
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unversioned aliases and paths are no built-ins; with no published catalog of that
    name (this root has no corpus store) they are refused by name and nothing is created."""
    root = AssetLayout.initialize(tmp_path.resolve() / "assets").root
    before = _tree(root)
    arguments = ["prompts", "coverage-report", "--asset-root", str(root)]
    for option in ("--catalog", "--baseline"):
        extra = [] if option == "--catalog" else ["--catalog", "pilot-v1"]
        assert main([*arguments, "--subject-id", SUBJECT, *extra, option, name]) == 2
        refused = capsys.readouterr()
        assert refused.out == ""
        assert f"not a released built-in prompt suite or a published catalog: {name}" in refused.err
    assert _tree(root) == before


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
from cole_voice_lab.recording_contracts import CatalogSnapshot

payload = Path(sys.argv[2]).read_bytes()
model = CatalogSnapshot if b'"prompt_aliases"' in payload else PromptSuite
catalog = model.model_validate_json(payload)
report = prompt_coverage_report(
    AssetLayout(Path(ROOT)), sys.argv[3], catalog, load_builtin_prompt_suite("pilot-v1.json")
)
status = main(
    ["prompts", "coverage-report", "--asset-root", ROOT, "--subject-id", sys.argv[3],
     "--catalog", "pilot-v1", "--baseline", "pilot-v1", "--json"]
)
print(json.dumps({"violations": VIOLATIONS, "report": report.to_json(), "cli_status": status}))
"""


def _audited_report(
    tmp_path: Path, layout: AssetLayout, catalog: PromptSuite | CatalogSnapshot
) -> dict[str, Any]:
    """Run the report (``catalog`` against pilot-v1) and its CLI under the audit hook."""
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(catalog.model_dump_json(), encoding="utf-8")
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
    result: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["violations"] == []
    assert result["cli_status"] == 0
    assert _tree(layout.root) == before
    return result


def _native_rig(layout: AssetLayout) -> Rig:
    rig = Rig(layout, RecordingTakeStore(layout), "")
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    return rig


def test_report_never_writes_creates_or_opens_sqlite_under_the_asset_root(
    tmp_path: Path,
) -> None:
    """TD1: an audit hook installed before import observes the report and its CLI."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout, pilot_v2 = evidence.layout, synthetic_pilot_v2_catalog()
    rig = _native_rig(layout)
    rig.group_id = rig.store.open_group(pilot_v1(), SUBJECT, DEVICE, "synthetic-host").group_id
    native = rig.store.save(rig.command(rig.seal(rig.reserve("reserve-native", spoken(5))))).take
    result = _audited_report(tmp_path, layout, pilot_v2)
    rows = {row["prompt_id"]: row for row in result["report"]["prompts"]}
    assert rows[spoken(5)]["retained_take_count"] == 2  # sidecar-less legacy plus native
    assert native.decision == "kept"
    assert result["report"]["lost_prompt_ids"] == list(ROOM_TONES)
    assert result["report"]["exit_status"] == 0


def test_report_through_a_published_snapshot_reads_files_only(tmp_path: Path) -> None:
    """TD1 with a corpus store: snapshot files are read, the database is never opened."""
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    layout = evidence.layout
    reworded = f"{released_pilot_v2().prompts[5].text} Again."
    with open_corpus_store(layout, create=True) as corpus:
        corpus.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
        corpus.create_catalog("cole-corpus", purpose="capture-pilot")
        seeded = corpus.publish_version("cole-corpus")
        seeded_snapshot = corpus.materialize(seeded)
        corpus.edit_prompt(spoken(5), text=reworded, keep_recordings=True, allow_rewording=True)
        kept = corpus.materialize(corpus.publish_version("cole-corpus"))
    rig = _native_rig(layout)
    group = rig.store.open_group(
        reference=seeded,
        suite=seeded_snapshot.suite,
        subject_id=SUBJECT,
        capture_device=DEVICE,
        capture_host="synthetic-host",
    )
    rig.group_id = group.group_id
    native = rig.store.save(rig.command(rig.seal(rig.reserve("reserve-native", spoken(5))))).take
    assert native.reservation.group.schema_version == RECORDING_GROUP_V2
    # The native take was recorded against the earlier text; the alias keeps it counting.
    result = _audited_report(tmp_path, layout, kept)
    report = result["report"]
    rows = {row["prompt_id"]: row for row in report["prompts"]}
    assert rows[spoken(5)]["retained_take_count"] == 2  # sidecar-less legacy plus native
    assert rows[spoken(5)]["covered"] is True and rows[spoken(5)]["changed"] is True
    assert report["catalog_sha256"] == kept.catalog_sha256
    assert report["changed_prompt_ids"] == [ROOM_TONES[0], spoken(5), ROOM_TONES[1]]
    assert report["lost_prompt_ids"] == list(ROOM_TONES)
    assert report["covered_prompt_count"] == 34 and report["exit_status"] == 0


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


def _assert_parity(
    layout: AssetLayout, index: RecordingIndex, catalog: PromptSuite | CatalogSnapshot
) -> None:
    report = prompt_coverage_report(layout, SUBJECT, catalog)
    page = index.history(coverage_catalog(catalog).catalog_sha256, page_size=200)
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


# Published corpus-store catalog versions --------------------------------------------------


def _record(rig: Rig, prompt_id: str, number: int, *, keep: bool) -> RecordingTake:
    draft = rig.seal(rig.reserve(f"reserve-{number}", prompt_id), f"seal-{number}")
    return rig.store.save(rig.command(draft, f"save-{number}")).take if keep else draft


def test_embedded_and_referenced_groups_of_one_suite_report_identically(tmp_path: Path) -> None:
    referenced_root = AssetLayout.initialize(tmp_path.resolve() / "referenced")
    reference, snapshot = publish_catalog(referenced_root, store_suite(3))
    embedded_root = AssetLayout.initialize(tmp_path.resolve() / "embedded")
    takes: dict[str, tuple[RecordingTake, RecordingTake]] = {}
    for layout in (embedded_root, referenced_root):
        rig = _native_rig(layout)
        if layout == embedded_root:
            group = rig.store.open_group(snapshot.suite, SUBJECT, DEVICE, "synthetic-host")
            assert group.schema_version == RECORDING_GROUP_V1
        else:
            group = rig.store.open_group(
                reference=reference,
                suite=snapshot.suite,
                subject_id=SUBJECT,
                capture_device=DEVICE,
                capture_host="synthetic-host",
            )
            assert group.schema_version == RECORDING_GROUP_V2
        rig.group_id = group.group_id
        kept = _record(rig, "spoken-0001", 1, keep=True)
        draft = _record(rig, "spoken-0002", 2, keep=False)
        takes[layout.root.name] = (kept, draft)
    identities = {"take_id", "group_id"}
    for embedded, referenced in zip(takes["embedded"], takes["referenced"], strict=True):
        assert history_entry_for_take(embedded).model_dump(
            exclude=identities
        ) == history_entry_for_take(referenced).model_dump(exclude=identities)
    for catalog in (snapshot, snapshot.suite):
        reports = [
            prompt_coverage_report(layout, SUBJECT, catalog).to_json()
            for layout in (embedded_root, referenced_root)
        ]
        assert reports[0] == reports[1]
        assert reports[0]["covered_prompt_count"] == 1
        assert reports[0]["missing_prompt_ids"] == ["spoken-0000", "spoken-0002"]


def test_each_group_catalog_is_resolved_once_and_only_for_the_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    reference, snapshot = publish_catalog(layout, store_suite(3))
    rig = _native_rig(layout)
    rig.group_id = open_referenced(rig.store, reference, snapshot).group_id
    for number, prompt_id in enumerate(("spoken-0001", "spoken-0002", "spoken-0001")):
        _record(rig, prompt_id, number, keep=True)
    resolved: list[str] = []
    real = RecordingTakeStore.group_catalog

    def counted(store: RecordingTakeStore, group: RecordingGroup) -> CatalogSnapshot:
        resolved.append(group.group_id)
        return real(store, group)

    monkeypatch.setattr(RecordingTakeStore, "group_catalog", counted)
    report = prompt_coverage_report(layout, "synthetic-subject", snapshot, snapshot)
    assert report.covered_prompt_count == 2 and report.prompts[1].retained_take_count == 2
    assert resolved == [rig.group_id]  # three takes, one group, one read for both catalogs
    (layout.root / reference.snapshot_path).unlink()
    # Another subject's report never reads that group's snapshot.
    assert prompt_coverage_report(layout, SUBJECT, snapshot).covered_prompt_count == 0
    assert resolved == [rig.group_id]
    with pytest.raises(CorpusSnapshotSecurityError) as caught:
        prompt_coverage_report(layout, "synthetic-subject", snapshot)
    assert caught.value.code == "CORPUS.SNAPSHOT_MISSING"


@pytest.mark.parametrize(
    "changes",
    [{"suite_id": "not-the-snapshot-suite"}, {"purpose": "held-out-evaluation"}],
    ids=["suite-id", "purpose"],
)
def test_one_report_refuses_a_later_take_whose_reference_disagrees_with_its_snapshot(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    """Per-call resolution is keyed by the whole reference, not only by the snapshot bytes."""
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    reference, snapshot = publish_catalog(layout, store_suite(3))
    rig = _native_rig(layout)
    rig.group_id = open_referenced(rig.store, reference, snapshot).group_id
    takes = [_record(rig, prompt_id, n, keep=True) for n, prompt_id in enumerate(PROMPT_PAIR)]
    # The report reads takes in id order: the untouched one resolves the snapshot first.
    legit, victim = sorted(takes, key=lambda take: take.reservation.take_id)
    payload = json.loads(canonical_json_bytes(victim))
    payload["reservation"]["group"]["catalog_reference"] |= changes
    payload["seal"]["reservation_sha256"] = canonical_sha256(payload["reservation"])
    forged = RecordingTake.model_validate_json(canonical_json_bytes(payload))
    directory = layout.root / TAKE_PROVENANCE_ROOT / victim.reservation.take_id
    for name, value in (
        ("reservation.json", payload["reservation"]),
        ("seal.json", payload["seal"]),
        ("head.json", payload),
    ):
        (directory / name).write_bytes(canonical_json_bytes(value) + b"\n")
        (directory / name).chmod(0o600)
    reader = RecordingTakeStore(layout, read_only=True)
    assert reader.get_take(victim.reservation.take_id) == forged
    assert reader.get_take(legit.reservation.take_id) == legit
    for attempt in (
        lambda: prompt_coverage_report(layout, "synthetic-subject", snapshot),
        lambda: select_finetune_corpus(layout, snapshot, "synthetic-subject", reference=reference),
    ):
        with pytest.raises(CorpusSnapshotSecurityError, match="declares suite") as caught:
            attempt()
        assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


PROMPT_PAIR = ("spoken-0001", "spoken-0002")


@dataclass(frozen=True)
class IdentityChange:
    """One kept take whose prompt a catalog lists as an alias across an identity change."""

    layout: AssetLayout
    snapshot: CatalogSnapshot
    recorded_suite: PromptSuite
    take: RecordingTake
    prompt_id: str


def identity_changing_alias(root: Path, change: str) -> IdentityChange:
    """A take recorded against a held-out (``holdout``) or room-tone (``silent-to-spoken``)
    prompt, and a capture-pilot snapshot whose same-id spoken prompt lists it as an alias.

    The store refuses such keep-recordings edits, so the snapshot is built in memory; the
    readers must still refuse to count the take.
    """
    layout = AssetLayout.initialize(root)
    base = store_suite(2)
    if change == "holdout":
        prompt_id = "spoken-0001"
        recorded_suite = PromptSuite(
            schema_version=PROMPT_SUITE_V2,
            suite_id="synthetic-evaluation",
            purpose="held-out-evaluation",
            prompts=tuple(prompt.model_copy(update={"holdout": True}) for prompt in base.prompts),
        )
        current = base.prompts[1].model_copy(update={"text": "A reworded synthetic sentence."})
        prompts: tuple[Prompt, ...] = (base.prompts[0], current)
    else:
        prompt_id = "room-tone-start"
        tone = Prompt(
            prompt_id=prompt_id,
            category="room-tone",
            style="Silence.",
            instructions="Stay silent for the whole take.",
            expected_seconds=5.0,
        )
        recorded_suite = PromptSuite(
            schema_version=PROMPT_SUITE_V2,
            suite_id=base.suite_id,
            purpose=base.purpose,
            prompts=(tone, *base.prompts),
        )
        current = Prompt(
            prompt_id=prompt_id,
            text="Now this prompt is a spoken sentence.",
            category="neutral",
            style="Plain reading, natural pace.",
            expected_seconds=2.0,
        )
        prompts = (current, *base.prompts)
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id=base.suite_id,
        purpose=base.purpose,
        prompts=prompts,
    )
    recorded = next(prompt for prompt in recorded_suite.prompts if prompt.prompt_id == prompt_id)
    snapshot = CatalogSnapshot(
        suite=suite,
        catalog_sha256=canonical_sha256(suite),
        prompt_aliases=(
            PromptAlias(
                prompt_sha256=canonical_sha256(current),
                accepted_prompt_sha256=canonical_sha256(recorded),
            ),
        ),
    )
    rig = _native_rig(layout)
    rig.group_id = rig.store.open_group(recorded_suite, SUBJECT, DEVICE, "synthetic-host").group_id
    take = _record(rig, prompt_id, 1, keep=True)
    assert take.reservation.prompt == recorded
    return IdentityChange(layout, snapshot, recorded_suite, take, prompt_id)


@pytest.mark.parametrize("change", ["holdout", "silent-to-spoken"])
def test_an_alias_that_changes_the_prompts_identity_covers_nothing(
    tmp_path: Path, change: str
) -> None:
    case = identity_changing_alias(tmp_path.resolve() / "assets", change)
    report = prompt_coverage_report(case.layout, SUBJECT, case.snapshot)
    row = next(row for row in report.prompts if row.prompt_id == case.prompt_id)
    assert (row.covered, row.retained_take_count) == (False, 0)
    assert case.prompt_id in report.missing_prompt_ids
    # Under the catalog it was recorded against, the take counts as before.
    own = prompt_coverage_report(case.layout, SUBJECT, case.recorded_suite)
    assert next(row for row in own.prompts if row.prompt_id == case.prompt_id).covered


def test_report_counts_legacy_recordings_through_keep_recordings_aliases(
    published: Published,
) -> None:
    """TC2 under published versions: rewording with kept recordings loses nothing."""
    layout, pilot_v2 = published.evidence.layout, released_pilot_v2()
    reworded = (spoken(1), spoken(3))
    before = prompt_coverage_report(layout, SUBJECT, pilot_v2)
    seeded = prompt_coverage_report(layout, SUBJECT, published.seeded, pilot_v2)
    assert seeded.changed_prompt_ids == seeded.lost_prompt_ids == ()
    kept = prompt_coverage_report(layout, SUBJECT, published.kept, pilot_v2)
    assert kept.catalog_sha256 == published.kept.catalog_sha256
    assert kept.baseline_catalog_sha256 == canonical_sha256(pilot_v2)
    assert kept.changed_prompt_ids == reworded and kept.lost_prompt_ids == ()
    assert (kept.covered_prompt_count, kept.total_prompt_count) == (34, 36)
    assert kept.missing_prompt_ids == ROOM_TONES and kept.exit_status == 0
    for report in (seeded, kept):
        assert [
            (row.prompt_id, row.covered, row.retained_take_count) for row in report.prompts
        ] == [(row.prompt_id, row.covered, row.retained_take_count) for row in before.prompts]
    rows = {row.prompt_id: row for row in kept.prompts}
    assert rows[spoken(1)].retained_take_count == 3
    assert rows[spoken(1)].changed is True and rows[spoken(1)].covered_in_baseline is True
    assert rows[spoken(1)].prompt_sha256 != canonical_sha256(pilot_v2.prompts[1])
    # Without the snapshot's aliases the reworded prompts' recordings do not count.
    plain = prompt_coverage_report(layout, SUBJECT, published.kept.suite)
    assert set(plain.missing_prompt_ids) == {*ROOM_TONES, *reworded}

    changed = prompt_coverage_report(layout, SUBJECT, published.changed, published.kept)
    assert changed.changed_prompt_ids == changed.lost_prompt_ids == (spoken(2),)
    assert changed.missing_prompt_ids == (ROOM_TONES[0], spoken(2), ROOM_TONES[1])
    assert changed.covered_prompt_count == 33 and changed.exit_status == 0
    assert prompt_coverage_report(layout, SUBJECT, published.changed).exit_status == 0


def test_report_coverage_equals_indexed_history_through_aliases(published: Published) -> None:
    """TD2 on the TC2 fixture under published versions with kept and changed prompts."""
    layout = published.evidence.layout
    versions = (published.seeded, published.kept, published.changed)
    with RecordingIndex(layout, subject_id=SUBJECT) as index:
        for catalog in versions:
            publish(layout, index, catalog)
        for catalog in versions:
            _assert_parity(layout, index, catalog)


def test_report_names_the_published_versions_it_compares(published: Published) -> None:
    layout = published.evidence.layout
    with open_corpus_store(layout, create=False) as corpus:
        seeded, kept = (corpus.resolve_reference("cole-corpus", version) for version in (1, 2))
    report = prompt_coverage_report(
        layout,
        SUBJECT,
        published.kept,
        published.seeded,
        reference=kept,
        baseline_reference=seeded,
    )
    assert (report.catalog_id, report.catalog_version) == ("cole-corpus", 2)
    assert (report.baseline_catalog_id, report.baseline_catalog_version) == ("cole-corpus", 1)
    payload = report.to_json()
    assert (payload["catalog_id"], payload["catalog_version"]) == ("cole-corpus", 2)
    assert (payload["baseline_catalog_id"], payload["baseline_catalog_version"]) == (
        "cole-corpus",
        1,
    )
    built_in = prompt_coverage_report(layout, SUBJECT, released_pilot_v2()).to_json()
    assert built_in["catalog_id"] is built_in["catalog_version"] is None
    assert built_in["baseline_catalog_id"] is built_in["baseline_catalog_version"] is None

    with pytest.raises(CorpusSnapshotSecurityError) as mismatch:
        prompt_coverage_report(layout, SUBJECT, published.kept, reference=seeded)
    assert mismatch.value.code == "CORPUS.SNAPSHOT_MISMATCH"
    with pytest.raises(CorpusSnapshotSecurityError):
        prompt_coverage_report(
            layout, SUBJECT, published.kept, published.kept, baseline_reference=seeded
        )
    with pytest.raises(ValueError, match="baseline reference needs the baseline catalog"):
        prompt_coverage_report(layout, SUBJECT, published.kept, baseline_reference=kept)


def test_one_prompt_revision_counts_the_recordings_the_report_counts(
    published: Published,
) -> None:
    """What `catalog edit-prompt` states: the report's retained takes, for one revision."""
    layout = published.evidence.layout
    for snapshot in (published.kept, published.changed):
        report = {
            row.prompt_id: row for row in prompt_coverage_report(layout, SUBJECT, snapshot).prompts
        }
        for prompt_id in (spoken(1), spoken(2), spoken(3), ROOM_TONES[0]):
            prompt = snapshot.prompt(prompt_id)
            assert prompt is not None
            digest = canonical_sha256(prompt)
            accepted = tuple(
                alias.accepted_prompt_sha256
                for alias in snapshot.prompt_aliases
                if alias.prompt_sha256 == digest
            )
            counted = prompt_recordings(layout, SUBJECT, prompt, accepted)
            assert (counted.prompt_id, counted.prompt_sha256) == (prompt_id, digest)
            assert len(counted.take_ids) == report[prompt_id].retained_take_count, prompt_id
            assert counted.unknown_record_ids == ()
            if prompt_id == spoken(1) and snapshot is published.kept:
                assert len(counted.take_ids) == 3
                # Without its aliases the reworded revision counts nothing.
                assert prompt_recordings(layout, SUBJECT, prompt).take_ids == ()
    assert prompt_recordings(layout, "someone-else", pilot_v1().prompts[1]).take_ids == ()


def test_only_a_revision_some_prompt_suite_v2_catalog_can_hold_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store holds no other revision, so any other prompt is a caller's mistake."""
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")

    def prompt(**fields: Any) -> Prompt:
        base: dict[str, Any] = {
            "prompt_id": "probe",
            "text": "A probe sentence.",
            "category": "neutral",
            "style": "Plain.",
            "expected_seconds": 2.0,
        }
        return Prompt.model_validate({**base, **fields})

    countable = (
        prompt(),
        prompt(holdout=True),
        prompt(labels=("test",)),
        prompt(text=None, category="procedure", instructions="Press Record.", labels=("test",)),
        prompt(text=None, category="room-tone", instructions="Stay silent.", labels=("quiet",)),
    )
    for candidate in countable:
        counted = prompt_recordings(layout, SUBJECT, candidate)
        assert counted.take_ids == () and counted.unknown_record_ids == (), candidate

    def no_evidence(*_: object) -> None:
        raise AssertionError("an uncountable prompt reads no evidence")

    monkeypatch.setattr(prompt_coverage, "_native_entries", no_evidence)
    for candidate, problem in (
        (
            prompt(text=None, category="procedure", instructions="Press Record."),
            "physical-test prompts require the test label",
        ),
        (prompt(holdout=True, labels=("test",)), "only physical-test catalogs may use the test"),
        # A prompt-suite/v1 room tone kept its text; no store revision can.
        (prompt(category="room-tone"), "room tone requires instructions and no text"),
        (
            prompt(category="room-tone", labels=("quiet",)),
            "prompt-suite/v2 room tone requires instructions",
        ),
        (prompt(labels=("test",), reference_candidate=True), "no references"),
    ):
        with pytest.raises(ValueError, match="fits no prompt-suite/v2 catalog") as refused:
            prompt_recordings(layout, SUBJECT, candidate, ("a" * 64,))
        assert problem in str(refused.value), candidate
        assert "no corpus-store revision" in str(refused.value)
    monkeypatch.undo()
    probe = prompt()
    with pytest.raises(PromptCoverageProjectionError, match="recording count failed"):
        prompt_recordings(layout, SUBJECT, probe, (canonical_sha256(probe),))
