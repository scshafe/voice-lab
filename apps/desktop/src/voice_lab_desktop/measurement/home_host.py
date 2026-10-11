"""Uploading measurement sessions to the home host through ``/api/v1/intake`` (phase 4b).

A completed session (``session.json`` and its WAVs, as the ritual wrote them) becomes the
voice-lab capture package ``journey.measurement_package`` defines, the same package the
server's compatibility path built in phase 3, in ``<measurement>/packages/vj-<id>/``. It is
built once and rebuilt to the same bytes, so a retried upload names the same manifest
digest. ``intake_client.upload_session`` then does what D-021 asks of every upload to the
home host: it validates the package with the strict validator, loads the standing
home-host approval bound in this machine's library (which must cover ``measurement``),
checks that the node answering for the approved endpoint is the approved node, writes the
ledger entry naming the manifest digest before the first byte, sends the files resumably,
and waits for the intake receipt whose manifest digest must be the package's. The session
stays here until then (``store.py``); the receipt's digests go into its ``upload.json``.

The library is this machine's private asset root for the approval and its ledger:
``<data dir>/library`` by default. The owner approves and binds the home host there once
(``voice-lab home-host approve --asset-root <library> …``; FIRST-RUN.md).
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.home_host import HomeHostApproval, HomeHostClass, load_approval
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.intake_client import (
    HomeHostClient,
    Progress,
    UploadError,
    UploadRefused,
    upload_session,
)
from cole_voice_lab.journey import measurement_package as package
from cole_voice_lab.journey.jsonio import read_json
from cole_voice_lab.journey.paths import packaged_chains
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.tailnet import TailscaleCliResolver

from ..auth import SignInRequired
from .package import PackageError, read_session_json, validate_manifest, verify_files
from .store import LocalSession, SessionStore

CAPTURE_HOST: Final = "voice-lab-desktop"
PACKAGES: Final = "packages"

#: Builds the client that talks to the approved node (tests use a loopback client).
type ClientFactory = Callable[[HomeHostApproval, Callable[[], str]], HomeHostClient]


class HomeHostNotReady(Exception):
    """Nothing can be uploaded from this machine yet (no approval bound, an approval that
    does not stand, the settings naming another server); the message says what to do."""


@dataclass
class UploadSummary:
    accepted: int = 0
    pending: int = 0
    failed: int = 0
    last_error: str | None = None


def library_layout(library: Path) -> AssetLayout:
    """This machine's library (the approval and the ledger), made private if new."""
    if not library.exists():
        return AssetLayout.initialize(library)
    return AssetLayout.validate(library)


def tailnet_client(approval: HomeHostApproval, bearer: Callable[[], str]) -> HomeHostClient:
    """The real client: the approved endpoint, resolved and checked through Tailscale."""
    resolver = TailscaleCliResolver(tailnet_suffix=approval.tailnet_suffix)
    return HomeHostClient(endpoint=approval.endpoint, resolver=resolver, bearer=bearer)


#: What each package class is, as the messages name it.
CLASS_NOUNS: Final[dict[str, str]] = {
    "measurement": "measurement sessions",
    "capture": "prompted sessions (capture packages)",
}


def standing_approval(layout: AssetLayout, server_url: str, *, now: datetime) -> HomeHostApproval:
    """The bound approval, when it stands and names the configured server (whatever
    classes it covers).

    Raises: ``HomeHostNotReady`` saying what is missing."""
    try:
        approval, _digest = load_approval(layout, now=now)
    except ContractError as exc:
        raise HomeHostNotReady(
            "no home-host approval is bound on this machine yet; approve the home host once "
            f"(voice-lab home-host approve --asset-root {layout.root} …; FIRST-RUN.md)"
        ) from exc
    except SecurityError as exc:
        raise HomeHostNotReady(str(exc)) from exc
    if urlsplit(server_url).hostname != approval.endpoint:
        raise HomeHostNotReady(
            f"the settings name {server_url}, but the approved home host is "
            f"https://{approval.endpoint}"
        )
    return approval


def require_class(approval: HomeHostApproval, package_class: HomeHostClass) -> None:
    """Raises: ``HomeHostNotReady`` when ``approval`` does not cover ``package_class``."""
    if package_class not in approval.classes:
        raise HomeHostNotReady(
            f"the home-host approval {approval.approval_id} does not cover "
            f"{CLASS_NOUNS.get(package_class, package_class)}; approve the home host again "
            f"with the class {package_class}"
        )


