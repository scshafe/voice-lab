"""CLI commands of the home-host intake: ``home-host …`` and ``intake publish|upload``.

@canonical cli_home_host -- registered into ``voice-lab``'s parser by ``cli.build_parser``.
``home-host approve`` previews and then authors the standing approval (D-021);
``home-host sign-in`` signs in to Pocket ID and keeps the refresh token in the OS keychain;
``home-host show`` prints the binding, the approval and the ledger's tail;
``intake upload`` sends one finalized package (``intake_client``); ``intake publish`` is the
publisher's one pass (``intake_publisher``), run by the PC's host unit.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from .errors import VoiceLabError
from .home_host import (
    HomeHostApproval,
    HomeHostLedger,
    HomeHostSignIn,
    approve,
    load_approval,
    load_sign_in,
    record_sha256,
    save_sign_in,
)
from .intake import validate_capture_package
from .intake_client import DEFAULT_WAIT_SECONDS, HomeHostClient, upload_session
from .intake_publisher import publish_pending
from .models import utc_now
from .oidc import bearer, fetch_jwks, platform_token_store, read_bearer_file, sign_in
from .paths import AssetLayout
from .synthetic_capture import build_synthetic_session
from .tailnet import TailscaleCliResolver


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 timestamp: {value}") from error
    if parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include an explicit UTC offset")
    return parsed


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError(f"expected 64 lowercase hexadecimal characters: {value}")
    return value


def _layout(options: argparse.Namespace) -> AssetLayout:
    configured = options.asset_root or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")
    if not configured:
        raise VoiceLabError("an explicit --asset-root or COLE_VOICE_LAB_ASSET_ROOT is required")
    return AssetLayout.validate(Path(configured).expanduser())


def _approve(options: argparse.Namespace) -> int:
    layout = _layout(options)
    approval = HomeHostApproval(
        approval_id=options.approval_id,
        approved=True,
        endpoint=options.endpoint,
        tailnet_suffix=options.tailnet_suffix,
        node_stable_id=options.node_stable_id,
        classes=tuple(sorted(set(options.classes))),
        approved_at=options.approved_at,
        expires_at=options.expires_at,
        approved_by=options.approved_by,
        statement=options.statement,
    )
    digest = record_sha256(approval)
    if options.preview:
        if options.reviewed_approval_sha256 or options.i_approve_this_destination:
            raise VoiceLabError("--preview writes nothing, so it takes no approval flag")
        _print({"approval": approval.model_dump(mode="json"), "approval_sha256": digest})
        return 0
    if not options.i_approve_this_destination or options.reviewed_approval_sha256 is None:
        raise VoiceLabError(
            "approving the home host requires --i-approve-this-destination and the "
            "--reviewed-approval-sha256 that `home-host approve --preview` printed"
        )
    binding = approve(
        layout, approval, reviewed_sha256=options.reviewed_approval_sha256, now=utc_now()
    )
    _print({"approved": True, "binding": binding.model_dump(mode="json")})
    return 0


def _show(options: argparse.Namespace) -> int:
    layout = _layout(options)
    approval, digest = load_approval(layout, now=utc_now())
    entries = HomeHostLedger(layout).entries()
    _print(
        {
            "approval": approval.model_dump(mode="json"),
            "approval_sha256": digest,
            "ledger_entries": len(entries),
            "ledger_tail": entries[-1][0].model_dump(mode="json") if entries else None,
        }
    )
    return 0


def _sign_in(options: argparse.Namespace) -> int:
    layout = _layout(options)
    settings = HomeHostSignIn(issuer=options.issuer, client_id=options.client_id)
    claims = sign_in(
        issuer=settings.issuer,
        client_id=settings.client_id,
        store=platform_token_store(settings.keychain_account()),
    )
    save_sign_in(layout, settings)
    _print({"signed_in": True, "subject": claims.get("sub"), "issuer": settings.issuer})
    return 0


def home_host_client(
    layout: AssetLayout, approval: HomeHostApproval, bearer_file: str | None
) -> HomeHostClient:
    """The client of the approved home host: the signed-in token from the OS keychain, or
    (acceptance tests only) a private bearer file. Raises: ``ContractError`` when this host
    never signed in."""
    if bearer_file is not None:
        path = Path(bearer_file)

        def token() -> str:
            return read_bearer_file(path)

    else:
        settings = load_sign_in(layout)

        def token() -> str:
            return bearer(
                issuer=settings.issuer,
                client_id=settings.client_id,
                store=platform_token_store(settings.keychain_account()),
            )

    return HomeHostClient(
        endpoint=approval.endpoint,
        resolver=TailscaleCliResolver(tailnet_suffix=approval.tailnet_suffix),
        bearer=token,
    )


def _upload(options: argparse.Namespace) -> int:
    layout = _layout(options)
    approval, _ = load_approval(layout, now=utc_now())
    client = home_host_client(layout, approval, options.bearer_file)
    status = upload_session(
        Path(options.session).expanduser(),
        layout=layout,
        client=client,
        wait_seconds=options.wait,
    )
    _print(status.model_dump(mode="json"))
    return 0


def _publish(options: argparse.Namespace) -> int:
    layout = _layout(options)
    summary = publish_pending(layout)
    _print(
        {
            "accepted": [receipt.model_dump(mode="json") for receipt in summary.accepted],
            "refused": [record.model_dump(mode="json") for record in summary.refused],
            "deferred": summary.deferred,
            "takes_recorded": summary.takes_recorded,
            "requests_applied": summary.requests_applied,
            "requests_refused": summary.requests_refused,
            "requests_waiting": summary.requests_waiting,
            "read_model": summary.read_model,
        }
    )
    return 0


def _synthetic(options: argparse.Namespace) -> int:
    session = build_synthetic_session(
        Path(options.directory).expanduser(),
        session_id=options.session_id,
        prompts=options.prompts,
        seconds=options.seconds,
    )
    package = validate_capture_package(session)
    _print(
        {
            "session": str(session),
            "session_id": package.manifest.session_id,
            "manifest_sha256": package.manifest_sha256,
        }
    )
    return 0


def _fetch_jwks(options: argparse.Namespace) -> int:
    count = fetch_jwks(options.issuer, Path(options.output))
    _print({"keys": count, "output": options.output})
    return 0


def register(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    intake_commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add ``home-host`` to ``commands`` and ``upload``/``publish`` to ``intake``."""
    home = commands.add_parser("home-host", help="the standing home-host approval (D-021)")
    home_commands = home.add_subparsers(dest="home_host_command", required=True)

    approve_parser = home_commands.add_parser(
        "approve", help="preview, then author, the standing home-host approval"
    )
    approve_parser.add_argument("--asset-root")
    approve_parser.add_argument("--approval-id", required=True)
    approve_parser.add_argument("--endpoint", required=True)
    approve_parser.add_argument("--tailnet-suffix", required=True)
    approve_parser.add_argument("--node-stable-id", required=True)
    approve_parser.add_argument(
        "--classes", nargs="+", required=True, choices=("capture", "measurement")
    )
    approve_parser.add_argument("--approved-at", type=_timestamp, required=True)
    approve_parser.add_argument("--expires-at", type=_timestamp, required=True)
    approve_parser.add_argument("--approved-by", required=True)
    approve_parser.add_argument("--statement", required=True)
    approve_parser.add_argument("--preview", action="store_true")
    approve_parser.add_argument("--reviewed-approval-sha256", type=_sha256)
    approve_parser.add_argument("--i-approve-this-destination", action="store_true")
    approve_parser.set_defaults(handler=_approve)

    show = home_commands.add_parser("show", help="the bound approval and the ledger's tail")
    show.add_argument("--asset-root")
    show.set_defaults(handler=_show)

    sign_in_parser = home_commands.add_parser(
        "sign-in", help="sign in to the identity provider in the browser"
    )
    sign_in_parser.add_argument("--asset-root")
    sign_in_parser.add_argument("--issuer", required=True)
    sign_in_parser.add_argument("--client-id", default="voice-lab-desktop")
    sign_in_parser.set_defaults(handler=_sign_in)

    jwks = home_commands.add_parser(
        "fetch-jwks", help="copy the identity provider's signing keys for the API (host unit)"
    )
    jwks.add_argument("--issuer", required=True)
    jwks.add_argument("--output", required=True)
    jwks.set_defaults(handler=_fetch_jwks)

    upload = intake_commands.add_parser(
        "upload", help="upload one finalized package to the home host and await its receipt"
    )
    upload.add_argument("session")
    upload.add_argument("--asset-root")
    upload.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    upload.add_argument(
        "--bearer-file", help="a private file holding a token (acceptance tests only)"
    )
    upload.set_defaults(handler=_upload)

    synthetic = intake_commands.add_parser(
        "synthetic", help="write a synthetic capture package (generated tones, no human audio)"
    )
    synthetic.add_argument("directory", help="a new directory for the package")
    synthetic.add_argument("--session-id", required=True)
    synthetic.add_argument("--prompts", type=int, default=2)
    synthetic.add_argument("--seconds", type=float, default=2.0)
    synthetic.set_defaults(handler=_synthetic)

    publish = intake_commands.add_parser(
        "publish", help="one publisher pass over incoming/ (the home host's intake unit)"
    )
    publish.add_argument("--asset-root")
    publish.set_defaults(handler=_publish)
