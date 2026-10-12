"""CLI commands of the voice release and the speech API: ``voice release …``, ``speech …``.

@canonical cli_speech -- registered into ``voice-lab``'s parser by ``cli.build_parser``.
``voice release designate`` previews, then (with ``--i-designate-this-run``) writes, the
next voice release (D-045): an act of judgement that is Cole's, never an agent's.
``voice release show|history`` print releases. ``speech run`` is the job runner's one pass
(the host unit ``voice-lab-speech``, no network); ``speech deliver`` the webhook delivery
pass (``voice-lab-speech-webhooks``, tailnet only); ``speech check-config`` validates a
configuration and prints its callers without anything secret.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from .errors import VoiceLabError
from .models import utc_now
from .paths import AssetLayout
from .speech.backend import F5SpeechBackend
from .speech.config import load_speech_config
from .speech.release import BlindReviewRef, current_release, designate, history
from .speech.runner import SpeechRunner
from .speech.webhooks import WebhookDispatcher

TRUSTED_DIGESTS_NAME = "trusted-digests.json"


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _layout(options: argparse.Namespace) -> AssetLayout:
    configured = options.asset_root or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")
    if not configured:
        raise VoiceLabError("an explicit --asset-root or COLE_VOICE_LAB_ASSET_ROOT is required")
    return AssetLayout.validate(Path(configured).expanduser())


def _designate(options: argparse.Namespace) -> int:
    layout = _layout(options)
    if (options.blind_review is None) != (options.score_sheet_sha256 is None):
        raise VoiceLabError("--blind-review and --score-sheet-sha256 go together")
    review = (
        None
        if options.blind_review is None
        else BlindReviewRef(
            review_id=options.blind_review, score_sheet_sha256=options.score_sheet_sha256
        )
    )
    release, digest = designate(
        layout,
        run_id=options.run,
        designated_by=options.designated_by,
        now=utc_now(),
        reference_prompt_id=options.reference_prompt,
        consent_id=options.consent_id,
        blind_review=review,
        write=options.i_designate_this_run,
    )
    _print(
        {
            "written": options.i_designate_this_run,
            "release": release.model_dump(mode="json"),
            "release_sha256": digest,
        }
    )
    if not options.i_designate_this_run:
        print("preview only: add --i-designate-this-run to designate it", flush=True)
    return 0


def _show(options: argparse.Namespace) -> int:
    release, digest = current_release(_layout(options))
    _print({"release": release.model_dump(mode="json"), "release_sha256": digest})
    return 0


def _history(options: argparse.Namespace) -> int:
    _print(
        [
            {"release": release.model_dump(mode="json"), "release_sha256": digest}
            for release, digest in history(_layout(options))
        ]
    )
    return 0


def _run(options: argparse.Namespace) -> int:
    config = load_speech_config(options.config)
    backend = F5SpeechBackend(
        config.runtime, trusted_digests=Path(config.state_root) / TRUSTED_DIGESTS_NAME
    )
    report = SpeechRunner(config, backend).run_pending()
    _print(asdict(report))
    return 0


def _deliver(options: argparse.Namespace) -> int:
    config = load_speech_config(options.config)
    _print(WebhookDispatcher(config).deliver_due())
    return 0


def _check_config(options: argparse.Namespace) -> int:
    config = load_speech_config(options.config)
    _print(
        {
            "callers": [
                {
                    "caller_id": caller.caller_id,
                    "webhook_urls": list(caller.webhook_urls),
                    "max_queued_jobs": caller.max_queued_jobs,
                    "daily_character_budget": caller.daily_character_budget,
                }
                for caller in config.callers
            ],
            "runtime_configured": config.runtime is not None,
            "audio_retention_hours": config.audio_retention_hours,
        }
    )
    return 0


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``voice`` and ``speech`` to ``commands``."""
    voice = commands.add_parser("voice", help="the voice release (D-045)")
    voice_commands = voice.add_subparsers(dest="voice_command", required=True)
    release = voice_commands.add_parser("release", help="designate and inspect voice releases")
    release_commands = release.add_subparsers(dest="release_command", required=True)

    designate_parser = release_commands.add_parser(
        "designate", help="preview, then designate, a finished fine-tune run as the voice"
    )
    designate_parser.add_argument("--asset-root")
    designate_parser.add_argument("--run", required=True)
    designate_parser.add_argument("--designated-by", required=True)
    designate_parser.add_argument("--reference-prompt")
    designate_parser.add_argument("--consent-id", default="cole-self-v1")
    designate_parser.add_argument("--blind-review")
    designate_parser.add_argument("--score-sheet-sha256")
    designate_parser.add_argument("--i-designate-this-run", action="store_true")
    designate_parser.set_defaults(handler=_designate)

    show = release_commands.add_parser("show", help="the current voice release")
    show.add_argument("--asset-root")
    show.set_defaults(handler=_show)

    history_parser = release_commands.add_parser("history", help="every voice release")
    history_parser.add_argument("--asset-root")
    history_parser.set_defaults(handler=_history)

    speech = commands.add_parser("speech", help="the speech API's host passes (D-046)")
    speech_commands = speech.add_subparsers(dest="speech_command", required=True)
    run = speech_commands.add_parser(
        "run", help="one job runner pass over the speech queue (host unit, no network)"
    )
    run.add_argument("--config", type=Path, required=True)
    run.set_defaults(handler=_run)
    deliver = speech_commands.add_parser(
        "deliver", help="one webhook delivery pass (host unit, tailnet only)"
    )
    deliver.add_argument("--config", type=Path, required=True)
    deliver.set_defaults(handler=_deliver)
    check = speech_commands.add_parser("check-config", help="validate a speech configuration")
    check.add_argument("--config", type=Path, required=True)
    check.set_defaults(handler=_check_config)
