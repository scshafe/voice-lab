"""Run the actual native workspace against private synthetic helper/service processes.

Creates bounded PCM, manifests, configuration and a corpus store (the exact fixture catalog,
published and active for the synthetic subject) outside Git. The configuration names no
catalog: the service serves the store's, as it does for the real app. Never opens a
microphone, emits audio, or installs/restarts an app. The review accessibility gate
briefly shows its owned synthetic window behind other windows.
"""

from __future__ import annotations

import argparse
import fcntl
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.corpus_store import open_corpus_store
from cole_voice_lab.models import PROMPT_SUITE_V2, CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    RECORDING_SCHEMA_MODELS,
    CatalogReference,
    RecordingBuildIdentity,
)
from cole_voice_lab.recording_service import recording_service_build_identity

PATTERN_PCM = bytes([0, 0, 0, 255, 255, 127, 0, 0, 128])
SUBJECT = "synthetic-subject"

FIXTURE_LOCKS = (
    "provenance/capture-desktop/.capture.lock",
    "provenance/recording-analysis/.owner.lock",
)


def seed_active_catalog(layout: AssetLayout, catalog: PromptSuite) -> CatalogReference:
    """Publish ``catalog`` exactly into the root's new corpus store and activate it.

    The store version keeps the fixture's suite id, purpose and prompt order, so the app
    receives these exact prompts from the service. Returns the active reference.
    Raises: ``ValueError`` when the store already holds a catalog, or when the published
    suite or its reference is not exactly ``catalog``; ``CorpusStoreError`` (``CORPUS.*``)
    when the store refuses the prompts, the catalog or the activation.
    """

    with open_corpus_store(layout, create=True) as corpus:
        if corpus.list_catalogs():
            raise ValueError("Fixture corpus store must be new")
        corpus.import_prompt_suite(catalog, source="manual")
        corpus.create_catalog(
            catalog.suite_id,
            purpose=catalog.purpose,
            description="Synthetic native verification fixture.",
        )
        reference = corpus.publish_version(
            catalog.suite_id, prompt_ids=tuple(prompt.prompt_id for prompt in catalog.prompts)
        )
        if corpus.materialize(reference).suite != catalog:
            raise ValueError("Fixture store published a different catalog")
        corpus.activate(SUBJECT, reference)
    if reference.catalog_sha256 != canonical_sha256(catalog):
        raise ValueError("Fixture store reference names a different catalog")
    return reference


def desktop_session_catalog(catalog: PromptSuite) -> PromptSuite:
    """The prompt-suite/v1 form of ``catalog`` a desktop (Qt) session records against.

    Desktop sessions predate prompt-suite/v2: a silent prompt carries its instructions as
    text. Spoken prompts are the same objects, so their legacy takes count under the store
    version exactly as pilot-v1 takes count under pilot-v2; the room tone does not.
    """

    return PromptSuite(
        suite_id=catalog.suite_id,
        purpose=catalog.purpose,
        prompts=tuple(
            prompt
            if prompt.text is not None
            else Prompt.model_validate(
                prompt.model_dump(exclude={"instructions"}) | {"text": prompt.instructions}
            )
            for prompt in catalog.prompts
        ),
    )


def package_catalog() -> PromptSuite:
    """A compact catalog whose every prompt is recorded, so packages cover it exactly."""

    return PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="synthetic-package",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="package-room-tone",
                instructions="Remain still and quiet. Do not speak.",
                category="room-tone",
                style="Keep the room quiet",
                expected_seconds=1.0,
            ),
            *(
                Prompt(
                    prompt_id=f"package-reading-{name}",
                    text=f"Synthetic package reading {name}.",
                    category="neutral",
                    style="Speak naturally",
                    expected_seconds=1.0,
                )
                for name in ("one", "two", "three")
            ),
        ),
    )


def package_pcm() -> bytes:
    """One exact 480-frame period, so repeated chunks stay continuous and unclipped."""

    return b"".join(
        round(2_000_000 * math.sin(index * math.tau / 480)).to_bytes(3, "little", signed=True)
        for index in range(480)
    )


