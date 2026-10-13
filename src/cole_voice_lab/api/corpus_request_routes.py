"""The corpus's decision and trim routes (D-052, docs/design/RECORDING-V1.md step 4).

@canonical api.corpus_request_routes -- the API cannot write the corpus: its lab mount is
read-only except ``incoming/`` (D-021). So a decision or a trim is queued there as a request
(``corpus_requests``) that the PC's one lab writer, the intake publisher, applies in order and
judges against the take's current revision. These routes check only what they can know
without writing anything else: the take id, that the PC knows the take, a trim's frames, and
the body. They never check ``expected_revision``: the read model may lag behind requests
already queued, so the publisher is the judge.

``POST /api/v1/corpus/takes/{take_id}/decision`` (``TakeDecisionRequest``) and
``PUT /api/v1/corpus/takes/{take_id}/trim`` (``TakeTrimRequest``), bodies of at most
``MAX_BODY_BYTES``:

- 202 ``CorpusRequestStatus`` with ``Location: /api/v1/corpus/requests/<request_id>``:
  ``queued`` when the request was written now or waits already, ``applied``/``refused`` when
  the publisher decided it (a retry with the same id and body answers as before);
- 409 ``request_conflict``: the id was used for another body (another take or change);
- 404 ``take_not_found``: not a take id, or a take the PC has no record of whose package is
  not uploading or waiting in ``incoming/`` either (a request for a take on its way waits for
  it);
- 422 ``trim_out_of_bounds``: a trim that ends after the take's last frame (checked against
  the take's record when it has one; the publisher checks again);
- 400 ``invalid_json``, 422 ``invalid_request``, 411 ``content_length_required``, 413
  ``body_too_large`` for the body;
- 503 ``request_queue_full`` while ``MAX_QUEUED_REQUESTS`` requests wait (the publisher is
  not running), with ``Retry-After``;
- 500 ``take_invalid`` when the take's record fails its checks.

``GET|HEAD /api/v1/corpus/requests/{request_id}``: 200 ``CorpusRequestStatus`` (the outcome
once there is one, ``queued`` while the request file waits), else 404 ``request_not_found``.

No audio is involved, so no transfer approval or ledger entry; the bearer token is checked as
on every ``/api/`` route.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ValidationError

from ..canonical import canonical_json_bytes
from ..corpus_requests import (
    MAX_QUEUED_REQUESTS,
    REQUEST_ID_PATTERN,
    CorpusRequest,
    CorpusRequestOutcome,
    DecisionChange,
    TrimChange,
    nanoseconds,
    queue_request,
    queued_requests,
    read_outcome,
    read_request_file,
    take_on_its_way,
)
from ..corpus_take_store import read_take_state
from ..corpus_takes import TAKE_ID_PATTERN
from ..errors import ContractError, SecurityError
from ..models import COLE_SUBJECT, utc_now
from ..paths import AssetLayout
from .corpus_contracts import CorpusRequestStatus, TakeDecisionRequest, TakeTrimRequest
from .routing import RouteRefusal, RouteRequest, RouteResponse

REQUESTS_PATH: Final = "/api/v1/corpus/requests"
MAX_BODY_BYTES: Final = 8 * 1024
#: How long a client waits before it sends again into a full queue.
QUEUE_RETRY_SECONDS: Final = 60
_DECISION_PATH: Final = re.compile(r"^/api/v1/corpus/takes/(?P<take_id>[^/]+)/decision$")
_TRIM_PATH: Final = re.compile(r"^/api/v1/corpus/takes/(?P<take_id>[^/]+)/trim$")
_STATUS_PATH: Final = re.compile(r"^/api/v1/corpus/requests/(?P<request_id>[^/]+)$")

type _Change = DecisionChange | TrimChange


def _only(request: RouteRequest, *methods: str) -> None:
    if request.method not in methods:
        raise RouteRefusal(405, "method_not_allowed", f"this route answers {', '.join(methods)}")


def _take_id(take_id: str) -> None:
    if not re.fullmatch(TAKE_ID_PATTERN, take_id):
        raise RouteRefusal(404, "take_not_found", "no take has this id")


def _status(
    request_id: str,
    queued: CorpusRequest | None,
    outcome: CorpusRequestOutcome | None,
) -> CorpusRequestStatus:
    if outcome is not None:
        return CorpusRequestStatus(
            request_id=request_id,
            take_id=outcome.take_id,
            status=outcome.status,
            received_at=outcome.received_at,
            outcome=outcome,
        )
    assert queued is not None
    return CorpusRequestStatus(
        request_id=request_id,
        take_id=queued.take_id,
        status="queued",
        received_at=queued.received_at,
        outcome=None,
    )


class CorpusRequestRoutes:
    """The decision, trim and request-status routes over the lab at ``layout``.

    ``clock`` is the API's (each request's ``received_at``); ``max_queued`` bounds the
    requests that may wait in ``incoming/``. One lock serializes this server's writers of the
    queue, so a request id is checked and queued in one step."""

    def __init__(
        self,
        layout: AssetLayout,
        subject_id: str = COLE_SUBJECT,
        *,
        clock: Callable[[], datetime] = utc_now,
        max_queued: int = MAX_QUEUED_REQUESTS,
    ) -> None:
        self.layout = layout
        self.subject_id = subject_id
        self.clock = clock
        self.max_queued = max_queued
        self._lock = threading.Lock()

    def __call__(self, request: RouteRequest) -> RouteResponse | None:
        if (match := _DECISION_PATH.fullmatch(request.path)) is not None:
            _only(request, "POST")
            _take_id(match["take_id"])
            body = self._body(request, TakeDecisionRequest)
            return self._submit(match["take_id"], body.request_id, body.change())
        if (match := _TRIM_PATH.fullmatch(request.path)) is not None:
            _only(request, "PUT")
            _take_id(match["take_id"])
            trim = self._body(request, TakeTrimRequest)
            return self._submit(match["take_id"], trim.request_id, trim.change())
        if (match := _STATUS_PATH.fullmatch(request.path)) is not None:
            _only(request, "GET", "HEAD")
            return self._request_status(match["request_id"])
        return None

    @staticmethod
    def _body[Body: BaseModel](request: RouteRequest, model: type[Body]) -> Body:
        if request.content_length is None:
            raise RouteRefusal(411, "content_length_required", "a JSON body needs Content-Length")
        payload = request.read_body(MAX_BODY_BYTES)
        try:
            return model.model_validate_json(payload)
        except ValidationError as error:
            if any(problem["type"] == "json_invalid" for problem in error.errors()):
                raise RouteRefusal(400, "invalid_json", "the body is not JSON") from error
            raise RouteRefusal(
                422, "invalid_request", f"the body is not a valid {model.__name__}"
            ) from error

    def _known(self, request_id: str) -> tuple[CorpusRequest | None, CorpusRequestOutcome | None]:
        """The request as queued (``None`` when no file waits) and its outcome (``None``
        while it has none). The queue is read before the outcome: the publisher writes the
        outcome before it removes the file, so a request is always found in one of them."""
        try:
            entry = next(
                (e for e in queued_requests(self.layout) if e.request_id == request_id), None
            )
            queued = None
            if entry is not None:
                try:
                    queued = read_request_file(self.layout.directory("incoming") / entry.name)
                except FileNotFoundError:
                    queued = None  # decided meanwhile: its outcome is there now
            return queued, read_outcome(self.layout, request_id)
        except (ContractError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "internal_error", "the request cannot be read") from error

    def _request_status(self, request_id: str) -> RouteResponse:
        if not re.fullmatch(REQUEST_ID_PATTERN, request_id):
            raise RouteRefusal(404, "request_not_found", "no request has this id")
        queued, outcome = self._known(request_id)
        if queued is None and outcome is None:
            raise RouteRefusal(404, "request_not_found", "this server has no such request")
        return self._answer(200, _status(request_id, queued, outcome))

    def _answer(self, status: int, view: CorpusRequestStatus) -> RouteResponse:
        return RouteResponse(
            status=status,
            route="corpus.request",
            body=canonical_json_bytes(view) + b"\n",
            headers=(("Location", f"{REQUESTS_PATH}/{view.request_id}"),),
        )

    def _frames(self, take_id: str) -> int | None:
        """The take's frame count from its record, or ``None`` for a take on its way.

        Raises: ``RouteRefusal`` 404 ``take_not_found``, 500 ``take_invalid``."""
        try:
            state = read_take_state(self.layout, take_id)
            if state is None:
                if take_on_its_way(self.layout, take_id):
                    return None
                raise RouteRefusal(404, "take_not_found", "this server has no record of the take")
        except (ContractError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "take_invalid", f"the take's record: {error}") from error
        if state.subject_id != self.subject_id:
            raise RouteRefusal(404, "take_not_found", "this server has no record of the take")
        return state.audio.audio.frames

    def _submit(self, take_id: str, request_id: str, change: _Change) -> RouteResponse:
        with self._lock:
            queued, outcome = self._known(request_id)
            if outcome is not None or queued is not None:
                if outcome is not None:
                    same = outcome.answers(take_id, change)
                else:
                    assert queued is not None
                    same = (queued.take_id, queued.change) == (take_id, change)
                if not same:
                    raise RouteRefusal(
                        409, "request_conflict", "this request id was used for another change"
                    )
                return self._answer(202, _status(request_id, queued, outcome))
            frames = self._frames(take_id)
            if (
                frames is not None
                and isinstance(change, TrimChange)
                and change.end_frame is not None
                and change.end_frame > frames
            ):
                raise RouteRefusal(422, "trim_out_of_bounds", f"the take has {frames} frames")
            return self._queue(take_id, request_id, change)

    def _queue(self, take_id: str, request_id: str, change: _Change) -> RouteResponse:
        try:
            waiting = queued_requests(self.layout)
        except OSError as error:
            raise RouteRefusal(500, "internal_error", "incoming/ cannot be read") from error
        if len(waiting) >= self.max_queued:
            raise RouteRefusal(
                503,
                "request_queue_full",
                f"{len(waiting)} requests wait for the PC's writer; send again later",
                (("Retry-After", str(QUEUE_RETRY_SECONDS)),),
            )
        received_at = self.clock()
        # Later than every request still queued, so the names' order is the arrival order
        # even when the clock steps back.
        stamp_ns = max([nanoseconds(received_at), *(e.stamp_ns + 1 for e in waiting)])
        queued = CorpusRequest(
            request_id=request_id, take_id=take_id, received_at=received_at, change=change
        )
        try:
            queue_request(self.layout, queued, stamp_ns)
        except OSError as error:
            raise RouteRefusal(500, "internal_error", "the request could not be queued") from error
        return self._answer(202, _status(request_id, queued, None))
