"""Build an exclusive signed native preview and frozen local Python service.

@canonical native recording release -- build/sign/configure only. Installation,
launch, microphone authorization, playback and transfers are separate actions.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import stat
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from .canonical import canonical_json_bytes, canonical_sha256, open_regular_no_follow, sha256_file
from .diagnostic_roots import RootPurpose, require_root_purpose
from .errors import ContractError, RuntimeUnavailableError, SecurityError, VoiceLabError
from .models import CaptureDevice, ContractModel, PromptSuitePurpose, Sha256, StableId
from .paths import (
    AssetLayout,
    _reject_existing_symlink_components,
    validate_absolute_path,
    validate_relative_path,
)
from .recording_contracts import (
    CatalogReference,
    OpenContextCommand,
    RecordingBuildIdentity,
    catalog_snapshot_path,
)
from .resources import resource_path
from .runtime import (
    RECORDING_PYTHON_DISTRIBUTIONS,
    inspect_macho_load_commands,
    missing_recording_python_distribution_message,
    recording_distribution_files,
)

_BUILD_ENVIRONMENT = {
    "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONSAFEPATH": "1",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}
# Launch flags the app appends itself (or, for --catalog-sha256, that only the service's own
# corpus store may answer): configured peer arguments can never supply them. Swift
# RecordingApplicationConfiguration.peer() refuses exactly this set.
_RESERVED_ARGUMENTS = {
    "--asset-root",
    "--process-generation",
    "--build-identity",
    "--expected-client-build",
    "--schema-directory",
    "--catalog-sha256",
}
# Settings name no catalog: the service serves the subject's active corpus-store catalog.
# This stand-in reference carries only the declared purpose, so the settings meet exactly
# the open-context rules the app will send (Core Audio device, bounded host, paired pilot
# approval that only a capture-corpus catalog may carry).
_SETTINGS_CHECK_DIGEST = "0" * 64


def _settings_check_reference(purpose: PromptSuitePurpose) -> CatalogReference:
    return CatalogReference(
        catalog_id="application-settings-check",
        catalog_version=1,
        catalog_sha256=_SETTINGS_CHECK_DIGEST,
        snapshot_sha256=_SETTINGS_CHECK_DIGEST,
        suite_id="application-settings-check",
        purpose=purpose,
        snapshot_path=catalog_snapshot_path(_SETTINGS_CHECK_DIGEST),
    )


def _require_release_root(
    layout: AssetLayout, purpose: PromptSuitePurpose, subject_id: str
) -> None:
    """Guards #2-#3 without a catalog: the root is physical-test iff the declared purpose is.

    A marked root also binds its one subject. The suite-ID prefix half of the catalog guard
    runs where a catalog exists: the service's pinned catalog and every group mint.
    """
    root: RootPurpose = "physical-test" if purpose == "physical-test" else "standard"
    require_root_purpose(layout, root, subject_id=subject_id)


class NativeRecordingApplicationSettings(ContractModel):
    """Operator settings for one native preview build.

    ``purpose`` is the purpose of the catalog this build is meant to record; it has no
    default, so a physical-test build says so explicitly. It guards the build's asset root
    only: at runtime the purpose that counts is that of the catalog the service pins from
    the corpus store. ``finetune_runtime_root`` is the F5-TTS virtual environment the app's
    fine-tune jobs run in (an absolute, normalized path); without it fine-tuning is
    unavailable. The build passes it to the service as ``--finetune-runtime-root``.
    """

    schema_version: Literal["cole-voice-lab.native-application-settings/v2"] = (
        "cole-voice-lab.native-application-settings/v2"
    )
    asset_root: str = Field(min_length=1, max_length=4096)
    purpose: PromptSuitePurpose
    subject_id: StableId
    capture_device: CaptureDevice
    capture_host: str = Field(min_length=1, max_length=255)
    pilot_approval_path: str | None = None
    pilot_approval_sha256: Sha256 | None = None
    finetune_runtime_root: (
        Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(validate_absolute_path)]
        | None
    ) = None

    @model_validator(mode="after")
    def _context(self) -> NativeRecordingApplicationSettings:
        OpenContextCommand(
            operation_id="application-settings-check",
            subject_id=self.subject_id,
            catalog_reference=_settings_check_reference(self.purpose),
            capture_device=self.capture_device,
            capture_host=self.capture_host,
            pilot_approval_path=self.pilot_approval_path,
            pilot_approval_sha256=self.pilot_approval_sha256,
        )
        if self.capture_device.backend != "coreaudio":
            raise ValueError("native application requires an explicit Core Audio device UID")
        return self


class NativeRecordingPeerConfiguration(ContractModel):
    executable: str = Field(min_length=1, max_length=4096)
    arguments: tuple[str, ...] = Field(max_length=32)
    environment: dict[str, str] | None
    build: RecordingBuildIdentity
    build_identity_path: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _bounded_launch(self) -> NativeRecordingPeerConfiguration:
        if any(len(item) > 4096 or item in _RESERVED_ARGUMENTS for item in self.arguments):
            raise ValueError("peer arguments exceed bounds or override canonical identity")
        if self.environment is not None and (
            len(self.environment) > 64
            or any(
                not key or len(key) > 128 or len(value) > 8192
                for key, value in self.environment.items()
            )
        ):
            raise ValueError("peer environment exceeds its bounded string map")
        return self


class NativeRecordingApplicationConfiguration(ContractModel):
    """The private launch configuration the native app reads (exact key set on both sides).

    It carries no catalog: after the handshake the app asks the service for the catalog the
    service pinned from the subject's active corpus-store version. ``purpose`` repeats the
    settings' declared catalog purpose so release verification can re-check the root.
    """

    schema_version: Literal["cole-voice-lab.recording-application/v2"] = (
        "cole-voice-lab.recording-application/v2"
    )
    asset_root: str
    schema_directory: str
    client_build: RecordingBuildIdentity
    client_build_path: str
    purpose: PromptSuitePurpose
    subject_id: StableId
    capture_device: CaptureDevice
    capture_host: str
    pilot_approval_path: str | None
    pilot_approval_sha256: Sha256 | None
    recorder: NativeRecordingPeerConfiguration
    service: NativeRecordingPeerConfiguration

    @model_validator(mode="after")
    def _exact_paths(self) -> NativeRecordingApplicationConfiguration:
        paths = [self.asset_root, self.schema_directory, self.client_build_path]
        paths += [
            self.recorder.executable,
            self.recorder.build_identity_path,
            self.service.executable,
            self.service.build_identity_path,
        ]
        for value in paths:
            if not Path(value).is_absolute() or os.path.normpath(value) != value:
                raise ValueError("application paths must be normalized and absolute")
        NativeRecordingApplicationSettings(
            asset_root=self.asset_root,
            purpose=self.purpose,
            subject_id=self.subject_id,
            capture_device=self.capture_device,
            capture_host=self.capture_host,
            pilot_approval_path=self.pilot_approval_path,
            pilot_approval_sha256=self.pilot_approval_sha256,
        )
        return self


class NativeRecordingApplicationRelease(ContractModel):
    schema_version: Literal["cole-voice-lab.native-application-release/v1"] = (
        "cole-voice-lab.native-application-release/v1"
    )
    application: str
    runtime_directory: str
    configuration: str
    configuration_sha256: Sha256
    minimum_macos: str
    service_wheel_sha256: Sha256
    client_build: RecordingBuildIdentity
    recorder_build: RecordingBuildIdentity
    service_build: RecordingBuildIdentity
    signature: Literal["local-ad-hoc-hardened"] = "local-ad-hoc-hardened"
    portable: Literal[False] = False
    synthetic_only: bool


def _run(arguments: Sequence[str], *, cwd: Path | None = None, timeout: int = 300) -> str:
    completed = subprocess.run(
        tuple(arguments),
        cwd=cwd,
        env=_BUILD_ENVIRONMENT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise ContractError(
            f"native release command failed: {arguments[0]}: {completed.stderr[-4000:]}"
        )
    return completed.stdout


def _write_private(path: Path, data: bytes, *, executable: bool = False) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700 if executable else 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _private_tree_copy(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise SecurityError(f"release source must be a real directory: {source}")
    destination.mkdir(mode=0o700)
    for entry in sorted(source.iterdir()):
        # Build metadata is regenerated output, not source. `python -m build --no-isolation`
        # (which scripts/verify runs) leaves it in the checkout, so a release built from a
        # verified tree must skip it rather than fail closed on it.
        if entry.name in {"__pycache__", ".build", ".swiftpm", ".DS_Store"} or entry.name.endswith(
            (".egg-info", ".dist-info")
        ):
            continue
        metadata = entry.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            _private_tree_copy(entry, destination / entry.name)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            if (
                entry.suffix.casefold()
                not in {
                    ".py",
                    ".typed",
                    ".json",
                    ".sql",
                    ".png",
                    ".icns",
                    ".md",
                    ".txt",
                    ".swift",
                    ".h",
                    ".c",
                    ".toml",
                    ".plist",
                    ".css",
                    ".html",
                    ".js",
                }
                or metadata.st_size > 32 * 1024**2
            ):
                raise SecurityError(f"release source is not bounded code or artwork: {entry}")
            with open_regular_no_follow(entry) as stream:
                _write_private(
                    destination / entry.name,
                    stream.read(),
                    executable=bool(metadata.st_mode & 0o111),
                )
        else:
            raise SecurityError(f"release source contains a link or unsupported entry: {entry}")


def _freeze_source(source_root: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE"):
        with open_regular_no_follow(source_root / name) as stream:
            _write_private(destination / name, stream.read())
    for name in ("src", "migrations"):
        _private_tree_copy(source_root / name, destination / name)
    (destination / "apps").mkdir(mode=0o700)
    _private_tree_copy(source_root / "apps/macos", destination / "apps/macos")
    forbidden = {".wav", ".flac", ".mp3", ".m4a", ".safetensors", ".pt", ".pth", ".ckpt"}
    if any(path.suffix.casefold() in forbidden for path in destination.rglob("*")):
        raise SecurityError("release code source contains audio, import hooks or model assets")


def _freeze_dependencies(source_python: Path, destination: Path) -> None:
    # The prepared runtime has no pip, Qt, build tools or mutable editable import.
    _run((str(source_python), "-I", "-B", "-m", "venv", "--without-pip", str(destination)))
    site = destination / "lib/python3.12/site-packages"
    if not site.is_dir():
        raise ContractError("native recording service requires the checked Python 3.12 layout")
    # A missing distribution is reported as null, so the refusal can name its install route.
    probe = (
        "import importlib.metadata as m,json\n"
        "def describe(name):\n"
        "    try:\n"
        "        found = m.distribution(name)\n"
        "    except m.PackageNotFoundError:\n"
        "        return None\n"
        "    return [str(found.locate_file('')), [str(f) for f in found.files or ()],\n"
        "        [e.name for e in found.entry_points if e.group in "
        "('console_scripts', 'gui_scripts')]]\n"
        "print(json.dumps({name: describe(name) for name in "
        + repr(RECORDING_PYTHON_DISTRIBUTIONS)
        + "}))\n"
    )
    installed = json.loads(_run((str(source_python), "-I", "-B", "-c", probe)))
    if not isinstance(installed, dict) or set(installed) != set(RECORDING_PYTHON_DISTRIBUTIONS):
        raise ContractError("service dependencies differ from the expected isolated closure")
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        if installed[name] is None:
            raise RuntimeUnavailableError(missing_recording_python_distribution_message(name))
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        root_text, declared, launchers = installed[name]
        root = Path(root_text).resolve(strict=True)
        # pip's launcher rows (../../../bin/<script>) are not part of the frozen service.
        files = recording_distribution_files(name, declared, launchers)
        if not files or len(files) > 100_000:
            raise ContractError(f"cannot freeze undeclared dependency files: {name}")
        for relative in sorted(files):
            validate_relative_path(relative)
            if relative.endswith(".pyc"):
                continue
            source = _reject_existing_symlink_components(root / relative)
            target = site / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open_regular_no_follow(source) as stream:
                if os.fstat(stream.fileno()).st_nlink != 1:
                    raise SecurityError("service dependency must not be a hard link")
                _write_private(target, stream.read())
    for directory, _, _ in os.walk(destination):
        os.chmod(directory, 0o700)


def _install_frozen_wheel(wheel: Path, environment: Path) -> None:
    site = environment / "lib/python3.12/site-packages"
    with zipfile.ZipFile(wheel) as archive:
        entries = archive.infolist()
        if len(entries) > 100_000 or sum(item.file_size for item in entries) > 256 * 1024**2:
            raise SecurityError("service wheel exceeds its bounded code payload")
        names: set[str] = set()
        for entry in entries:
            if entry.is_dir():
                continue
            relative = validate_relative_path(entry.filename)
            if relative in names or ".data/" in relative or stat.S_ISLNK(entry.external_attr >> 16):
                raise SecurityError("service wheel has duplicate, relocated or linked files")
            names.add(relative)
            target = site / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_private(target, archive.read(entry))


def _native_build_environment() -> dict[str, object]:
    compiler = Path(_run(("/usr/bin/xcrun", "--find", "swiftc")).strip()).resolve(strict=True)
    sdk = Path(_run(("/usr/bin/xcrun", "--show-sdk-path")).strip()).resolve(strict=True)
    settings = sdk / "SDKSettings.json"
    return {
        "compiler": str(compiler),
        "compiler_sha256": sha256_file(compiler)[0],
        "swift_version": _run((str(compiler), "--version")).strip(),
        "sdk": str(sdk),
        "sdk_settings_sha256": sha256_file(settings)[0],
        "system_version_plist_sha256": sha256_file(
            Path("/System/Library/CoreServices/SystemVersion.plist")
        )[0],
        "kernel_build": os.uname().version,
    }


def native_service_arguments(settings: NativeRecordingApplicationSettings) -> tuple[str, ...]:
    """The configured service's base arguments (the app appends the reserved identity flags).

    The service must own the same subject the app opens a context for; its own default
    would otherwise reject every configured subject as a changed workspace. A configured
    fine-tune runtime follows as ``--finetune-runtime-root PATH``.
    Returns: the argument tuple.
    """
    arguments: tuple[str, ...] = (
        "-I",
        "-B",
        "-m",
        "cole_voice_lab.recording_service",
        "--native-workspace",
        "--subject-id",
        settings.subject_id,
    )
    if settings.finetune_runtime_root is not None:
        arguments += ("--finetune-runtime-root", settings.finetune_runtime_root)
    return arguments


def create_native_recording_application(
    output: Path,
    *,
    source_root: Path,
    python: Path,
    settings: NativeRecordingApplicationSettings,
    synthetic_pcm: Path | None = None,
) -> NativeRecordingApplicationRelease:
    """Build a new preview, frozen code/service runtime and private signed identities.

    Never overwrite an output, install, launch, capture, play, download or transfer.
    Partial failed-build artifacts remain reviewable beside the chosen output. The release
    pins no catalog: the app loads the subject's active corpus-store catalog at launch.
    Returns: the written release evidence.
    Raises: ``ContractError`` before any release work when the asset root's purpose differs
    from ``settings.purpose`` (``physical-test catalogs require an asset root created
    with …`` / ``this asset root is marked for physical tests …``) or a marked root belongs
    to another subject, when the platform is not macOS, when the output is not a new
    ``*Preview.app`` or already exists, or when a build command fails; ``SecurityError`` for
    an unsafe diagnostic marker, synthetic PCM or release source; ``OSError`` when a file
    cannot be read or written.
    """
    layout = AssetLayout.validate(Path(settings.asset_root))
    # Refuse a root/purpose mismatch before any release work exists.
    _require_release_root(layout, settings.purpose, settings.subject_id)
    if sys.platform != "darwin":
        raise ContractError("native recording application packaging requires macOS")
    target = _reject_existing_symlink_components(output)
    if (
        target.suffix != ".app"
        or "preview" not in target.stem.casefold()
        or not target.parent.is_dir()
    ):
        raise ContractError("choose a new explicitly named Preview.app in an existing directory")
    runtime = target.with_suffix(".runtime")
    if os.path.lexists(target) or os.path.lexists(runtime):
        raise ContractError("native preview output already exists; choose a new release name")
    source_root = _reject_existing_symlink_components(source_root)
    # Keep this lexical launcher: resolving the venv alias loses its site-packages.
    python = Path(os.path.abspath(python.expanduser()))
    resolved_python = python.resolve(strict=True)
    if not resolved_python.is_file() or not os.access(python, os.X_OK):
        raise ContractError("native preview Python launcher is not executable")
    if synthetic_pcm is not None:
        synthetic_pcm = _reject_existing_symlink_components(synthetic_pcm)
        with open_regular_no_follow(synthetic_pcm) as stream:
            metadata = os.fstat(stream.fileno())
            if (
                metadata.st_nlink != 1
                or metadata.st_mode & 0o077
                or not 0 < metadata.st_size <= 1_048_576
                or metadata.st_size % 3
            ):
                raise SecurityError("synthetic PCM must be one private bounded mono24-bit fixture")
    runtime.mkdir(mode=0o700)
    work = runtime / "build"
    work.mkdir(mode=0o700)
    frozen = work / "source"
    _freeze_source(source_root, frozen)
    wheelhouse = work / "wheels"
    wheelhouse.mkdir(mode=0o700)
    _run(
        (
            str(python),
            "-I",
            "-B",
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheelhouse),
        ),
        cwd=frozen,
    )
    wheels = tuple(wheelhouse.glob("*.whl"))
    if len(wheels) != 1:
        raise ContractError("native release requires exactly one locally built service wheel")
    service_environment = runtime / "python"
    _freeze_dependencies(python, service_environment)
    _install_frozen_wheel(wheels[0], service_environment)
    service_python = service_environment / "bin/python"
    _run(
        (
            str(service_python),
            "-I",
            "-B",
            "-c",
            "from cole_voice_lab.runtime import freeze_recording_python_runtime_receipt; "
            "print(freeze_recording_python_runtime_receipt())",
        )
    )
    schemas = runtime / "schemas"
    schemas.mkdir(mode=0o700)
    _run((str(service_python), "-I", "-B", "-m", "cole_voice_lab.schema", "--output", str(schemas)))
    service_build = RecordingBuildIdentity.model_validate_json(
        _run(
            (
                str(service_python),
                "-I",
                "-B",
                "-c",
                "from cole_voice_lab.recording_service import recording_service_build_identity; "
                "print(recording_service_build_identity().model_dump_json())",
            )
        )
    )
    package = frozen / "apps/macos"
    build_directory = work / "swift"
    for product in ("ColeVoiceRecorderPreview", "ColeVoiceRecorderHelper"):
        _run(
            (
                "/usr/bin/xcrun",
                "swift",
                "build",
                "--package-path",
                str(package),
                "--scratch-path",
                str(build_directory),
                "--configuration",
                "release",
                "--product",
                product,
            ),
            timeout=600,
        )
    products = Path(
        _run(
            (
                "/usr/bin/xcrun",
                "swift",
                "build",
                "--package-path",
                str(package),
                "--scratch-path",
                str(build_directory),
                "--configuration",
                "release",
                "--show-bin-path",
            )
        ).strip()
    )
    native_environment = _native_build_environment()
    native_sources = [
        (str(path.relative_to(package)), *sha256_file(path))
        for path in sorted(package.rglob("*"))
        if path.is_file()
    ]
    native_source_sha = canonical_sha256(native_sources)
    native_environment_sha = canonical_sha256(native_environment)
    target.mkdir(mode=0o700)
    contents = target / "Contents"
    contents.mkdir(mode=0o700)
    binaries = contents / "MacOS"
    binaries.mkdir(mode=0o700)
    resources = contents / "Resources"
    resources.mkdir(mode=0o700)
    helper_directory = contents / "Helpers"
    helper_directory.mkdir(mode=0o700)
    app = binaries / "ColeVoiceRecorderPreview"
    helper = helper_directory / "ColeVoiceRecorderHelper"
    for original, copied in ((products / app.name, app), (products / helper.name, helper)):
        _write_private(copied, original.read_bytes(), executable=True)
    probes = [inspect_macho_load_commands(path) for path in (app, helper, resolved_python)]
    if any(probe.minimum_macos is None for probe in probes):
        raise ContractError("every app/helper/service executable must declare its deployment floor")
    minimum = max(probe.minimum_macos for probe in probes if probe.minimum_macos is not None)
    if not set(probes[0].architectures).intersection(
        probes[1].architectures, probes[2].architectures
    ):
        raise ContractError("native app, helper and Python have no compatible architecture")
    minimum_text = ".".join(str(number) for number in minimum[:2]) + (
        f".{minimum[2]}" if minimum[2] else ""
    )
    icon = resource_path("desktop-icon.icns").read_bytes()
    if icon[:4] != b"icns" or len(icon) <= 8 or int.from_bytes(icon[4:8], "big") != len(icon):
        raise ContractError("native preview icon has an invalid ICNS container")
    _write_private(resources / "ColeVoiceRecorder.icns", icon)
    configuration_path = runtime / "application.json"
    _write_private(
        resources / "RecordingConfigurationPath.json",
        canonical_json_bytes(
            {
                "schema_version": "cole-voice-lab.recording-configuration-path/v1",
                "configuration_path": str(configuration_path),
            }
        ),
    )
    bundle_metadata = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": app.name,
        "CFBundleIdentifier": "private.cole.voice-lab.recording.preview",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleIconFile": "ColeVoiceRecorder.icns",
        "CFBundleName": "Cole Voice Recorder Preview",
        "CFBundleDisplayName": "Cole Voice Recorder Preview",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": minimum_text,
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": (
            "Record your selected voice prompts with your explicitly selected microphone."
        ),
    }
    _write_private(contents / "Info.plist", plistlib.dumps(bundle_metadata))
    entitlements = work / "audio-input.entitlements.plist"
    _write_private(entitlements, plistlib.dumps({"com.apple.security.device.audio-input": True}))
    for signed in (helper, target):
        _run(
            (
                "/usr/bin/codesign",
                "--force",
                "--sign",
                "-",
                "--options",
                "runtime",
                "--entitlements",
                str(entitlements),
                "--timestamp=none",
                str(signed),
            )
        )
    _run(("/usr/bin/codesign", "--verify", "--strict", "--deep", "--verbose=2", str(target)))
    client_build = RecordingBuildIdentity(
        build_id="native-recording-preview-v1",
        source_sha256=native_source_sha,
        executable_sha256=sha256_file(app)[0],
        environment_sha256=native_environment_sha,
    )
    recorder_build = client_build.model_copy(
        update={
            "build_id": "native-recording-helper-v1",
            "executable_sha256": sha256_file(helper)[0],
        }
    )
    client_path = runtime / "client-build.json"
    recorder_path = runtime / "recorder-build.json"
    service_path = runtime / "service-build.json"
    for path, identity in (
        (client_path, client_build),
        (recorder_path, recorder_build),
        (service_path, service_build),
    ):
        _write_private(path, canonical_json_bytes(identity))
    recorder_arguments: tuple[str, ...] = ()
    if synthetic_pcm is not None:
        recorder_arguments = (
            "--synthetic-pcm",
            str(synthetic_pcm),
            "--synthetic-chunk-frames",
            "480",
        )
    configuration = NativeRecordingApplicationConfiguration(
        asset_root=str(layout.root),
        schema_directory=str(schemas),
        client_build=client_build,
        client_build_path=str(client_path),
        purpose=settings.purpose,
        subject_id=settings.subject_id,
        capture_device=settings.capture_device,
        capture_host=settings.capture_host,
        pilot_approval_path=settings.pilot_approval_path,
        pilot_approval_sha256=settings.pilot_approval_sha256,
        recorder=NativeRecordingPeerConfiguration(
            executable=str(helper),
            arguments=recorder_arguments,
            environment=_BUILD_ENVIRONMENT,
            build=recorder_build,
            build_identity_path=str(recorder_path),
        ),
        service=NativeRecordingPeerConfiguration(
            executable=str(service_python),
            arguments=native_service_arguments(settings),
            environment=_BUILD_ENVIRONMENT,
            build=service_build,
            build_identity_path=str(service_path),
        ),
    )
    configuration_bytes = canonical_json_bytes(configuration)
    if len(configuration_bytes) > 1_048_576:
        raise ContractError("native application configuration exceeds its control bound")
    _write_private(configuration_path, configuration_bytes)
    release = NativeRecordingApplicationRelease(
        application=str(target),
        runtime_directory=str(runtime),
        configuration=str(configuration_path),
        configuration_sha256=sha256_file(configuration_path)[0],
        minimum_macos=minimum_text,
        service_wheel_sha256=sha256_file(wheels[0])[0],
        client_build=client_build,
        recorder_build=recorder_build,
        service_build=service_build,
        synthetic_only=synthetic_pcm is not None,
    )
    _write_private(runtime / "release.json", canonical_json_bytes(release))
    _write_private(
        runtime / "native-build-environment.json", canonical_json_bytes(native_environment)
    )
    _write_private(
        runtime / "README.txt",
        b"Local native preview; not installed or launched. Keep this app and adjacent runtime "
        b"together at their configured paths. No recordings or models are bundled. The Python "
        b"base runtime and macOS are external and checksum-bound; replacing them requires "
        b"rebuilding. Local ad-hoc signing is not Developer ID/notarization and does not promise "
        b"microphone permission persistence. Hardware permission attribution remains unverified "
        b"until approved. The frozen previous Qt release remains separate; it cannot edit "
        b"native-format drafts, which remain preserved.\n",
    )
    parent = os.open(runtime, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return release


def verify_native_recording_application(release_path: Path) -> NativeRecordingApplicationRelease:
    """Verify private release/configuration, signed peers and frozen service identity.

    Executes only an isolated Python build-identity probe. Does not launch an app,
    start a service, discover recordings, open audio devices or request permission. The
    configuration carries no catalog, so no catalog digest is verified here; the root is
    re-checked against the configured purpose.
    Returns: the verified release evidence.
    Raises: ``ContractError`` when the asset root's purpose differs from the configured
    purpose or a marked root belongs to another subject, or when the codesign check fails;
    ``SecurityError`` when release metadata, configuration, peer manifests, executables,
    the frozen Python identity or the schemas are missing, changed, non-canonical or unsafe;
    ``ValidationError`` for a release or configuration that is not the current contract
    (a ``recording-application/v1`` configuration that embeds a catalog included);
    ``OSError`` when a file cannot be read.
    """
    release_path = _reject_existing_symlink_components(release_path)

    def read_private(path: Path, maximum: int = 1_048_576) -> bytes:
        with open_regular_no_follow(_reject_existing_symlink_components(path)) as stream:
            metadata = os.fstat(stream.fileno())
            if (
                metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o077
                or metadata.st_size > maximum
            ):
                raise SecurityError("release metadata must remain owned, private and bounded")
            return stream.read(maximum + 1)

    raw = read_private(release_path)
    release = NativeRecordingApplicationRelease.model_validate_json(raw)
    if raw != canonical_json_bytes(release):
        raise SecurityError("release evidence must remain canonical")
    runtime = Path(release.runtime_directory)
    if release_path != runtime / "release.json":
        raise SecurityError("release evidence is outside its declared runtime directory")
    configuration_bytes = read_private(Path(release.configuration))
    if sha256_file(Path(release.configuration))[0] != release.configuration_sha256:
        raise SecurityError("native application configuration digest changed")
    configuration = NativeRecordingApplicationConfiguration.model_validate_json(configuration_bytes)
    if configuration_bytes != canonical_json_bytes(configuration):
        raise SecurityError("native application configuration must remain canonical")
    _require_release_root(
        AssetLayout.validate(Path(configuration.asset_root)),
        configuration.purpose,
        configuration.subject_id,
    )
    expected = (release.client_build, release.recorder_build, release.service_build)
    configured = (
        configuration.client_build,
        configuration.recorder.build,
        configuration.service.build,
    )
    if expected != configured:
        raise SecurityError("release peer identities differ from their configuration")
    manifests = (
        configuration.client_build_path,
        configuration.recorder.build_identity_path,
        configuration.service.build_identity_path,
    )
    for manifest_path_text, identity in zip(manifests, expected, strict=True):
        if read_private(Path(manifest_path_text), 4096) != canonical_json_bytes(identity):
            raise SecurityError("release peer build manifest changed")
    app = Path(release.application)
    executable = app / "Contents/MacOS/ColeVoiceRecorderPreview"
    helper = _reject_existing_symlink_components(Path(configuration.recorder.executable))
    if helper != app / "Contents/Helpers/ColeVoiceRecorderHelper":
        raise SecurityError("configured helper is outside its signed native application")
    for path, identity in ((executable, release.client_build), (helper, release.recorder_build)):
        if sha256_file(path)[0] != identity.executable_sha256:
            raise SecurityError("signed native executable differs from its build identity")
    _run(("/usr/bin/codesign", "--verify", "--strict", "--deep", "--verbose=2", str(app)))
    python = Path(configuration.service.executable)
    if python != runtime / "python/bin/python":
        raise SecurityError("configured Python is outside its frozen runtime")
    observed = RecordingBuildIdentity.model_validate_json(
        _run(
            (
                str(python),
                "-I",
                "-B",
                "-c",
                "from cole_voice_lab.recording_service import recording_service_build_identity; "
                "print(recording_service_build_identity().model_dump_json())",
            )
        )
    )
    if observed != release.service_build:
        raise SecurityError("frozen Python source, executable or runtime identity changed")
    for name in (
        "recording-service-request",
        "recording-response",
        "recording-recorder-request",
    ):
        path = Path(configuration.schema_directory) / f"{name}.schema.json"
        with open_regular_no_follow(_reject_existing_symlink_components(path)) as stream:
            if not stream.read(1):
                raise SecurityError("native recording schema is missing or empty")
    return release


def main(argv: Sequence[str] | None = None) -> int:
    """Build one native preview from a private settings file and print its release evidence.

    Returns: 0 after a successful build.
    Raises: ``SystemExit(2)`` (argparse usage error on stderr) for unknown arguments (the
    removed ``--expect-catalog-sha256`` included), an unreadable, oversized or invalid
    settings file (``native-application-settings/v1`` files that embed a catalog included),
    or any build refusal ``create_native_recording_application`` raises.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--synthetic-pcm", type=Path)
    options = parser.parse_args(argv)
    try:
        with open_regular_no_follow(
            _reject_existing_symlink_components(options.settings)
        ) as stream:
            if os.fstat(stream.fileno()).st_size > 1_048_576:
                raise SecurityError("native application settings exceed their bounded contract")
            settings = NativeRecordingApplicationSettings.model_validate_json(
                stream.read(1_048_577)
            )
        release = create_native_recording_application(
            options.output,
            source_root=options.source_root,
            python=options.python,
            settings=settings,
            synthetic_pcm=options.synthetic_pcm,
        )
    except (VoiceLabError, OSError, ValueError) as error:
        parser.error(str(error))
    print(release.model_dump_json(indent=2))
    return 0


NATIVE_RECORDING_APPLICATION_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "native-recording-application-settings": NativeRecordingApplicationSettings,
    "native-recording-peer-configuration": NativeRecordingPeerConfiguration,
    "native-recording-application-configuration": NativeRecordingApplicationConfiguration,
    "native-recording-application-release": NativeRecordingApplicationRelease,
}

if __name__ == "__main__":
    raise SystemExit(main())
