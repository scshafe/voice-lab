"""Drive the real release native stack for the acceptance rows measurable without a display.

The three configured processes are the production ones: the release Swift client, the
release helper with explicitly injected synthetic PCM, and the real Python recording
service. Nothing here opens a microphone, emits audio, transfers a recording, or touches
an installed application. Every number this module returns names the boundary it timed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recording_fixtures import REPOSITORY_ROOT, require_external_private_root
from recording_native_audit import audit_site, read_audit
from recording_native_client_audit import audited_native_samples
from test_recording_contracts import synthetic_contracts  # noqa: F401  (receipt template)
from test_recording_takes import DEVICE, Rig

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.corpus_store import read_catalog_snapshot
from cole_voice_lab.models import PROMPT_SUITE_V2, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_analysis import (
    RecordingAnalysisQueue,
    RecordingAnalysisSupervisor,
)
from cole_voice_lab.recording_contracts import (
    RECORDING_SCHEMA_MODELS,
    CaptureReservation,
    CaptureSealReceipt,
    CatalogReference,
    OpenContextCommand,
    RecordingBuildIdentity,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_library import RecordingLibrary
from cole_voice_lab.recording_takes import RecordingTakeStore

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
from recording_native_scratch import native_recording_scratch_path  # noqa: E402
from verify_native_workspace import seed_active_catalog  # noqa: E402

SAMPLE_RATE_HZ = 48_000
CAPTURE_EVIDENCE_VERSION = "cole-voice-lab.recording-benchmark-capture/v2"
EXACT_FRAMES = "exact-frames"
MAXIMUM_DURATION_LIMIT = "maximum-duration-limit"
# The benchmark hosts the workspace at its fixed size; a drift this large is impossible
# in a passing run and is used to report a region that stopped being measured at all.
WINDOW_WIDTH_POINTS = 1060.0
SUBJECT = "synthetic-subject"
PACKAGE = REPOSITORY_ROOT / "apps" / "macos"
# A scratch path of its own, so an unoptimized debug tree can never be mistaken for the
# release binaries an acceptance claim rests on.
RELEASE_SCRATCH = REPOSITORY_ROOT / "build" / "native-recording-release"
STRICT = ["-Xswiftc", "-strict-concurrency=complete", "-Xswiftc", "-warnings-as-errors"]


class NativeBenchmarkUnsafeShutdown(RuntimeError):
    """A surviving fixture worker makes every subsequent case unsafe to measure."""

    def __init__(self, failures: tuple[BaseException, ...]) -> None:
        self.failures = failures
        super().__init__("Native fixture worker did not join; subsequent cases must not run")


def release_binaries() -> dict[str, Any]:
    """Build the configured release products and record exactly what was built.

    Sanitizers are never requested here: an instrumented binary's timings are not the
    release behavior this gate is about, and the identity says so explicitly so a report
    cannot be read as release evidence unless it actually is.
    """

    scratch = native_recording_scratch_path(RELEASE_SCRATCH, repository=REPOSITORY_ROOT)
    location = ["--package-path", str(PACKAGE), "--scratch-path", str(scratch)]
    command = ["xcrun", "swift", "build", "-c", "release", *location, *STRICT]
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)
    directory = Path(
        subprocess.run(
            ["xcrun", "swift", "build", "-c", "release", *location, "--show-bin-path"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if directory.name != "release":
        raise RuntimeError("The release build did not resolve a release binary directory")
    harness = directory / "RecordingHarness"
    helper = directory / "ColeVoiceRecorderHelper"
    swift_version = subprocess.run(
        ["xcrun", "swift", "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return {
        "binary_directory": str(directory),
        "swift_scratch_path": str(scratch),
        "swift_configuration": "release",
        "sanitizers": [],
        "swift_build_command": command,
        "swift_version": swift_version,
        "client_sha256": sha256_file(harness)[0],
        "recorder_sha256": sha256_file(helper)[0],
        "client_mach_o": subprocess.run(
            ["vtool", "-show-build", str(harness)], capture_output=True, text=True, check=False
        ).stdout.strip(),
    }


def benchmark_catalog(prompt_count: int) -> PromptSuite:
    """Prompt identities only; no instruction text reaches a trace or a report.

    A prompt-suite/v2 room tone followed by spoken prompts, because the service serves the
    fixture's corpus store and the store publishes only v2 suites whose live texts differ.
    Raises: ``ValueError`` when ``prompt_count`` leaves no room for a room tone and an
    utterance; ``pydantic.ValidationError`` when the suite breaks the PromptSuite contract.
    """

    if prompt_count < 2:
        raise ValueError("benchmark requires a room tone and an utterance")
    return PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="synthetic-recording-benchmark-v1",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id="synthetic-0000",
                instructions="Synthetic benchmark instructions.",
                category="room-tone",
                style="Synthetic",
                expected_seconds=10.0,
            ),
            *(
                Prompt(
                    prompt_id=f"synthetic-{index:04d}",
                    text=f"Synthetic benchmark instructions {index}.",
                    category="neutral",
                    style="Synthetic",
                    expected_seconds=10.0,
                )
                for index in range(1, prompt_count)
            ),
        ),
    )


def native_pcm(signal: str, frames: int = 480) -> bytes:
    """One exact period of bounded 24-bit mono PCM for the helper's synthetic input."""

    import math

    if signal == "silence":
        return bytes(frames * 3)
    if signal != "sine":
        raise ValueError("benchmark PCM is generated only as a bounded sine or as silence")
    return b"".join(
        round(2_000_000 * math.sin(index * math.tau / frames)).to_bytes(3, "little", signed=True)
        for index in range(frames)
    )


