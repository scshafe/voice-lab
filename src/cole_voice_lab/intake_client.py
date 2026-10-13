"""Uploading a finalized package to the home host's intake API (D-021, D-024).

@canonical intake_client -- ``upload_session`` validates the package (a capture package, a
take package: ``corpus_takes``, D-052, or an imported Swift take: ``corpus_take_imports``,
D-053) locally with the same strict validator the publisher runs (so a symlink, an undeclared
file or a tampered recording is refused before a byte is sent), loads the standing home-host
approval, checks that the node answering for the approved endpoint is the approved node,
writes the ledger
entry naming the manifest digest, and then sends the package with the resumable protocol of
``api.staging``: every file from the byte the server already holds, in ``CHUNK_BYTES``
pieces with ``Content-Range``. It re-validates the local package before finalizing and
waits for the publisher's receipt, whose manifest digest must equal its own. The take stays
in the capturing host's library throughout; acceptance is a receipt, never a deletion.

``HomeHostClient`` connects to exactly the address the identity check returned, with the
certificate verified for the endpoint name (as the vault client does). A loopback variant
exists for tests only.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from .api.contracts import (
    ApiRefusal,
    ChunkAnswer,
    FileDeclaration,
    IntakeSessionRequest,
    IntakeSessionStatus,
)
from .canonical import canonical_json_bytes, open_regular_no_follow
from .corpus_take_imports import ValidatedTakeImport
from .corpus_takes import ValidatedTake
from .errors import SecurityError, VoiceLabError
from .home_host import (
    HomeHostApproval,
    HomeHostClass,
    HomeHostLedger,
    load_approval,
    record_sha256,
)
from .intake import ValidatedPackage, validate_package
from .models import utc_now
from .paths import AssetLayout
from .tailnet import ResolvedPeer, TailnetResolver, identity_problem

CHUNK_BYTES: Final = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS: Final = 120.0
# Finalizing re-hashes the whole package on the server.
FINALIZE_TIMEOUT_SECONDS: Final = 600.0
MAX_ROUNDS: Final = 3
MAX_ANSWER_BYTES: Final = 4 * 1024 * 1024
DEFAULT_WAIT_SECONDS: Final = 900.0
POLL_SECONDS: Final = 2.0

#: ``progress(path, bytes the server holds, size)`` after every chunk the server took.
type Progress = Callable[[str, int, int], None]


class UploadError(VoiceLabError):
    """The upload did not finish; it can be run again and resumes where it stopped."""


class UploadPending(UploadError):
    """Every byte is on the server, which has not published the package yet.

    It waits in the server's ``incoming/``: the publisher has not run, or it deferred the
    package (a take whose catalog version is not on the server yet). Running the upload again
    later collects the receipt; nothing is sent again."""

    def __init__(self, state: str) -> None:
        super().__init__(
            f"the package is {state} but not yet published; run the upload again later to "
            "collect the receipt"
        )
        self.state = state


class UploadRefused(VoiceLabError):
    """The server, or the publisher, refused the package; running it again will not help."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    document: Mapping[str, Any]


class HomeHostClient:
    """HTTPS to the approved node, one request per connection, bearer on every request."""

    def __init__(
        self,
        *,
        endpoint: str,
        resolver: TailnetResolver | None,
        bearer: Callable[[], str],
        tls_context: Callable[[], ssl.SSLContext] = ssl.create_default_context,
        loopback_port: int | None = None,
    ) -> None:
        if (resolver is None) == (loopback_port is None):
            raise ValueError("a client has a resolver, or (in tests only) a loopback port")
        self.endpoint = endpoint
        self._resolver = resolver
        self._bearer = bearer
        self._token: str | None = None
        self._tls_context = tls_context
        self._loopback_port = loopback_port
        self._peer: ResolvedPeer | None = None

    @classmethod
    def loopback_for_tests(cls, port: int, bearer: Callable[[], str]) -> HomeHostClient:
        """A client of a test server on 127.0.0.1, over plain HTTP, with no identity check."""
        return cls(endpoint="127.0.0.1", resolver=None, bearer=bearer, loopback_port=port)

    def check_identity(self, approval: HomeHostApproval) -> ResolvedPeer | None:
        """Resolve the endpoint and require the approved node; pin its address.

        Raises: ``SecurityError`` when the node answering is not the approved one.
        """
        if self._resolver is None:
            return None
        if self.endpoint != approval.endpoint:
            raise SecurityError("the client's endpoint is not the approved endpoint")
        peer = self._resolver.resolve(self.endpoint)
        problem = identity_problem(self.endpoint, approval.identity(), peer)
        if problem is not None:
            raise SecurityError(f"the home host failed its identity check: {problem}")
        self._peer = peer
        return peer

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self._loopback_port is not None:
            return http.client.HTTPConnection("127.0.0.1", self._loopback_port, timeout=timeout)
        if self._peer is None:
            raise SecurityError("the home host's identity was not checked")
        connection = http.client.HTTPSConnection(
            self.endpoint, self._peer.port, timeout=timeout, context=self._tls_context()
        )
        address = (self._peer.address, self._peer.port)
        context = self._tls_context()
        endpoint = self.endpoint

        def connect() -> None:
            raw = socket.create_connection(address, timeout=timeout)
            connection.sock = context.wrap_socket(raw, server_hostname=endpoint)

        connection.connect = connect  # type: ignore[method-assign]
        return connection

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Answer:
        """One request; network failures are ``UploadError`` (the upload may be resumed)."""
        if self._token is None:
            self._token = self._bearer()
        all_headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }
        connection = self._connection(timeout)
        try:
            connection.request(method, path, body=body, headers=all_headers)
            response = connection.getresponse()
            data = response.read(MAX_ANSWER_BYTES + 1)
            status = response.status
        except (OSError, http.client.HTTPException) as error:
            raise UploadError(f"the home host is unreachable ({type(error).__name__})") from None
        finally:
            connection.close()
        if 300 <= status < 400:
            raise UploadError(f"the home host redirected ({status}); sign in again")
        try:
            document = json.loads(data) if data else {}
        except ValueError:
            document = {}
        if not isinstance(document, dict):
            document = {}
        return Answer(status, document)


