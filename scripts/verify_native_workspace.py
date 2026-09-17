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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def verified_catalog() -> PromptSuite:
    """A compact catalog for suites whose takes must pass independent verification."""

    return PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="synthetic-verified",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="verified-room-tone",
                instructions="Remain still and quiet. Do not speak.",
                category="room-tone",
                style="Keep the room quiet",
                expected_seconds=1.0,
            ),
            *(
                Prompt(
                    prompt_id=f"verified-reading-{name}",
                    text=f"Synthetic verified reading {name}.",
                    category="neutral",
                    style="Speak naturally",
                    expected_seconds=1.0,
                )
                for name in ("one", "two", "three")
            ),
        ),
    )


def verified_pcm() -> bytes:
    """One exact 480-frame sine period: repeated chunks stay continuous and unclipped, so a
    take recorded from it passes independent verification (the transport pattern does not)."""

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


SERVICE_MODULE_ARGUMENTS = ("-m", "cole_voice_lab.recording_service")
SYNTHETIC_DEVICE = CaptureDevice(
    backend="coreaudio",
    device_id="synthetic-coreaudio-uid",
    display_name="Synthetic UMIK-1",
)


def private_fixture_root(root: Path, *, package: Path) -> Path:
    """Resolve and create an empty private (0700) fixture root outside any source checkout.

    Raises: ``ValueError`` when the root lies inside the working directory or ``package``,
    is not private, or is not empty.
    """

    root = root.resolve()
    if root.is_relative_to(Path.cwd().resolve()) or root.is_relative_to(package.resolve()):
        raise ValueError("Workspace fixture instances must remain outside source checkouts")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("Workspace fixtures require a private external root")
    if any(root.iterdir()):
        raise ValueError("Workspace evidence destination must be empty")
    return root


def write_recording_schemas(root: Path) -> Path:
    """Write every recording wire schema under ``root/schemas``; return that directory."""

    schemas = root / "schemas"
    schemas.mkdir(mode=0o700)
    for name, model in RECORDING_SCHEMA_MODELS.items():
        write_canonical_json(schemas / f"{name}.schema.json", model.model_json_schema())
    return schemas


@dataclass(frozen=True)
class NativeBuilds:
    """The built helper and harness, with the identities the fixture configuration pins."""

    helper: Path
    harness: Path
    recorder: RecordingBuildIdentity
    client: RecordingBuildIdentity


def native_builds(package: Path, binary_directory: Path) -> NativeBuilds:
    """Identify the helper and harness in ``binary_directory`` built from ``package``."""

    source_identity = canonical_sha256(
        [
            (str(path.relative_to(package)), sha256_file(path)[0])
            for path in sorted((package / "Sources").rglob("*"))
            if path.is_file()
        ]
    )
    swift_version = subprocess.check_output(["xcrun", "swift", "--version"], text=True)
    native_environment = canonical_sha256({"swift": swift_version, "synthetic": True})
    helper = (binary_directory / "ColeVoiceRecorderHelper").resolve()
    harness = (binary_directory / "RecordingHarness").resolve()
    return NativeBuilds(
        helper=helper,
        harness=harness,
        recorder=RecordingBuildIdentity(
            build_id="synthetic-workspace-helper",
            source_sha256=source_identity,
            executable_sha256=sha256_file(helper)[0],
            environment_sha256=native_environment,
        ),
        client=RecordingBuildIdentity(
            build_id="synthetic-workspace-ui",
            source_sha256=source_identity,
            executable_sha256=sha256_file(harness)[0],
            environment_sha256=native_environment,
        ),
    )


def fixture_environment() -> dict[str, str]:
    """The environment both fixture peers run with: this process's PATH, HOME, TMPDIR and
    PYTHONPATH (when set), unbuffered."""

    environment = {
        key: os.environ[key]
        for key in ["PATH", "HOME", "TMPDIR", "PYTHONPATH"]
        if key in os.environ
    }
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def isolated_service_runtime(
    root: Path, wrapper: Path, module: str, environment: dict[str, str]
) -> RecordingBuildIdentity:
    """Copy the service package plus one test-only wrapper into ``root/python-runtime``.

    The wrapper becomes ``cole_voice_lab.<module>``; ``environment["PYTHONPATH"]`` is pointed
    at the copy, and the service build identity is computed from the copy, exactly as the
    service will compute its own.
    Returns: that build identity.
    """

    from cole_voice_lab import corpus_store, recording_index, recording_service, sqlite_migrations

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
    shutil.copy2(wrapper, package_copy / f"{module}.py")
    environment["PYTHONPATH"] = str(runtime)
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
    return RecordingBuildIdentity.model_validate_json(identity_data)