def fabricate_history(layout: AssetLayout, reference: CatalogReference, count: int) -> list[str]:
    """Write `count` real durable takes through the production store and library.

    These are the same canonical records the service reads back, so a thousand-take
    history is an actual thousand-take history rather than a padded index. The context
    names ``reference``, the store version the service will pin, exactly as the app's does.
    Raises: ``CorpusSnapshotSecurityError`` (``CORPUS.SNAPSHOT_MISSING`` /
    ``CORPUS.SNAPSHOT_MISMATCH``) when the referenced snapshot is absent or altered; the
    take store's and index's own errors when a fabricated take is refused.
    """

    if count == 0:
        return []
    store = RecordingTakeStore(layout)
    rig = Rig(layout, store, "")
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    index = RecordingIndex(layout, subject_id=SUBJECT)
    take_ids: list[str] = []
    try:
        snapshot = read_catalog_snapshot(layout, reference)
        index.register_catalog(snapshot)
        # Save and Next resolves the next missing prompt, which needs the catalog's own
        # metadata to be complete before the first fabricated take, not after the last.
        index.set_completeness(reference.catalog_sha256, "complete")
        library = RecordingLibrary(layout, snapshot, SUBJECT)
        library.bind_native_store(rig.store, index)
        context = rig.store.open_context(
            OpenContextCommand(
                operation_id="benchmark-context",
                subject_id=SUBJECT,
                catalog_reference=reference,
                capture_device=DEVICE,
                capture_host="synthetic-host",
            )
        )
        rig.group_id = context.group.group_id
        for ordinal in range(count):
            cursor = rig.store.get_cursor(SUBJECT)
            assert cursor is not None
            reservation = rig.store.reserve(
                operation_id=f"benchmark-reserve-{ordinal}",
                group_id=cursor.group_id,
                prompt_id=cursor.selection.prompt_id,
                process_generation="helper-generation",
                selection=cursor.selection,
            )
            take = rig.seal(reservation, f"benchmark-seal-{ordinal}")
            # The sealed draft is what the cursor now displays; Save must name that
            # selection, not the one that was current before the take existed.
            sealed = rig.store.get_cursor(SUBJECT)
            assert sealed is not None
            library.save_native_take(
                rig.store,
                rig.command(
                    take,
                    f"benchmark-save-{ordinal}",
                    selection=sealed.selection,
                    advance=True,
                ),
            )
            take_ids.append(take.reservation.take_id)
    finally:
        index.close()
    return take_ids


def unanalyzed_take_count(layout: AssetLayout) -> int:
    """Takes with no terminal analysis state, which is the work the service will face.

    Saving a take does not itself enqueue analysis; the queue learns about unanalyzed
    takes when it reconciles. Counting the queue would therefore report an empty backlog
    for a library of a thousand unchecked takes, which is the opposite of the truth.
    """

    terminal = {"verified", "needs-review", "invalid"}
    total = 0
    for head in (layout.root / "provenance/recording-takes").glob("take-*/head.json"):
        try:
            total += json.loads(head.read_text(encoding="utf-8")).get("analysis") not in terminal
        except (OSError, ValueError):
            total += 1
    return total


def settle_analysis(layout: AssetLayout, *, drain: bool, timeout: float = 900.0) -> int:
    """Prove idle fixture settlement, or report the deliberate competing backlog.

    A worker failure or deadline is fixture failure, never an idle design point.
    Queue/index ownership ends only after the supervisor confirms its threads joined.
    """

    store = RecordingTakeStore(layout)
    index = RecordingIndex(layout, subject_id=SUBJECT)
    queue: RecordingAnalysisQueue | None = None
    supervisor: RecordingAnalysisSupervisor | None = None
    errors: list[BaseException] = []
    result: int | None = None
    joined = True
    try:
        queue = RecordingAnalysisQueue(store, index)

        def check_worker() -> None:
            if supervisor is not None and supervisor.fatal_error is not None:
                raise supervisor.fatal_error

        def pending() -> int:
            """Count unfinished jobs across the whole queue, page by bounded page."""
            assert queue is not None
            total, sequence = 0, 0
            while True:
                check_worker()
                page = queue.index.analysis_jobs(after_sequence=sequence, limit=200)
                check_worker()
                if not page:
                    return total
                total += sum(
                    json.loads(payload)["status"] in {"pending", "running", "stale"}
                    for _, payload in page
                )
                sequence = page[-1][0]

        if not drain:
            result = unanalyzed_take_count(layout)
        else:
            supervisor = RecordingAnalysisSupervisor(queue)
            supervisor.start()
            deadline = time.monotonic() + timeout
            while True:
                check_worker()
                unfinished, unanalyzed = pending(), unanalyzed_take_count(layout)
                check_worker()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "Idle native benchmark fixture exceeded its analysis deadline "
                        f"({timeout}s): {unfinished} unfinished jobs, {unanalyzed} unanalyzed takes"
                    )
                # Both conditions matter: a zero queue can precede reconciliation,
                # and zero unanalyzed takes can coexist with unfinished queue jobs.
                if unfinished == 0 and unanalyzed == 0:
                    result = 0
                    break
                time.sleep(min(0.25, remaining))
    except BaseException as error:
        errors.append(error)
    finally:
        if supervisor is not None:
            try:
                supervisor.close()
            except BaseException as error:
                joined = False
                errors.append(error)
            # A worker can fail after the final empty check or during shutdown.
            fatal = supervisor.fatal_error
            if fatal is not None and all(fatal is not error for error in errors):
                errors.append(fatal)
        if joined and drain and result == 0 and not errors:
            try:
                unfinished, unanalyzed = pending(), unanalyzed_take_count(layout)
                if unfinished or unanalyzed:
                    raise RuntimeError(
                        "Idle native benchmark fixture retained work after analysis shutdown: "
                        f"{unfinished} unfinished jobs, {unanalyzed} unanalyzed takes"
                    )
            except BaseException as error:
                errors.append(error)
        if joined:
            for resource in (queue, index):
                if resource is not None:
                    try:
                        resource.close()
                    except BaseException as error:
                        errors.append(error)
        # A failed join deliberately retains queue/index handles. Closing a shared
        # SQLite handle under an unfinished publication can crash the whole runner.
    if not joined:
        raise NativeBenchmarkUnsafeShutdown(tuple(errors)) from BaseExceptionGroup(
            "native fixture initiating and shutdown failures", errors
        )
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("native fixture analysis and shutdown failed", errors)
    assert result is not None
    return result