def _model[ModelT: BaseModel](answer: Answer, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate_json(json.dumps(answer.document))
    except ValidationError as error:
        raise UploadError(f"the home host answered with an invalid {model.__name__}") from error


def _refusal(answer: Answer) -> ApiRefusal | None:
    try:
        return ApiRefusal.model_validate_json(json.dumps(answer.document))
    except ValidationError:
        return None


def _fail(answer: Answer) -> VoiceLabError:
    refusal = _refusal(answer)
    code = refusal.error if refusal else f"status-{answer.status}"
    detail = refusal.detail if refusal else "no refusal body"
    if answer.status in {408, 429} or answer.status >= 500:
        return UploadError(f"{code}: {detail}")
    return UploadRefused(code, detail)


type AnyPackage = ValidatedPackage | ValidatedTake | ValidatedTakeImport


def session_request(package: AnyPackage) -> IntakeSessionRequest:
    """The declaration of every file of a validated package."""
    files = tuple(
        FileDeclaration(path=path, sha256=item.sha256, size_bytes=item.size_bytes)
        for path, item in sorted(package._snapshot.files.items())
    )
    return IntakeSessionRequest(
        session_id=package.session_id,
        manifest_sha256=package.manifest_sha256,
        files=files,
    )


def _session_path(session_id: str, *rest: str) -> str:
    return "/".join(("/api/v1/intake/sessions", session_id, *rest))


def _send_file(
    client: HomeHostClient,
    source: Path,
    session_id: str,
    declared: FileDeclaration,
    received: int,
    progress: Progress | None = None,
) -> None:
    path = _session_path(session_id, "files", urllib.parse.quote(declared.path, safe=""))
    restarted = False
    with open_regular_no_follow(source) as stream:
        while True:
            stream.seek(received)
            length = min(CHUNK_BYTES, declared.size_bytes - received)
            body = stream.read(length)
            if len(body) != length:
                raise SecurityError(f"{declared.path} changed while it was being sent")
            content_range = (
                f"bytes {received}-{received + length - 1}/{declared.size_bytes}"
                if length
                else f"bytes */{declared.size_bytes}"
            )
            answer = client.request(
                "PUT",
                path,
                body=body,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": content_range,
                },
            )
            if answer.status in {200, 202}:
                chunk = _model(answer, ChunkAnswer)
                if progress is not None:
                    done = (
                        declared.size_bytes if chunk.state == "received" else chunk.received_bytes
                    )
                    progress(declared.path, done, declared.size_bytes)
                if chunk.state == "received":
                    return
                received = chunk.received_bytes
                continue
            refusal = _refusal(answer)
            code = refusal.error if refusal else None
            if code == "file_already_received":
                return
            if code == "range_not_contiguous" and refusal and refusal.received_bytes is not None:
                received = refusal.received_bytes
                continue
            if code == "checksum_mismatch" and not restarted:
                restarted, received = True, 0
                continue
            raise _fail(answer)


