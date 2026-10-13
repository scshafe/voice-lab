"""CLI commands of step 9 (D-053): ``voice-lab corpus-import plan|approve|send`` on the Mac.

@canonical cli_corpus_import -- registered into ``voice-lab``'s parser by ``cli.build_parser``.
``plan`` packages the Swift app's takes for the PC's catalog version and writes the export
plan, printing it for review; ``approve`` records the owner's transfer approval of one plan by
its digest; ``send`` uploads an approved plan's packages (``corpus_import``).
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from .cli_home_host import home_host_client
from .corpus_import import (
    DEFAULT_SEND_WAIT_SECONDS,
    approval_path,
    approve_plan,
    fetch_catalog_view,
    plan_export,
    read_catalog_view,
    send_plan,
)
from .errors import VoiceLabError
from .home_host import load_approval
from .models import utc_now
from .paths import AssetLayout


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _layout(options: argparse.Namespace) -> AssetLayout:
    configured = options.asset_root or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")
    if not configured:
        raise VoiceLabError("an explicit --asset-root or COLE_VOICE_LAB_ASSET_ROOT is required")
    return AssetLayout.validate(Path(configured).expanduser())


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError(f"expected 64 lowercase hexadecimal characters: {value}")
    return value


def _plan(options: argparse.Namespace) -> int:
    layout = _layout(options)
    if options.catalog is not None:
        view = read_catalog_view(Path(options.catalog).expanduser())
    else:
        approval, _ = load_approval(layout, now=utc_now())
        view = fetch_catalog_view(home_host_client(layout, approval, options.bearer_file), approval)
    planning = plan_export(layout, view)
    _print(planning.report(layout))
    return 0


def _approve(options: argparse.Namespace) -> int:
    if not options.i_approve_this_transfer:
        raise VoiceLabError(
            "approving a transfer requires --i-approve-this-transfer, after reviewing the plan "
            "`voice-lab corpus-import plan` printed"
        )
    layout = _layout(options)
    approval, written = approve_plan(
        layout,
        options.plan,
        approved_by=options.approved_by,
        statement=options.statement,
        now=utc_now(),
    )
    _print(
        {
            "approved": True,
            "approved_now": written,
            "approval": approval.model_dump(mode="json"),
            "approval_path": str(approval_path(layout, options.plan).relative_to(layout.root)),
        }
    )
    return 0


def _send(options: argparse.Namespace) -> int:
    layout = _layout(options)
    approval, _ = load_approval(layout, now=utc_now())
    results = send_plan(
        layout,
        options.plan,
        client=home_host_client(layout, approval, options.bearer_file),
        wait_seconds=options.wait,
    )
    counts = Counter(result.status for result in results)
    _print(
        {
            "plan_sha256": options.plan,
            "counts": dict(sorted(counts.items())),
            "takes": [
                {
                    "take_id": result.take_id,
                    "source_take_id": result.source_take_id,
                    "status": result.status,
                    "accepted_before": result.before,
                    "detail": result.detail,
                }
                for result in results
            ],
        }
    )
    return 0 if set(counts) <= {"accepted", "deferred"} else 1


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``corpus-import`` to ``commands``."""
    group = commands.add_parser(
        "corpus-import", help="the Swift app's takes to the PC's corpus (D-053, Mac side)"
    )
    group_commands = group.add_subparsers(dest="corpus_import_command", required=True)

    plan = group_commands.add_parser(
        "plan", help="package the takes the PC's catalog counts and write the export plan"
    )
    plan.add_argument("--asset-root")
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--catalog", help="the PC's GET /api/v1/catalog answer, saved as JSON")
    source.add_argument(
        "--catalog-from-pc",
        action="store_true",
        help="fetch GET /api/v1/catalog from the approved home host (signed in)",
    )
    plan.add_argument("--bearer-file", help="a private file holding a token (tests only)")
    plan.set_defaults(handler=_plan)

    approve = group_commands.add_parser(
        "approve", help="record the transfer approval of one export plan, by its digest"
    )
    approve.add_argument("--asset-root")
    approve.add_argument("--plan", type=_sha256, required=True, help="the plan's SHA-256")
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--statement", required=True)
    approve.add_argument("--i-approve-this-transfer", action="store_true")
    approve.set_defaults(handler=_approve)

    send = group_commands.add_parser(
        "send", help="upload the packages of an approved export plan to the home host"
    )
    send.add_argument("--asset-root")
    send.add_argument("--plan", type=_sha256, required=True, help="the plan's SHA-256")
    send.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_SEND_WAIT_SECONDS,
        help="seconds to wait for the PC to publish a take before moving on",
    )
    send.add_argument("--bearer-file", help="a private file holding a token (tests only)")
    send.set_defaults(handler=_send)