def build_native_fixture(
    root: Path,
    *,
    binaries: dict[str, Any],
    history_count: int,
    cycles: int,
    take_seconds: float,
    signal: str,
    workload: str,
) -> dict[str, Any]:
    """Create one private external fixture whose every input is declared and bounded."""

    require_external_private_root(root)
    frames = int(round(take_seconds * SAMPLE_RATE_HZ))
    if not 1 <= frames <= 28_800_000:
        raise ValueError("a benchmark take must be between one frame and ten minutes")
    assets = AssetLayout.initialize(root / "assets")
    schemas = root / "schemas"
    schemas.mkdir(mode=0o700)
    for name, model in RECORDING_SCHEMA_MODELS.items():
        write_canonical_json(schemas / f"{name}.schema.json", model.model_json_schema())
    # Room tone, the fabricated history, every recorded cycle and one spare target.
    catalog = benchmark_catalog(history_count + cycles + 2)
    # The configuration names no catalog: the service pins the store's active version.
    reference = seed_active_catalog(assets, catalog)
    take_ids = fabricate_history(assets, reference, history_count)
    backlog = settle_analysis(assets, drain=workload == "idle")

    audit_log = root / "wav-opens.log"
    site = audit_site(root / "audit-site", assets.root, audit_log)
    directory = Path(binaries["binary_directory"])
    helper, harness = directory / "ColeVoiceRecorderHelper", directory / "RecordingHarness"
    source_identity = canonical_sha256(
        [
            (str(path.relative_to(PACKAGE)), sha256_file(path)[0])
            for path in sorted((PACKAGE / "Sources").rglob("*"))
            if path.is_file()
        ]
    )
    native_environment = canonical_sha256(
        {"swift": binaries["swift_version"], "configuration": "release", "synthetic": True}
    )
    recorder_build = RecordingBuildIdentity(
        build_id="release-benchmark-helper",
        source_sha256=source_identity,
        executable_sha256=binaries["recorder_sha256"],
        environment_sha256=native_environment,
    )
    client_build = RecordingBuildIdentity(
        build_id="release-benchmark-ui",
        source_sha256=source_identity,
        executable_sha256=binaries["client_sha256"],
        environment_sha256=native_environment,
    )
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # The audit hook must load in the service and in every analysis worker it spawns.
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(site), *([os.environ["PYTHONPATH"]] if "PYTHONPATH" in os.environ else [])]
    )
    identity_data = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cole_voice_lab import recording_service; "
            "print(recording_service.recording_service_build_identity().model_dump_json())",
        ],
        env=environment,
        capture_output=True,
        check=True,
    ).stdout
    service_build = RecordingBuildIdentity.model_validate_json(identity_data)
    # The identity preflight is the first real Python startup under this hook.
    # Reject missing/unknown controls now, before spending a native capture run.
    read_audit(audit_log)
    for name, identity in (
        ("recorder", recorder_build),
        ("client", client_build),
        ("service", service_build),
    ):
        write_canonical_json(root / f"{name}-build.json", identity)
    pcm = root / "pattern.pcm"
    pcm.write_bytes(native_pcm(signal))
    # The helper refuses a synthetic source any other account can read, so state the mode
    # rather than inheriting whatever umask the caller happened to run with.
    pcm.chmod(0o600)
    configuration = {
        "schema_version": "cole-voice-lab.recording-application/v2",
        "asset_root": str(assets.root),
        "schema_directory": str(schemas),
        "client_build": client_build.model_dump(mode="json"),
        "client_build_path": str(root / "client-build.json"),
        "purpose": catalog.purpose,
        "subject_id": SUBJECT,
        "capture_device": DEVICE.model_dump(mode="json"),
        "capture_host": "synthetic-host",
        "pilot_approval_path": None,
        "pilot_approval_sha256": None,
        "recorder": {
            "executable": str(helper),
            "arguments": [
                "--synthetic-pcm",
                str(pcm),
                "--synthetic-chunk-frames",
                "480",
                "--synthetic-total-frames",
                str(frames),
            ],
            "environment": environment,
            "build": recorder_build.model_dump(mode="json"),
            "build_identity_path": str(root / "recorder-build.json"),
        },
        "service": {
            "executable": str(Path(sys.executable).absolute()),
            "arguments": [
                "-m",
                "cole_voice_lab.recording_service",
                "--native-workspace",
                "--subject-id",
                SUBJECT,
            ],
            "environment": environment,
            "build": service_build.model_dump(mode="json"),
            "build_identity_path": str(root / "service-build.json"),
        },
    }
    config_file = root / "recording-application.json"
    write_canonical_json(config_file, configuration)
    return {
        "configuration": config_file,
        "harness": harness,
        # The library size this fixture actually built, counted from durable evidence
        # rather than taken from the number the design point asked for.
        "durable_take_count": sum(
            1
            for path in (assets.root / "provenance/recording-takes").iterdir()
            if path.is_dir() and path.name.startswith("take-")
        ),
        "asset_root": assets.root,
        "audit_log": audit_log,
        "frames": frames,
        "fabricated_take_ids": take_ids,
        # Takes the service must still check when it starts. Zero for an idle run by
        # construction, and the real competing workload for the other.
        "unanalyzed_takes_at_start": backlog,
    }


# Operations the row "historical audio inspections requested by start/poll/stop/save"
# actually covers. Navigation and snapshot reads are judged by the same rule because the
# row's own target says unchanged state reads open no audio either.
COMMAND_OPERATIONS = (
    "record_to_first_synthetic_confirmed_frame",
    "capture_poll_window",
    "stop_model_transition",
    "stop_to_sealed_review",
    "save_next_durable_acknowledgment",
    "plain_save_durable_acknowledgment",
    "metadata_snapshot_at_1000",
    "next_missing_lookup",
)