def application_configuration(
    root: Path,
    assets: AssetLayout,
    schemas: Path,
    *,
    purpose: str,
    builds: NativeBuilds,
    helper_arguments: list[str],
    service_build: RecordingBuildIdentity,
    service_arguments: list[str],
    environment: dict[str, str],
    capture_device: CaptureDevice = SYNTHETIC_DEVICE,
) -> dict[str, Any]:
    """The recording-application/v2 configuration of a fixture root.

    Writes the three build identity files under ``root`` and returns the configuration; the
    caller writes it. It names no catalog: the service pins the store's active version.
    """

    for name, identity in [
        ("recorder", builds.recorder),
        ("client", builds.client),
        ("service", service_build),
    ]:
        write_canonical_json(root / f"{name}-build.json", identity)
    return {
        "schema_version": "cole-voice-lab.recording-application/v2",
        "asset_root": str(assets.root),
        "schema_directory": str(schemas),
        "client_build": builds.client.model_dump(mode="json"),
        "client_build_path": str(root / "client-build.json"),
        "purpose": purpose,
        "subject_id": SUBJECT,
        "capture_device": capture_device.model_dump(mode="json"),
        "capture_host": "synthetic-host",
        "pilot_approval_path": None,
        "pilot_approval_sha256": None,
        "recorder": {
            "executable": str(builds.helper),
            "arguments": helper_arguments,
            "environment": environment,
            "build": builds.recorder.model_dump(mode="json"),
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
        choices=("transport", "review", "review-legacy", "recovery", "finetune", "speak"),
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
    root = private_fixture_root(options.output, package=options.package)
    if options.suite == "finetune":
        from native_finetune_fixture import run_finetune_suite

        run_finetune_suite(root, package=options.package, binary_directory=options.binary_directory)
        return
    if options.suite == "speak":
        from native_speak_fixture import run_speak_suite

        run_speak_suite(root, package=options.package, binary_directory=options.binary_directory)
        return
    assets = AssetLayout.initialize(root / "assets")
    schemas = write_recording_schemas(root)
    verified_signal = options.suite == "recovery"
    catalog = verified_catalog() if verified_signal else workspace_catalog()
    # The transport harness requires the app to record against exactly this reference.
    write_canonical_json(root / "catalog-reference.json", seed_active_catalog(assets, catalog))
    if options.suite == "review-legacy":
        from native_review_fixture import create_legacy

        create_legacy(assets, desktop_session_catalog(catalog), accepted=options.accepted_legacy)
    builds = native_builds(options.package, options.binary_directory)
    harness = builds.harness
    environment = fixture_environment()

    service_build = recording_service_build_identity()
    service_arguments = [*SERVICE_MODULE_ARGUMENTS, "--native-workspace", "--subject-id", SUBJECT]
    if options.suite == "recovery":
        # The helper's seal barrier reads this FIFO. Recovery needs that fault hook, not the
        # service-side barrier fixture, so the ordinary canonical service stays in place.
        os.mkfifo(root / "seal-release.fifo", 0o600)
    if options.delayed:
        service_build = isolated_service_runtime(
            root,
            Path(__file__).resolve().parents[1] / "tests/native_workspace_service_fixture.py",
            "_synthetic_workspace_fixture",
            environment,
        )
        for barrier in ["seal", "save", "analysis"]:
            os.mkfifo(root / f"{barrier}-release.fifo", 0o600)
        service_arguments = [
            "-m",
            "cole_voice_lab._synthetic_workspace_fixture",
            "--test-barriers",
            str(root),
            "--native-workspace",
            "--subject-id",
            SUBJECT,
        ]

    pcm = root / "pattern.pcm"
    # Recovery needs takes that actually pass independent verification; the three-sample
    # transport pattern is two-thirds full-scale and fails the clipping check.
    pcm.write_bytes(verified_pcm() if verified_signal else PATTERN_PCM)
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
    configuration = application_configuration(
        root,
        assets,
        schemas,
        purpose=catalog.purpose,
        builds=builds,
        helper_arguments=helper_arguments,
        service_build=service_build,
        service_arguments=service_arguments,
        environment=environment,
    )
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
