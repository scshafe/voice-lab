"""Approve uploads to my PC (D-021): the action, and its one confirm dialog, offered wherever
uploads wait for the home-host approval: Status, Uploads, and the studio's status line.

QML reads this model as ``approval`` (a context property, as ``status`` and ``uploads``). The
logic is ``voice_lab_desktop/approval.py``; this is its plumbing:

- ``refresh`` reads, from this app's library and its settings alone (no Tailscale), whether
  uploads wait for an approval (``needed``) or the one that stands expires within 30 days
  (``expiringSoon``), and says so in one line (``summary``);
- ``begin`` asks Tailscale on the task pool (``approval.propose``), then opens the dialog
  with what Approve would write: the endpoint, the node, the classes, the expiry (local
  date) and who approves. When an approval stands for that endpoint and node already,
  nothing is shown but a toast that says so;
- ``confirm`` (the dialog's Approve) is the review: it writes and binds exactly that approval,
  under the digest shown, on the task pool (``approval.confirm``); ``approved`` then lets the
  app sync and upload at once (also when an approval made elsewhere meanwhile stands);
- ``cancel`` closes the dialog. A refusal (Tailscale not running, no such peer, a node
  without ``tag:voice-lab``, an unusable library) opens it with the reason and Close only;
  so does an unexpected error, which is then raised as the bug it is.

Settings saved (``configChanged``) drop a proposal being made or shown: it was for the old
server or library. ``confirm`` refuses one the settings no longer match.

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

CONFIRM_TITLE: Final = "Approve uploads to my PC?"
CONFIRM_TEXT: Final = (
    "The app sends your recordings to this PC only, until the date below. Each upload is "
    "written in this machine's ledger before its first byte (D-021)."
)
REFUSED_TITLE: Final = "Nothing was approved"


class ApprovalModel(ViewModel):
    changed = Signal()
    dialogChanged = Signal()
    #: An approval stands for the settings now (written here, or found made meanwhile): what
    #: waits may be sent now.
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
        #: The server the approval is for when not the settings' (tests: the name the loopback
        #: home host stands for). A seam, not a closure over this model: no reference cycle
        #: leaves this QObject to the cyclic garbage collector.
        self.server_url: Callable[[], str] | None = None
        self._proposal: approval.Proposal | None = None
        #: Counts the settings saved: a lookup begun before the last one is for old settings.
        self._generation = 0
        #: Approve was pressed and the approval is being written.
        self._confirming = False
        context.configChanged.connect(self._settings_changed)
        self.refresh()

    def _server(self) -> str:
        return self.server_url() if self.server_url is not None else self.context.config.server_url

    @Slot()
    def refresh(self) -> None:
        """Whether uploads wait for an approval, from the library and the settings."""
        found = approval.standing(self.context.config.library_dir, self._server(), now=self.clock())
        self._set(
            self.changed, needed=found.needed, expiringSoon=found.expiring, summary=found.text
        )

    @Slot()
    def _settings_changed(self) -> None:
        """Settings saved: a proposal being made or shown was for the old server or library,
        so it is dropped (one being written goes on: Approve was pressed under the old
        settings, and ``confirm`` checked them then)."""
        self._generation += 1
        if self._v["dialogOpen"] and not self._confirming:
            shown = self._proposal is not None
            self._proposal = None
            self._close()
            if shown:
                self.toast.emit(approval.STALE, "info")
        self.refresh()

    def _still_current(self, proposal: approval.Proposal) -> bool:
        return approval.still_current(proposal, self._server(), self.context.config.library_dir)

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
        server_url, approved_by, now = self._server(), self._approver(), self.clock()
        resolver_factory, generation = self.resolver_factory, self._generation
        self._set(self.changed, busy=True)

        def work() -> approval.Proposal:
            return approval.propose(
                library_dir,
                server_url,
                now=now,
                approved_by=approved_by,
                resolver_factory=resolver_factory,
            )

        def done(proposal: approval.Proposal | None, error: BaseException | None) -> None:
            self._proposed(generation, proposal, error)

        self.context.tasks.submit(work, done)

    def _proposed(
        self, generation: int, proposal: approval.Proposal | None, error: BaseException | None
    ) -> None:
        self._set(self.changed, busy=False)
        stale = proposal is not None and not self._still_current(proposal)
        if generation != self._generation or stale:
            # The settings changed while Tailscale was asked: its answer is for the old ones.
            self.toast.emit(approval.STALE, "info")
            return
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
            classesText=approval.classes_text(made.classes),
            expiresText=f"{approval.local_date(made.expires_at)} ({approval.APPROVAL_DAYS} days)",
            approvedBy=made.approved_by,
            reasonText=proposal.reason,
        )

    @Slot()
    def confirm(self) -> None:
        """The dialog's Approve: the approval shown is written and bound, off the GUI thread
        (refused when the settings no longer name its server and library)."""
        proposal = self._proposal
        if proposal is None or self._v["busy"]:
            return
        server_url, library_dir = self._server(), self.context.config.library_dir
        now = self.clock()
        self._set(self.changed, busy=True)
        self._confirming = True

        def work() -> approval.Approved:
            return approval.confirm(
                proposal, server_url=server_url, library_dir=library_dir, now=now
            )

        self.context.tasks.submit(work, self._confirmed)

    def _confirmed(self, result: approval.Approved | None, error: BaseException | None) -> None:
        self._set(self.changed, busy=False)
        self._confirming = False
        self._proposal = None
        if error is not None:
            self._refused(error)
            return
        assert result is not None  # the task pool hands a result or an error
        self._close()
        made = result.approval
        if result.written:
            self.refresh()
            self.toast.emit(
                f"Uploads to {made.endpoint} are approved until "
                f"{approval.local_date(made.expires_at)} (approval {made.approval_id}).",
                "ok",
            )
        else:
            self._already(made)
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
            f"{approval.local_date(standing.expires_at)} (approval {standing.approval_id}); "
            "nothing was written.",
            "info",
        )

    def _refused(self, error: BaseException) -> None:
        """The dialog says why nothing was approved, with Close only. Anything but a refusal
        is a bug: it is shown the same way (no Approve is left that does nothing), then
        raised."""
        refused = isinstance(error, approval.ApprovalRefused)
        self._proposal = None
        self._set(
            self.dialogChanged,
            dialogOpen=True,
            canApprove=False,
            dialogTitle=REFUSED_TITLE,
            dialogText=str(error) if refused else f"Something failed: {type(error).__name__}.",
            reasonText="",
        )
        if not refused:
            raise error

    def _close(self) -> None:
        self._set(self.dialogChanged, dialogOpen=False, canApprove=False)