HISTORICAL_INSPECTION_BOUNDARY = (
    "Every WAV open under the asset root by the recording service process itself, "
    "attributed by the shared uptime clock to the start, poll, stop, save, snapshot and "
    "next-missing windows, together with actual native source-open/read/decode requests "
    "in those windows. Fixed command-issue source exemptions apply; descriptor holdings "
    "are diagnostic only. Source and process-role positive controls are required. "
    "Analysis workers are separate processes and their "
    "background reads are excluded by process role, as the design requires. The take being "
    "captured and the operator-selected review source are excluded by the row itself."
)


def historical_inspections(
    samples: list[dict[str, Any]], audit: list[dict[str, Any]]
) -> dict[str, Any]:
    """Count audio reads the interactive commands themselves requested."""

    source_audit = [entry for entry in audit if not entry.get("control", False)]
    service = [entry for entry in source_audit if entry["role"] == "service"]
    windows: list[tuple[int, int]] = []
    offenders: list[dict[str, Any]] = []
    for sample in samples:
        if sample["operation"] not in COMMAND_OPERATIONS:
            continue
        windows.append((sample["started_uptime_ns"], sample["ended_uptime_ns"]))
        own = sample.get("take_original_path")
        service_offenders = sample.get("service_historical_inspection_events")
        if service_offenders is None:
            # Legacy report derivation only. native_case_result requires actual event
            # controls before any current case may feed an acceptance sample sink.
            service_offenders = [
                entry
                for entry in service
                if sample["started_uptime_ns"] <= entry["uptime_ns"] <= sample["ended_uptime_ns"]
                and entry["path"] != own
            ]
        offenders.extend(
            {"operation": sample["operation"], "path": entry["path"], "process": "service"}
            for entry in service_offenders
        )
        offenders.extend(
            {"operation": sample["operation"], "path": path, "process": "client"}
            for path in sample.get("client_opened_historical_paths", [])
        )
    return {
        "measured_boundary": HISTORICAL_INSPECTION_BOUNDARY,
        "value_count": len(offenders),
        "sample_count": sum(sample["operation"] in COMMAND_OPERATIONS for sample in samples),
        "offenders": offenders[:50],
        "audited_service_opens": len(service),
        "audited_worker_opens": sum(entry["role"] == "worker" for entry in source_audit),
        "startup_controls": [entry for entry in audit if entry.get("control", False)],
        # The positive control for the whole mechanism. A zero violation count means
        # something only if this run could place a cross-process open inside a window at
        # all; without it, a clock that did not line up would look exactly like success.
        "audited_opens_inside_windows": sum(
            any(start <= entry["uptime_ns"] <= end for start, end in windows)
            for entry in source_audit
        ),
        "command_windows": len(windows),
    }


def run_native_case(
    root: Path,
    *,
    binaries: dict[str, Any],
    history_count: int,
    take_seconds: float,
    signal: str = "sine",
    cycles: int = 1,
    workload: str = "idle",
    history_seconds: float = 10.0,
    sample_sink: list[dict[str, Any]] | None = None,
    timeout: float = 7200.0,
    completion_policy: str = EXACT_FRAMES,
) -> dict[str, Any]:
    """Run one release native case and return its samples beside its scalar observations."""

    if completion_policy not in (EXACT_FRAMES, MAXIMUM_DURATION_LIMIT) or (
        completion_policy == MAXIMUM_DURATION_LIMIT and take_seconds != 600.0
    ):
        raise RuntimeError("Native maximum-duration policy requires the explicit 600-second case")
    fixture = build_native_fixture(
        root,
        binaries=binaries,
        history_count=history_count,
        cycles=cycles,
        take_seconds=take_seconds,
        signal=signal,
        workload=workload,
    )
    destination = root / "native"
    # Streamed to files rather than captured, so a long case reports its own progress
    # while it runs instead of only after it ends.
    out_path, error_path = root / "harness-stdout.log", root / "harness-stderr.log"
    with out_path.open("w", encoding="utf-8") as out, error_path.open("w", encoding="utf-8") as err:
        completed = subprocess.run(
            [
                str(fixture["harness"]),
                "benchmark",
                str(fixture["configuration"]),
                str(cycles),
                completion_policy,
                str(destination),
            ],
            stdout=out,
            stderr=err,
            timeout=timeout,
            check=False,
        )
    out_path.chmod(0o600)
    error_path.chmod(0o600)
    report_path = destination / "benchmark-report.json"
    if completed.returncode != 0 or not report_path.exists():
        # A failed case contributes no samples to any verdict: partial data from a session
        # that fell over must never feed a gate. It is retained for diagnosis, and the
        # cycle the harness reached is named so the failure can be placed.
        reached = ""
        if report_path.exists():
            partial = json.loads(report_path.read_text(encoding="utf-8"))
            reached = (
                f" reached cycle {len(partial.get('timer_frame_agreement', []))}"
                f" of {partial.get('cycles')}; partial evidence at {report_path}"
            )
        raise RuntimeError(
            f"release native benchmark case failed ({completed.returncode}){reached}: "
            f"{error_path.read_text(encoding='utf-8').strip()[-800:]}"
        )
    native = json.loads(report_path.read_text(encoding="utf-8"))
    if native.get("failure") is not None:
        raise RuntimeError(f"release native benchmark case reported {native['failure']}")
    return native_case_result(
        native=native,
        audit=read_audit(fixture["audit_log"]),
        root=root,
        history_count=history_count,
        durable_take_count=fixture["durable_take_count"],
        unanalyzed_takes_at_start=fixture["unanalyzed_takes_at_start"],
        take_seconds=take_seconds,
        signal=signal,
        workload=workload,
        history_seconds=history_seconds,
        sample_sink=sample_sink,
        requested_cycles=cycles,
        completion_policy=completion_policy,
    )