def workspace_catalog() -> PromptSuite:
    """The 208-prompt interactive catalog used by the transport and review suites."""

    return PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="synthetic-workspace",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="room-tone",
                instructions="Remain still and quiet. Do not speak.",
                category="room-tone",
                style="Keep the room quiet",
                expected_seconds=1.0,
            ),
            Prompt(
                prompt_id="reading-one",
                text="Synthetic reading one. " * 65,
                category="neutral",
                style="Comfortable pace and natural delivery",
                expected_seconds=20.0,
            ),
            Prompt(
                prompt_id="reading-two",
                text="Synthetic reading two is the next canonical prompt.",
                category="neutral",
                style="Speak naturally",
                expected_seconds=2.0,
            ),
            *(
                Prompt(
                    prompt_id=f"extra-{index:03}",
                    text=f"Synthetic extra prompt {index}.",
                    category="neutral",
                    style="Speak naturally",
                    expected_seconds=1.0,
                )
                for index in range(205)
            ),
        ),
    )


def wait_for_released_locks(
    asset_root: Path, relative: tuple[str, ...], timeout: float = 60.0
) -> None:
    """Block until every named exclusive lock is free.

    A previous harness child can still own capture and the analysis queue for a moment
    after its own process has exited. Waiting on the actual resources is deterministic;
    a sleep would only be a guess about how long that takes.
    """

    deadline = time.monotonic() + timeout
    for name in relative:
        lock = asset_root / name
        if not lock.exists():
            continue
        while True:
            with open(lock, "r+b") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    break
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{name} was never released by the previous fixture run")


