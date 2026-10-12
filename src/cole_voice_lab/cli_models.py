"""CLI commands of the model comparison: ``voice-lab families record|compare`` (D-049).

@canonical cli_models -- registered into ``voice-lab``'s parser by ``cli.build_parser``.
``families record`` writes one comparison record of a finished run (designated or a
candidate): its training summary, with ``--benchmark`` an inference benchmark through the
speech backend of the configuration's runtime (holding the GPU slot, under the consent record
``--consent-id`` names for ``private-home-host-synthesis`` on ``home-host``), and with
``--blind-review`` a link to the blind review that judged it, made from the score sheet
itself. ``families compare`` prints, per run, the newest training summary, benchmark and
review side by side, and what keeps them from comparing like with like.

A benchmark renders Cole's voice, so it renders only inside the speech unit's sandbox: no
network and only the GPU's devices. On the PC that is the oneshot unit
``voice-lab-speech-benchmark.service`` (deploy/host-manual/); the command refuses to
render in a process that has a network interface other than loopback unless
``--i-render-outside-the-sandbox`` says the operator accepts it (the record then says so).
Synthetic audio never leaves the run's scratch: the benchmark keeps digests and timings only.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from .canonical import open_regular_no_follow
from .errors import VoiceLabError
from .model_comparison import (
    MAX_SCORE_SHEET_BYTES,
    SCRATCH_PREFIX,
    ModelComparison,
    QualityLink,
    benchmark_run,
    blind_review_link,
    comparison_id,
    comparison_rows,
    comparison_warnings,
    read_comparisons,
    record_comparison,
    sweep_benchmark_scratch,
    training_summary,
)
from .models import utc_now
from .paths import AssetLayout
from .speech.config import load_speech_config
from .speech.contracts import GpuSlotHolder
from .speech.families import ReleaseBackend
from .speech.gpu import hold_slot, want_slot, withdraw_want

OUTSIDE_THE_SANDBOX: Final = "--i-render-outside-the-sandbox"
BENCHMARK_STATE: Final = "benchmark"
NET_DEVICES: Final = Path("/proc/self/net/dev")


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _layout(options: argparse.Namespace) -> AssetLayout:
    configured = options.asset_root or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")
    if not configured:
        raise VoiceLabError("an explicit --asset-root or COLE_VOICE_LAB_ASSET_ROOT is required")
    return AssetLayout.validate(Path(configured).expanduser())


def network_interfaces(path: Path = NET_DEVICES) -> tuple[str, ...]:
    """The network interfaces of this process's network namespace (``/proc/self/net/dev``,
    read without a socket: the speech units allow ``AF_UNIX`` only).

    Raises: ``OSError`` where there is no such file (macOS).
    """
    with open(path, encoding="ascii", errors="replace") as stream:
        lines = stream.read(1024 * 1024).splitlines()[2:]
    return tuple(sorted(line.split(":", 1)[0].strip() for line in lines if ":" in line))


def in_speech_sandbox(interfaces: Callable[[], tuple[str, ...]] | None = None) -> bool:
    """Whether this process has no network but loopback, as the speech units run
    (``PrivateNetwork=yes``); a host whose interfaces cannot be listed is not one."""
    try:
        return set((interfaces or network_interfaces)()) <= {"lo"}
    except OSError:
        return False


def _read_score_sheet(path: Path) -> bytes:
    with open_regular_no_follow(path.expanduser(), nonblocking=True) as stream:
        data = stream.read(MAX_SCORE_SHEET_BYTES + 1)
    if len(data) > MAX_SCORE_SHEET_BYTES:
        raise VoiceLabError(f"the score sheet exceeds {MAX_SCORE_SHEET_BYTES} bytes")
    return data


def _review(layout: AssetLayout, options: argparse.Namespace) -> QualityLink | None:
    given = (options.blind_review, options.score_sheet, options.score_sheet_sha256)
    if all(value is None for value in given):
        if options.review_candidate is not None:
            raise VoiceLabError("--review-candidate goes with --blind-review")
        return None
    if any(value is None for value in given):
        raise VoiceLabError("--blind-review, --score-sheet and --score-sheet-sha256 go together")
    return blind_review_link(
        layout,
        options.blind_review,
        _read_score_sheet(Path(options.score_sheet)),
        score_sheet_sha256=options.score_sheet_sha256,
        candidate_id=options.review_candidate,
        run_id=options.run,
    )


def _record(options: argparse.Namespace) -> int:
    layout = _layout(options)
    if options.benchmark and (options.config is None or options.consent_id is None):
        raise VoiceLabError("--benchmark needs --config and --consent-id")
    sandboxed = in_speech_sandbox()
    if options.benchmark and not sandboxed and not options.outside_the_sandbox:
        raise VoiceLabError(
            "a benchmark renders Cole's voice and runs only in the speech unit's sandbox (no "
            "network): start voice-lab-speech-benchmark.service (deploy/host-manual/), "
            f"or pass {OUTSIDE_THE_SANDBOX} to accept rendering here"
        )
    review = _review(layout, options)
    now = utc_now()
    identifier = comparison_id(now)
    training = training_summary(layout, options.run, training_device=options.training_device)
    inference = None
    if options.benchmark:
        config = load_speech_config(options.config)
        # Its own resident scratch and caches, apart from the speech runner's.
        state = Path(config.state_root) / BENCHMARK_STATE
        backend = ReleaseBackend(config.model_copy(update={"state_root": str(state)}))
        holder = GpuSlotHolder(kind="benchmark", job_id=identifier, started_at=now)
        slot = Path(config.gpu_slot_root)
        want_slot(slot, holder, wait_seconds=options.slot_timeout, now=now)
        try:
            with hold_slot(slot, holder, timeout_seconds=options.slot_timeout):
                withdraw_want(slot)
                scratch = layout.directory("tmp") / f"{SCRATCH_PREFIX}{identifier}"
                sweep_benchmark_scratch(layout, keep=scratch)
                try:
                    inference = benchmark_run(
                        layout,
                        options.run,
                        backend,
                        consent_id=options.consent_id,
                        subject_id=config.subject_id,
                        now=now,
                        work_directory=scratch,
                        segment_deadline_seconds=float(config.segment_deadline_seconds),
                        sandboxed=sandboxed,
                    )
                finally:
                    backend.unload()
        finally:
            withdraw_want(slot)
    comparison = ModelComparison(
        comparison_id=identifier,
        model_family=training.model_family,
        run_id=options.run,
        created_at=now,
        training=training,
        inference=inference,
        quality=review,
    )
    path = record_comparison(layout, comparison)
    _print({"written": str(path.relative_to(layout.root)), **comparison.model_dump(mode="json")})
    return 0


def _compare(options: argparse.Namespace) -> int:
    records = read_comparisons(_layout(options), tuple(options.run or ()))
    rows = comparison_rows(records)
    _print({"rows": rows, "warnings": comparison_warnings(rows)})
    return 0


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``families`` to ``commands`` (``models`` is the candidate registry's)."""
    families = commands.add_parser("families", help="compare fine-tuned model families (D-049)")
    model_commands = families.add_subparsers(dest="families_command", required=True)
    record = model_commands.add_parser(
        "record", help="record a finished run's training cost, benchmark and review"
    )
    record.add_argument("--asset-root")
    record.add_argument("--run", required=True)
    record.add_argument(
        "--training-device", help="where the run trained, when its runtime did not record it"
    )
    record.add_argument("--blind-review", help="the blind review that judged the run")
    record.add_argument("--score-sheet", help="that review's score sheet (JSON), read and checked")
    record.add_argument("--score-sheet-sha256", help="the score sheet's SHA-256, as recorded")
    record.add_argument(
        "--review-candidate", help="the run's candidate id in the review (default: the run id)"
    )
    record.add_argument(
        "--benchmark",
        action="store_true",
        help="render the held-out text set through the speech backend (holds the GPU slot; "
        "only in the speech sandbox)",
    )
    record.add_argument("--config", type=Path, help="the speech configuration (its runtime)")
    record.add_argument(
        "--consent-id",
        help="an approved consent record for private-home-host-synthesis on home-host",
    )
    record.add_argument(
        OUTSIDE_THE_SANDBOX,
        dest="outside_the_sandbox",
        action="store_true",
        help="render the benchmark in this process although it has a network (recorded)",
    )
    record.add_argument("--slot-timeout", type=float, default=600.0)
    record.set_defaults(handler=_record)
    compare = model_commands.add_parser(
        "compare", help="per run: the newest training summary, benchmark and review"
    )
    compare.add_argument("--asset-root")
    compare.add_argument("--run", action="append")
    compare.set_defaults(handler=_compare)