def validate_capture_cycle(
    agreement: dict[str, Any],
    boundary: dict[str, Any],
    *,
    completion_policy: str,
    expected_frames: int,
    recorder_build: dict[str, Any],
) -> bool:
    """Validate v2 final-review evidence; return only a proven maximum-limit shortfall.

    @canonical benchmark_capture_completion -- the case adapter and aggregate acceptance
    use this same rule. Harness wall times are observations, not controller deadline proof.
    """
    if not all(isinstance(value, dict) for value in (agreement, boundary, recorder_build)):
        raise RuntimeError("Native capture completion requires structured boundary evidence")
    if completion_policy not in (EXACT_FRAMES, MAXIMUM_DURATION_LIMIT):
        raise RuntimeError("Native capture completion policy is not recognized")
    fields = (
        "expected_frames",
        "timer_frames",
        "timer_frames_before_issue",
        "sealed_frames",
        "final_timer_frames",
        "final_waveform_frames",
        "selected_source_frames",
        "selected_range_start_frame",
        "selected_range_end_frame",
    )
    if any(type(agreement.get(field)) is not int or agreement[field] < 0 for field in fields):
        raise RuntimeError("Native frame evidence requires explicit nonnegative frame counts")
    if (
        agreement["expected_frames"] != expected_frames
        or expected_frames <= 0
        or boundary.get("expected_frames") != expected_frames
        or boundary.get("completion_policy") != completion_policy
    ):
        raise RuntimeError("Native capture policy or target differs from its declared design point")
    reached = agreement["sealed_frames"] == expected_frames
    for field in ("reached_configured_frames", "reached_exact_configured_frames"):
        if type(agreement.get(field)) is not bool:
            raise RuntimeError("Native frame evidence requires explicit completion")
        if agreement[field] != reached:
            raise RuntimeError("Native configured-frame completion contradicts sealed frames")
    if (
        agreement.get("final_timer_mode") != "initial-review-retained-duration"
        or agreement.get("final_timer_source") != "RecordingMediaTimer.durationFrames"
    ):
        raise RuntimeError("Native timer evidence does not name the actual initial-review duration")
    if agreement["timer_frames"] != agreement["timer_frames_before_issue"] or agreement[
        "timer_frames_before_issue"
    ] != boundary.get("timer_frames_before_issue"):
        raise RuntimeError("Native final timer evidence changed its original pre-issue observation")
    clocks = (
        boundary.get("capture_started_uptime_ns"),
        boundary.get("terminal_observed_uptime_ns"),
        agreement.get("final_timer_observed_uptime_ns"),
    )
    if any(type(clock) is not int or clock <= 0 for clock in clocks) or list(clocks) != sorted(
        clocks
    ):
        raise RuntimeError(
            "Native final timer evidence requires explicit ordered observation clocks"
        )
    try:
        reservation = CaptureReservation.model_validate_json(
            json.dumps(boundary.get("reservation"))
        )
        seal = CaptureSealReceipt.model_validate_json(json.dumps(boundary.get("seal")))
        build = RecordingBuildIdentity.model_validate_json(json.dumps(recorder_build))
    except ValueError as error:
        raise RuntimeError(
            "Native capture requires valid independent reservation and seal evidence"
        ) from error
    if (
        seal.take_id != reservation.take_id
        or seal.process_generation != reservation.process_generation
        or seal.reservation_sha256 != canonical_sha256(reservation)
        or boundary.get("reservation_sha256") != seal.reservation_sha256
        or seal.requested_at != reservation.requested_at
        or seal.continuity.device_uid != reservation.group.capture_device.device_id
        or seal.recorder_build != build
        or seal.audio.frames > reservation.maximum_frames
        or seal.audio.frames != agreement["sealed_frames"]
        or boundary.get("sealed_frames") != seal.audio.frames
        or boundary.get("terminal_reason") != seal.continuity.stop_reason
        or not seal.continuity.success
        or not seal.continuity.continuity_checked
        or seal.continuity.received_frames != seal.audio.frames
    ):
        raise RuntimeError(
            "Native capture seal does not bind its exact reservation, build and frame counts"
        )
    if (
        agreement.get("selected_take_id") != seal.take_id
        or agreement.get("final_waveform_take_id") != seal.take_id
        or agreement.get("final_waveform_generation") != seal.process_generation
        or agreement.get("selected_source_sha256") != seal.original_sha256
        or agreement.get("selected_source_path") != seal.original_path
        or agreement["selected_source_frames"] != seal.audio.frames
        or agreement["selected_range_start_frame"] != 0
        or agreement["selected_range_end_frame"] != seal.audio.frames
        or boundary.get("final_waveform_frames") != agreement["final_waveform_frames"]
    ):
        raise RuntimeError(
            "Native final timer source, waveform identity or initial full range is inconsistent"
        )
    exact = (
        agreement["final_timer_frames"] == agreement["final_waveform_frames"] == seal.audio.frames
    )
    if type(agreement.get("exact")) is not bool or agreement["exact"] != exact:
        raise RuntimeError("Native timer agreement contradicts actual final frame counts")
    stop = boundary.get("stop_attempt")
    if not isinstance(stop, dict) or stop.get("controller_acknowledgment_proven") is not False:
        raise RuntimeError("Native capture requires explicit local Stop boundary evidence")
    for name in ("before", "at_issue", "after_call"):
        state = stop.get(name)
        if state is not None and (
            not isinstance(state, dict)
            or state.get("take_id") != seal.take_id
            or state.get("generation") != seal.process_generation
        ):
            raise RuntimeError("Native Stop boundary changed its take or generation")
    if boundary.get("wait_timed_out") is not False or boundary.get("wait_exit") == "timeout":
        raise RuntimeError("Native capture timed out before its declared completion")
    expected_wait_exit = (
        "frame_target_observed"
        if agreement["timer_frames_before_issue"] >= expected_frames
        else "terminal_before_target"
    )
    if boundary.get("wait_exit") != expected_wait_exit:
        raise RuntimeError("Native capture wait outcome contradicts its retained frame observation")
    if completion_policy == EXACT_FRAMES:
        if not all(
            isinstance(stop.get(name), dict) for name in ("before", "at_issue", "after_call")
        ):
            raise RuntimeError("Native exact-frame capture lacks complete explicit Stop states")
        if not reached:
            raise RuntimeError(
                "Exact-frame capture sealed fewer or more frames than its declared target"
            )
        if (
            stop.get("model_stop_called") is not True
            or stop.get("model_stop_accepted") is not True
            or stop.get("reason") != "model_stop_accepted"
            or type(stop.get("issued_uptime_ns")) is not int
            or not clocks[0] <= stop["issued_uptime_ns"] <= clocks[2]
            or stop.get("before", {}).get("can_stop") is not True
            or stop.get("at_issue", {}).get("can_stop") is not True
            or stop.get("after_call", {}).get("phase") != "stopping"
            or stop.get("after_call", {}).get("can_stop") is not False
        ):
            raise RuntimeError("Native exact-frame capture lacks an accepted explicit Stop")
        return False
    if (
        expected_frames != 28_800_000
        or reservation.maximum_frames != expected_frames
        or seal.continuity.stop_reason != "maximum-duration"
        or not exact
        or stop.get("issued_uptime_ns") is not None
        or stop.get("model_stop_called") is not False
        or stop.get("model_stop_accepted") is not False
        or stop.get("reason") != "maximum_duration_policy_no_stop"
        or stop.get("at_issue") is not None
        or stop.get("after_call") is not None
        or not isinstance(stop.get("before"), dict)
        or stop["before"].get("can_stop") is not False
        or stop["before"].get("phase") not in ("stopping", "draftReview")
    ):
        raise RuntimeError(
            "Native maximum-duration completion lacks a valid automatic hard-limit boundary"
        )
    return not reached


