"""Approve uploads to my PC (D-021): the action, and its one confirm dialog, offered wherever
uploads wait for the home-host approval: Status, Uploads, and the studio's status line.

QML reads this model as ``approval`` (a context property, as ``status`` and ``uploads``). The
logic is ``voice_lab_desktop/approval.py``; this is its plumbing:

- ``refresh`` reads, from this app's library and its settings alone (no Tailscale), whether
  uploads wait for an approval (``needed``) or the one that stands expires within 30 days
  (``expiringSoon``), and says so in one line (``summary``);
- ``begin`` asks Tailscale on the task pool (``approval.propose``), then opens the dialog
  with what Approve would write: the endpoint, the node, the classes, the expiry and who
  approves. When an approval stands for that endpoint and node already, nothing is shown
  but a toast that says so;
- ``confirm`` (the dialog's Approve) is the review: it writes and binds exactly that approval,
  on the task pool (``approval.confirm``), and ``approved`` then lets the app sync and upload
  at once;
- ``cancel`` closes the dialog. A refusal (Tailscale not running, no such peer, a node
  without ``tag:voice-lab``, an unusable library) opens it with the reason and Close only.

Every connection made here goes to a declared ``@Slot`` (#41): QML reads this object.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Final

from PySide6.QtCore import Signal, Slot

from cole_voice_lab.home_host import HomeHostApproval
from cole_voice_lab.models import utc_now

from .. import approval
from ..tokens import jwt_claims
from ..viewmodel import AppContext, ViewModel, qproperty

#: The classes an approval made here covers, as the dialog names them.
CLASSES_TEXT: Final = "capture (takes, prompted sessions) · measurement (measurement sessions)"
CONFIRM_TITLE: Final = "Approve uploads to my PC?"
CONFIRM_TEXT: Final = (
    "The app sends your recordings to this PC only, until the date below. Each upload is "
    "written in this machine's ledger before its first byte (D-021)."
)
REFUSED_TITLE: Final = "Uploads are not approved"


class ApprovalModel(ViewModel):
    changed = Signal()
    dialogChanged = Signal()
    #: A new approval was written and bound: what waits may be sent now.
    approved = Signal()

    needed = qproperty(bool, "needed", changed)
    expiringSoon = qproperty(bool, "expiringSoon", changed)
    summary = qproperty(str, "summary", changed)
    busy = qproperty(bool, "busy", changed)
    # -- the dialog
    dialogOpen = qproperty(bool, "dialogOpen", dialogChanged)
    canApprove = qproperty(bool, "canApprove", dialogChanged)
    dialogTitle = qproperty(str, "dialogTitle", dialogChanged)
    dialogText = qproperty(str, "dialogText", dialogChanged)
    endpoint = qproperty(str, "endpoint", dialogChanged)
    nodeId = qproperty(str, "nodeId", dialogChanged)
    classesText = qproperty(str, "classesText", dialogChanged)
    expiresText = qproperty(str, "expiresText", dialogChanged)
    approvedBy = qproperty(str, "approvedBy", dialogChanged)
    reasonText = qproperty(str, "reasonText", dialogChanged)

    def __init__(self, context: AppContext) -> None:
        super().__init__(
            context,
            {
                "needed": False,
                "expiringSoon": False,
                "summary": "",
                "busy": False,
                "dialogOpen": False,
                "canApprove": False,
                "dialogTitle": "",
                "dialogText": "",
                "endpoint": "",
                "nodeId": "",
                "classesText": "",
                "expiresText": "",
                "approvedBy": "",
                "reasonText": "",
            },
        )
        #: Asks Tailscale for a tailnet suffix (tests: recorded answers, never the binary).
        self.resolver_factory: approval.ResolverFactory = approval.tailscale_resolver
        self.clock: Callable[[], datetime] = utc_now
        #: The server the approval is for: the settings' (tests: the name the loopback home
        #: host stands for).
        self.server: Callable[[], str] = lambda: self.context.config.server_url
        self._proposal: approval.Proposal | None = None
        context.configChanged.connect(self.refresh)
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        """Whether uploads wait for an approval, from the library and the settings."""
        found = approval.standing(self.context.config.library_dir, self.server(), now=self.clock())
        self._set(
            self.changed, needed=found.needed, expiringSoon=found.expiring, summary=found.text
        )

    def _approver(self) -> str:
        tokens = self.context.session.tokens
        if tokens is None:
            return approval.APPROVER
        try:
            return approval.approver_name(jwt_claims(tokens.id_token))
        except (ValueError, UnicodeDecodeError):
            return approval.APPROVER

    @Slot()
    def begin(self) -> None:
        """Approve uploads to my PC: Tailscale is asked off the GUI thread, then the dialog
        shows what Approve would write."""
        if self._v["busy"]:
            return
        library_dir = self.context.config.library_dir
        server_url, approved_by, now = self.server(), self._approver(), self.clock()
        resolver_factory = self.resolver_factory
        self._set(self.changed, busy=True)

        def work() -> approval.Proposal:
            return approval.propose(
                library_dir,
                server_url,
                now=now,
                approved_by=approved_by,
                resolver_factory=resolver_factory,
            )

        self.context.tasks.submit(work, self._proposed)

    def _proposed(self, proposal: approval.Proposal | None, error: BaseException | None) -> None:
        self._set(self.changed, busy=False)
        if error is not None:
            self._refused(error)
            return
        assert proposal is not None  # the task pool hands a result or an error
        made = proposal.approval
        if made is None:
            assert proposal.standing is not None  # nothing to write: one stands
            self._already(proposal.standing)
            return
        self._proposal = proposal
        self._set(
            self.dialogChanged,
            dialogOpen=True,
            canApprove=True,
            dialogTitle=CONFIRM_TITLE,
            dialogText=CONFIRM_TEXT,
            endpoint=made.endpoint,
            nodeId=made.node_stable_id,
            classesText=CLASSES_TEXT,
            expiresText=f"{made.expires_at.date().isoformat()} ({approval.APPROVAL_DAYS} days)",
            approvedBy=made.approved_by,
            reasonText=proposal.reason,
        )

    @Slot()
    def confirm(self) -> None:
        """The dialog's Approve: the approval shown is written and bound, off the GUI
        thread."""
        proposal = self._proposal
        if proposal is None or self._v["busy"]:
            return
        library_dir, now = self.context.config.library_dir, self.clock()
        self._set(self.changed, busy=True)
        self.context.tasks.submit(
            lambda: approval.confirm(library_dir, proposal, now=now), self._confirmed
        )

    def _confirmed(self, result: approval.Approved | None, error: BaseException | None) -> None:
        self._set(self.changed, busy=False)
        self._proposal = None
        if error is not None:
            self._refused(error)
            return
        assert result is not None  # the task pool hands a result or an error
        self._close()
        if not result.written:
            self._already(result.approval)
            return
        made = result.approval
        self.refresh()
        self.toast.emit(
            f"Uploads to {made.endpoint} are approved until "
            f"{made.expires_at.date().isoformat()} (approval {made.approval_id}).",
            "ok",
        )
        self.approved.emit()

    @Slot()
    def cancel(self) -> None:
        """The dialog's Cancel or Close (or Escape): nothing is written."""
        self._proposal = None
        self._close()

    def _already(self, standing: HomeHostApproval) -> None:
        self.refresh()
        self.toast.emit(
            f"Uploads to {standing.endpoint} are approved already, until "
            f"{standing.expires_at.date().isoformat()} (approval {standing.approval_id}); "
            "nothing was written.",
            "info",
        )

    def _refused(self, error: BaseException) -> None:
        if not isinstance(error, approval.ApprovalRefused):
            raise error
        self._proposal = None
        self._set(
            self.dialogChanged,
            dialogOpen=True,
            canApprove=False,
            dialogTitle=REFUSED_TITLE,
            dialogText=str(error),
            reasonText="",
        )

    def _close(self) -> None:
        self._set(self.dialogChanged, dialogOpen=False, canApprove=False)