def approval_for(
    layout: AssetLayout,
    server_url: str,
    *,
    now: datetime,
    package_class: HomeHostClass = "measurement",
) -> HomeHostApproval:
    """The bound approval, when it stands, names the configured server and covers
    ``package_class``.

    Raises: ``HomeHostNotReady`` saying what is missing."""
    approval = standing_approval(layout, server_url, now=now)
    require_class(approval, package_class)
    return approval


def build_package(local: LocalSession, packages_root: Path) -> Path:
    """The capture package of a completed session (built once; the same bytes after).

    Raises: ``PackageRefusal`` when the session cannot become a capture package: its
    ``session.json`` is damaged or breaks the intake's rules (``session_damaged``), a file
    changed on disk since it was recorded (``files_changed``; it is never sent), or the
    package rules refuse it."""
    try:
        manifest = read_session_json(local.path)
        validate_manifest(manifest)
    except PackageError as exc:
        raise package.PackageRefusal("session_damaged", {"reason": str(exc)}) from exc
    changed = verify_files(local.path, manifest)
    if changed:
        raise package.PackageRefusal("files_changed", {"files": changed})
    package.require_package_mapping(manifest, read_json(packaged_chains()))
    target = packages_root / package.package_session_id(manifest["sessionId"])
    if target.exists():
        validate_capture_package(target)
        return target
    packages_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = packages_root / f".{target.name}.{secrets.token_hex(6)}"
    work.mkdir(mode=0o700)
    try:
        located = {file["name"]: local.path / file["name"] for file in manifest["files"]}
        package.write_package(
            manifest,
            located,
            work,
            subject_id=package.DEFAULT_SUBJECT_ID,
            capture_host=CAPTURE_HOST,
            move=False,
        )
        validate_capture_package(work)
        os.rename(work, target)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return target


def upload_pending(
    store: SessionStore,
    *,
    library: AssetLayout,
    server_url: str,
    bearer: Callable[[], str],
    client_factory: ClientFactory = tailnet_client,
    progress: Progress | None = None,
    on_start: Callable[[str], None] | None = None,
    on_session: Callable[[str, str], None] | None = None,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> UploadSummary:
    """Every pending session, oldest first, through ``/api/v1/intake``.

    Raises: ``HomeHostNotReady`` before anything is sent when this machine cannot upload;
    ``SignInRequired`` when the sign-in is gone (the session stays pending)."""
    approval = approval_for(library, server_url, now=clock())
    summary = UploadSummary()
    packages_root = store.root.parent / PACKAGES
    for local in store.pending():
        if on_start:
            on_start(local.session_id)
        state = "pending"
        try:
            pkg = build_package(local, packages_root)
            status = upload_session(
                pkg,
                layout=library,
                client=client_factory(approval, bearer),
                package_class="measurement",
                progress=progress,
                clock=clock,
                sleep=sleep,
            )
        except (package.PackageRefusal, UploadRefused) as exc:
            code = getattr(exc, "code", "")
            if code in ("unauthorized", "forbidden"):
                store.record_attempt(local.path, "sign-in required")
                raise SignInRequired("the server refused the sign-in; sign in again") from exc
            store.mark_failed(local.path, _message(exc))
            summary.failed += 1
            summary.last_error = _message(exc)
            state = "failed"
        except SignInRequired:
            store.record_attempt(local.path, "sign-in required")
            raise
        except (UploadError, PackageError, SecurityError, ContractError, OSError) as exc:
            store.record_attempt(local.path, _message(exc))
            summary.pending += 1
            summary.last_error = _message(exc)
        else:
            receipt = status.receipt
            assert receipt is not None  # upload_session returns only an accepted status
            store.mark_accepted(
                local.path,
                server_url,
                receipt={
                    "packageSessionId": status.session_id,
                    "manifestSha256": status.manifest_sha256,
                    "approvalId": approval.approval_id,
                },
            )
            summary.accepted += 1
            state = "accepted"
        if on_session:
            on_session(local.session_id, state)
    return summary


def _message(exc: BaseException) -> str:
    if isinstance(exc, package.PackageRefusal):
        if exc.code == "files_changed":
            count = len(exc.detail.get("files", []))
            return f"{count} file(s) changed on disk since the session was recorded; not uploaded"
        return f"it cannot become a capture package ({exc.code})"
    return str(exc) or type(exc).__name__