def native_case_result(
    *,
    native: dict[str, Any],
    audit: list[dict[str, Any]],
    root: Path,
    history_count: int,
    durable_take_count: int,
    unanalyzed_takes_at_start: int,
    take_seconds: float,
    signal: str,
    workload: str,
    history_seconds: float = 10.0,
    sample_sink: list[dict[str, Any]] | None = None,
    requested_cycles: int | None = None,
    completion_policy: str = EXACT_FRAMES,
) -> dict[str, Any]:
    """Turn one case's harness report and audit into samples and scalar observations.

    Separated from running the case so the same rules apply whether the numbers are being
    produced now or re-derived later from the durable evidence a finished case left behind.
    """

    if native.get("failure") is not None:
        raise RuntimeError("Failed native case cannot contribute acceptance samples")
    if requested_cycles is not None and native.get("cycles") != requested_cycles:
        raise RuntimeError("Native report cycle count differs from the requested design point")
    if any(
        sample.get("operation") == "stop_reaches_controller" for sample in native.get("samples", [])
    ):
        raise RuntimeError("Native local Stop evidence cannot claim controller acknowledgment")
    if native.get("capture_evidence_version") != CAPTURE_EVIDENCE_VERSION:
        raise RuntimeError(
            "Current native acceptance requires explicit v2 final-review capture evidence"
        )
    if native.get("completion_policy") != completion_policy:
        raise RuntimeError(
            "Native capture completion policy differs from its declared design point"
        )
    boundaries = native.get("capture_boundaries")
    if not isinstance(boundaries, list) or not isinstance(
        native.get("timer_frame_agreement"), list
    ):
        raise RuntimeError(
            "Native capture requires exactly one boundary and final timer record per cycle"
        )
    expected_cycles = list(range(native["cycles"]))
    for rows in (boundaries, native["timer_frame_agreement"]):
        cycle_ids = [row.get("cycle") for row in rows if isinstance(row, dict)]
        if (
            len(cycle_ids) != len(rows)
            or any(type(cycle) is not int for cycle in cycle_ids)
            or sorted(cycle_ids) != expected_cycles
        ):
            raise RuntimeError(
                "Native cycle evidence is missing or duplicates a capture boundary/frame window"
            )
    boundary_by_cycle = {boundary["cycle"]: boundary for boundary in boundaries}
    sources = native.get("captured_sources")
    if not isinstance(sources, list) or len(sources) != native["cycles"]:
        raise RuntimeError("Native capture requires its independently inspected original sources")
    permitted_limit_evidence = []
    for entry in native["timer_frame_agreement"]:
        boundary = boundary_by_cycle[entry["cycle"]]
        permitted = validate_capture_cycle(
            entry,
            boundary,
            completion_policy=completion_policy,
            expected_frames=int(round(take_seconds * SAMPLE_RATE_HZ)),
            recorder_build=native.get("recorder_build"),
        )
        if (
            sum(
                isinstance(source, dict)
                and source.get("take_id") == entry["selected_take_id"]
                and source.get("source_sha256") == entry["selected_source_sha256"]
                and isinstance(source.get("source_path"), str)
                and source["source_path"].endswith("/" + entry["selected_source_path"])
                for source in sources
            )
            != 1
        ):
            raise RuntimeError("Native final timer does not bind its inspected selected source")
        if permitted:
            permitted_limit_evidence.append(
                {
                    "capture_evidence_version": CAPTURE_EVIDENCE_VERSION,
                    "agreement": entry,
                    "boundary": boundary,
                    "recorder_build": native["recorder_build"],
                }
            )
    counts = native.get("save_action_counts")
    if not isinstance(counts, dict) or (
        counts.get("completed_cycles") != native["cycles"]
        or counts.get("save_next_acknowledged", 0) + counts.get("plain_save_acknowledged", 0)
        != native["cycles"]
        or len(native["timer_frame_agreement"]) != native["cycles"]
    ):
        raise RuntimeError(
            "Native case did not prove every declared capture/Stop/durable-Save cycle"
        )
    save_samples = [
        sample
        for sample in native["samples"]
        if sample["operation"]
        in ("save_next_durable_acknowledgment", "plain_save_durable_acknowledgment")
    ]
    expected_cycles = list(range(native["cycles"]))
    for rows in (save_samples, native["timer_frame_agreement"]):
        cycle_ids = [row.get("cycle") for row in rows]
        if (
            any(type(cycle) is not int for cycle in cycle_ids)
            or sorted(cycle_ids) != expected_cycles
        ):
            raise RuntimeError("Native cycle evidence is missing or duplicates a Save/frame window")
    for operation in (
        "record_to_first_synthetic_confirmed_frame",
        "capture_poll_window",
        "stop_model_transition",
        "stop_to_sealed_review",
    ):
        required_operation_cycles = (
            []
            if completion_policy == MAXIMUM_DURATION_LIMIT
            and operation in ("stop_model_transition", "stop_to_sealed_review")
            else expected_cycles
        )
        cycle_ids = [
            sample.get("cycle") for sample in native["samples"] if sample["operation"] == operation
        ]
        if (
            any(type(cycle) is not int for cycle in cycle_ids)
            or sorted(cycle_ids) != required_operation_cycles
        ):
            raise RuntimeError(
                "Native cycle evidence is missing or duplicates a capture command window"
            )
        for sample in (sample for sample in native["samples"] if sample["operation"] == operation):
            boundary = boundary_by_cycle[sample["cycle"]]
            issued = (
                boundary["stop_attempt"]["issued_uptime_ns"]
                if operation.startswith("stop_")
                else boundary["capture_started_uptime_ns"]
            )
            if sample.get("started_uptime_ns") != issued:
                raise RuntimeError(
                    "Native command sample does not bind its recorded issue boundary"
                )
    for operation, counter in (
        ("save_next_durable_acknowledgment", "save_next_acknowledged"),
        ("plain_save_durable_acknowledgment", "plain_save_acknowledged"),
    ):
        if sum(sample["operation"] == operation for sample in save_samples) != counts[counter]:
            raise RuntimeError("Native Save counters differ from their actual command windows")
    for sample in native["samples"]:
        if (
            sample["operation"] == "save_next_durable_acknowledgment"
            and sample.get("advance_intent") is not True
        ):
            raise RuntimeError("A Save & Next sample lacks an actual advancing command intent")
        if (
            sample["operation"] == "plain_save_durable_acknowledgment"
            and sample.get("advance_intent") is not False
        ):
            raise RuntimeError("A plain Save sample lacks an actual nonadvancing command intent")
    # Validate the complete stream before adding even the first sample to a shared
    # report. A missing reader hook or mislabeled process cannot manufacture zero.
    native = native | {"samples": audited_native_samples(native, audit)}
    context = {
        "configured_history_level": history_count,
        "durable_take_count_at_start": durable_take_count,
        "take_seconds": take_seconds,
        "history_seconds": history_seconds,
        "signal": signal,
        "workload": workload,
        "capture_adapter": "synthetic_helper_pcm",
        "completion_policy": completion_policy,
        "backend": "native",
        "swift_configuration": "release",
    }
    samples = []
    for sample in native["samples"]:
        service_opens = sample["service_open_events"]
        samples.append(
            {
                **context,
                **sample,
                "historical_take_count": history_count + sample["cycle"],
                "wav_open_count": len(service_opens)
                + len(sample["client_opened_historical_paths"]),
                "wav_inspection_count": len(service_opens),
                "historical_wav_open_count": len(sample["service_historical_inspection_events"])
                + len(sample["client_opened_historical_paths"]),
                "trace_id": f"{root.name}-{sample['operation']}-{sample['cycle']}",
                "timestamp": datetime.now(UTC).isoformat(),
                "level": "info",
                "message": "release native recording command measurement",
                "service": "cole_voice_lab.recording_native_benchmark",
                "inspected_audio_seconds": 0.0,
                "events": [],
            }
        )
    agreement = native["timer_frame_agreement"]
    idle, peak = native["warm_idle_footprint"], native["peak_footprint"]
    # The row names the recorder and the waveform. That is the client and the helper; an
    # analysis worker is a separate process doing background work this row is not about,
    # and its footprint is reported beside the row rather than counted against it.
    growth = (peak["client_bytes"] + peak["recorder_bytes"]) - (
        idle["client_bytes"] + idle["recorder_bytes"]
    )
    observations = {
        "historical_audio_inspections": historical_inspections(native["samples"], audit),
        "timer_frame_agreement": {
            "measured_boundary": (
                "Initial review's retained-duration frame input from the actual timer view "
                "against the final waveform, full selected source and independent capture seal. "
                "The pre-Stop live timer is retained separately; no presented frame is claimed."
            ),
            "exact": bool(agreement) and all(entry["exact"] for entry in agreement),
            "sample_count": len(agreement),
            "disagreements": [entry for entry in agreement if not entry["exact"]],
        },
        "stable_layout": {
            "measured_boundary": (
                "The largest change in any reserved region's measured origin or size, in "
                "logical points, across every transport and background-processing state of "
                "the run at a fixed window size and settings. Measured on a realized but "
                "never presented hosting view, so no presented frame is claimed."
            ),
            # A region that stopped being measured is a layout failure, not zero drift.
            # It is reported as a whole window width of drift, which no bound tolerates and
            # which, unlike an infinity, is still a number this report can be written as.
            "value_points": WINDOW_WIDTH_POINTS
            if native.get("layout_missing_regions")
            else native["layout_max_drift_points"],
            "sample_count": native["layout_measurement_count"],
            "missing_regions": native.get("layout_missing_regions", []),
        },
        "recorder_memory_growth": {
            "measured_boundary": (
                "Peak combined physical footprint of the native client and the recorder "
                "helper over their warmed idle footprint before the first take. The service "
                "and its analysis workers are measured too, and reported beside this row "
                "rather than counted against a row about the recorder and the waveform."
            ),
            "value_bytes": growth,
            "sample_count": native["footprint_sample_count"],
            "warm_idle_footprint": idle,
            "peak_footprint": peak,
        },
    }
    result = {
        "samples": samples,
        "observations": observations,
        # A cycle whose take never reached the length the design point declares was not
        # measured at that design point, however good its numbers look.
        "short_take_cycles": sum(
            entry["sealed_frames"] != entry["expected_frames"] for entry in agreement
        ),
        "permitted_maximum_duration_limit_shortfall_cycles": len(permitted_limit_evidence),
        "maximum_duration_limit_evidence": permitted_limit_evidence,
        "capture_evidence_version": CAPTURE_EVIDENCE_VERSION,
        "native_report": native,
        "save_action_counts": counts,
        "fixture_root": str(root),
        "unanalyzed_takes_at_start": unanalyzed_takes_at_start,
        "warm_idle_footprint": idle,
        "history_count": native["history_count"],
    }
    # Failed cases contribute no samples: validate the entire scalar/result payload
    # before publishing its otherwise plausible command windows into a shared report.
    if sample_sink is not None:
        sample_sink.extend(samples)
    return result