def run_with_live_capture_owner(root: Path, asset_root: Path, harness: Path, config: Path) -> None:
    """Run the busy scenario while a second process genuinely owns capture."""

    marker = root / "ownership-held"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "native_recovery_holder.py"),
            "--asset-root",
            str(asset_root),
            "--held-marker",
            str(marker),
        ],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        deadline = time.monotonic() + 30.0
        while not marker.exists():
            if holder.poll() is not None:
                raise RuntimeError("capture ownership holder exited before taking the lock")
            if time.monotonic() >= deadline:
                raise TimeoutError("capture ownership holder never acquired the lock")
        subprocess.run(
            [str(harness), "recovery-busy", str(config), str(root / "native-busy")],
            check=True,
            timeout=240,
        )
    finally:
        marker.unlink(missing_ok=True)
        holder.wait(timeout=60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("transport", "review", "review-legacy", "recovery"),
        default="transport",
    )
    parser.add_argument("--accepted-legacy", action="store_true")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--binary-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--delayed",
        action="store_true",
        help="Block actual seal, durable Save, and analysis behind private test FIFOs",
    )
    options = parser.parse_args()
    os.umask(0o077)
    root = options.output.resolve()
    if root.is_relative_to(Path.cwd().resolve()) or root.is_relative_to(options.package.resolve()):
        raise ValueError("Workspace fixture instances must remain outside source checkouts")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("Workspace fixtures require a private external root")
    if any(root.iterdir()):
        raise ValueError("Workspace evidence destination must be empty")
    assets = AssetLayout.initialize(root / "assets")
    schemas = root / "schemas"
    schemas.mkdir(mode=0o700)
    for name, model in RECORDING_SCHEMA_MODELS.items():
        write_canonical_json(schemas / f"{name}.schema.json", model.model_json_schema())
    verified_signal = options.suite == "recovery"
    catalog = package_catalog() if verified_signal else workspace_catalog()
    # The transport harness requires the app to record against exactly this reference.
    write_canonical_json(root / "catalog-reference.json", seed_active_catalog(assets, catalog))
    if options.suite == "review-legacy":
        from native_review_fixture import create_legacy

        create_legacy(assets, desktop_session_catalog(catalog), accepted=options.accepted_legacy)
    device = CaptureDevice(
        backend="coreaudio",
        device_id="synthetic-coreaudio-uid",
        display_name="Synthetic UMIK-1",
    )
    source_identity = canonical_sha256(
        [
            (str(path.relative_to(options.package)), sha256_file(path)[0])
            for path in sorted((options.package / "Sources").rglob("*"))
            if path.is_file()
        ]
    )
    swift_version = subprocess.check_output(["xcrun", "swift", "--version"], text=True)
    native_environment = canonical_sha256({"swift": swift_version, "synthetic": True})
    helper = (options.binary_directory / "ColeVoiceRecorderHelper").resolve()
    harness = (options.binary_directory / "RecordingHarness").resolve()
    recorder_build = RecordingBuildIdentity(
        build_id="synthetic-workspace-helper",
        source_sha256=source_identity,
        executable_sha256=sha256_file(helper)[0],
        environment_sha256=native_environment,
    )
    client_build = RecordingBuildIdentity(
        build_id="synthetic-workspace-ui",
        source_sha256=source_identity,
        executable_sha256=sha256_file(harness)[0],
        environment_sha256=native_environment,
    )
    environment = {
        key: os.environ[key]
        for key in ["PATH", "HOME", "TMPDIR", "PYTHONPATH"]
        if key in os.environ
    }
    environment["PYTHONUNBUFFERED"] = "1"

    service_build = recording_service_build_identity()
    service_arguments = [
        "-m",
        "cole_voice_lab.recording_service",
        "--native-workspace",
        "--subject-id",
        SUBJECT,
    ]
    if options.suite == "recovery":
        # The helper's seal barrier reads this FIFO. Recovery needs that fault hook, not the
        # service-side barrier fixture, so the ordinary canonical service stays in place.
        os.mkfifo(root / "seal-release.fifo", 0o600)
    if options.delayed:
        from cole_voice_lab import (
            corpus_store,
            recording_index,
            recording_service,
            sqlite_migrations,
        )

        runtime = root / "python-runtime"
        package_copy = runtime / "cole_voice_lab"
        shutil.copytree(
            Path(recording_service.__file__).resolve().parent,
            package_copy,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        # Match wheel resources before fingerprinting the isolated fixture runtime.
        migrations = package_copy / "resources/recording_migrations"
        migrations.mkdir(parents=True, exist_ok=True, mode=0o700)
        for relative, _, _ in recording_index.recording_index_migration_resources():
            name = Path(relative).name
            (migrations / name).write_bytes(recording_index._migration_bytes(name))
        # The service build identity fingerprints the corpus store migration too.
        for relative, _, _ in corpus_store.corpus_store_migration_resources():
            name = Path(relative).name
            (migrations / name).write_bytes(sqlite_migrations.migration_bytes(name))
        fixture_source = (
            Path(__file__).resolve().parents[1] / "tests/native_workspace_service_fixture.py"
        )
        shutil.copy2(fixture_source, package_copy / "_synthetic_workspace_fixture.py")
        environment["PYTHONPATH"] = str(runtime)
        for barrier in ["seal", "save", "analysis"]:
            os.mkfifo(root / f"{barrier}-release.fifo", 0o600)
        identity_data = subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "from cole_voice_lab import recording_service; "
                    "print(recording_service.recording_service_build_identity().model_dump_json())"
                ),
            ],
            env=environment,
        )
        service_build = RecordingBuildIdentity.model_validate_json(identity_data)
        service_arguments = [
            "-m",
            "cole_voice_lab._synthetic_workspace_fixture",
            "--test-barriers",
            str(root),
            "--native-workspace",
            "--subject-id",
            SUBJECT,
        ]

    for name, identity in [
        ("recorder", recorder_build),
        ("client", client_build),
        ("service", service_build),
    ]:
        write_canonical_json(root / f"{name}-build.json", identity)
    pcm = root / "pattern.pcm"
    # Package preparation needs takes that actually pass independent verification; the
    # three-sample transport pattern is two-thirds full-scale and fails the clipping check.
    pcm.write_bytes(package_pcm() if verified_signal else PATTERN_PCM)
    helper_arguments = [
        "--synthetic-pcm",
        str(pcm),
        "--synthetic-chunk-frames",
        "480",
        "--synthetic-total-frames",
        "4801",
    ]
    if options.delayed or options.suite == "recovery":
        helper_arguments += ["--synthetic-seal-barrier-directory", str(root)]
    # The configuration names no catalog: the service pins the store's active version.
    configuration = {
        "schema_version": "cole-voice-lab.recording-application/v2",
        "asset_root": str(assets.root),
        "schema_directory": str(schemas),
        "client_build": client_build.model_dump(mode="json"),
        "client_build_path": str(root / "client-build.json"),
        "purpose": catalog.purpose,
        "subject_id": SUBJECT,
        "capture_device": device.model_dump(mode="json"),
        "capture_host": "synthetic-host",
        "pilot_approval_path": None,
        "pilot_approval_sha256": None,
        "recorder": {
            "executable": str(helper),
            "arguments": helper_arguments,
            "environment": environment,
            "build": recorder_build.model_dump(mode="json"),
            "build_identity_path": str(root / "recorder-build.json"),
        },
        "service": {
            "executable": str(Path(sys.executable).absolute()),
            "arguments": service_arguments,
            "environment": environment,
            "build": service_build.model_dump(mode="json"),
            "build_identity_path": str(root / "service-build.json"),
        },
    }
    config_file = root / "recording-application.json"
    write_canonical_json(config_file, configuration)
    if options.suite == "recovery":
        # Physical P09: Stop before the first buffer. The zero-frame run owns a separate asset
        # root, so its preserved partial never becomes the crash/restart runs' finding.
        zero_frame = root / "zero-frame"
        zero_frame.mkdir(mode=0o700)
        os.mkfifo(zero_frame / "start-release.fifo", 0o600)
        zero_frame_configuration = dict(configuration)
        zero_frame_assets = AssetLayout.initialize(zero_frame / "assets")
        # A separate root needs its own store: the service serves only its root's catalog.
        seed_active_catalog(zero_frame_assets, catalog)
        zero_frame_configuration["asset_root"] = str(zero_frame_assets.root)
        zero_frame_recorder = dict(configuration["recorder"])
        zero_frame_recorder["arguments"] = [
            *helper_arguments[: helper_arguments.index("--synthetic-seal-barrier-directory")],
            "--synthetic-start-barrier-directory",
            str(zero_frame),
        ]
        zero_frame_configuration["recorder"] = zero_frame_recorder
        zero_frame_file = zero_frame / "recording-application.json"
        write_canonical_json(zero_frame_file, zero_frame_configuration)
        subprocess.run(
            [str(harness), "recovery-zero-frame", str(zero_frame_file), str(zero_frame / "native")],
            check=True,
            timeout=240,
        )
        # The seal barrier is single use. Claiming its entry marker here makes the writer's
        # own final-header hook fail at the real seal boundary, which is the only way to
        # produce an actual unsealed native partial without touching production code.
        (root / "seal-entered").write_bytes(b"\x01")
        restart = dict(configuration)
        recorder = dict(configuration["recorder"])
        recorder["arguments"] = [
            argument
            for index, argument in enumerate(helper_arguments)
            if argument != "--synthetic-seal-barrier-directory"
            and helper_arguments[index - 1] != "--synthetic-seal-barrier-directory"
        ]
        restart["recorder"] = recorder
        # A restarted session in a different acoustic setup must open its own context; the
        # previous group keeps its own device exactly as it was recorded.
        restart["capture_device"] = CaptureDevice(
            backend="coreaudio",
            device_id="synthetic-coreaudio-uid-changed",
            display_name="Synthetic UMIK-1 (moved)",
        ).model_dump(mode="json")
        restart["capture_host"] = "synthetic-host-moved"
        restart_file = root / "recording-application-restart.json"
        write_canonical_json(restart_file, restart)
        for suite, config, destination in (
            ("recovery-crash", config_file, root / "native-crash"),
            ("recovery-restart", restart_file, root / "native-restart"),
        ):
            wait_for_released_locks(assets.root, FIXTURE_LOCKS)
            subprocess.run(
                [str(harness), suite, str(config), str(destination)], check=True, timeout=240
            )
        wait_for_released_locks(assets.root, FIXTURE_LOCKS)
        run_with_live_capture_owner(root, assets.root, harness, restart_file)
    else:
        subprocess.run(
            [str(harness), options.suite, str(config_file), str(root / "native")],
            check=True,
            timeout=90,
        )
    if options.suite in {"review", "review-legacy"}:
        from verify_native_review import verify

        verify(root, legacy=options.suite == "review-legacy")
    if options.suite == "recovery":
        from verify_native_recovery import verify as verify_recovery

        verify_recovery(root)


if __name__ == "__main__":
    main()
