from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from recording_fixtures import synthetic_catalog
from test_recording_takes import DEVICE, Rig, publish_catalog

from cole_voice_lab.canonical import canonical_json_bytes, sha256_file
from cole_voice_lab.catalog_ports import CorpusSnapshotSecurityError
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import accept_capture_session, validate_capture_package
from cole_voice_lab.models import PROMPT_SUITE_V2, Prompt, PromptSuite, utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_analysis import (
    RecordingAnalysisQueue,
    analysis_build_identity,
    inspect_analysis_job,
)
from cole_voice_lab.recording_contracts import (
    CatalogSnapshot,
    DecisionCommand,
    PreparePackageCommand,
)
from cole_voice_lab.recording_edits import assert_recording_not_removed
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_packages import (
    RecordingPackageError,
    RecordingPackageStore,
    load_recording_package_map,
    partition_recording_package_selection,
    resolve_native_package_alias,
)
from cole_voice_lab.recording_takes import RecordingTakeStore
from cole_voice_lab.references import load_accepted_utterance


@pytest.fixture(scope="session")
def analyzer_build() -> Any:
    return analysis_build_identity()


@pytest.fixture
def package_rig(
    tmp_path: Path, analyzer_build: Any
) -> Iterator[tuple[Rig, RecordingAnalysisQueue, RecordingPackageStore]]:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    rig = Rig(layout, RecordingTakeStore(layout), "", now=utc_now() - timedelta(seconds=20))
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    group = rig.store.open_group(
        synthetic_catalog(3),
        "synthetic-subject",
        DEVICE.model_copy(update={"display_name": "Synthetic UMIK-1"}),
        "synthetic-host",
    )
    rig.group_id = group.group_id
    index = RecordingIndex(layout, subject_id=group.subject_id)
    index.register_catalog(CatalogSnapshot.embedded(group.catalog))

    def project(take: Any) -> None:
        index.update_take(take)

    rig.store.projection_hook = project
    queue = RecordingAnalysisQueue(rig.store, index, build=analyzer_build)
    packages = RecordingPackageStore(
        layout, take_store=rig.store, index=index, analyzer_build=analyzer_build
    )
    try:
        yield rig, queue, packages
    finally:
        queue.close()
        index.close()


def saved_take(
    rig: Rig, queue: RecordingAnalysisQueue, number: int, *, prompt: str | None = None
) -> Any:
    rig.now = utc_now() - timedelta(seconds=5)
    rig.store.clock = lambda: rig.now
    take = rig.seal(
        rig.reserve(f"reserve-{number}", prompt or f"synthetic-{number:04d}"), f"seal-{number}"
    )
    rig.store.clock = utc_now
    queue.submit(take.reservation.take_id)
    job = queue.claim()
    assert job is not None
    result = inspect_analysis_job(rig.layout, job)
    assert result.result.state == "verified"
    queue.complete(result)
    take = rig.store.get_take(take.reservation.take_id)
    return rig.store.save(rig.command(take, operation=f"save-{number}")).take


def full_selection(
    rig: Rig, queue: RecordingAnalysisQueue
) -> tuple[list[Any], PreparePackageCommand]:
    takes = [saved_take(rig, queue, number) for number in range(3)]
    return takes, PreparePackageCommand(
        operation_id="prepare-full",
        group_id=rig.group_id,
        take_ids=tuple(take.reservation.take_id for take in takes),
        room_tone_take_ids=(takes[0].reservation.take_id,),
    )


