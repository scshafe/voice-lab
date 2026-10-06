"""TP1: physical-test evidence never leaves a marked diagnostic root (design §5.2-§5.3).

Every catalog here is synthetic; no built-in physical-test resource exists or is read.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import macos_only
import pytest
from recording_fixtures import synthetic_catalog
from test_recording_packages import saved_take
from test_recording_service import activate_catalog, request
from test_recording_takes import DEVICE, Rig, allow_mixed_purpose_store, store_suite

import cole_voice_lab.native_recording_application as application
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
from cole_voice_lab.capture import TakeEngine, capture_prompt_suite
from cole_voice_lab.cli import main
from cole_voice_lab.consent import approve_self_consent, consent_record_path
from cole_voice_lab.datasets import (
    DatasetSelection,
    PilotGateApproval,
    build_dataset_manifest,
    load_pilot_gate,
)
from cole_voice_lab.desktop import run_desktop
from cole_voice_lab.desktop_application import create_desktop_application
from cole_voice_lab.diagnostic_roots import (
    DIAGNOSTIC_ROOT_MARKER,
    DiagnosticRootMarker,
    initialize_diagnostic_root,
    root_purpose,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import accept_capture_session
from cole_voice_lab.models import Prompt, PromptSuite, PromptSuitePurpose, utc_now
from cole_voice_lab.native_recording_application import (
    NativeRecordingApplicationConfiguration,
    NativeRecordingApplicationRelease,
    NativeRecordingApplicationSettings,
    NativeRecordingPeerConfiguration,
    create_native_recording_application,
    verify_native_recording_application,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_analysis import RecordingAnalysisQueue, analysis_build_identity
from cole_voice_lab.recording_contracts import (
    CatalogCommand,
    CatalogReference,
    CatalogSnapshot,
    ContextEvent,
    ContinueLegacyRecordingCommand,
    OpenContextCommand,
    PreparePackageCommand,
    RecordingPackageMap,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_packages import RecordingPackageError, RecordingPackageStore
from cole_voice_lab.recording_service import (
    RecordingCancellation,
    RecordingServiceError,
    RecordingWorkspaceBackend,
)
from cole_voice_lab.recording_takes import RecordingTakeStore
from cole_voice_lab.references import load_accepted_utterance
from cole_voice_lab.storage_binding import open_catalog_editor

SUBJECT = "cole-physical-diagnostic-synthetic"
OTHER_SUBJECT = "cole-physical-diagnostic-other"
UMIK = DEVICE.model_copy(update={"display_name": "Synthetic UMIK-1"})


def physical_catalog() -> PromptSuite:
    """Room tone, one spoken step and one procedure step, all labelled `test`."""

    def prompt(prompt_id: str, category: str, **content: str) -> Prompt:
        return Prompt.model_validate(
            {
                "prompt_id": prompt_id,
                "category": category,
                "style": "Synthetic physical-test step.",
                "labels": ("test",),
                "expected_seconds": 10.0,
                **content,
            }
        )

    return PromptSuite(
        schema_version="cole-voice-lab.prompt-suite/v2",
        suite_id="cole-physical-test-synthetic",
        purpose="physical-test",
        prompts=(
            prompt("synthetic-0000", "room-tone", instructions="Remain still and silent."),
            prompt("synthetic-0001", "neutral", text="A synthetic physical-test sentence."),
            prompt("synthetic-0002", "procedure", instructions="Unplug the synthetic device."),
        ),
    )


def marked_root(tmp_path: Path, name: str = "marked", subject: str = SUBJECT) -> AssetLayout:
    layout, _ = initialize_diagnostic_root(tmp_path.resolve() / name / "assets", subject_id=subject)
    return layout


def standard_root(tmp_path: Path, name: str = "standard") -> AssetLayout:
    return AssetLayout.initialize(tmp_path.resolve() / name / "assets")


def context_command(
    catalog: PromptSuite, subject: str, operation: str = "open"
) -> OpenContextCommand:
    """An embedded open-context (the legacy-continuation form)."""
    return OpenContextCommand(
        operation_id=operation,
        subject_id=subject,
        catalog=catalog,
        catalog_sha256=canonical_sha256(catalog),
        capture_device=UMIK,
        capture_host="synthetic-host",
        new_setup=True,
    )


def referenced_command(
    reference: CatalogReference, subject: str, operation: str = "open"
) -> OpenContextCommand:
    """The open-context the app sends: the catalog the service delivered, by reference."""
    return OpenContextCommand(
        operation_id=operation,
        subject_id=subject,
        catalog_reference=reference,
        capture_device=UMIK,
        capture_host="synthetic-host",
        new_setup=True,
    )


def legacy_command(catalog: PromptSuite, subject: str) -> ContinueLegacyRecordingCommand:
    return ContinueLegacyRecordingCommand(
        operation_id="open",
        session_id="legacy-session",
        session_evidence_sha256="1" * 64,
        context=context_command(catalog, subject),
    )


@pytest.fixture(scope="module")
def analyzer_build() -> Any:
    return analysis_build_identity()


@dataclass
class PackageRoot:
    rig: Rig
    queue: RecordingAnalysisQueue
    packages: RecordingPackageStore


@contextmanager
def package_root(
    layout: AssetLayout, catalog: PromptSuite, subject: str, build: Any
) -> Iterator[PackageRoot]:
    rig = Rig(layout, RecordingTakeStore(layout), "", now=utc_now() - timedelta(seconds=20))
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    rig.group_id = rig.store.open_group(catalog, subject, UMIK, "synthetic-host").group_id
    index = RecordingIndex(layout, subject_id=subject)
    index.register_catalog(CatalogSnapshot.embedded(catalog))
    rig.store.projection_hook = index.update_take
    queue = RecordingAnalysisQueue(rig.store, index, build=build)
    packages = RecordingPackageStore(
        layout, take_store=rig.store, index=index, analyzer_build=build
    )
    try:
        yield PackageRoot(rig, queue, packages)
    finally:
        queue.close()
        index.close()


def prepare(root: PackageRoot, numbers: tuple[int, ...], operation: str) -> RecordingPackageMap:
    takes = [saved_take(root.rig, root.queue, number) for number in numbers]
    return root.packages.prepare(
        PreparePackageCommand(
            operation_id=operation,
            group_id=root.rig.group_id,
            take_ids=tuple(take.reservation.take_id for take in takes),
            room_tone_take_ids=(takes[0].reservation.take_id,),
        )
    )


def deliver(source: AssetLayout, package: RecordingPackageMap, destination: AssetLayout) -> Path:
    incoming = destination.directory("incoming") / package.session_id
    shutil.copytree(source.root / package.package_path, incoming)
    return incoming


def pilot_gate_cli(layout: AssetLayout, manifest_sha256: str) -> int:
    return main(
        [
            "dataset",
            "pilot-gate",
            "--asset-root",
            str(layout.root),
            "--gate-id",
            "diagnostic-gate",
            "--pilot-manifest-sha256",
            manifest_sha256,
            "--statement",
            "Synthetic diagnostic evidence must never authorize a corpus.",
            "--approve",
            "--headphone-review-completed",
            "--allow-full-corpus",
        ]
    )


def gate_file(layout: AssetLayout, manifest_sha256: str) -> tuple[str, str]:
    record = PilotGateApproval(
        gate_id="written-gate",
        pilot_manifest_sha256s=(manifest_sha256,),
        approved=True,
        headphone_review_completed=True,
        full_corpus_capture_allowed=True,
        approved_at=utc_now() - timedelta(seconds=1),
        approved_by="cole",
        statement="Synthetic gate written directly to prove the loader refusal.",
    )
    relative = "provenance/pilot-gates/written-gate.json"
    return relative, write_canonical_json(layout.root / relative, record)


def no_pilot_gates(layout: AssetLayout) -> bool:
    directory = layout.root / "provenance/pilot-gates"
    return not directory.exists() or not any(directory.iterdir())


def consent_approval(layout: AssetLayout, subject: str) -> None:
    approve_self_consent(
        layout,
        acknowledgement=True,
        purposes=("private-zero-shot-evaluation",),
        execution_boundaries=("capture-host",),
        expires_at=utc_now() + timedelta(days=1),
        subject_id=subject,
    )


def write_marker_variant(layout: AssetLayout, variant: str) -> None:
    marker = layout.root / DIAGNOSTIC_ROOT_MARKER
    valid = DiagnosticRootMarker(purpose="physical-test", subject_id=SUBJECT, created_at=utc_now())
    outside = layout.directory("tmp") / "marker.json"
    if variant == "dangling-symlink":
        marker.symlink_to(layout.directory("tmp") / "absent.json")
    elif variant == "symlink":
        write_canonical_json(outside, valid, mode=0o400)
        marker.symlink_to(outside)
    elif variant == "directory":
        marker.mkdir(mode=0o700)
    elif variant == "group-readable":
        write_canonical_json(marker, valid, mode=0o640)
    elif variant == "hard-link":
        write_canonical_json(outside, valid, mode=0o400)
        os.link(outside, marker)
    elif variant == "non-canonical":
        marker.write_text(json.dumps(valid.model_dump(mode="json"), indent=2) + "\n")
        marker.chmod(0o400)
    elif variant == "parse-failure":
        payload = valid.model_dump(mode="json") | {"subject_id": "cole-self"}
        marker.write_bytes(canonical_json_bytes(payload) + b"\n")
        marker.chmod(0o400)
    elif variant == "foreign-owner":
        write_canonical_json(marker, valid, mode=0o400)
    else:
        raise AssertionError(variant)


@pytest.mark.parametrize(
    "variant",
    [
        "dangling-symlink",
        "symlink",
        "directory",
        "group-readable",
        "hard-link",
        "non-canonical",
        "parse-failure",
        "foreign-owner",
    ],
)
def test_every_unsafe_marker_state_refuses_guarded_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    layout = standard_root(tmp_path)
    assert root_purpose(layout) == "standard"
    reference, _ = activate_catalog(layout, store_suite(2), subject_id="synthetic-subject")
    write_marker_variant(layout, variant)
    if variant == "foreign-owner":
        uid = os.geteuid()
        monkeypatch.setattr("cole_voice_lab.diagnostic_roots.os.geteuid", lambda: uid + 1)
    with pytest.raises(SecurityError):
        root_purpose(layout)
    store = RecordingTakeStore(layout)
    with pytest.raises(SecurityError):
        store.open_group(synthetic_catalog(2), "synthetic-subject", UMIK, "host")
    assert not any((layout.root / "provenance").rglob("group-*.json"))
    with pytest.raises(SecurityError):
        consent_approval(layout, "cole-self")
    assert not consent_record_path(layout, "cole-self-v1").exists()
    backend = RecordingWorkspaceBackend(layout, "synthetic-subject", discover=False)
    try:
        for command in (
            CatalogCommand(operation_id="catalog"),
            referenced_command(reference, "synthetic-subject"),
            legacy_command(synthetic_catalog(2), "synthetic-subject"),
        ):
            with pytest.raises(RecordingServiceError, match="marker is unsafe") as failure:
                backend.execute(request(command), RecordingCancellation())
            assert failure.value.recording_error.code == "RECORDING.GATE_NOT_READY"
    finally:
        backend.index.close()


def test_assets_init_marks_only_new_or_empty_roots_and_installs_no_consent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def init(root: Path, *extra: str) -> int:
        return main(["assets", "init", "--asset-root", str(root), *extra])

    diagnostic = ("--diagnostic", "physical-test", "--subject-id", SUBJECT)
    root = tmp_path.resolve() / "new" / "assets"
    assert init(root, *diagnostic) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostic_root"]["subject_id"] == SUBJECT
    assert root_purpose(AssetLayout.validate(root)) == "physical-test"
    marker = root / DIAGNOSTIC_ROOT_MARKER
    assert marker.stat().st_mode & 0o777 == 0o400
    # The marker is the only provenance entry: no pending consent is installed.
    assert [path.name for path in (root / "provenance").iterdir()] == ["diagnostic-root.json"]
    before = marker.read_bytes()
    assert init(root, *diagnostic) == 2
    assert "not empty" in capsys.readouterr().err
    assert marker.read_bytes() == before

    empty = tmp_path.resolve() / "empty" / "assets"
    AssetLayout.initialize(empty)
    assert init(empty, *diagnostic) == 0
    capsys.readouterr()

    used = tmp_path.resolve() / "used" / "assets"
    assert init(used) == 0
    capsys.readouterr()
    assert init(used, *diagnostic) == 2
    assert "provenance/ is not empty" in capsys.readouterr().err
    raw_only = tmp_path.resolve() / "raw-only" / "assets"
    (raw_only / "raw").mkdir(parents=True)
    (raw_only / "raw" / "leftover").write_text("synthetic")
    assert init(raw_only, *diagnostic) == 2
    capsys.readouterr()
    for path in (used, raw_only):
        assert not (path / DIAGNOSTIC_ROOT_MARKER).exists()

    unprefixed = tmp_path.resolve() / "unprefixed" / "assets"
    assert init(unprefixed, "--diagnostic", "physical-test", "--subject-id", "cole-self") == 2
    assert "cole-physical-diagnostic-" in capsys.readouterr().err
    assert init(unprefixed, "--diagnostic", "physical-test") == 2
    assert init(unprefixed, "--subject-id", SUBJECT) == 2
    assert not unprefixed.exists()


class _ReleaseWorkStarted(Exception):
    pass


def _release(
    tmp_path: Path, layout: AssetLayout, purpose: PromptSuitePurpose, subject: str, name: str
) -> Path:
    from test_recording_contracts import synthetic_contracts

    build = synthetic_contracts()["capture-seal"].recorder_build
    runtime = tmp_path.resolve() / f"{name} Preview.runtime"
    runtime.mkdir(mode=0o700)
    peer = NativeRecordingPeerConfiguration(
        executable=str(runtime / "python/bin/python"),
        arguments=(),
        environment=None,
        build=build,
        build_identity_path=str(runtime / "service-build.json"),
    )
    configuration = NativeRecordingApplicationConfiguration(
        asset_root=str(layout.root),
        schema_directory=str(runtime / "schemas"),
        client_build=build,
        client_build_path=str(runtime / "client-build.json"),
        purpose=purpose,
        subject_id=subject,
        capture_device=UMIK,
        capture_host="synthetic-host",
        pilot_approval_path=None,
        pilot_approval_sha256=None,
        recorder=peer,
        service=peer,
    )
    configuration_path = runtime / "configuration.json"
    configuration_path.write_bytes(canonical_json_bytes(configuration))
    configuration_path.chmod(0o600)
    release = NativeRecordingApplicationRelease(
        application=str(tmp_path.resolve() / f"{name} Preview.app"),
        runtime_directory=str(runtime),
        configuration=str(configuration_path),
        configuration_sha256=sha256_file(configuration_path)[0],
        minimum_macos="13.0",
        service_wheel_sha256="0" * 64,
        client_build=build,
        recorder_build=build,
        service_build=build,
        synthetic_only=True,
    )
    release_path = runtime / "release.json"
    release_path.write_bytes(canonical_json_bytes(release))
    release_path.chmod(0o600)
    return release_path


@macos_only.APPLICATION_PACKAGING
def test_native_builder_and_verify_bind_the_declared_purpose_to_the_root_and_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build names no catalog; its declared purpose must fit the root it records into."""

    def release_work(*args: Any, **kwargs: Any) -> Any:
        raise _ReleaseWorkStarted

    monkeypatch.setattr(application, "_freeze_source", release_work)
    monkeypatch.setattr(application, "_run", release_work)
    marked = marked_root(tmp_path)
    standard = standard_root(tmp_path)
    cases: list[tuple[AssetLayout, PromptSuitePurpose, str, str | None]] = [
        (marked, "physical-test", SUBJECT, None),
        (marked, "capture-pilot", SUBJECT, "marked for physical tests"),
        (marked, "capture-corpus", SUBJECT, "marked for physical tests"),
        (marked, "held-out-evaluation", SUBJECT, "marked for physical tests"),
        (marked, "physical-test", OTHER_SUBJECT, "another subject"),
        (standard, "physical-test", SUBJECT, "physical-test catalogs require"),
        # The unmarked one-off pattern: a diagnostic subject on a standard purpose and root.
        (standard, "capture-pilot", SUBJECT, None),
        (standard, "held-out-evaluation", "cole-self", None),
    ]
    for number, (layout, purpose, subject, refusal) in enumerate(cases):
        settings = NativeRecordingApplicationSettings(
            asset_root=str(layout.root),
            purpose=purpose,
            subject_id=subject,
            capture_device=UMIK,
            capture_host="synthetic-host",
        )
        output = tmp_path.resolve() / f"Case {number} Preview.app"
        build = functools.partial(
            create_native_recording_application,
            output,
            source_root=tmp_path,
            python=Path(sys.executable),
            settings=settings,
        )
        if refusal is None:
            with pytest.raises(_ReleaseWorkStarted):
                build()
        else:
            with pytest.raises(ContractError, match=refusal):
                build()
            assert not output.with_suffix(".runtime").exists()
        release = _release(tmp_path, layout, purpose, subject, f"Verify {number}")
        if refusal is None:
            # Past the purpose guard, verification reaches the missing peer manifests.
            with pytest.raises(SecurityError):
                verify_native_recording_application(release)
        else:
            with pytest.raises(ContractError, match=refusal):
                verify_native_recording_application(release)
    # An unsafe marker refuses the build and the verification before anything else.
    unsafe = standard_root(tmp_path, "unsafe")
    write_marker_variant(unsafe, "group-readable")
    with pytest.raises(SecurityError):
        create_native_recording_application(
            tmp_path.resolve() / "Unsafe Preview.app",
            source_root=tmp_path,
            python=Path(sys.executable),
            settings=NativeRecordingApplicationSettings(
                asset_root=str(unsafe.root),
                purpose="capture-pilot",
                subject_id="cole-self",
                capture_device=UMIK,
                capture_host="synthetic-host",
            ),
        )
    with pytest.raises(SecurityError):
        verify_native_recording_application(
            _release(tmp_path, unsafe, "capture-pilot", "cole-self", "Unsafe")
        )

    settings_path = tmp_path.resolve() / "settings.json"
    physical = NativeRecordingApplicationSettings(
        asset_root=str(marked.root),
        purpose="physical-test",
        subject_id=SUBJECT,
        capture_device=UMIK,
        capture_host="synthetic-host",
    )
    arguments = [
        "--output",
        str(tmp_path.resolve() / "Main Preview.app"),
        "--settings",
        str(settings_path),
        "--source-root",
        str(tmp_path),
    ]
    # A pre-store settings file still embeds its catalog: refused before any release work.
    legacy = physical.model_dump(mode="json", exclude={"purpose"}) | {
        "schema_version": "cole-voice-lab.native-application-settings/v1",
        "catalog": physical_catalog().model_dump(mode="json"),
    }
    settings_path.write_bytes(json.dumps(legacy).encode())
    settings_path.chmod(0o600)
    with pytest.raises(SystemExit) as exit_status:
        application.main(arguments)
    assert exit_status.value.code == 2
    settings_path.unlink()
    settings_path.write_bytes(physical.model_dump_json().encode())
    settings_path.chmod(0o600)
    # The release no longer pins a catalog, so the pin flag is gone.
    with pytest.raises(SystemExit) as exit_status:
        application.main(
            [*arguments, "--expect-catalog-sha256", canonical_sha256(physical_catalog())]
        )
    assert exit_status.value.code == 2
    assert "unrecognized arguments: --expect-catalog-sha256" in capsys.readouterr().err
    assert not (tmp_path.resolve() / "Main Preview.runtime").exists()
    with pytest.raises(_ReleaseWorkStarted):
        application.main(arguments)


