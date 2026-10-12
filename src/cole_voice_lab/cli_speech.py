"""CLI commands of the voice release and the speech API: ``voice release …``, ``speech …``.

@canonical cli_speech -- registered into ``voice-lab``'s parser by ``cli.build_parser``.
``voice release designate`` previews, then (with ``--i-designate-this-run``) writes, the
next voice release (D-045): an act of judgement that is Cole's, never an agent's. It
designates only a run of the pinned subject (``cole-self``; ``--config``, when given, is
loaded and checked) and reads no consent record (D-050).
``voice release show|history`` print releases. ``speech run`` is the job runner with its
resident model until SIGTERM (the host unit ``voice-lab-speech``, no network); ``--once`` is
one pass by hand, refused (exit 2) while the unit runs; with ``--release-link`` it exits
``EXIT_OUTDATED`` once a deploy has moved that link off its own code, for systemd to start the
new code, and ``EXIT_MISCONFIGURED`` (78, not restarted) when the link is missing or
unreadable. ``speech deliver`` is the webhook delivery pass (``voice-lab-speech-webhooks``,
tailnet only); ``speech check-config`` validates a configuration and prints its callers
without anything secret; ``speech new-text-key`` is the operator's repair of a lost text key.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from dataclasses import asdict
from pathlib import Path

from .errors import VoiceLabError
from .models import utc_now
from .paths import AssetLayout
from .speech.config import load_speech_config
from .speech.families import ReleaseBackend, family_ids
from .speech.release import BlindReviewRef, current_release, designate, history
from .speech.runner import (
    EXIT_MISCONFIGURED,
    EXIT_OUTDATED,
    RunnerMisconfigured,
    RunnerOutdated,
    SpeechRunner,
    release_code_check,
)
from .speech.store import SpeechStore, TextKeyError
from .speech.webhooks import WebhookDispatcher


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
    if options.config is not None:
        load_speech_config(options.config)
    release, digest = designate(
        layout,
        run_id=options.run,
        designated_by=options.designated_by,
        now=utc_now(),
        reference_prompt_id=options.reference_prompt,
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
    check = None if options.release_link is None else release_code_check(options.release_link)
    runner = SpeechRunner(config, ReleaseBackend(config), code_check=check)
    try:
        if options.once:
            _print(asdict(runner.run_once()))
            return 0
        stopping = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.set())
        runner.serve(stop=stopping.is_set)
    except RunnerMisconfigured:
        return EXIT_MISCONFIGURED
    except RunnerOutdated:
        return EXIT_OUTDATED
    return 0


def _new_text_key(options: argparse.Namespace) -> int:
    if not options.i_accept_the_lost_key:
        raise VoiceLabError(
            "a new text key leaves earlier digests unverifiable: restore the old key from a"
            " backup if there is one, else add --i-accept-the-lost-key"
        )
    store = SpeechStore(load_speech_config(options.config))
    try:
        records, entries = store.make_text_key()
    except TextKeyError as error:
        raise VoiceLabError(str(error)) from error
    _print({"made": True, "job_records": records, "ledger_entries": entries})
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
            "runtime_family": None if config.runtime is None else config.runtime.model_family,
            "model_families": list(family_ids()),
            "subject_id": config.subject_id,
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
    designate_parser.add_argument(
        "--config", type=Path, help="the speech configuration, loaded and checked"
    )
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
        "run", help="the speech job runner with its resident model (host unit, no network)"
    )
    run.add_argument("--config", type=Path, required=True)
    run.add_argument(
        "--once",
        action="store_true",
        help="one pass by hand, then unload and exit; refused while the runner unit runs",
    )
    run.add_argument(
        "--release-link",
        type=Path,
        help="the active release link (/opt/voice-lab/current): exit 75 once it moves away",
    )
    run.set_defaults(handler=_run)
    deliver = speech_commands.add_parser(
        "deliver", help="one webhook delivery pass (host unit, tailnet only)"
    )
    deliver.add_argument("--config", type=Path, required=True)
    deliver.set_defaults(handler=_deliver)
    new_key = speech_commands.add_parser(
        "new-text-key",
        help="make a new text key where a lost one was (deploy/speech/README.md, 'The text key')",
    )
    new_key.add_argument("--config", type=Path, required=True)
    new_key.add_argument("--i-accept-the-lost-key", action="store_true")
    new_key.set_defaults(handler=_new_text_key)
    check = speech_commands.add_parser("check-config", help="validate a speech configuration")
    check.add_argument("--config", type=Path, required=True)
    check.set_defaults(handler=_check_config)