def test_background_package_alias_uses_projection_reader_without_shared_lock(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab import recording_packages as module

    rig, queue, packages = package_rig
    _, command = full_selection(rig, queue)
    package = packages.prepare(command)
    reader = RecordingTakeStore(rig.layout, read_only=True)
    entry = package.entries[0]

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("background alias entered the interactive metadata flock")

    monkeypatch.setattr(module, "_read_native_take_snapshot", forbidden)
    monkeypatch.setattr(reader, "_metadata_lock", forbidden)
    resolved = resolve_native_package_alias(
        rig.layout,
        subject_id=entry.subject_id,
        session_id=package.session_id,
        prompt_id=entry.prompt.prompt_id,
        source_sha256=entry.original_sha256,
        take_store=reader,
    )
    assert resolved is not None and resolved.reservation.take_id == entry.take_id
    with pytest.raises(ContractError, match="read-only store"):
        resolve_native_package_alias(
            rig.layout,
            subject_id=entry.subject_id,
            session_id=package.session_id,
            prompt_id=entry.prompt.prompt_id,
            source_sha256=entry.original_sha256,
            take_store=rig.store,
        )


def test_prepare_full_strict_package_preserves_originals_dates_and_identity(
    package_rig: Any,
) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    original = [
        (
            sha256_file(rig.layout.root / take.seal.original_path),
            (rig.layout.root / take.seal.original_path).stat().st_ino,
        )
        for take in takes
    ]
    package_map = packages.prepare(command)
    package_path = rig.layout.root / package_map.package_path
    validated = validate_capture_package(package_path)
    assert validated.manifest_sha256 == package_map.manifest_sha256
    assert package_map.selected_suite == rig.store.get_group(rig.group_id).catalog
    assert (
        load_recording_package_map(rig.layout, package_map.session_id, package=validated)
        == package_map
    )
    for take, entry, identity in zip(takes, package_map.entries, original, strict=True):
        source = rig.layout.root / take.seal.original_path
        exported = package_path / entry.package_relative_path
        assert sha256_file(source) == identity[0] == sha256_file(exported)
        assert source.stat().st_ino == identity[1] != exported.stat().st_ino
        assert source.stat().st_nlink == exported.stat().st_nlink == 1
        assert (
            entry.take_id == take.reservation.take_id
            and entry.recorded_at == take.seal.first_sample_at
        )
        assert entry.frame_range == take.frame_range and entry.edit_revision == 0
        assert (
            resolve_native_package_alias(
                rig.layout,
                subject_id=entry.subject_id,
                session_id=package_map.session_id,
                prompt_id=entry.prompt.prompt_id,
                source_sha256=entry.original_sha256,
            ).reservation.take_id
            == entry.take_id
        )
    assert packages.prepare(command) == package_map
    assert not any("take-map" in path.name for path in package_path.rglob("*"))
    assert tuple(file.path for file in validated.manifest.files) == tuple(
        entry.package_relative_path for entry in package_map.entries
    )


def test_package_plan_stops_loading_takes_at_metadata_budget(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.recording_packages as module

    rig, queue, packages = package_rig
    _, command = full_selection(rig, queue)
    group = rig.store.get_group(rig.group_id)
    baseline_bytes = len(canonical_json_bytes(group)) + len(canonical_json_bytes(command))
    original = rig.store.get_take
    loaded: list[str] = []

    def tracked(take_id: str) -> Any:
        loaded.append(take_id)
        return original(take_id)

    monkeypatch.setattr(rig.store, "get_take", tracked)
    monkeypatch.setattr(module, "_PACKAGE_MAX_BYTES", baseline_bytes - 1)
    with pytest.raises(RecordingPackageError, match="bounded metadata"):
        packages.plan(command)
    assert loaded == []
    monkeypatch.setattr(module, "_PACKAGE_MAX_BYTES", baseline_bytes + 1)
    with pytest.raises(RecordingPackageError, match="bounded metadata"):
        packages.plan(command)
    assert loaded == [command.take_ids[0]]
    assert not packages._plan_path(command.operation_id).exists()


def test_one_off_saves_without_tone_but_explicit_prepare_requires_it(package_rig: Any) -> None:
    rig, queue, packages = package_rig
    utterance = saved_take(rig, queue, 1)
    assert utterance.decision == "kept"
    wrong = PreparePackageCommand(
        operation_id="prepare-missing-tone",
        group_id=rig.group_id,
        take_ids=(utterance.reservation.take_id,),
        room_tone_take_ids=(utterance.reservation.take_id,),
    )
    with pytest.raises(RecordingPackageError, match="room-tone"):
        packages.plan(wrong)
    tone = saved_take(rig, queue, 0)
    command = PreparePackageCommand(
        operation_id="prepare-one-off",
        group_id=rig.group_id,
        take_ids=(utterance.reservation.take_id, tone.reservation.take_id),
        room_tone_take_ids=(tone.reservation.take_id,),
    )
    package_map = packages.prepare(command)
    assert package_map.selected_suite is not None
    assert len(package_map.selected_suite.prompts) == 2
    assert package_map.selected_suite.purpose == "capture-pilot"
    assert package_map.selected_suite.suite_id.startswith("native-subset-")
    assert (
        validate_capture_package(rig.layout.root / package_map.package_path).qc_report.status
        == "pass"
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "before-plan",
        "after-plan",
        "before-copy-sync",
        "after-copy-sync",
        "before-copy-publish",
        "after-copy-publish",
        "before-finalize",
        "after-ready",
        "before-operation",
        "after-operation",
        "before-map",
        "after-map",
        "before-directory-publish",
        "after-directory-publish",
        "before-alias-projection",
        "after-alias-projection",
    ],
)
def test_interruption_replay_publishes_one_package_ready_last(
    package_rig: Any, boundary: str
) -> None:
    rig, queue, packages = package_rig
    _, command = full_selection(rig, queue)
    fired = False

    def fault(actual: str) -> None:
        nonlocal fired
        if actual == boundary and not fired:
            fired = True
            raise OSError("synthetic interrupted preparation")

    packages.fault_hook = fault
    with pytest.raises(OSError):
        packages.prepare(command)
    packages.fault_hook = None
    package_map = packages.prepare(command)
    assert (
        validate_capture_package(rig.layout.root / package_map.package_path).manifest_sha256
        == package_map.manifest_sha256
    )
    assert packages.prepare(command) == package_map
    assert len(tuple(packages._path("operations").glob("*.json"))) == 1


@pytest.mark.parametrize(
    "kind", ["bytes", "replace", "public", "link", "undeclared", "changed-decision"]
)
def test_tamper_or_changed_selection_never_publishes_a_package(package_rig: Any, kind: str) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    plan = packages.plan(command)
    original = rig.layout.root / takes[1].seal.original_path
    old = original.stat()
    if kind in {"bytes", "replace"}:
        content = bytearray(original.read_bytes())
        content[60] ^= 127
        if kind == "replace":
            original.unlink()
        original.write_bytes(content)
        original.chmod(0o600)
        os.utime(original, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif kind == "public":
        original.chmod(0o644)
    elif kind == "link":
        os.link(original, original.with_suffix(".linked"))
    elif kind == "undeclared":
        stage = packages.takes._directory(f"tmp/.native-package-stage-{plan.session_id}")
        (stage / "undeclared.txt").write_text("synthetic", encoding="utf-8")
    else:

        def change(actual: str) -> None:
            if actual == "after-ready":
                take = rig.store.get_take(takes[1].reservation.take_id)
                changed = take.model_copy(
                    update={"decision": "removed", "state_revision": take.state_revision + 1}
                )
                rig.store._write(
                    rig.store._take_path(take.reservation.take_id, "head.json"), changed
                )

        packages.fault_hook = change
    with pytest.raises((ContractError, SecurityError)):
        packages.prepare(command)
    assert not (rig.layout.root / f"tmp/capture-{plan.session_id}").exists()
    assert not packages._session_path(plan.session_id).exists()


def test_local_map_required_but_transferred_v1_remains_session_only(
    package_rig: Any, tmp_path: Path
) -> None:
    rig, queue, packages = package_rig
    _, command = full_selection(rig, queue)
    package_map = packages.prepare(command)
    validated = validate_capture_package(rig.layout.root / package_map.package_path)
    path = packages._session_path(package_map.session_id)
    contents = path.read_bytes()
    path.unlink()
    with pytest.raises(SecurityError, match="missing"):
        load_recording_package_map(rig.layout, package_map.session_id, package=validated)
    path.write_bytes(contents)
    path.chmod(0o600)
    foreign = AssetLayout.initialize(tmp_path.resolve() / "foreign-assets")
    assert load_recording_package_map(foreign, package_map.session_id, package=validated) is None


def test_idempotent_strict_intake_and_alias_removal_gate(package_rig: Any) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    package_map = packages.prepare(command)
    stage = rig.layout.root / package_map.package_path
    incoming = rig.layout.directory("incoming") / package_map.session_id
    shutil.copytree(stage, incoming)
    receipt = accept_capture_session(rig.layout, incoming, min_free_bytes=0)
    assert (rig.layout.root / receipt.raw_path).stat().st_mode & 0o222 == 0
    shutil.copytree(stage, incoming)
    assert accept_capture_session(rig.layout, incoming, min_free_bytes=0) == receipt
    loaded = load_accepted_utterance(
        rig.layout, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="synthetic-0001"
    )
    assert loaded.evidence.sha256 == takes[1].seal.original_sha256

    take = rig.store.get_take(takes[1].reservation.take_id)
    removed = rig.store.decide(
        DecisionCommand(
            operation_id="remove-aliased",
            selection=rig.command(take).selection,
            take_id=take.reservation.take_id,
            expected_decision_revision=take.decision_revision,
            action="remove",
            reason="Synthetic exclusion",
        )
    )
    assert removed.take.decision == "removed"
    with pytest.raises(ContractError, match="not kept"):
        load_accepted_utterance(
            rig.layout, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="synthetic-0001"
        )
    assert (
        sha256_file(stage / package_map.entries[1].package_relative_path)[0]
        == take.seal.original_sha256
    )

    restored = rig.store.decide(
        DecisionCommand(
            operation_id="restore-aliased",
            selection=rig.command(removed.take).selection,
            take_id=take.reservation.take_id,
            expected_decision_revision=removed.take.decision_revision,
            action="restore",
            reason=None,
        )
    )
    assert restored.take.decision == "kept"
    assert (
        load_accepted_utterance(
            rig.layout, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="synthetic-0001"
        ).evidence.sha256
        == take.seal.original_sha256
    )


def test_native_package_aliases_do_not_duplicate_history_after_index_rebuild(
    package_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.recording_index_backfill import backfill_recording_library
    from cole_voice_lab.recording_library import RecordingLibrary

    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    package = packages.prepare(command)
    incoming = rig.layout.directory("incoming") / package.session_id
    shutil.copytree(rig.layout.root / package.package_path, incoming)
    accept_capture_session(rig.layout, incoming, min_free_bytes=0)
    group = rig.store.get_group(rig.group_id)
    library = RecordingLibrary(rig.layout, group.catalog, group.subject_id)
    index = packages.index
    assert index is not None
    index.reconcile_takes(rig.store.iter_takes())
    backfill_recording_library(library, index)
    page = index.history(group.catalog_sha256)
    assert (
        page.total_take_count == 3
        and page.total_group_count == 1
        and page.covered_prompt_count == 3
    )
    assert {row.take_id for row in page.takes} == {take.reservation.take_id for take in takes}
    assert all(row.date_precision == "take" for row in page.takes)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("warm indexed history reopened historical WAVs")

    monkeypatch.setattr("cole_voice_lab.recording_library.inspect_canonical_wav", forbidden)
    monkeypatch.setattr("cole_voice_lab.recording_index_backfill.inspect_canonical_wav", forbidden)
    assert index.history(group.catalog_sha256).total_take_count == 3


def test_foreign_native_v1_reports_session_date_and_unknown_exact_catalog(
    package_rig: Any, tmp_path: Path
) -> None:
    from cole_voice_lab.recording_index_backfill import backfill_recording_library
    from cole_voice_lab.recording_library import RecordingLibrary

    rig, queue, packages = package_rig
    _, command = full_selection(rig, queue)
    package = packages.prepare(command)
    foreign = AssetLayout.initialize(tmp_path.resolve() / "foreign")
    copied = foreign.directory("tmp") / f"capture-{package.session_id}"
    shutil.copytree(rig.layout.root / package.package_path, copied)
    group = rig.store.get_group(rig.group_id)
    library = RecordingLibrary(foreign, group.catalog, group.subject_id)
    history, sessions, _ = library.verified_history_snapshot()
    assert history == () and len(sessions) == 1
    assert sessions[0].session_started_at is not None and not sessions[0].catalog_known
    with RecordingIndex(foreign, subject_id=group.subject_id) as index:
        backfill_recording_library(library, index)
        page = index.history(group.catalog_sha256)
        assert page.completeness == "unknown" and page.total_take_count == 0
        assert page.groups[0].date_precision == "session"


def test_deleted_package_registry_still_requires_local_origin_map(package_rig: Any) -> None:
    rig, queue, packages = package_rig
    _, command = full_selection(rig, queue)
    package = packages.prepare(command)
    shutil.rmtree(packages.directory)
    with pytest.raises(SecurityError, match="missing"):
        load_recording_package_map(rig.layout, package.session_id)


def test_trimmed_take_exports_original_pcm_and_current_recipe_identity(package_rig: Any) -> None:
    from cole_voice_lab.recording_contracts import EditTakeCommand, RecordingFrameRange
    from cole_voice_lab.recording_edits import native_edit_identity

    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    original = rig.layout.root / takes[1].seal.original_path
    before = original.read_bytes()
    edited = rig.store.edit(
        EditTakeCommand(
            operation_id="trim-before-package",
            selection=rig.command(takes[1]).selection,
            take_id=takes[1].reservation.take_id,
            source_sha256=takes[1].seal.original_sha256,
            expected_edit_revision=0,
            frame_range=RecordingFrameRange(source_frames=480, start_frame=20, end_frame=400),
        )
    ).take
    queue.submit(edited.reservation.take_id)
    job = queue.claim()
    assert job is not None
    queue.complete(inspect_analysis_job(rig.layout, job))
    package = packages.prepare(command)
    entry = package.entries[1]
    assert entry.edit_revision == 1 and entry.frame_range == edited.frame_range
    assert entry.edit_identity_sha256 == native_edit_identity(rig.layout, entry.take_id)
    assert (
        rig.layout.root / package.package_path / entry.package_relative_path
    ).read_bytes() == before
    assert original.read_bytes() == before


def test_exact_bundled_pilot_prepares_unchanged_full_suite(package_rig: Any) -> None:
    from cole_voice_lab.resources import load_prompt_suite

    rig, queue, packages = package_rig
    suite = load_prompt_suite("pilot")
    group = rig.store.open_group(
        suite,
        "synthetic-subject",
        DEVICE.model_copy(update={"display_name": "Synthetic UMIK-1"}),
        "synthetic-host",
    )
    rig.group_id = group.group_id
    takes = [
        saved_take(rig, queue, number, prompt=prompt.prompt_id)
        for number, prompt in enumerate(suite.prompts)
    ]
    command = PreparePackageCommand(
        operation_id="bundled-full-pilot",
        group_id=group.group_id,
        take_ids=tuple(t.reservation.take_id for t in takes),
        room_tone_take_ids=tuple(
            t.reservation.take_id for t in takes if t.reservation.prompt.category == "room-tone"
        ),
    )
    package = packages.prepare(command)
    assert package.selected_suite == suite
    assert (
        validate_capture_package(rig.layout.root / package.package_path).manifest.prompt_suite_id
        == suite.suite_id
    )


@pytest.mark.parametrize("state", ["draft", "pending", "missing-analysis", "new-build"])
def test_prepare_requires_current_independent_verification_and_keep(
    package_rig: Any, state: str
) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    take = takes[1]
    if state == "new-build":
        packages.analyzer_build = packages.analyzer_build.model_copy(
            update={"build_id": "new-analyzer"}
        )
    elif state == "missing-analysis":
        job = queue.page()[1]
        rig.store._analysis_path(job.ticket.job_sha256).unlink()
    else:
        current = take.model_dump()
        if state == "draft":
            current.update(decision="draft", decision_revision=0)
        else:
            current.update(analysis="pending")
        rig.store._write(
            rig.store._take_path(take.reservation.take_id, "head.json"),
            type(take).model_validate(current),
        )
    with pytest.raises(ContractError, match="verification|verified"):
        packages.plan(command)


def test_package_selection_explicit_tones_retakes_and_context_partition(package_rig: Any) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    tone = takes[0]
    with pytest.raises(ContractError, match="utterance"):
        packages.plan(command.model_copy(update={"take_ids": (tone.reservation.take_id,)}))
    retake = saved_take(rig, queue, 8, prompt="synthetic-0001")
    with pytest.raises(ContractError, match="one take"):
        packages.plan(
            command.model_copy(update={"take_ids": (*command.take_ids, retake.reservation.take_id)})
        )
    packages.plan(command)
    with pytest.raises(ContractError, match="operation ID"):
        packages.plan(command.model_copy(update={"take_ids": command.take_ids[:2]}))
    group = rig.store.get_group(rig.group_id)
    rig.store.clock = lambda: utc_now() - timedelta(seconds=10)
    other = rig.store.open_group(
        group.catalog, group.subject_id, group.capture_device, "another-host"
    )
    rig.group_id = other.group_id
    second = saved_take(rig, queue, 9, prompt="synthetic-0001")
    assert len(partition_recording_package_selection((tone, second))) == 2
    mixed = command.model_copy(
        update={
            "operation_id": "mixed-context",
            "take_ids": (tone.reservation.take_id, second.reservation.take_id),
        }
    )
    with pytest.raises(ContractError, match="split"):
        packages.plan(mixed)


@pytest.mark.parametrize("wrong", ["device", "held-out"])
def test_strict_package_prerequisites_preserve_one_off_save(package_rig: Any, wrong: str) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    current = rig.store.get_group(rig.group_id)
    if wrong == "device":
        group = rig.store.open_group(current.catalog, current.subject_id, DEVICE, "other-host")
    else:
        suite = current.catalog.model_copy(
            update={
                "purpose": "held-out-evaluation",
                "prompts": tuple(
                    prompt.model_copy(update={"holdout": True})
                    for prompt in current.catalog.prompts
                ),
            }
        )
        group = rig.store.open_group(
            suite, current.subject_id, current.capture_device, current.capture_host
        )
    with pytest.raises(ContractError, match="UMIK|held-out"):
        packages.plan(command.model_copy(update={"group_id": group.group_id}))
    assert all(rig.store.get_take(take.reservation.take_id).decision == "kept" for take in takes)


def test_pending_committed_remove_blocks_all_raw_alias_use_without_repair(package_rig: Any) -> None:
    from cole_voice_lab.datasets import DatasetSelection, build_dataset_manifest

    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    package = packages.prepare(command)
    incoming = rig.layout.directory("incoming") / package.session_id
    shutil.copytree(rig.layout.root / package.package_path, incoming)
    receipt = accept_capture_session(rig.layout, incoming, min_free_bytes=0)
    take = rig.store.get_take(takes[1].reservation.take_id)

    def fail(point: str) -> None:
        if point == "before-take-head":
            raise OSError("synthetic pending Remove")

    rig.store.fault_hook = fail
    with pytest.raises(OSError):
        rig.store.decide(
            DecisionCommand(
                operation_id="pending-remove",
                selection=rig.command(take).selection,
                take_id=take.reservation.take_id,
                expected_decision_revision=take.decision_revision,
                action="remove",
                reason="Synthetic exclusion",
            )
        )
    pending = rig.store._path("provenance/recording-takes/.pending.json")
    assert pending.exists()
    with pytest.raises(ContractError, match="not kept"):
        load_accepted_utterance(
            rig.layout,
            raw_manifest_sha256=receipt.manifest_sha256,
            prompt_id=take.reservation.prompt.prompt_id,
        )
    # The dataset builder reaches that same gate before any approval/review processing.
    with pytest.raises(ContractError, match="not kept"):
        build_dataset_manifest(
            rig.layout,
            dataset_id="excluded-dataset",
            corpus_stage="pilot",
            selections=(
                DatasetSelection(
                    raw_manifest_sha256=receipt.manifest_sha256,
                    prompt_id=take.reservation.prompt.prompt_id,
                    split="train",
                    headphone_review_path="provenance/reviews/synthetic.json",
                    headphone_review_sha256="a" * 64,
                ),
            ),
            prompt_suites={package.selected_suite.suite_id: package.selected_suite},
            consent_path="provenance/consents/synthetic.json",
            consent_sha256="b" * 64,
            created_by="synthetic-test",
        )
    assert pending.exists()  # Read-only eligibility never clears another executor's projection.
    rig.store.fault_hook = None
    assert rig.store.get_take(take.reservation.take_id).decision == "removed"
    assert packages.prepare(command) == package
    assert rig.store.get_take(take.reservation.take_id).decision == "removed"


@pytest.mark.parametrize("changed", ["manifest", "source", "transcript"])
def test_map_validator_binds_strict_v1_manifest_original_and_transcript(
    package_rig: Any, changed: str
) -> None:
    from cole_voice_lab.recording_packages import validate_recording_package_map

    rig, queue, packages = package_rig
    _, command = full_selection(rig, queue)
    package = packages.prepare(command)
    validated = validate_capture_package(rig.layout.root / package.package_path)
    if changed == "manifest":
        altered = package.model_copy(update={"manifest_sha256": "f" * 64})
    else:
        entry = package.entries[1]
        entry = (
            entry.model_copy(update={"original_sha256": "f" * 64})
            if changed == "source"
            else entry.model_copy(
                update={"prompt": entry.prompt.model_copy(update={"text": "Changed exact words"})}
            )
        )
        altered = package.model_copy(
            update={"entries": (package.entries[0], entry, package.entries[2])}
        )
    with pytest.raises(SecurityError):
        validate_recording_package_map(altered, validated)


def test_alias_rejects_wrong_manifest_subject_source_and_take_head(package_rig: Any) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)
    package = packages.prepare(command)
    entry = package.entries[1]
    kwargs = dict(
        subject_id=entry.subject_id,
        session_id=package.session_id,
        prompt_id=entry.prompt.prompt_id,
        source_sha256=entry.original_sha256,
        manifest_sha256=package.manifest_sha256,
    )
    for field, value in (
        ("manifest_sha256", "f" * 64),
        ("source_sha256", "f" * 64),
        ("subject_id", "another-subject"),
    ):
        with pytest.raises(SecurityError):
            resolve_native_package_alias(rig.layout, **(kwargs | {field: value}))
    rig.store._take_path(takes[1].reservation.take_id, "head.json").unlink()
    with pytest.raises(SecurityError, match="identity"):
        resolve_native_package_alias(rig.layout, **kwargs)


@pytest.mark.parametrize("directory", [False, True])
def test_atomic_package_publication_preserves_a_conflicting_destination(
    tmp_path: Path, directory: bool
) -> None:
    from cole_voice_lab.recording_packages import _publish_without_replace

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    if directory:
        source.mkdir()
        destination.mkdir()
        (source / "original.txt").write_text("new synthetic")
        (destination / "existing.txt").write_text("preserved synthetic")
    else:
        source.write_text("new synthetic")
        destination.write_text("preserved synthetic")
    with pytest.raises(FileExistsError):
        _publish_without_replace(source, destination)
    if directory:
        assert (destination / "existing.txt").read_text() == "preserved synthetic"
        assert (source / "original.txt").read_text() == "new synthetic"
    else:
        assert (
            destination.read_text() == "preserved synthetic"
            and source.read_text() == "new synthetic"
        )


def test_package_publication_keeps_its_own_refusal_on_an_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import cole_voice_lab.canonical as canonical
    from cole_voice_lab.recording_packages import RecordingPackageError, _publish_without_replace

    source = tmp_path / "source"
    source.write_text("new synthetic")
    monkeypatch.setattr(canonical, "_system_platform", lambda: "plan9")
    with pytest.raises(RecordingPackageError, match="unavailable on this OS"):
        _publish_without_replace(source, tmp_path / "destination")
    assert source.read_text() == "new synthetic"


def test_package_publication_reports_a_refused_rename_flag_as_an_os_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ctypes
    import errno

    import cole_voice_lab.canonical as canonical
    from cole_voice_lab.recording_packages import RecordingPackageError, _publish_without_replace

    def refused(*_arguments: object) -> int:
        ctypes.set_errno(errno.EINVAL)
        return -1

    library = type("Library", (), {})()
    library.renameat2 = refused
    monkeypatch.setattr(canonical, "_system_platform", lambda: "linux")
    monkeypatch.setattr(canonical, "_c_library", lambda: library)
    with pytest.raises(OSError) as caught:
        _publish_without_replace(tmp_path / "source", tmp_path / "destination")
    assert caught.value.errno == errno.EINVAL
    assert not isinstance(caught.value, RecordingPackageError)


def test_remove_after_package_registration_before_projection_stays_removed(
    package_rig: Any,
) -> None:
    rig, queue, packages = package_rig
    takes, command = full_selection(rig, queue)

    def fail(point: str) -> None:
        if point == "before-alias-projection":
            raise OSError("synthetic projection interruption")

    packages.fault_hook = fail
    with pytest.raises(OSError):
        packages.prepare(command)
    packages.fault_hook = None
    take = rig.store.get_take(takes[1].reservation.take_id)
    removed = rig.store.decide(
        DecisionCommand(
            operation_id="remove-before-export-projection",
            selection=rig.command(take).selection,
            take_id=take.reservation.take_id,
            expected_decision_revision=take.decision_revision,
            action="remove",
            reason="Synthetic exclusion",
        )
    ).take
    package = packages.prepare(command)
    assert rig.store.get_take(take.reservation.take_id) == removed
    with pytest.raises(ContractError, match="not kept"):
        assert_recording_not_removed(
            rig.layout,
            subject_id=package.group.subject_id,
            session_id=package.session_id,
            prompt_id=take.reservation.prompt.prompt_id,
            source_sha256=take.seal.original_sha256,
            manifest_sha256=package.manifest_sha256,
        )


def test_a_referenced_group_packages_its_resolved_subset_and_never_its_group(
    package_rig: Any,
) -> None:
    from cole_voice_lab.recording_packages import validate_recording_package_map

    rig, queue, packages = package_rig
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="synthetic-seed",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="synthetic-0000",
                category="room-tone",
                style="Silence",
                instructions="Stay silent.",
                expected_seconds=10.0,
            ),
            *(
                Prompt(
                    prompt_id=f"synthetic-{number:04d}",
                    text=f"Synthetic sentence {number}.",
                    category="neutral",
                    style="Plain reading, natural pace.",
                    expected_seconds=2.0,
                )
                for number in (1, 2)
            ),
        ),
    )
    reference, snapshot = publish_catalog(rig.layout, suite)
    group = rig.store.open_group(
        reference=reference,
        suite=snapshot.suite,
        subject_id="synthetic-subject",
        capture_device=DEVICE.model_copy(update={"display_name": "Synthetic UMIK-1"}),
        capture_host="synthetic-host",
    )
    rig.group_id = group.group_id
    takes = [saved_take(rig, queue, number) for number in (0, 1)]
    command = PreparePackageCommand(
        operation_id="prepare-referenced",
        group_id=group.group_id,
        take_ids=tuple(take.reservation.take_id for take in takes),
        room_tone_take_ids=(takes[0].reservation.take_id,),
    )
    package = packages.prepare(command)
    assert package.group.catalog is None and package.group.catalog_reference == reference
    selected = package.selected_suite
    assert selected is not None and selected.suite_id.startswith("native-subset-")
    assert selected.schema_version == PROMPT_SUITE_V2 and selected.purpose == "capture-pilot"
    assert selected.prompts == snapshot.suite.prompts[:2]
    assert b'"catalog":' not in canonical_json_bytes(package)
    validated = validate_capture_package(rig.layout.root / package.package_path)
    validate_recording_package_map(package, validated)
    # The map contract refuses a v2 map without its suite; the validator never falls back.
    forged = package.model_copy(update={"selected_suite": None})
    with pytest.raises(RecordingPackageError, match="requires its selected suite") as caught:
        validate_recording_package_map(forged, validated)
    assert caught.value.recording_error.code == "RECORDING.PACKAGE_INCOMPLETE"
    # A later plan resolves the snapshot again and fails closed without it.
    (rig.layout.root / reference.snapshot_path).unlink()
    fresh = RecordingPackageStore(
        rig.layout,
        take_store=RecordingTakeStore(rig.layout),
        analyzer_build=packages.analyzer_build,
    )
    with pytest.raises(CorpusSnapshotSecurityError, match="missing"):
        fresh.plan(command.model_copy(update={"operation_id": "prepare-again"}))
