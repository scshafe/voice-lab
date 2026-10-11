"""Prompted packages to the home host through ``/api/v1/intake`` (phase 4c, D-042).

A packaged prompted session is a capture package; it travels like a measurement session
(``measurement/home_host.py``): ``intake_client.upload_session`` with the package class
``capture``, which the standing home-host approval must cover (D-021), the ledger entry
before the first byte, a resumable transfer and the intake receipt, whose digests go into
``packages/<session id>.upload.json`` beside the package (never inside it: the package's
file set is exactly what its manifest declares).

Until spike S-1 passes, PortAudio takes stay on this machine: nothing is sent unless the
settings turn ``prompted_uploads`` on (D-042), which the owner does once S-1 has passed on
that platform.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake_client import Progress, UploadError, UploadRefused, upload_session
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout

from ..auth import SignInRequired
from ..measurement import home_host
from ..measurement.pin import now_iso
from .store import PromptedStore, SessionRecord

HELD_FOR_S1: Final = (
    "Stays on this machine until spike S-1 passes (D-042); then turn on prompted uploads "
    "(prompted_uploads = true in config.toml)."
)


@dataclass(frozen=True)
class PromptedPackage:
    """A packaged prompted session and its upload state."""

    session: SessionRecord
    path: Path
    upload: dict[str, Any]

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def upload_state(self) -> str:
        """``pending`` | ``accepted`` | ``failed``."""
        return str(self.upload.get("state", "pending"))


def packages(store: PromptedStore) -> list[PromptedPackage]:
    """Every packaged session (oldest first) with its upload state."""
    out = []
    for session in store.sessions():
        if session.state != "packaged":
            continue
        path = store.package_dir(session.session_id)
        if path.is_dir():
            out.append(PromptedPackage(session, path, store.upload_state(session.session_id)))
    return out


def pending(store: PromptedStore) -> list[PromptedPackage]:
    return [package for package in packages(store) if package.upload_state == "pending"]


def _update(store: PromptedStore, session_id: str, **changes: Any) -> None:
    data = store.upload_state(session_id)
    data.update(changes)
    store.write_upload_state(session_id, data)


def record_attempt(store: PromptedStore, session_id: str, error: str | None) -> None:
    attempts = int(store.upload_state(session_id).get("attempts", 0)) + 1
    _update(
        store,
        session_id,
        state="pending",
        attempts=attempts,
        lastAttemptAt=now_iso(),
        lastError=error,
    )


def retry_failed(store: PromptedStore, session_id: str) -> None:
    _update(store, session_id, state="pending", lastError=None)


def upload_pending(
    store: PromptedStore,
    *,
    library: AssetLayout,
    server_url: str,
    bearer: Callable[[], str],
    client_factory: home_host.ClientFactory = home_host.tailnet_client,
    progress: Progress | None = None,
    on_start: Callable[[str], None] | None = None,
    on_session: Callable[[str, str], None] | None = None,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> home_host.UploadSummary:
    """Every pending prompted package, oldest first, as ``capture``.

    Raises: ``HomeHostNotReady`` before anything is sent (no standing approval, another
    server, an approval without ``capture``); ``SignInRequired`` when the sign-in is gone."""
    approval = home_host.approval_for(library, server_url, now=clock(), package_class="capture")
    summary = home_host.UploadSummary()
    for package in pending(store):
        session_id = package.session_id
        if on_start:
            on_start(session_id)
        state = "pending"
        try:
            status = upload_session(
                package.path,
                layout=library,
                client=client_factory(approval, bearer),
                package_class="capture",
                progress=progress,
                clock=clock,
                sleep=sleep,
            )
        except UploadRefused as exc:
            if exc.code in ("unauthorized", "forbidden"):
                record_attempt(store, session_id, "sign-in required")
                raise SignInRequired("the server refused the sign-in; sign in again") from exc
            _update(store, session_id, state="failed", failedAt=now_iso(), lastError=str(exc))
            summary.failed += 1
            summary.last_error = str(exc)
            state = "failed"
        except SignInRequired:
            record_attempt(store, session_id, "sign-in required")
            raise
        except (UploadError, SecurityError, ContractError, OSError) as exc:
            message = str(exc) or type(exc).__name__
            record_attempt(store, session_id, message)
            summary.pending += 1
            summary.last_error = message
        else:
            receipt = status.receipt
            assert receipt is not None  # upload_session returns only an accepted status
            _update(
                store,
                session_id,
                state="accepted",
                acceptedAt=now_iso(),
                server=server_url,
                lastError=None,
                receipt={
                    "packageSessionId": status.session_id,
                    "manifestSha256": status.manifest_sha256,
                    "approvalId": approval.approval_id,
                },
            )
            summary.accepted += 1
            state = "accepted"
        if on_session:
            on_session(session_id, state)
    return summary
