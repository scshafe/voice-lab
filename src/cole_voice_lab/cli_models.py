"""CLI commands of the model comparison: ``voice-lab families record|compare`` (D-049).

@canonical cli_models -- registered into ``voice-lab``'s parser by ``cli.build_parser``.
``families record`` writes one comparison record of a finished run (designated or a
candidate): its training summary, with ``--benchmark`` an inference benchmark through the
speech backend of the configuration's runtime (holding the GPU slot, under the consent record
``--consent-id`` names for ``private-home-host-synthesis`` on ``home-host``), and with
``--blind-review`` a link to the blind review that judged it. ``families compare`` prints the
newest record of each run side by side. Synthetic audio never leaves the run's scratch: the
benchmark keeps digests and timings only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .errors import VoiceLabError
from .model_comparison import (
    ModelComparison,
    benchmark_run,
    comparison_id,
    comparison_rows,
    read_comparisons,
    record_comparison,
    require_blind_review,
    training_summary,
)
from .models import utc_now
from .paths import AssetLayout
from .speech.config import load_speech_config
from .speech.contracts import GpuSlotHolder
from .speech.families import ReleaseBackend
from .speech.gpu import hold_slot, want_slot, withdraw_want
from .speech.release import BlindReviewRef


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _layout(options: argparse.Namespace) -> AssetLayout:
    configured = options.asset_root or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")
    if not configured:
        raise VoiceLabError("an explicit --asset-root or COLE_VOICE_LAB_ASSET_ROOT is required")
    return AssetLayout.validate(Path(configured).expanduser())


def _record(options: argparse.Namespace) -> int:
    layout = _layout(options)
    if (options.blind_review is None) != (options.score_sheet_sha256 is None):
        raise VoiceLabError("--blind-review and --score-sheet-sha256 go together")
    if options.benchmark and (options.config is None or options.consent_id is None):
        raise VoiceLabError("--benchmark needs --config and --consent-id")
    review = (
        None
        if options.blind_review is None
        else BlindReviewRef(
            review_id=options.blind_review, score_sheet_sha256=options.score_sheet_sha256
        )
    )
    if review is not None:
        require_blind_review(layout, review)
    now = utc_now()
    training = training_summary(layout, options.run, training_device=options.training_device)
    inference = None
    if options.benchmark:
        config = load_speech_config(options.config)
        backend = ReleaseBackend(config)
        identifier = comparison_id(now)
        holder = GpuSlotHolder(kind="benchmark", job_id=identifier, started_at=now)
        slot = Path(config.gpu_slot_root)
        want_slot(slot, holder)
        try:
            with hold_slot(slot, holder, timeout_seconds=options.slot_timeout):
                withdraw_want(slot)
                try:
                    inference = benchmark_run(
                        layout,
                        options.run,
                        backend,
                        consent_id=options.consent_id,
                        subject_id=config.subject_id,
                        now=now,
                        work_directory=layout.directory("tmp") / f".model-benchmark-{identifier}",
                        segment_deadline_seconds=float(config.segment_deadline_seconds),
                    )
                finally:
                    backend.unload()
        finally:
            withdraw_want(slot)
    else:
        identifier = comparison_id(now)
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
    _print(comparison_rows(records))
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
    record.add_argument("--blind-review")
    record.add_argument("--score-sheet-sha256")
    record.add_argument(
        "--benchmark",
        action="store_true",
        help="render the held-out text set through the speech backend (holds the GPU slot)",
    )
    record.add_argument("--config", type=Path, help="the speech configuration (its runtime)")
    record.add_argument(
        "--consent-id",
        help="an approved consent record for private-home-host-synthesis on home-host",
    )
    record.add_argument("--slot-timeout", type=float, default=600.0)
    record.set_defaults(handler=_record)
    compare = model_commands.add_parser("compare", help="the newest record of each run")
    compare.add_argument("--asset-root")
    compare.add_argument("--run", action="append")
    compare.set_defaults(handler=_compare)
