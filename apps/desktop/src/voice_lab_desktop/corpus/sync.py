"""The sync engine: each take's outbox to the PC, one operation at a time, in order (D-052).

``SyncEngine.run_pass`` is one blocking pass, run off the GUI thread (``client.py`` hands it
to the app's task pool). A pass:

1. asks the PC for its active catalog (``CatalogCache.refresh``, ``If-None-Match``). No
   answer, or a refusal: the pass stops there and nothing in any outbox changes. This read
   sends nothing of the corpus, so it runs even while sending is held, and so does the last
   step's read of the PC's library (``LibraryCache.refresh``, ``If-None-Match``), which the
   prompt studio counts coverage from;
2. sends nothing while the settings hold ``corpus_sync`` off (``enabled``, read before every
   operation): off until spike S-1 has passed on this platform (D-042's reason);
3. takes the takes in the order they were saved and, for each, the operations at the front
   of its outbox until one cannot finish now:

   - **upload**: ``cole_voice_lab.intake_client.upload_session`` with the package class
     ``capture``, under the standing home-host approval bound in this machine's library and
     its ledger (D-021), as ``prompted/uploads.py`` and ``measurement/home_host.py`` do; the
     session id is the take id. It waits for the intake receipt. A take the PC defers (its
     catalog version is not there yet) stays first in the outbox, its changes behind it, and
     the receipt is collected by a later pass;
   - **decision or trim**: the request (its id made once, kept for every retry) is
     submitted, then polled (``request_status``) until the PC's writer decides it. ``applied``:
     the revision here becomes the PC's.

**Only the PC's own verdicts on a take end its operations.** The intake's refusal of the
package (``refused_by_intake``) or another package under its id (``session_conflict``)
stops the take for good. A change refused for the take's own reason (``revision_conflict``:
another client changed the take; ``trim_out_of_bounds``; ``request_conflict``) is dropped
with every change queued behind it; the PC's state for the take is adopted once its library
shows at least the revision the refusal named (``TakeSync.refresh`` until then), and a
notice says so. ``take_not_found`` (the PC refused the take, or never saw it) checks the
upload again, once, then drops the change. Everything else is transient and keeps the
queue: no answer, the door or a proxy, a lost sign-in, a busy session, a server error, a
request the PC cannot read (version skew, noticed once), the tailnet check, the sign-in
provider. The operation counts the try and backs off; a pass the PC does not answer stops,
and the scheduler retries. Everything is on disk, so nothing is lost across a restart, and a
retried request reuses its id, so the PC never applies a change twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import httpx

from cole_voice_lab.api.contracts import IntakeSessionStatus
from cole_voice_lab.api.corpus_contracts import CorpusRequestStatus
from cole_voice_lab.errors import VoiceLabError
from cole_voice_lab.home_host import HomeHostApproval
from cole_voice_lab.intake_client import UploadError, UploadRefused, upload_session
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_ports import StorageError

from ..api import ApiClient, ApiError, ApiRefused, ApiUnavailable
from ..auth import AuthError, SignInRequired
from ..config import ConfigError
from ..measurement import home_host
from .catalog import CatalogCache, CatalogError
from .library import LibraryCache, LibraryError
from .records import ChangeOp, DecisionOp, NoticeKind, UploadOp, UploadReceipt
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
#: The intake's verdicts on the package itself: the take is never sent again.
#: ``refused_by_intake``: the publisher refused the take (``upload_session``);
#: ``session_conflict``: the PC holds another package under the take's id.
FINAL_UPLOAD_CODES: Final = frozenset({"refused_by_intake", "session_conflict"})
#: The door's or the API's refusals of the sign-in, with or without an ``ApiRefusal`` body
#: (``intake_client`` names a bodiless one ``status-<code>``).
SIGN_IN_UPLOAD_CODES: Final = frozenset({"unauthorized", "forbidden", "status-401", "status-403"})
#: The PC's verdicts on a change, for the take's own reason: adopt the PC's state.
ADOPT_OUTCOME_CODES: Final = frozenset({"revision_conflict", "trim_out_of_bounds"})
ADOPT_API_CODES: Final = frozenset({"request_conflict", "trim_out_of_bounds"})
#: The API cannot read the change's body: this app and the PC disagree on its format.
SKEW_API_CODES: Final = frozenset({"invalid_request", "invalid_json"})
#: Failures that say nothing about a take: the network, the sign-in's provider, the tailnet
#: check, this machine, a local package that fails its checks. The operation is tried later.
TRANSIENT: Final = (httpx.HTTPError, AuthError, VoiceLabError, OSError, ApiError)
#: What ``upload_session`` says when the PC holds the package but has not published it.
_NOT_PUBLISHED: Final = "but not yet published"
_UNREACHABLE: Final = "the home host is unreachable"
SKEW_NOTICE: Final = (
    "The PC cannot read this change ({code}): this app and the PC disagree on its format. It "
    "waits here until they agree (update the app or the PC)."
)


def _network(error: BaseException) -> bool:
    """Whether ``error`` means the PC (or the sign-in's provider) could not be reached."""
    if isinstance(error, SignInRequired):
        return False
    return isinstance(error, (httpx.HTTPError, StorageError, AuthError)) or (
        isinstance(error, ApiUnavailable) and error.status is None
    )


def _message(error: BaseException) -> str:
    return str(error) or type(error).__name__


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
    #: What blocks them is the home-host approval (none stands for this server covering
    #: ``capture``): Approve uploads to my PC mends it (``approval.py``).
    upload_needs_approval: bool = False
    catalog_changed: bool = False
    uploaded: int = 0
    applied: int = 0
    refused: int = 0
    #: Takes whose outbox still holds operations after the pass.
    waiting: int = 0
    #: The PC's library changed since it was last read (``library_error``: why it was not).
    library_changed: bool = False
    library_error: str = ""


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
        library: LibraryCache | None = None,
    ) -> None:
        self.spool = spool
        self.catalog = catalog
        self.library = library
        self.api = api
        self.uploader = uploader
        self.enabled = enabled
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.request_wait_seconds = request_wait_seconds
        self.poll_seconds = poll_seconds

    def run_pass(self, *, force: bool = False) -> PassResult:
        """One pass (``force``: operations in a backoff are tried now too). What it can
        describe is in the result, never raised."""
        result = PassResult(started_at=self.clock())
        try:
            self._run(result, force=force)
        finally:
            self.spool.record_pass(reached=result.reached, error=result.error or None)
        return result

    def _waiting(self) -> int:
        return sum(
            1 for take in self.spool.takes() if take.sync.outbox or take.sync.refresh is not None
        )

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
        except ApiRefused as exc:
            # The PC answered but will not serve this client (the door, a proxy, an older
            # server): nothing is sent this pass.
            result.reached, result.error = True, f"the PC's catalog: {exc}"
            result.waiting = self._waiting()
            return
        except (ApiError, CatalogError) as exc:
            result.error = f"the PC's catalog: {exc}"
        except TRANSIENT as exc:
            result.offline, result.error = _network(exc), _message(exc)
            return
        result.reached = True
        try:
            self._send(api, result, force=force)
        finally:
            if not (result.offline or result.sign_in_required):
                self._refresh_library(api, result)

    def _refresh_library(self, api: ApiClient, result: PassResult) -> None:
        """The PC's library, read last (after this pass's uploads and changes). A failure says
        nothing about the takes: it is noted and the pass ends as it was."""
        if self.library is None:
            return
        try:
            result.library_changed = self.library.refresh(api)
        except SignInRequired as exc:
            result.sign_in_required, result.error = True, str(exc)
        except (LibraryError, *TRANSIENT) as exc:
            result.library_error = f"the PC's library: {_message(exc)}"

    def _send(self, api: ApiClient, result: PassResult, *, force: bool) -> None:
        if not self.enabled():
            result.gated = True
            return
        uploads: _Uploads | None = None
        try:
            uploader = self.uploader()
            library, approval = uploader.approval()
            uploads = _Uploads(uploader, library, approval)
        except (home_host.HomeHostNotReady, ConfigError, *TRANSIENT) as exc:
            result.upload_blocked = _message(exc)
            result.upload_needs_approval = isinstance(exc, home_host.HomeHostNotReady)
        try:
            for take in self.spool.takes():
                if not take.sync.outbox and take.sync.refresh is None:
                    continue
                if not self._take(take.take_id, api, uploads, result, force):
                    result.waiting += 1
        except _Stop as exc:
            result.offline, result.error = exc.offline, str(exc)
            result.waiting = self._waiting()
        except SignInRequired as exc:
            result.sign_in_required, result.error = True, str(exc)
            result.waiting = self._waiting()

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
        Returns: whether nothing more waits for it."""
        while True:
            take = self.spool.take(take_id)
            if take.sync.refresh is not None:
                if not self._refresh(take, api, result):
                    return False
                continue
            if not take.sync.outbox:
                return True
            if not self.enabled():
                result.gated = True
                return False
            # The operation is read and marked in flight in one step: nothing folds into it
            # from here, so what is sent is what is kept.
            with self.spool.sending_head(take_id) as head:
                if head is None:
                    return True
                take, op = head
                if not force and op.not_before is not None and self.clock() < op.not_before:
                    return False
                request_id = None if isinstance(op, UploadOp) else op.request_id
                try:
                    if isinstance(op, UploadOp):
                        proceed = uploads is not None and self._upload(take, op, uploads, result)
                    elif take.sync.upload not in ("accepted", "remote"):
                        proceed = False  # the take's upload comes first
                    else:
                        proceed = self._change(take, op, api, result)
                except (_Stop, SignInRequired):
                    raise
                except ApiUnavailable as exc:
                    self.spool.tried(take_id, request_id, error=str(exc), retry_at=None)
                    raise _Stop(str(exc), offline=exc.status is None) from exc
                except TRANSIENT as exc:
                    message = _message(exc)
                    retry_at = self._retry_at(op.attempts + 1)
                    self.spool.tried(take_id, request_id, error=message, retry_at=retry_at)
                    result.error = f"{take_id}: {message}"
                    result.offline = result.offline or _network(exc)
                    return False
            if not proceed:
                return False

    def _refresh(self, take: SpooledTake, api: ApiClient, result: PassResult) -> bool:
        """Read the PC's state for a take waiting for it; ``True`` once it is installed."""
        try:
            history = api.prompt_takes(take.prompt_id)
        except SignInRequired:
            raise
        except ApiUnavailable as exc:
            raise _Stop(str(exc), offline=exc.status is None) from exc
        except TRANSIENT as exc:
            result.error = f"{take.take_id}: the PC's state: {_message(exc)}"
            result.offline = result.offline or _network(exc)
            return False
        pc = next((t for t in history.takes if t.take_id == take.take_id), None)
        return pc is not None and self.spool.refreshed(take.take_id, pc)

    # -- the upload ---------------------------------------------------------------------------

    def _upload(
        self, take: SpooledTake, op: UploadOp, uploads: _Uploads, result: PassResult
    ) -> bool:
        """Returns: whether the take's next operation may go now."""
        take_id = take.take_id
        try:
            status = uploads.uploader.upload(take.package, uploads.library, uploads.approval)
        except UploadRefused as exc:
            if exc.code in FINAL_UPLOAD_CODES:
                self.spool.upload_refused(take_id, code=exc.code, message=str(exc))
                result.refused += 1
                return True
            if exc.code in SIGN_IN_UPLOAD_CODES:
                self.spool.tried(take_id, None, error="sign-in required", retry_at=None)
                raise SignInRequired("the server refused the sign-in; sign in again") from exc
            # The door, a proxy, a busy session, a declaration the PC answers 4xx to: not a
            # verdict on the take.
            message = _message(exc)
            self.spool.tried(take_id, None, error=message, retry_at=self._retry_at(op.attempts + 1))
            result.error = f"{take_id}: {message}"
            return False
        except SignInRequired:
            self.spool.tried(take_id, None, error="sign-in required", retry_at=None)
            raise
        except UploadError as exc:
            message = _message(exc)
            if _UNREACHABLE in message:
                self.spool.tried(take_id, None, error=message, retry_at=None)
                raise _Stop(message, offline=True) from exc
            retry_at = self._retry_at(op.attempts + 1)
            if _NOT_PUBLISHED in message:
                self.spool.upload_waiting(take_id, error=message, retry_at=retry_at)
            else:
                self.spool.tried(take_id, None, error=message, retry_at=retry_at)
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
            return self._api_refused(take, op, api, result, exc)
        outcome = status.outcome
        if status.status == "queued" or outcome is None:
            return False  # the PC's writer has not decided it yet; polled next pass
        if status.status == "applied":
            assert outcome.revision is not None  # an applied outcome names its revision
            self.spool.change_applied(take_id, request_id, outcome.revision)
            result.applied += 1
            return True
        code = outcome.code or "refused"
        if code in ADOPT_OUTCOME_CODES:
            self._adopt(take, op, api, result, code=code, revision=outcome.revision)
            return True
        if code == "take_not_found":
            return self._take_not_found(take, op, result, renew=True)
        # Set aside unread (``invalid_request``), or a code this client does not know: not a
        # verdict on the take. Its id is spent, so the change goes again under a new one.
        message = f"the PC set the request aside ({code}): {outcome.detail or ''}".strip()
        self.spool.renew(
            take_id, request_id, error=message, retry_at=self._retry_at(op.attempts + 1)
        )
        result.error = f"{take_id}: {message}"
        return False

    def _api_refused(
        self, take: SpooledTake, op: ChangeOp, api: ApiClient, result: PassResult, exc: ApiRefused
    ) -> bool:
        take_id, request_id = take.take_id, op.request_id
        retry_at = self._retry_at(op.attempts + 1)
        if exc.code == "take_not_found":
            return self._take_not_found(take, op, result, renew=False)
        if exc.code == "request_not_found" and self.spool.resubmit(
            take_id, request_id, error=str(exc)
        ):
            return True  # the PC lost a request it had answered: the same id again
        if exc.code in ADOPT_API_CODES:
            self._adopt(take, op, api, result, code=exc.code, revision=None)
            return True
        notice: tuple[NoticeKind, str] | None = None
        if exc.code in SKEW_API_CODES:
            notice = ("change-blocked", SKEW_NOTICE.format(code=exc.code))
        self.spool.tried(take_id, request_id, error=str(exc), retry_at=retry_at, notice=notice)
        result.error = f"{take_id}: {exc}"
        return False

    def _await_outcome(self, api: ApiClient, status: CorpusRequestStatus) -> CorpusRequestStatus:
        """Poll a queued request until the PC's writer decides it, within the pass's wait."""
        deadline = self.monotonic() + self.request_wait_seconds
        while status.status == "queued" and self.monotonic() < deadline:
            self.sleep(self.poll_seconds)
            status = api.request_status(status.request_id)
        return status

    def _take_not_found(
        self, take: SpooledTake, op: ChangeOp, result: PassResult, *, renew: bool
    ) -> bool:
        """The PC has no record of the take: check its upload again, once (``renew``: the PC
        refused this request id, so the change goes again under a new one); then give up."""
        if self.spool.recheck_upload(take.take_id, op.request_id, renew=renew):
            return True
        self.spool.give_up(
            take.take_id,
            op.request_id,
            code="take_not_found",
            detail="the PC has no record of this take",
        )
        result.refused += 1
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
    ) -> None:
        """The PC refused the change for the take's own reason: drop what is queued and adopt
        the PC's state (now if its library shows it, else when a later pass reads it)."""
        pc = None
        try:
            history = api.prompt_takes(take.prompt_id)
            pc = next((t for t in history.takes if t.take_id == take.take_id), None)
        except (SignInRequired, *TRANSIENT):
            pc = None  # read again by a later pass (``TakeSync.refresh``)
        self.spool.adopt(take.take_id, op.request_id, pc=pc, code=code, revision=revision)
        result.refused += 1