def test_open_context_and_group_mints_bind_marked_roots_and_their_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked = marked_root(tmp_path)
    standard = standard_root(tmp_path)
    physical = physical_catalog()
    # The store publishes prompt-suite/v2 catalogs; a legacy continuation embeds a v1 one.
    pilot, legacy_pilot = store_suite(3), synthetic_catalog(3)
    published: dict[tuple[str, str], CatalogReference] = {}

    def activate(layout: AssetLayout, subject: str, catalog: PromptSuite) -> CatalogReference:
        key = (str(layout.root), catalog.suite_id)
        if key in published:
            with open_catalog_editor(layout) as corpus:
                corpus.activate(subject, published[key])
        else:
            # The marked root's store holds a capture and a physical-test catalog.
            with monkeypatch.context() as patch:
                allow_mixed_purpose_store(patch)
                published[key], _ = activate_catalog(
                    layout, catalog, catalog_id=catalog.suite_id, subject_id=subject
                )
        return published[key]

    refused: list[tuple[AssetLayout, str, PromptSuite, PromptSuite, str]] = [
        (marked, SUBJECT, pilot, legacy_pilot, "marked for physical tests"),
        (marked, OTHER_SUBJECT, physical, physical, "another subject"),
        (standard, SUBJECT, physical, physical, "physical-test catalogs require"),
    ]
    for layout, subject, catalog, legacy, refusal in refused:
        reference = activate(layout, subject, catalog)
        backend = RecordingWorkspaceBackend(layout, subject, discover=False)
        try:
            with monkeypatch.context() as patch:

                def forbidden(*args: Any, **kwargs: Any) -> Any:
                    raise AssertionError("root refusal must precede replay, gates and groups")

                for name in ("context_replay", "prepare_corpus_gate", "open_context", "open_group"):
                    patch.setattr(backend.store, name, forbidden)
                patch.setattr(backend.index, "register_catalog", forbidden)
                commands: list[Any] = [
                    CatalogCommand(operation_id="catalog"),
                    referenced_command(reference, subject),
                    legacy_command(legacy, subject),
                ]
                for command in commands:
                    with pytest.raises(RecordingServiceError, match=refusal) as failure:
                        backend.execute(request(command), RecordingCancellation())
                    assert failure.value.recording_error.code == "RECORDING.GATE_NOT_READY"
            for embedded in (catalog, legacy):
                with pytest.raises(ContractError, match=refusal):
                    backend.store.open_group(
                        embedded, subject, UMIK, "synthetic-host", new_setup=True
                    )
            assert not any((layout.root / "provenance").rglob("group-*.json"))
        finally:
            backend.index.close()

    opened: list[tuple[AssetLayout, PromptSuite]] = [(marked, physical), (standard, pilot)]
    for layout, catalog in opened:
        reference = activate(layout, SUBJECT, catalog)
        backend = RecordingWorkspaceBackend(layout, SUBJECT, discover=False)
        try:
            event = backend.execute(
                request(referenced_command(reference, SUBJECT)), RecordingCancellation()
            )
            assert isinstance(event, ContextEvent)
            assert event.group.catalog_reference == reference and event.group.catalog is None
            assert event.group.subject_id == SUBJECT
        finally:
            backend.index.close()


