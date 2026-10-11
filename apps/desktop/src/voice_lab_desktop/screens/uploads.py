"""Uploads (design §6.1): the local spool, per-session progress, resume, receipts.

The spool is the measurement capture's session store (``measurement/store.py``): a session
stays on this machine until the server's intake publisher accepts it. Since phase 4b a
session travels through ``/api/v1/intake`` as a capture package, with the D-021 ledger entry
before its first byte (``measurement/home_host.py``); an interrupted upload resumes from
the bytes the server already holds, and the receipt's digests are kept with the session.
The screen also says whether this machine can upload at all: a home-host approval must be
bound in its library and must name the configured server.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Signal, Slot

from cole_voice_lab.models import utc_now

from ..auth import SignInRequired
from ..measurement import home_host
from ..measurement.store import LocalSession, SessionStore
from ..viewmodel import AppContext, ViewModel, qproperty

STATE_LABELS = {
    "pending": ("waiting", "info"),
    "uploading": ("uploading", "info"),
    "accepted": ("accepted", "ok"),
    "failed": ("refused", "danger"),
    "not-uploadable": ("not uploadable", "neutral"),
}


def _mb(size: int) -> str:
    return f"{size / 1048576:.1f} MB"


def session_row(session: LocalSession, uploading: str = "") -> dict[str, Any]:
    """A spool entry as the Uploads list shows it."""
    state = "uploading" if session.session_id == uploading else session.upload_state
    label, tone = STATE_LABELS.get(state, (state, "neutral"))
    upload = session.upload
    if session.status == "aborted":
        detail = "Aborted: kept here, never uploaded."
    elif session.status != "completed":
        detail = "Interrupted before it was saved: never uploaded."
    elif state == "accepted":
        detail = f"Accepted at {upload.get('acceptedAt', '?')} by {upload.get('server', '?')}."
        receipt = upload.get("receipt")
        if isinstance(receipt, dict) and receipt.get("manifestSha256"):
            digest = str(receipt["manifestSha256"])
            detail += f" Package {receipt.get('packageSessionId', '?')}, manifest {digest[:12]}…"
    elif state == "failed":
        detail = f"Refused at {upload.get('failedAt', '?')}: {upload.get('lastError') or '?'}"
    elif upload.get("attempts"):
        last = upload.get("lastError") or "interrupted"
        detail = f"{upload['attempts']} attempt(s); the last: {last}. It resumes where it stopped."
    else:
        detail = "Not sent yet."
    return {
        "sessionId": session.session_id,
        "state": state,
        "stateLabel": label,
        "tone": tone,
        "detail": detail,
        "canRetry": state == "failed",
    }


class UploadsModel(ViewModel):
    changed = Signal()
    progressChanged = Signal()
    spoolChanged = Signal()

    rows = qproperty("QVariantList", "rows", changed)
    pendingCount = qproperty(int, "pendingCount", changed)
    failedCount = qproperty(int, "failedCount", changed)
    busy = qproperty(bool, "busy", changed)
    uploading = qproperty(str, "uploading", changed)
    ready = qproperty(bool, "ready", changed)
    readiness = qproperty(str, "readiness", changed)
    progressValue = qproperty(float, "progressValue", progressChanged)
    progressLabel = qproperty(str, "progressLabel", progressChanged)
    progressDetail = qproperty(str, "progressDetail", progressChanged)

    def __init__(self, context: AppContext, spool: SessionStore) -> None:
        super().__init__(
            context,
            {
                "rows": [],
                "pendingCount": 0,
                "failedCount": 0,
                "busy": False,
                "uploading": "",
                "ready": False,
                "readiness": "",
                "progressValue": 0.0,
                "progressLabel": "",
                "progressDetail": "",
            },
        )
        self.spool = spool
        #: How the app reaches the approved node (tests: a loopback client).
        self.client_factory: home_host.ClientFactory = home_host.tailnet_client
        #: How an upload waits between its polls for the publisher's receipt.
        self.sleep: Callable[[float], None] = time.sleep
        self.refresh()

    @property
    def pending_count(self) -> int:
        return int(self._v["pendingCount"])

    @property
    def failed_count(self) -> int:
        return int(self._v["failedCount"])

    def activate(self) -> None:
        self.refresh()
        self.check_readiness()

    @Slot()
    def refresh(self) -> None:
        sessions = self.spool.sessions()
        rows = [session_row(s, self._v["uploading"]) for s in reversed(sessions)]
        self._set(
            self.changed,
            rows=rows,
            pendingCount=sum(1 for s in sessions if s.upload_state == "pending"),
            failedCount=sum(1 for s in sessions if s.upload_state == "failed"),
        )
        self.spoolChanged.emit()

    def check_readiness(self) -> None:
        """Whether this machine can upload: the server is set and a standing home-host
        approval covering measurement sessions names it."""
        config = self.context.config
        if not config.server_url:
            self._set(self.changed, ready=False, readiness="Set the server on the Status screen.")
            return
        try:
            library = home_host.library_layout(config.library_dir)
            approval = home_host.approval_for(library, config.server_url, now=utc_now())
        except Exception as exc:  # noqa: BLE001 - HomeHostNotReady, or an unsafe library
            self._set(self.changed, ready=False, readiness=str(exc))
            return
        expires = approval.expires_at.date().isoformat()
        text = (
            f"Uploads go to {approval.endpoint} under the home-host approval "
            f"{approval.approval_id} (until {expires}); each is recorded in this "
            "machine's ledger."
        )
        self._set(self.changed, ready=True, readiness=text)

    @Slot(str)
    def retry(self, session_id: str) -> None:
        """A refused session back to waiting (after the server was fixed)."""
        for session in self.spool.sessions():
            if session.session_id == session_id and session.upload_state == "failed":
                self.spool.retry_failed(session.path)
        self.refresh()

    @Slot()
    def uploadAll(self) -> None:  # noqa: N802 (QML name)
        if self._v["busy"]:
            return
        context = self.context
        if not context.session.signed_in:
            self.toast.emit("Sign in to upload.", "info")
            return
        try:
            server = context.config.require_server()
            library = home_host.library_layout(context.config.library_dir)
        except Exception as exc:  # noqa: BLE001 - ConfigError or an unsafe library, shown
            self.toast.emit(str(exc), "warn")
            return
        self._set(self.changed, busy=True)
        tasks = context.tasks

        def started(session_id: str) -> None:
            tasks.on_gui_thread(lambda: self._started(session_id))

        def progress(name: str, sent: int, size: int) -> None:
            tasks.on_gui_thread(lambda: self._progress(name, sent, size))

        def work() -> home_host.UploadSummary:
            return home_host.upload_pending(
                self.spool,
                library=library,
                server_url=server,
                bearer=context.session.bearer,
                client_factory=self.client_factory,
                progress=progress,
                on_start=started,
                sleep=self.sleep,
            )

        def done(summary: home_host.UploadSummary | None, error: BaseException | None) -> None:
            self._set(self.changed, busy=False, uploading="")
            self._set(self.progressChanged, progressValue=0.0, progressLabel="", progressDetail="")
            if error is not None:
                if isinstance(error, SignInRequired):
                    self.toast.emit(self.failure(error), "warn")
                elif isinstance(error, home_host.HomeHostNotReady):
                    self._set(self.changed, ready=False, readiness=str(error))
                    self.toast.emit(f"Not uploaded: {error}", "warn")
                elif isinstance(error, OSError):
                    self.toast.emit(f"Upload failed: {type(error).__name__}", "danger")
                else:
                    raise error
            elif summary is not None:
                if summary.accepted:
                    self.toast.emit(f"{summary.accepted} session(s) accepted by the server.", "ok")
                if summary.failed:
                    message = f"{summary.failed} session(s) refused: {summary.last_error}"
                    self.toast.emit(message, "danger")
                if summary.pending:
                    message = (
                        f"{summary.pending} session(s) not uploaded yet ({summary.last_error})"
                    )
                    self.toast.emit(message + "; they resume next time.", "warn")
            self.refresh()

        context.tasks.submit(work, done)

    def _started(self, session_id: str) -> None:
        self._set(self.changed, uploading=session_id)
        self._set(self.progressChanged, progressValue=0.0, progressLabel=f"Uploading {session_id}")
        self.refresh()

    def _progress(self, name: str, sent: int, size: int) -> None:
        self._set(
            self.progressChanged,
            progressValue=sent / size if size else 1.0,
            progressLabel=f"Uploading {self._v['uploading']}: {name}",
            progressDetail=f"{_mb(sent)} of {_mb(size)}",
        )