def upload_session(
    session: Path,
    *,
    layout: AssetLayout,
    client: HomeHostClient,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    package_class: HomeHostClass = "capture",
    progress: Progress | None = None,
    expected_manifest_sha256: str | None = None,
) -> IntakeSessionStatus:
    """Upload one finalized package and wait for its intake receipt.

    ``package_class`` is what the package is (a prompted ``capture``, which a take package
    and an imported take are too, or a UMIK-1 ``measurement`` session,
    journey/measurement_package.py): the standing approval must cover it. ``progress`` hears
    every chunk the server took. ``expected_manifest_sha256``, when given, is the manifest
    digest a transfer approval names (``corpus_import``): a package with another manifest is
    refused before anything is recorded or sent.

    Returns: the ``accepted`` status, whose receipt names this package's manifest digest.
    Raises: ``SecurityError``/``ContractError`` when the local package or the approval
    fails its checks (nothing is sent); ``UploadRefused`` when the server or the publisher
    refuses it; ``UploadPending`` (an ``UploadError``) when every byte is on the server but it
    has not published the package within ``wait_seconds``; ``UploadError`` when it did not
    finish (run it again: it resumes).
    """
    package = validate_package(session)
    if expected_manifest_sha256 is not None and package.manifest_sha256 != (
        expected_manifest_sha256
    ):
        raise SecurityError("the package's manifest is not the one its transfer approval names")
    approval, approval_sha256 = load_approval(layout, now=clock())
    if package_class not in approval.classes:
        raise SecurityError(f"the home-host approval does not cover {package_class} packages")
    request = session_request(package)
    peer = client.check_identity(approval)
    ledger = HomeHostLedger(layout)
    common = {
        "approval_id": approval.approval_id,
        "approval_sha256": approval_sha256,
        "endpoint": approval.endpoint,
        "node_stable_id": peer.node_stable_id if peer else approval.node_stable_id,
        "session_id": request.session_id,
        "manifest_sha256": request.manifest_sha256,
    }
    ledger.append(event="upload-started", at=clock(), **common)
    try:
        status = _transfer(client, session, package, request, progress)
        status = _await_publication(client, request, status, wait_seconds, sleep, monotonic)
    except UploadRefused as refused:
        ledger.append(event="upload-refused", at=clock(), detail=str(refused)[:2000], **common)
        raise
    receipt = status.receipt
    if (
        receipt is None
        or receipt.session_id != request.session_id
        or receipt.manifest_sha256 != package.manifest_sha256
    ):
        raise SecurityError("the intake receipt does not name this package's manifest")
    ledger.append(
        event="upload-completed", at=clock(), receipt_sha256=record_sha256(receipt), **common
    )
    return status


def _transfer(
    client: HomeHostClient,
    session: Path,
    package: AnyPackage,
    request: IntakeSessionRequest,
    progress: Progress | None = None,
) -> IntakeSessionStatus:
    body = canonical_json_bytes(request)
    answer = client.request(
        "POST",
        "/api/v1/intake/sessions",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    if answer.status not in {200, 201}:
        raise _fail(answer)
    status = _model(answer, IntakeSessionStatus)
    for _round in range(MAX_ROUNDS):
        if status.state != "open":
            return status
        for file in status.files:
            if file.complete:
                continue
            declared = request.declared(file.path)
            if declared is None:
                raise SecurityError("the server reports a file this package does not declare")
            _send_file(
                client,
                session / file.path,
                request.session_id,
                declared,
                file.received_bytes,
                progress,
            )
        again = validate_package(session)
        if again._snapshot != package._snapshot:
            raise SecurityError("the package changed while it was being uploaded")
        answer = client.request(
            "POST",
            _session_path(request.session_id, "finalize"),
            timeout=FINALIZE_TIMEOUT_SECONDS,
        )
        if answer.status == 200:
            return _model(answer, IntakeSessionStatus)
        refusal = _refusal(answer)
        if refusal is None or refusal.error not in {"incomplete", "checksum_mismatch"}:
            raise _fail(answer)
        answer = client.request("GET", _session_path(request.session_id))
        if answer.status != 200:
            raise _fail(answer)
        status = _model(answer, IntakeSessionStatus)
    raise UploadError("the upload did not complete after several rounds; run it again")


def _await_publication(
    client: HomeHostClient,
    request: IntakeSessionRequest,
    status: IntakeSessionStatus,
    wait_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> IntakeSessionStatus:
    deadline = monotonic() + wait_seconds
    while True:
        if status.state == "accepted":
            return status
        if status.state == "refused":
            raise UploadRefused("refused_by_intake", status.refusal or "no reason recorded")
        if monotonic() >= deadline:
            raise UploadPending(status.state)
        sleep(POLL_SECONDS)
        answer = client.request("GET", _session_path(request.session_id))
        if answer.status != 200:
            raise _fail(answer)
        status = _model(answer, IntakeSessionStatus)