def test_physical_test_packages_stay_inside_their_marked_root(
    tmp_path: Path, analyzer_build: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    marked = marked_root(tmp_path)
    standard = standard_root(tmp_path)
    other = marked_root(tmp_path, "other", OTHER_SUBJECT)
    with package_root(marked, physical_catalog(), SUBJECT, analyzer_build) as root:
        tone, spoken, step = (saved_take(root.rig, root.queue, number) for number in range(3))
        with pytest.raises(RecordingPackageError, match="Test steps") as failure:
            root.packages.plan(
                PreparePackageCommand(
                    operation_id="with-procedure",
                    group_id=root.rig.group_id,
                    take_ids=tuple(take.reservation.take_id for take in (tone, spoken, step)),
                    room_tone_take_ids=(tone.reservation.take_id,),
                )
            )
        assert failure.value.recording_error.code == "RECORDING.PACKAGE_INCOMPLETE"
        package = root.packages.prepare(
            PreparePackageCommand(
                operation_id="diagnostic-package",
                group_id=root.rig.group_id,
                take_ids=(tone.reservation.take_id, spoken.reservation.take_id),
                room_tone_take_ids=(tone.reservation.take_id,),
            )
        )
        assert package.selected_suite.suite_id.startswith("cole-physical-test-subset-")
        assert package.selected_suite.purpose == "physical-test"

        # Intake: a standard root and another subject's marked root refuse before any raw write.
        for layout, refusal in (
            (standard, "accepted only by their marked diagnostic root"),
            (other, "another subject"),
        ):
            incoming = deliver(marked, package, layout)
            with pytest.raises(ContractError, match=refusal):
                accept_capture_session(layout, incoming, min_free_bytes=0)
            assert incoming.is_dir() and not any(layout.directory("raw").iterdir())
        receipt = accept_capture_session(marked, deliver(marked, package, marked), min_free_bytes=0)
        assert receipt.manifest_sha256 == package.manifest_sha256

        # Corpus readers refuse the marked root itself, even for its own accepted package.
        with pytest.raises(ContractError, match="physical-test asset roots"):
            load_accepted_utterance(
                marked, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="synthetic-0001"
            )
        assert pilot_gate_cli(marked, receipt.manifest_sha256) == 2
        assert "physical-test asset roots" in capsys.readouterr().err
        assert no_pilot_gates(marked)
        relative, digest = gate_file(marked, receipt.manifest_sha256)
        with pytest.raises(ContractError, match="physical-test asset roots"):
            load_pilot_gate(marked, relative_path=relative, expected_sha256=digest)

        # A package that bypassed intake is still refused in a standard root by its suite ID.
        shutil.copytree(marked.root / receipt.raw_path, standard.root / receipt.raw_path)
        with pytest.raises(ContractError, match="physical-test capture packages"):
            load_accepted_utterance(
                standard, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="synthetic-0001"
            )
        assert pilot_gate_cli(standard, receipt.manifest_sha256) == 2
        assert "physical-test capture packages" in capsys.readouterr().err
        assert no_pilot_gates(standard)

        # Guard #4: a group whose purpose no longer matches its root is refused at plan.
        marker = marked.root / DIAGNOSTIC_ROOT_MARKER
        marker.unlink()
        with pytest.raises(RecordingPackageError, match="physical-test catalogs") as failure:
            root.packages.plan(
                PreparePackageCommand(
                    operation_id="root-mismatch",
                    group_id=root.rig.group_id,
                    take_ids=(tone.reservation.take_id, spoken.reservation.take_id),
                    room_tone_take_ids=(tone.reservation.take_id,),
                )
            )
        assert failure.value.recording_error.code == "RECORDING.PACKAGE_INCOMPLETE"


def test_unmarked_one_off_pattern_opens_but_never_supplies_corpus_evidence(
    tmp_path: Path, analyzer_build: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    standard = standard_root(tmp_path)
    catalog = synthetic_catalog(3)
    with package_root(standard, catalog, SUBJECT, analyzer_build) as root:
        package = prepare(root, (0, 1, 2), "one-off-package")
        assert package.selected_suite == catalog
        receipt = accept_capture_session(
            standard, deliver(standard, package, standard), min_free_bytes=0
        )
        with pytest.raises(ContractError, match="diagnostic subjects"):
            load_accepted_utterance(
                standard, raw_manifest_sha256=receipt.manifest_sha256, prompt_id="synthetic-0001"
            )
        with pytest.raises(ContractError, match="diagnostic subjects"):
            build_dataset_manifest(
                standard,
                dataset_id="diagnostic-dataset",
                corpus_stage="pilot",
                selections=(
                    DatasetSelection(
                        raw_manifest_sha256=receipt.manifest_sha256,
                        prompt_id="synthetic-0001",
                        split="train",
                        headphone_review_path="provenance/reviews/synthetic.json",
                        headphone_review_sha256="a" * 64,
                    ),
                ),
                prompt_suites={catalog.suite_id: catalog},
                consent_path="provenance/consents/synthetic.json",
                consent_sha256="b" * 64,
                created_by="synthetic-test",
            )
        review = [
            "reference",
            "review",
            "--asset-root",
            str(standard.root),
            "--review-id",
            "diagnostic-review",
            "--raw-manifest-sha256",
            receipt.manifest_sha256,
            "--prompt-id",
            "synthetic-0001",
            "--headphones-used",
            "--transcript-matches",
            "--audio-quality-passed",
        ]
        assert main(review) == 2
        assert "diagnostic subjects" in capsys.readouterr().err
        assert not list(standard.root.rglob("diagnostic-review*"))
        assert pilot_gate_cli(standard, receipt.manifest_sha256) == 2
        assert "diagnostic subjects" in capsys.readouterr().err
        assert no_pilot_gates(standard)
        relative, digest = gate_file(standard, receipt.manifest_sha256)
        with pytest.raises(ContractError, match="diagnostic subjects"):
            load_pilot_gate(standard, relative_path=relative, expected_sha256=digest)
        corpus = PromptSuite.model_validate(catalog.model_dump() | {"purpose": "capture-corpus"})
        with pytest.raises(ContractError, match="diagnostic subjects"):
            root.rig.store.prepare_corpus_gate(
                corpus, SUBJECT, approval_path=relative, approval_sha256=digest
            )
        assert not (
            standard.root / "provenance/recording-groups/.prepared-gates"
        ).exists() or not any(
            (standard.root / "provenance/recording-groups/.prepared-gates").iterdir()
        )

    with pytest.raises(SecurityError, match="diagnostic subjects"):
        consent_approval(standard, SUBJECT)
    marked = marked_root(tmp_path)
    with pytest.raises(SecurityError, match="physical-test asset roots"):
        consent_approval(marked, "cole-self")
    for layout in (standard, marked):
        assert not consent_record_path(layout, "cole-self-v1").exists()


def test_legacy_capture_qt_and_launchers_refuse_marked_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marked = marked_root(tmp_path)
    catalog = synthetic_catalog(3)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy capture must refuse the root before touching devices")

    monkeypatch.setattr("cole_voice_lab.cli.list_avfoundation_audio_devices", forbidden)
    with pytest.raises(ContractError, match="marked for physical tests"):
        TakeEngine(
            layout=marked, prompt_suite=catalog, subject_id=SUBJECT, device=UMIK, session_id="s-1"
        )
    with pytest.raises(ContractError, match="marked for physical tests"):
        capture_prompt_suite(
            layout=marked, prompt_suite=catalog, subject_id=SUBJECT, device=UMIK, session_id="s-2"
        )
    assert not any(marked.directory("tmp").iterdir())
    with pytest.raises(ContractError, match="marked for physical tests"):
        run_desktop(asset_root=marked.root)
    output = tmp_path.resolve() / "Launcher.app"
    with pytest.raises(ContractError, match="marked for physical tests"):
        create_desktop_application(output, python=Path(sys.executable), asset_root=marked.root)
    assert not output.exists()
    for command in ("run", "console"):
        arguments = ["capture", command, "--asset-root", str(marked.root), "--session-id", "s-3"]
        assert main(arguments) == 2
        assert "marked for physical tests" in capsys.readouterr().err
