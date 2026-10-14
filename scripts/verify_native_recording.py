#!/usr/bin/env python3
"""Build/test native recording with synthetic private fixtures and no audio devices.

SwiftPM outputs live in build/native-recording, or in distinct children of an
existing resolved private external COLE_VOICE_NATIVE_SCRATCH_ROOT. Reports and generated fixture
instances live in a private external temporary directory. No app is installed,
restarted, or connected to the microphone. The accessibility gate briefly
displays its owned synthetic window behind other windows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from recording_native_scratch import native_recording_scratch_path


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print(
        json.dumps({"event": "native_verification_command", "command": command}),
        flush=True,
    )
    subprocess.run(command, cwd=cwd, check=True, env=env)


def output(command: list[str], *, cwd: Path) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()


def swift_testing_paths(developer: Path) -> tuple[Path, Path]:
    """Where the selected developer tools keep Swift Testing: its framework and its library.

    The Command Line Tools keep them under ``Library/Developer``; a full Xcode keeps them in
    the macOS platform, ``Platforms/MacOSX.platform/Developer`` (``Library/Frameworks`` and
    ``usr/lib``, with ``lib_TestingInterop.dylib``). Selecting the Command Line Tools to find
    them is no way round: they lack the SwiftUIMacros plugin RecordingUI's ``@State`` needs.
    Raises: ``RuntimeError`` naming every place looked in when neither has the framework.
    """
    xcode = developer / "Platforms" / "MacOSX.platform" / "Developer"
    layouts = (
        (
            developer / "Library" / "Developer" / "Frameworks",
            developer / "Library" / "Developer" / "usr" / "lib",
        ),
        (xcode / "Library" / "Frameworks", xcode / "usr" / "lib"),
    )
    for frameworks, libraries in layouts:
        if (frameworks / "Testing.framework").is_dir():
            return frameworks, libraries
    looked = ", ".join(str(frameworks / "Testing.framework") for frameworks, _ in layouts)
    raise RuntimeError(
        "The selected developer tools do not contain the Swift Testing framework "
        f"(looked in {looked})"
    )


def isolated_script(repository: Path, module: str) -> list[str]:
    """Run a verifier module with explicit fixture imports and no ambient Python path."""
    return [
        str(repository / ".venv/bin/python"),
        "-I",
        "-B",
        "-c",
        "import runpy, sys; sys.path[:0] = [sys.argv.pop(1), sys.argv.pop(1)]; "
        "runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)",
        str(repository / "scripts"),
        str(repository / "tests"),
        module,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=(
            "contracts",
            "shell",
            "capture",
            "capture-sanitizers",
            "bridge",
            "waveform",
            "workspace",
            "transport",
            "review",
            "accessibility",
            "review-sanitizers",
            "finetune",
            "speak",
            "vault",
            "recovery",
            "packaged-app",
            "acceptance",
            "all",
        ),
        default="all",
    )
    parser.add_argument(
        "--benchmark-report",
        type=Path,
        help="Release benchmark report.json; the acceptance suite renders its matrix from it",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Private external directory for retained synthetic evidence",
    )
    args = parser.parse_args(argv)
    requested_suite = args.suite
    if requested_suite == "acceptance" and args.benchmark_report is None:
        parser.error("The acceptance suite requires --benchmark-report from the release benchmark")
    # Acceptance produces all of its own parity evidence in this fresh run. It
    # cannot borrow old snapshots or replace a full identity with a partial one.
    if requested_suite == "acceptance":
        args.suite = "all"
    os.umask(0o077)
    repository = Path(__file__).resolve().parent.parent
    package = repository / "apps" / "macos"
    build = native_recording_scratch_path(
        repository / "build" / "native-recording", repository=repository
    )
    artifacts = (
        args.output.expanduser().resolve()
        if args.output
        else Path(tempfile.mkdtemp(prefix="cole-native-recording-verification-")).resolve()
    )
    if artifacts == repository or artifacts.is_relative_to(repository):
        parser.error("Native fixture instances and snapshots must be outside the repository")
    if artifacts.exists() and any(artifacts.iterdir()):
        parser.error(
            "Native verification output must be empty; preserve existing evidence in place"
        )
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    if artifacts.stat().st_mode & 0o077:
        parser.error("Existing evidence directory must be private (0700)")
    benchmark = None
    if args.benchmark_report is not None:
        try:
            benchmark = json.loads(args.benchmark_report.expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            parser.error(f"Cannot read benchmark report: {type(error).__name__}")
        if not isinstance(benchmark, dict):
            parser.error("Benchmark report must be a JSON object")
    swift = ["xcrun", "swift"]
    location = ["--package-path", str(package), "--scratch-path", str(build)]
    strict = [
        "-Xswiftc",
        "-strict-concurrency=complete",
        "-Xswiftc",
        "-warnings-as-errors",
    ]
    developer = Path(output(["xcode-select", "-p"], cwd=repository))
    test_frameworks, test_libraries = swift_testing_paths(developer)
    test_location = [
        "--package-path",
        str(package),
        "--scratch-path",
        str(
            native_recording_scratch_path(
                repository / "build/native-recording-tests", repository=repository
            )
        ),
    ]
    test_flags = strict + [
        "-Xswiftc",
        "-F",
        "-Xswiftc",
        str(test_frameworks),
        "-Xlinker",
        "-F",
        "-Xlinker",
        str(test_frameworks),
        "-Xlinker",
        "-rpath",
        "-Xlinker",
        str(test_frameworks),
        "-Xlinker",
        "-rpath",
        "-Xlinker",
        str(test_libraries),
    ]
    run(swift + ["build"] + location + strict, cwd=repository)
    binary_directory = Path(
        output(swift + ["build"] + location + ["--show-bin-path"], cwd=repository)
    )
    harness = binary_directory / "RecordingHarness"
    if args.suite in {"review", "accessibility", "review-sanitizers", "all"}:
        test_filter = (
            "RecordingReviewTests|RecordingPlaybackTests|RecordingWaveformTests|"
            "NativeCaptureTests|SyntheticCaptureBackendTests|RecordingHarnessTests"
        )
        if args.suite in {"review-sanitizers", "all"}:
            for review_sanitizer in ("address", "thread"):
                sanitizer_location = [
                    "--package-path",
                    str(package),
                    "--scratch-path",
                    str(
                        native_recording_scratch_path(
                            repository / f"build/native-recording-review-{review_sanitizer}",
                            repository=repository,
                        )
                    ),
                ]
                run(
                    swift
                    + ["test", "--sanitize", review_sanitizer]
                    + sanitizer_location
                    + test_flags
                    + ["--filter", test_filter],
                    cwd=repository,
                )
        if args.suite != "review-sanitizers":
            run(
                swift + ["test"] + test_location + test_flags + ["--filter", test_filter],
                cwd=repository,
            )
            modes = [("review", False)]
            if args.suite in {"review", "all"}:
                modes += [("review-legacy", False), ("review-legacy", True)]
            for mode, accepted in modes:
                destination = artifacts / (mode + ("-accepted" if accepted else ""))
                command = [
                    *isolated_script(repository, "verify_native_workspace"),
                    "--suite",
                    mode,
                    "--package",
                    str(package),
                    "--binary-directory",
                    str(binary_directory),
                    "--output",
                    str(destination),
                ]
                if accepted:
                    command.append("--accepted-legacy")
                run(command, cwd=repository)
    if args.suite in {"vault", "all"}:
        run(
            swift + ["test"] + test_location + test_flags + ["--filter", "RecordingVaultTests"],
            cwd=repository,
        )
        run(
            [
                *isolated_script(repository, "verify_native_workspace"),
                "--suite",
                "vault",
                "--package",
                str(package),
                "--binary-directory",
                str(binary_directory),
                "--output",
                str(artifacts / "vault"),
            ],
            cwd=repository,
        )
    if args.suite in {"workspace", "all"}:
        run(
            swift + ["test"] + test_location + test_flags + ["--filter", "RecordingWorkspaceTests"],
            cwd=repository,
        )
        run([str(harness), "workspace", str(artifacts / "workspace")], cwd=repository)
    if args.suite in {"transport", "all"}:
        for delayed in (False, True):
            run(
                [
                    *isolated_script(repository, "verify_native_workspace"),
                    "--package",
                    str(package),
                    "--binary-directory",
                    str(binary_directory),
                    "--output",
                    str(artifacts / ("transport-delayed" if delayed else "transport")),
                    *(["--delayed"] if delayed else []),
                ],
                cwd=repository,
            )
    if args.suite in {"finetune", "all"}:
        run(
            swift + ["test"] + test_location + test_flags + ["--filter", "RecordingFineTuneTests"],
            cwd=repository,
        )
        run(
            [
                *isolated_script(repository, "verify_native_workspace"),
                "--suite",
                "finetune",
                "--package",
                str(package),
                "--binary-directory",
                str(binary_directory),
                "--output",
                str(artifacts / "finetune"),
            ],
            cwd=repository,
        )
    if args.suite in {"speak", "all"}:
        run(
            swift
            + ["test"]
            + test_location
            + test_flags
            + ["--filter", "RecordingSpeakModelTests|RecordingSpeakTests"],
            cwd=repository,
        )
        run(
            [
                *isolated_script(repository, "verify_native_workspace"),
                "--suite",
                "speak",
                "--package",
                str(package),
                "--binary-directory",
                str(binary_directory),
                "--output",
                str(artifacts / "speak"),
            ],
            cwd=repository,
        )
    if args.suite in {"recovery", "all"}:
        run(
            swift
            + ["test"]
            + test_location
            + test_flags
            + ["--filter", "RecordingRecoveryTests|RecordingRecoveryInventoryTests"],
            cwd=repository,
        )
        run(
            [
                *isolated_script(repository, "verify_native_workspace"),
                "--suite",
                "recovery",
                "--package",
                str(package),
                "--binary-directory",
                str(binary_directory),
                "--output",
                str(artifacts / "recovery"),
            ],
            cwd=repository,
        )
    if args.suite in {"packaged-app", "all"}:
        # The rollback replay needs the explicitly named frozen release; without it that
        # case skips, and a skipped rollback must never read as a verified one.
        packaging_environment = dict(os.environ)
        packaging_environment["COVERAGE_FILE"] = str(artifacts / "packaged-app.coverage")
        run(
            [
                str(repository / ".venv/bin/python"),
                "-I",
                "-B",
                "-m",
                "pytest",
                str(repository / "tests/test_native_recording_application.py"),
                "-q",
            ],
            cwd=repository,
            env=packaging_environment,
        )
    identity: dict[str, object] = {
        "verified_at": datetime.now(UTC).isoformat(),
        "suite": requested_suite,
        "executed_suite": args.suite,
        "synthetic_accessibility_window_shown": args.suite in {"review", "accessibility", "all"},
        "installed_app_restarted": False,
        "host_macos": output(["sw_vers"], cwd=repository),
        "architecture": platform.machine(),
        "developer_directory": output(["xcode-select", "-p"], cwd=repository),
        "swift": output(["xcrun", "swiftc", "--version"], cwd=repository),
        "sdk_path": output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], cwd=repository),
        "sdk_version": output(["xcrun", "--sdk", "macosx", "--show-sdk-version"], cwd=repository),
        "sdk_build": output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-build-version"], cwd=repository
        ),
        "configuration": "debug-strict-concurrency",
        "swift_scratch_path": str(build),
        "swift_test_scratch_path": test_location[test_location.index("--scratch-path") + 1],
        "swift_build_command": swift + ["build"] + location + strict,
        "macos_13_runtime_tested": False,
        "hardware_tested": False,
        "test_framework": str(test_frameworks / "Testing.framework"),
        "test_framework_build": output(
            [
                "xcrun",
                "vtool",
                "-show-build",
                str(test_frameworks / "Testing.framework" / "Testing"),
            ],
            cwd=repository,
        ),
    }
    native_sources = [package / "Package.swift"]
    native_sources.extend(path for path in (package / "Sources").rglob("*") if path.is_file())
    native_sources.extend(path for path in (package / "Tests").rglob("*") if path.is_file())
    source_digest = hashlib.sha256()
    for path in sorted(native_sources):
        source_digest.update(str(path.relative_to(package)).encode())
        source_digest.update(b"\0")
        source_digest.update(hashlib.sha256(path.read_bytes()).digest())
    identity["native_source_sha256"] = source_digest.hexdigest()
    executable_identities = {}
    for name in (
        "ColeVoiceRecorderPreview",
        "ColeVoiceRecorderHelper",
        "RecordingHarness",
        "RecordingBridgeHarness",
    ):
        executable = binary_directory / name
        deployment = output(["xcrun", "vtool", "-show-build", str(executable)], cwd=repository)
        if not re.search(r"\bminos\s+13\.0\b", deployment):
            raise RuntimeError(f"{name} does not declare the macOS 13.0 deployment floor")
        executable_identities[name] = {
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "deployment": deployment,
        }
    identity["executables"] = executable_identities
    if args.suite in {"contracts", "all"}:
        run(
            swift + ["test"] + test_location + test_flags + ["--filter", "RecordingContractsTests"],
            cwd=repository,
        )
        fixtures = artifacts / "contracts"
        python = repository / ".venv" / "bin" / "python"
        run(
            [
                *isolated_script(repository, "test_recording_contracts"),
                "--fixtures",
                str(fixtures),
            ],
            cwd=repository,
        )
        run([str(harness), "contracts", str(fixtures)], cwd=repository)
        python_check = """
