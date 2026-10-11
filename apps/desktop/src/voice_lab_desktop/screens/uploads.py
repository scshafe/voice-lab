"""Uploads (design §6.1): the local spool, per-session progress, resume, receipts.

The spool holds two kinds of session, both kept on this machine until the server's intake
publisher accepts them:

- measurement sessions (``measurement/store.py``): since phase 4b each travels through
  ``/api/v1/intake`` as a capture package of the class ``measurement``, with the D-021
  ledger entry before its first byte (``measurement/home_host.py``);
- prompted sessions (phase 4c): their capture packages (``prompted/package.py``) travel the
  same way as the class ``capture`` (``prompted/uploads.py``), but only once spike S-1 has
  passed and the owner turned ``prompted_uploads`` on (D-042); until then they are held
  here, and their row says so.

An interrupted upload resumes from the bytes the server already holds, and the receipt's
digests are kept with the session. The screen also says whether this machine can upload at
all: a standing home-host approval must be bound in its library, name the configured server
and cover the session's class.
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
from ..prompted import uploads as prompted_uploads
from ..prompted.store import PromptedStore
from ..viewmodel import AppContext, ViewModel, qproperty

STATE_LABELS = {
    "pending": ("waiting", "info"),
    "uploading": ("uploading", "info"),
    "accepted": ("accepted", "ok"),
    "failed": ("refused", "danger"),
    "not-uploadable": ("not uploadable", "neutral"),
    "held": ("held here", "neutral"),
}


def _mb(size: int) -> str:
    return f"{size / 1048576:.1f} MB"


def _detail(upload: dict[str, Any], state: str) -> str:
    """What an uploadable session's row says about its upload."""
    if state == "accepted":
        detail = f"Accepted at {upload.get('acceptedAt', '?')} by {upload.get('server', '?')}."
        receipt = upload.get("receipt")
        if isinstance(receipt, dict) and receipt.get("manifestSha256"):
            digest = str(receipt["manifestSha256"])
            detail += f" Package {receipt.get('packageSessionId', '?')}, manifest {digest[:12]}…"
        return detail
    if state == "failed":
        return f"Refused at {upload.get('failedAt', '?')}: {upload.get('lastError') or '?'}"
    if upload.get("attempts"):
        last = upload.get("lastError") or "interrupted"
        return f"{upload['attempts']} attempt(s); the last: {last}. It resumes where it stopped."
    return "Not sent yet."


def _row(session_id: str, state: str, detail: str, kind: str) -> dict[str, Any]:
    label, tone = STATE_LABELS.get(state, (state, "neutral"))
    return {
        "sessionId": session_id,
        "state": state,
        "stateLabel": label,
        "tone": tone,
        "detail": detail,
        "canRetry": state == "failed",
        "kind": kind,
    }


def session_row(session: LocalSession, uploading: str = "") -> dict[str, Any]:
    """A measurement session as the Uploads list shows it."""
    state = "uploading" if session.session_id == uploading else session.upload_state
    if session.status == "aborted":
        detail = "Aborted: kept here, never uploaded."
    elif session.status != "completed":
        detail = "Interrupted before it was saved: never uploaded."
    else:
        detail = _detail(session.upload, state)
    return _row(session.session_id, state, detail, "measurement")


def prompted_row(
    package: prompted_uploads.PromptedPackage, *, uploading: str, held: str
) -> dict[str, Any]:
    """A prompted package as the Uploads list shows it; ``held`` says why it stays here
    (empty: it may be sent)."""
    state = package.upload_state
    if package.session_id == uploading:
        state = "uploading"
    elif state == "pending" and held:
        return _row(package.session_id, "held", f"Prompted session. {held}", "prompted")
    detail = "Prompted session. " + _detail(package.upload, state)
    return _row(package.session_id, state, detail, "prompted")