NATIVE_SMOKE = (
    {"history_count": 4, "take_seconds": 0.5, "signal": "sine", "cycles": 3, "workload": "idle"},
)

# The document's design points. Latency is powered at each named library size and at one
# take length; the longer takes and the silence case are measured and reported separately,
# which is exactly how the document treats them.
NATIVE_ACCEPTANCE = (
    {"history_count": 0, "take_seconds": 1.0, "signal": "sine", "cycles": 100, "workload": "idle"},
    {"history_count": 4, "take_seconds": 1.0, "signal": "sine", "cycles": 100, "workload": "idle"},
    {"history_count": 36, "take_seconds": 1.0, "signal": "sine", "cycles": 100, "workload": "idle"},
    {
        "history_count": 1000,
        "take_seconds": 1.0,
        "signal": "sine",
        "cycles": 100,
        "workload": "idle",
    },
    {
        "history_count": 1000,
        "take_seconds": 1.0,
        "signal": "sine",
        "cycles": 100,
        "workload": "competing-analysis",
    },
    {
        "history_count": 36,
        "take_seconds": 1.0,
        "signal": "silence",
        "cycles": 20,
        "workload": "idle",
    },
    {"history_count": 36, "take_seconds": 10.0, "signal": "sine", "cycles": 20, "workload": "idle"},
    {"history_count": 36, "take_seconds": 30.0, "signal": "sine", "cycles": 20, "workload": "idle"},
    {"history_count": 36, "take_seconds": 120.0, "signal": "sine", "cycles": 3, "workload": "idle"},
    {
        "history_count": 36,
        "take_seconds": 600.0,
        "signal": "sine",
        "cycles": 1,
        "workload": "idle",
        "completion_policy": MAXIMUM_DURATION_LIMIT,
    },
)