import json
import sys
from pathlib import Path
from pydantic import ValidationError
from cole_voice_lab.recording_contracts import RECORDING_SCHEMA_MODELS
directory = Path(sys.argv[1])
fixtures = json.loads((directory / 'index.json').read_text())
for fixture in fixtures:
    try:
        payload = (directory / fixture['file']).read_bytes()
        RECORDING_SCHEMA_MODELS[fixture['model']].model_validate_json(payload)
        accepted = True
    except ValidationError:
        accepted = False
    if accepted != fixture['valid']:
        raise RuntimeError('Python/native conformance disagreement: ' + fixture['file'])
print(json.dumps({'python_checked_swift_encoded': len(fixtures)}))
"""
        run([str(python), "-I", "-B", "-c", python_check, str(fixtures / "swift")], cwd=repository)
    if args.suite in {"shell", "all"}:
        run(
            swift + ["test"] + test_location + test_flags + ["--filter", "RecordingRuntimeTests"],
            cwd=repository,
        )
        snapshots = artifacts / "shell"
        run([str(harness), "shell", str(snapshots)], cwd=repository)
        records = json.loads((snapshots / "geometry.json").read_text())
        baselines: dict[tuple[object, ...], dict[str, list[float]]] = {}
        maximum_delta = 0.0
        for record in records:
            key = tuple(
                record[field]
                for field in (
                    "width",
                    "height",
                    "scale",
                    "appearance",
                    "scriptSize",
                    "focusMode",
                    "roomTone",
                )
            )
            baseline = baselines.setdefault(key, record["regions"])
            for name, frame in record["regions"].items():
                maximum_delta = max(
                    maximum_delta,
                    *(
                        abs(actual - initial)
                        for actual, initial in zip(frame, baseline[name], strict=True)
                    ),
                )
        identity["shell_geometry_cases"] = len(records)
        identity["maximum_transport_region_delta_points"] = maximum_delta
    if args.suite in {"capture", "capture-sanitizers", "waveform", "all"}:
        variants = ["release"] if args.suite == "capture" else ["address", "thread"]
        if args.suite == "all":
            variants.insert(0, "release")
        for variant in variants:
            sanitizer = {
                "release": ["-O2"],
                "address": [
                    "-O1",
                    "-g",
                    "-fsanitize=address,undefined",
                    "-fno-omit-frame-pointer",
                ],
                "thread": ["-O1", "-g", "-fsanitize=thread", "-fno-omit-frame-pointer"],
            }[variant]
            executable = build / f"capture-buffer-tests-{variant}"
            run(
                [
                    "xcrun",
                    "clang",
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Wpedantic",
                    "-Werror",
                    "-mmacosx-version-min=13.0",
                ]
                + sanitizer
                + [
                    "-I",
                    str(package / "Sources/CaptureBuffer/include"),
                    str(package / "Sources/CaptureBuffer/CaptureBuffer.c"),
                    str(package / "Tests/CaptureBufferTests.c"),
                    "-framework",
                    "AudioToolbox",
                    "-o",
                    str(executable),
                ],
                cwd=repository,
            )
            run([str(executable)], cwd=repository)
            capture_evidence = artifacts / f"capture-{variant}"
            capture_evidence.mkdir(mode=0o700)
            capture_location = test_location.copy()
            capture_flags = test_flags.copy()
            if variant != "release":
                capture_location[-1] = str(
                    native_recording_scratch_path(
                        repository / f"build/native-recording-tests-{variant}",
                        repository=repository,
                    )
                )
                capture_flags.extend(["--sanitize", variant])
            run(
                swift
                + ["test"]
                + capture_location
                + capture_flags
                + [
                    "--filter",
                    "RecordingWaveformTests|NativeCaptureTests|SyntheticCaptureBackendTests"
                    if args.suite in {"waveform", "all"}
                    else "NativeCaptureTests|SyntheticCaptureBackendTests",
                ],
                cwd=repository,
                env={
                    **os.environ,
                    "COLE_VOICE_CAPTURE_TEST_EVIDENCE_ROOT": str(capture_evidence),
                },
            )
            python_check_capture = """