class UploadsModel(ViewModel):
    changed = Signal()
    progressChanged = Signal()
    spoolChanged = Signal()

    rows = qproperty("QVariantList", "rows", changed)
    pendingCount = qproperty(int, "pendingCount", changed)
    failedCount = qproperty(int, "failedCount", changed)
    heldCount = qproperty(int, "heldCount", changed)
    busy = qproperty(bool, "busy", changed)
    uploading = qproperty(str, "uploading", changed)
    ready = qproperty(bool, "ready", changed)
    readiness = qproperty(str, "readiness", changed)
    progressValue = qproperty(float, "progressValue", progressChanged)
    progressLabel = qproperty(str, "progressLabel", progressChanged)
    progressDetail = qproperty(str, "progressDetail", progressChanged)

    def __init__(
        self, context: AppContext, spool: SessionStore, prompted: PromptedStore | None = None
    ) -> None:
        super().__init__(
            context,
            {
                "rows": [],
                "pendingCount": 0,
                "failedCount": 0,
                "heldCount": 0,
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
        self.prompted = prompted or PromptedStore(context.config.data_dir)
        #: The classes the bound approval covers (``None``: not known yet).
        self._classes: tuple[str, ...] | None = None
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
        self.check_readiness()
        self.refresh()

    def _prompted_held(self) -> str:
        """Why prompted packages stay here now ("" when they may be sent)."""
        if not self.context.config.prompted_uploads:
            return prompted_uploads.HELD_FOR_S1
        if self._classes is not None and "capture" not in self._classes:
            return (
                "The home-host approval does not cover prompted sessions (capture packages); "
                "approve the home host again with the class capture."
            )
        return ""

    @Slot()
    def refresh(self) -> None:
        uploading = self._v["uploading"]
        sessions = self.spool.sessions()
        rows = [session_row(s, uploading) for s in reversed(sessions)]
        held = self._prompted_held()
        packages = prompted_uploads.packages(self.prompted)
        prompted = [prompted_row(p, uploading=uploading, held=held) for p in reversed(packages)]
        rows.extend(prompted)
        pending = sum(1 for s in sessions if s.upload_state == "pending")
        pending += sum(1 for row in prompted if row["state"] in ("pending", "uploading"))
        failed = sum(1 for row in rows if row["state"] == "failed")
        held_count = sum(1 for row in prompted if row["state"] == "held")
        self._set(
            self.changed,
            rows=rows,
            pendingCount=pending,
            failedCount=failed,
            heldCount=held_count,
        )
        self.spoolChanged.emit()

    def check_readiness(self) -> None:
        """Whether this machine can upload: the server is set and a standing home-host
        approval names it (which classes it covers is said per session)."""
        config = self.context.config
        if not config.server_url:
            self._classes = None
            self._set(self.changed, ready=False, readiness="Set the server on the Status screen.")
            return
        try:
            library = home_host.library_layout(config.library_dir)
            approval = home_host.standing_approval(library, config.server_url, now=utc_now())
        except Exception as exc:  # noqa: BLE001 - HomeHostNotReady, or an unsafe library
            self._classes = None
            self._set(self.changed, ready=False, readiness=str(exc))
            return
        self._classes = tuple(approval.classes)
        expires = approval.expires_at.date().isoformat()
        covers = ", ".join(home_host.CLASS_NOUNS.get(c, c) for c in approval.classes)
        text = (
            f"Uploads go to {approval.endpoint} under the home-host approval "
            f"{approval.approval_id} (until {expires}), covering {covers}; each is recorded "
            "in this machine's ledger."
        )
        self._set(self.changed, ready=True, readiness=text)

    @Slot(str)
    def retry(self, session_id: str) -> None:
        """A refused session back to waiting (after the server was fixed)."""
        for session in self.spool.sessions():
            if session.session_id == session_id and session.upload_state == "failed":
                self.spool.retry_failed(session.path)
        for package in prompted_uploads.packages(self.prompted):
            if package.session_id == session_id and package.upload_state == "failed":
                prompted_uploads.retry_failed(self.prompted, session_id)
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
        send_prompted = not self._prompted_held()
        self._set(self.changed, busy=True)
        tasks = context.tasks

        def started(session_id: str) -> None:
            tasks.on_gui_thread(lambda: self._started(session_id))

        def progress(name: str, sent: int, size: int) -> None:
            tasks.on_gui_thread(lambda: self._progress(name, sent, size))

        def work() -> home_host.UploadSummary:
            total = home_host.UploadSummary()
            not_ready: list[home_host.HomeHostNotReady] = []
            runs: list[Callable[[], home_host.UploadSummary]] = []
            if self.spool.pending():
                runs.append(
                    lambda: home_host.upload_pending(
                        self.spool,
                        library=library,
                        server_url=server,
                        bearer=context.session.bearer,
                        client_factory=self.client_factory,
                        progress=progress,
                        on_start=started,
                        sleep=self.sleep,
                    )
                )
            if send_prompted and prompted_uploads.pending(self.prompted):
                runs.append(
                    lambda: prompted_uploads.upload_pending(
                        self.prompted,
                        library=library,
                        server_url=server,
                        bearer=context.session.bearer,
                        client_factory=self.client_factory,
                        progress=progress,
                        on_start=started,
                        sleep=self.sleep,
                    )
                )
            for run in runs:
                try:
                    summary = run()
                except home_host.HomeHostNotReady as exc:
                    not_ready.append(exc)
                    continue
                total.accepted += summary.accepted
                total.pending += summary.pending
                total.failed += summary.failed
                total.last_error = summary.last_error or total.last_error
            if not_ready and not (total.accepted or total.pending or total.failed):
                raise not_ready[0]
            if not_ready:
                total.pending += 1
                total.last_error = str(not_ready[0])
            return total

        def done(summary: home_host.UploadSummary | None, error: BaseException | None) -> None:
            self._set(self.changed, busy=False, uploading="")
            self._set(self.progressChanged, progressValue=0.0, progressLabel="", progressDetail="")
            if error is not None:
                if isinstance(error, SignInRequired):
                    self.toast.emit(self.failure(error), "warn")
                elif isinstance(error, home_host.HomeHostNotReady):
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
            self.check_readiness()
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