def native_case_bytes(case: dict[str, Any]) -> int:
    """Declared fixture size for one case: every take is retained, nothing is deleted."""

    frame_bytes = 3 * SAMPLE_RATE_HZ
    return int(
        case["cycles"] * case["take_seconds"] * frame_bytes
        + case["history_count"] * (480 * 3 + 8192)
        + 64 * 1024 * 1024
    )


def indexed_footprint(result: dict[str, Any]) -> int:
    """Warmed idle memory of the processes that hold the index projection and its caches."""

    idle = result["warm_idle_footprint"]
    return int(idle["client_bytes"] + idle["service_bytes"])


def merge_observations(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Combine per-case observations into one verdict input, always toward the worse value."""

    if not results:
        return {}
    observations = [result["observations"] for result in results]
    inspections = [entry["historical_audio_inspections"] for entry in observations]
    agreements = [entry["timer_frame_agreement"] for entry in observations]
    layouts = [entry["stable_layout"] for entry in observations]
    memories = [entry["recorder_memory_growth"] for entry in observations]
    merged = {
        "historical_audio_inspections": {
            "measured_boundary": HISTORICAL_INSPECTION_BOUNDARY,
            # Any inspection in any case is a violation, so these add rather than average.
            "value_count": sum(entry["value_count"] for entry in inspections),
            "sample_count": sum(entry["sample_count"] for entry in inspections),
            "offenders": [item for entry in inspections for item in entry["offenders"]][:50],
            "audited_service_opens": sum(entry["audited_service_opens"] for entry in inspections),
            "audited_worker_opens": sum(entry["audited_worker_opens"] for entry in inspections),
            "audited_opens_inside_windows": sum(
                entry.get("audited_opens_inside_windows", 0) for entry in inspections
            ),
            "command_windows": sum(entry.get("command_windows", 0) for entry in inspections),
        },
        "timer_frame_agreement": {
            "measured_boundary": agreements[0]["measured_boundary"],
            "exact": all(entry["exact"] for entry in agreements),
            "sample_count": sum(entry["sample_count"] for entry in agreements),
            "disagreements": [item for entry in agreements for item in entry["disagreements"]],
        },
        "stable_layout": {
            "measured_boundary": layouts[0]["measured_boundary"],
            "value_points": max(entry["value_points"] for entry in layouts),
            "sample_count": sum(entry["sample_count"] for entry in layouts),
        },
        "recorder_memory_growth": {
            "measured_boundary": memories[0]["measured_boundary"],
            "value_bytes": max(entry["value_bytes"] for entry in memories),
            "sample_count": sum(entry["sample_count"] for entry in memories),
            "per_case_bytes": [entry["value_bytes"] for entry in memories],
        },
    }
    # The indexed-history row compares one library size against another, so it exists only
    # when this run actually measured both. It is never approximated from a single level.
    # Only the idle workload: a run with a competing analysis backlog is a different
    # measurement. Where a design point repeats, the largest large-library reading and the
    # smallest small-library one are taken, so repetition can only widen the reported
    # growth rather than let iteration order choose a flattering pair.
    idle: dict[int, list[int]] = {}
    for result in results:
        if result["case"].get("workload") == "idle":
            idle.setdefault(result["case"]["history_count"], []).append(indexed_footprint(result))
    if 1000 in idle and min(idle) < 1000:
        small_level = min(idle)
        large_bytes, small_bytes = max(idle[1000]), min(idle[small_level])
        merged["history_memory_growth"] = {
            "measured_boundary": (
                "Warmed idle physical footprint of the native client and the service, which "
                "are the processes holding the index projection and its caches, at a "
                "one-thousand-take library, over the same measurement at a "
                f"{small_level}-take library, both before any take is recorded."
            ),
            "value_bytes": large_bytes - small_bytes,
            "sample_count": len(idle[1000]) + len(idle[small_level]),
            "large_library_bytes": large_bytes,
            "small_library_bytes": small_bytes,
            "small_library_take_count": small_level,
        }
    return merged