import json
import sys
from pathlib import Path
from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.recording_contracts import NativeRecoveryReceipt
directory = Path(sys.argv[1])
records = []
for name in ('first-take', 'second-take'):
    receipt = NativeRecoveryReceipt.model_validate_json(
        (directory / name / 'native-recovery.json').read_bytes())
    seal = receipt.seal
    if seal is None or receipt.state != 'sealed' or receipt.error is not None:
        raise RuntimeError('Native capture did not produce a successful seal')
    inspection = inspect_canonical_wav(directory / name / 'original.wav')
    if (inspection.audio != seal.audio or inspection.sha256 != seal.original_sha256
            or inspection.size_bytes != seal.original_size_bytes):
        raise RuntimeError('Independent Python WAV inspection disagrees with native seal')
    records.append({'take_id': receipt.take_id, 'frames': inspection.audio.frames,
                    'original_sha256': inspection.sha256, 'status': 'passed'})
report = {'synthetic_only': True, 'native_receipts': records}
(directory / 'python-validation.json').write_text(json.dumps(report, indent=2) + '\\n')
print(json.dumps(report))
"""
            run(
                [
                    str(repository / ".venv/bin/python"),
                    "-I",
                    "-B",
                    "-c",
                    python_check_capture,
                    str(capture_evidence),
                ],
                cwd=repository,
            )
        identity["capture_variants"] = variants
    if args.suite in {"bridge", "all"}:
        run(
            swift
            + ["test"]
            + test_location
            + test_flags
            + ["--filter", r"^RecordingBridgeTests\.RecordingBridgeTests/"],
            cwd=repository,
        )
        # Process footprint must be sampled without unrelated test allocations.
        run(
            swift
            + ["test"]
            + test_location
            + test_flags
            + ["--filter", "RecordingProcessChannelResourceTests"],
            cwd=repository,
            env={
                **os.environ,
                "COLE_VOICE_LAB_TEST_HELPER_EXECUTABLE": str(
                    binary_directory / "ColeVoiceRecorderHelper"
                ),
            },
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = (
            str(repository / "tests") + os.pathsep + environment.get("PYTHONPATH", "")
        )
        bridge = artifacts / "bridge"
        subprocess.run(
            [
                *isolated_script(repository, "verify_native_bridge"),
                "--package",
                str(package),
                "--binary-directory",
                str(binary_directory),
                "--output",
                str(bridge),
            ],
            cwd=repository,
            env=environment,
            check=True,
            timeout=60,
        )
        subprocess.run(
            [
                *isolated_script(repository, "verify_native_bridge_failures"),
                str(bridge / "bridge.json"),
            ],
            cwd=repository,
            env=environment,
            check=True,
            timeout=60,
        )
        identity["bridge"] = json.loads((bridge / "bridge-result.json").read_text())
        identity["bridge_failures"] = json.loads((bridge / "bridge-failures.json").read_text())
        subprocess.run(
            [
                *isolated_script(repository, "verify_native_helper_telemetry"),
                str(bridge / "bridge.json"),
            ],
            cwd=repository,
            env=environment,
            check=True,
            timeout=60,
        )
        identity["helper_telemetry"] = json.loads((bridge / "helper-telemetry.json").read_text())
    if args.suite in {"waveform", "all"}:
        run(
            swift
            + ["test"]
            + test_location
            + test_flags
            + [
                "--filter",
                "RecordingWaveformTests|NativeCaptureTests|SyntheticCaptureBackendTests",
            ],
            cwd=repository,
        )
        waveform = artifacts / "waveform"
        run(
            [str(binary_directory / "RecordingWaveformHarness"), str(waveform)],
            cwd=repository,
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(repository / "tests"), environment.get("PYTHONPATH", "")]
        )
        subprocess.run(
            [
                *isolated_script(repository, "verify_native_waveform"),
                "--package",
                str(package),
                "--binary-directory",
                str(binary_directory),
                "--output",
                str(waveform / "process"),
            ],
            cwd=repository,
            env=environment,
            check=True,
            timeout=30,
        )
        subprocess.run(
            [
                *isolated_script(repository, "verify_native_waveform"),
                "--package",
                str(package),
                "--binary-directory",
                str(binary_directory),
                "--output",
                str(waveform / "process-cache-failure"),
                "--archive-failure",
            ],
            cwd=repository,
            env=environment,
            check=True,
            timeout=30,
        )
        identity["waveform_process_cache_failure"] = json.loads(
            (waveform / "process-cache-failure/waveform-process-result.json").read_text()
        )
        identity["waveform"] = json.loads((waveform / "waveform-result.json").read_text())
        identity["waveform_process"] = json.loads(
            (waveform / "process/waveform-process-result.json").read_text()
        )
    if args.suite in {"packaged-app", "all"}:
        rollback = os.environ.get("COLE_VOICE_ROLLBACK_RELEASE")
        identity["packaged_app_rollback_release_present"] = (
            Path(rollback).expanduser().is_dir() if rollback else False
        )
    if args.suite in {"recovery", "all"}:
        recovery = artifacts / "recovery"
        identity["recovery_crash"] = json.loads(
            (recovery / "native-crash/recovery-report.json").read_text()
        )
        identity["recovery_restart"] = json.loads(
            (recovery / "native-restart/recovery-restart-report.json").read_text()
        )
        identity["recovery_python"] = json.loads(
            (recovery / "native-restart/python-recovery-evidence.json").read_text()
        )
    if args.suite in {"finetune", "all"}:
        finetune = artifacts / "finetune"
        identity["finetune"] = json.loads((finetune / "native/finetune-report.json").read_text())
        identity["finetune_unavailable"] = json.loads(
            (finetune / "native-unavailable/finetune-unavailable-report.json").read_text()
        )
        identity["finetune_python"] = json.loads(
            (finetune / "python-finetune-evidence.json").read_text()
        )
    if args.suite in {"speak", "all"}:
        speak = artifacts / "speak"
        identity["speak"] = json.loads((speak / "native/speak-report.json").read_text())
        identity["speak_python"] = json.loads((speak / "python-speak-evidence.json").read_text())
    if args.suite in {"vault", "all"}:
        vault = artifacts / "vault"
        identity["vault"] = json.loads((vault / "native/vault-report.json").read_text())
        identity["vault_python"] = json.loads((vault / "python-vault-evidence.json").read_text())
    if args.suite in {"acceptance", "all"}:
        # The state inventory is checked from the rendered files themselves, so a phase
        # that stopped being drawn is a missing artifact rather than a silent gap.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from recording_acceptance_matrix import (
            build_matrix,
            expected_state_snapshots,
            missing_state_snapshots,
            render_markdown,
        )

        snapshots = artifacts / "shell"
        missing = missing_state_snapshots(snapshots)
        if missing:
            raise RuntimeError("Acceptance state snapshots are missing: " + ", ".join(missing))
        identity["state_snapshot_inventory"] = expected_state_snapshots(snapshots)

    (artifacts / "verification.json").write_text(
        json.dumps(identity, indent=2, sort_keys=True) + "\n"
    )
    # The matrix is built from the identity that was just written, not from the one still
    # in memory: it must read the same durable evidence a later reader would read, and
    # reading it earlier silently reported every verified row as unverified.
    if args.suite in {"acceptance", "all"} and args.benchmark_report is not None:
        assert benchmark is not None
        matrix = build_matrix(benchmark, artifacts)
        (artifacts / "acceptance-matrix.json").write_text(
            json.dumps(matrix, indent=2) + "\n", encoding="utf-8"
        )
        (artifacts / "acceptance-matrix.md").write_text(render_markdown(matrix), encoding="utf-8")
        print(
            json.dumps(
                {
                    "event": "acceptance_matrix",
                    "outstanding": len(matrix["outstanding"]),
                    "acceptance_closed": matrix["acceptance_closed"],
                }
            )
        )
    print(json.dumps({"result": "passed", "suite": requested_suite, "artifacts": str(artifacts)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
