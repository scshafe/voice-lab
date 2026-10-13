"""The sync engine: each take's outbox to the PC, one operation at a time, in order (D-052).

``SyncEngine.run_pass`` is one blocking pass, run off the GUI thread (``client.py`` hands it
to the app's task pool). A pass:

1. asks the PC for its active catalog (``CatalogCache.refresh``, ``If-None-Match``). No
   answer: the pass stops there, offline, and nothing in any outbox changes. This read sends
   nothing of the corpus, so it runs even while sending is held;
2. sends nothing unless the settings turn ``corpus_sync`` on (``enabled``): off until spike
   S-1 has passed on this platform (D-042's reason; the owner turns it on);
3. takes the takes in the order they were saved and, for each, the operations at the front
   of its outbox until one cannot finish now:

   - **upload**: ``cole_voice_lab.intake_client.upload_session`` with the package class
     ``capture``, under the standing home-host approval bound in this machine's library and
     its ledger (D-021), as ``prompted/uploads.py`` and ``measurement/home_host.py`` do; the
     session id is the take id. It waits for the intake receipt. A take the PC defers (its
     catalog version is not there yet) stays first in the outbox, its changes behind it, and
     the receipt is collected by a later pass; a take the PC refuses is never sent again,
     and nothing queued for it is either (a notice says so);
   - **decision or trim**: the request (its id made once, kept for every retry) is
     submitted, then polled (``request_status``) until the PC's writer decides it. ``applied``:
     the revision here becomes the PC's. ``revision_conflict`` (another client changed the
     take) or any other refusal: the PC is canonical; its state for the take is adopted (from
     the prompt's history), every change still queued for the take is dropped, and a
     "changed elsewhere" notice is kept for the UI. ``take_not_found``: the PC refused the take
     or never saw it, so the upload is checked again, once.

A network failure leaves every outbox as it is (the operation's tries are counted) and stops
the pass; the scheduler retries with a backoff. Everything is on disk, so nothing is lost
across a restart, and a retried request reuses its id, so the PC never applies a change twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from cole_voice_lab.api.contracts import IntakeSessionStatus
from cole_voice_lab.api.corpus_contracts import CorpusRequestStatus
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.home_host import HomeHostApproval
from cole_voice_lab.intake_client import UploadError, UploadRefused, upload_session
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout

from ..api import ApiClient, ApiError, ApiRefused, ApiUnavailable
from ..auth import SignInRequired
from ..config import ConfigError
from ..measurement import home_host
from .catalog import CatalogCache, CatalogError
from .records import ChangeOp, DecisionOp, UploadOp, UploadReceipt
from .spool import CorpusSpool, SpooledTake, backoff

#: How long one upload waits for the PC's receipt before a later pass collects it.
UPLOAD_WAIT_SECONDS: Final = 30.0
#: How long a submitted change is polled for the PC's writer within one pass.
REQUEST_WAIT_SECONDS: Final = 10.0
POLL_SECONDS: Final = 1.0
#: An operation that failed is not tried again for this long, doubled per try, at most
#: ``RETRY_CAP_SECONDS``.
RETRY_BASE_SECONDS: Final = 30.0
RETRY_CAP_SECONDS: Final = 900.0
#: What ``upload_session`` says when the PC holds the package but has not published it.
_NOT_PUBLISHED: Final = "but not yet published"
_UNREACHABLE: Final = "the home host is unreachable"


class _Stop(Exception):
    """The pass stops: the PC did not answer (``offline``) or answered that it cannot serve
    now (5xx, 429)."""

    def __init__(self, message: str, *, offline: bool) -> None:
        super().__init__(message)
        self.offline = offline


@dataclass
class TakeUploader:
    """How take packages reach the PC: this machine's library (the approval and the ledger),
    the configured server, the bearer, and the client that talks to the approved node
    (``home_host.tailnet_client``; tests use a loopback one)."""

    library_dir: Path
    server_url: str
    bearer: Callable[[], str]
    client_factory: home_host.ClientFactory = home_host.tailnet_client
    wait_seconds: float = UPLOAD_WAIT_SECONDS
    clock: Callable[[], datetime] = utc_now
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

    def approval(self) -> tuple[AssetLayout, HomeHostApproval]:
        """The library and its standing approval, which must name the server and cover
        ``capture``. Raises: ``HomeHostNotReady``; ``SecurityError`` for an unsafe library."""
        library = home_host.library_layout(self.library_dir)
        approval = home_host.approval_for(
            library, self.server_url, now=self.clock(), package_class="capture"
        )
        return library, approval

    def upload(
        self, package: Path, library: AssetLayout, approval: HomeHostApproval
    ) -> IntakeSessionStatus:
        """One take package, resumably, and its receipt (``upload_session``)."""
        return upload_session(
            package,
            layout=library,
            client=self.client_factory(approval, self.bearer),
            package_class="capture",
            wait_seconds=self.wait_seconds,
            clock=self.clock,
            sleep=self.sleep,
            monotonic=self.monotonic,
        )


@dataclass
class PassResult:
    """What one pass did."""

    started_at: datetime
    #: The PC answered (its catalog, or that it has none).
    reached: bool = False
    #: The PC did not answer, during the pass or before it.
    offline: bool = False
    sign_in_required: bool = False
    #: Sending is held (``corpus_sync`` is off).
    gated: bool = False
    error: str = ""
    #: Why no upload could start (no standing approval, another server, ...).
    upload_blocked: str = ""
    catalog_changed: bool = False
    uploaded: int = 0
    applied: int = 0
    refused: int = 0
    #: Takes whose outbox still holds operations after the pass.
    waiting: int = 0


@dataclass(frozen=True)
class _Uploads:
    uploader: TakeUploader
    library: AssetLayout
    approval: HomeHostApproval


class SyncEngine:
    """Sends the spool's outboxes to the PC (``run_pass``)."""

    def __init__(
        self,
        spool: CorpusSpool,
        catalog: CatalogCache,
        *,
        api: Callable[[], ApiClient],
        uploader: Callable[[], TakeUploader],
        enabled: Callable[[], bool],
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        request_wait_seconds: float = REQUEST_WAIT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.spool = spool
        self.catalog = catalog
        self.api = api
        self.uploader = uploader
        self.enabled = enabled
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.request_wait_seconds = request_wait_seconds
        self.poll_seconds = poll_seconds

    def run_pass(self, *, force: bool = False) -> PassResult:
        """One pass (``force``: operations in a backoff are tried now too). Raises nothing
        it can describe: the result says what happened."""
        result = PassResult(started_at=self.clock())
        try:
            self._run(result, force=force)
        finally:
            self.spool.record_pass(reached=result.reached, error=result.error or None)
        return result

    def _run(self, result: PassResult, *, force: bool) -> None:
        try:
            api = self.api()
        except ConfigError as exc:
            result.offline, result.error = True, str(exc)
            return
        try:
            result.catalog_changed = self.catalog.refresh(api).changed
        except ApiUnavailable as exc:
            result.offline, result.error = exc.status is None, str(exc)
            return
        except SignInRequired as exc:
            result.sign_in_required, result.error = True, str(exc)
            return
        except (ApiError, CatalogError) as exc:
            result.error = f"the PC's catalog: {exc}"
        result.reached = True
        if not self.enabled():
            result.gated = True
            return
        uploads: _Uploads | None = None
        try:
            uploader = self.uploader()
            library, approval = uploader.approval()
            uploads = _Uploads(uploader, library, approval)
        except (
            home_host.HomeHostNotReady,
            ConfigError,
            SecurityError,
            ContractError,
            OSError,
        ) as exc:
            result.upload_blocked = str(exc) or type(exc).__name__
        try:
            for take in self.spool.takes():
                if take.sync.outbox and not self._take(take.take_id, api, uploads, result, force):
                    result.waiting += 1
        except _Stop as exc:
            result.offline, result.error = exc.offline, str(exc)
            result.waiting = sum(1 for take in self.spool.takes() if take.sync.outbox)
        except SignInRequired as exc:
            result.sign_in_required, result.error = True, str(exc)
            result.waiting = sum(1 for take in self.spool.takes() if take.sync.outbox)

    def _retry_at(self, attempts: int) -> datetime:
        return self.clock() + backoff(attempts, base=RETRY_BASE_SECONDS, cap=RETRY_CAP_SECONDS)

    def _take(
        self,
        take_id: str,
        api: ApiClient,
        uploads: _Uploads | None,
        result: PassResult,
        force: bool,
    ) -> bool:
        """The take's operations, front first, until one cannot finish now.
        Returns: whether its outbox is empty."""
        while True:
            take = self.spool.take(take_id)
            if not take.sync.outbox:
                return True
            op = take.sync.outbox[0]
            if not force and op.not_before is not None and self.clock() < op.not_before:
                return False
            if isinstance(op, UploadOp):
                if uploads is None or not self._upload(take, op, uploads, result):
                    return False
            elif take.sync.upload != "accepted" or not self._change(take, op, api, result):
                return False

    # -- the upload ---------------------------------------------------------------------------

    def _upload(
        self, take: SpooledTake, op: UploadOp, uploads: _Uploads, result: PassResult
    ) -> bool:
        """Returns: whether the take's next operation may go now."""
        take_id = take.take_id
        try:
            status = uploads.uploader.upload(take.package, uploads.library, uploads.approval)
        except UploadRefused as exc:
            if exc.code in ("unauthorized", "forbidden"):
                self.spool.tried(take_id, None, error="sign-in required", retry_at=None)
                raise SignInRequired("the server refused the sign-in; sign in again") from exc
            self.spool.upload_refused(take_id, code=exc.code, message=str(exc))
            result.refused += 1
            return True
        except SignInRequired:
            self.spool.tried(take_id, None, error="sign-in required", retry_at=None)
            raise
        except UploadError as exc:
            message = str(exc) or type(exc).__name__
            if _UNREACHABLE in message:
                self.spool.tried(take_id, None, error=message, retry_at=None)
                raise _Stop(message, offline=True) from exc
            retry_at = self._retry_at(op.attempts + 1)
            if _NOT_PUBLISHED in message:
                self.spool.upload_waiting(take_id, error=message, retry_at=retry_at)
            else:
                self.spool.tried(take_id, None, error=message, retry_at=retry_at)
            return False
        except (SecurityError, ContractError, OSError) as exc:
            message = str(exc) or type(exc).__name__
            self.spool.tried(take_id, None, error=message, retry_at=self._retry_at(op.attempts + 1))
            result.error = f"{take_id}: {message}"
            return False
        receipt = status.receipt
        assert receipt is not None  # upload_session returns only an accepted status
        self.spool.upload_accepted(
            take_id,
            UploadReceipt(
                manifest_sha256=receipt.manifest_sha256,
                approval_id=uploads.approval.approval_id,
                server=uploads.uploader.server_url,
                accepted_at=self.clock(),
            ),
        )
        result.uploaded += 1
        return True

    # -- decisions and trims ------------------------------------------------------------------

    def _change(self, take: SpooledTake, op: ChangeOp, api: ApiClient, result: PassResult) -> bool:
        """Returns: whether the take's next operation may go now."""
        take_id, request_id = take.take_id, op.request_id
        try:
            if op.submitted_at is None:
                if isinstance(op, DecisionOp):
                    status = api.submit_decision(take_id, op.body())
                else:
                    status = api.submit_trim(take_id, op.body())
                self.spool.change_submitted(take_id, request_id)
            else:
                status = api.request_status(request_id)
            status = self._await_outcome(api, status)
        except ApiRefused as exc:
            if exc.code == "take_not_found":
                return self._take_not_found(take, op, api, result, renew=False)
            if exc.code == "request_not_found" and op.attempts < 3:
                # The PC lost a request it had answered: it is submitted again, same id.
                self.spool.resubmit(take_id, request_id, error=str(exc))
                return True
            self._adopt(take, op, api, result, code=exc.code, revision=None, detail=str(exc))
            return True
        except ApiUnavailable as exc:
            self.spool.tried(take_id, request_id, error=str(exc), retry_at=None)
            raise _Stop(str(exc), offline=exc.status is None) from exc
        except ApiError as exc:
            message = str(exc) or type(exc).__name__
            self.spool.tried(
                take_id, request_id, error=message, retry_at=self._retry_at(op.attempts + 1)
            )
            result.error = f"{take_id}: {message}"
            return False
        outcome = status.outcome
        if status.status == "queued" or outcome is None:
            return False  # the PC's writer has not decided it yet; polled next pass
        if status.status == "applied":
            assert outcome.revision is not None  # an applied outcome names its revision
            self.spool.change_applied(take_id, request_id, outcome.revision)
            result.applied += 1
            return True
        code = outcome.code or "refused"
        if code == "take_not_found":
            return self._take_not_found(take, op, api, result, renew=True)
        self._adopt(take, op, api, result, code=code, revision=outcome.revision)
        return True

    def _await_outcome(self, api: ApiClient, status: CorpusRequestStatus) -> CorpusRequestStatus:
        """Poll a queued request until the PC's writer decides it, within the pass's wait."""
        deadline = self.monotonic() + self.request_wait_seconds
        while status.status == "queued" and self.monotonic() < deadline:
            self.sleep(self.poll_seconds)
            status = api.request_status(status.request_id)
        return status

    def _take_not_found(
        self, take: SpooledTake, op: ChangeOp, api: ApiClient, result: PassResult, *, renew: bool
    ) -> bool:
        """The PC has no record of the take: check its upload again, once (``renew``: the PC
        refused this request id, so the change goes again under a new one)."""
        if self.spool.recheck_upload(take.take_id, op.request_id, renew=renew):
            return True
        self._adopt(
            take,
            op,
            api,
            result,
            code="take_not_found",
            revision=None,
            detail="the PC has no record of this take",
            read_pc=False,
        )
        return True

    def _adopt(
        self,
        take: SpooledTake,
        op: ChangeOp,
        api: ApiClient,
        result: PassResult,
        *,
        code: str,
        revision: int | None,
        detail: str = "",
        read_pc: bool = True,
    ) -> None:
        """The PC refused the change: adopt its state for the take, drop what is queued."""
        pc = None
        if read_pc:
            try:
                history = api.prompt_takes(take.prompt_id)
                pc = next((t for t in history.takes if t.take_id == take.take_id), None)
            except ApiUnavailable as exc:
                # Nothing changes here: the next pass reads the same refusal and adopts then.
                raise _Stop(str(exc), offline=exc.status is None) from exc
            except ApiError:
                pc = None
        self.spool.adopt(
            take.take_id, op.request_id, pc=pc, code=code, revision=revision, detail=detail
        )
        result.refused += 1
