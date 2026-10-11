"""Uploads (design §6.1): the local spool, per-session progress, resume, receipts.

The spool is the measurement capture's session store (``measurement/store.py``): a session
stays on this machine until the server accepts it, and every attempt resumes from the bytes
the server already holds. In 4a sessions travel by voice-journey-capture's protocol
(``measurement/upload.py``); 4b moves them to ``/api/v1/intake`` with a D-021 ledger entry.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal, Slot

from ..auth import SignInRequired
from ..measurement.store import LocalSession, SessionStore
from ..measurement.upload import CHUNK_BYTES, IntakeClient, UploadSummary, upload_pending
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
                "progressValue": 0.0,
                "progressLabel": "",
                "progressDetail": "",
            },
        )
        self.spool = spool
        self.chunk_bytes = CHUNK_BYTES
        self.refresh()

    @property
    def pending_count(self) -> int:
        return int(self._v["pendingCount"])

    @property
    def failed_count(self) -> int:
        return int(self._v["failedCount"])

    def activate(self) -> None:
        self.refresh()

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
        except Exception as exc:  # noqa: BLE001 - ConfigError, shown
            self.toast.emit(str(exc), "warn")
            return
        self._set(self.changed, busy=True)
        client = IntakeClient(server, context.session, context.http, self.chunk_bytes)
        tasks = context.tasks

        def started(session_id: str) -> None:
            tasks.on_gui_thread(lambda: self._started(session_id))

        def progress(name: str, sent: int, size: int) -> None:
            tasks.on_gui_thread(lambda: self._progress(name, sent, size))

        def work() -> UploadSummary:
            return upload_pending(client, self.spool, server, progress, on_start=started)

        def done(summary: UploadSummary | None, error: BaseException | None) -> None:
            self._set(self.changed, busy=False, uploading="")
            self._set(self.progressChanged, progressValue=0.0, progressLabel="", progressDetail="")
            if error is not None:
                if isinstance(error, SignInRequired):
                    self.toast.emit(self.failure(error), "warn")
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
