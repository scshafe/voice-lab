"""The voice-lab server's ``/api/v1``, as the desktop app reads it.

Every answer is read through the backend's own strict contract models
(``cole_voice_lab.api.contracts``, ``cole_voice_lab.api.journey_contracts``; D-037), so the
app and the server agree on one schema: an answer that does not fit is an error here, never
a half-understood screen. A refusal is the server's ``ApiRefusal`` (a stable ``error`` code
and a message that carries no secret), raised as :class:`ApiRefused`.

The bearer token goes to the server's door and the API, which both verify it (design
§3.5). A ``401`` refreshes the token once and retries; a redirect means the door wanted a
browser sign-in instead of accepting the token (:class:`~voice_lab_desktop.auth.SignInRequired`).
Nothing is cached on disk: audio is returned as bytes and kept in memory by the player.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from cole_voice_lab.api.contracts import ApiMeta, ApiRefusal
from cole_voice_lab.api.journey_contracts import (
    GoalAbandoned,
    GoalCreated,
    GoalRequest,
    JourneyDocument,
    JourneyFeatureDetail,
    JourneyRecording,
    JourneyRecordings,
    JourneyReviewQueue,
    JourneySummary,
    JourneyTranscript,
    RefereeNext,
    RefereeResults,
    RefereeVerdictAnswer,
    RefereeVerdictRequest,
    ReviewVerdictAnswer,
    ReviewVerdictRequest,
    WatchStatus,
)

from .auth import Session, SignInRequired

#: The largest audio answer the app takes into memory (a long memo is tens of MB).
MAX_AUDIO_BYTES: Final = 512 * 1024 * 1024
AUDIO_TIMEOUT: Final = httpx.Timeout(300.0, connect=15.0)
API_VERSION: Final = "v1"

type DocumentKind = Literal["trends", "journeys", "coach", "goals"]


class ApiError(Exception):
    """A request that did not give an answer. The message is safe to show."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ApiUnavailable(ApiError):
    """Try again later: the server did not answer, or answered 5xx/408/429."""


class ApiRefused(ApiError):
    """The server refused the request (``ApiRefusal``); ``code`` is its stable code."""


class ApiClient:
    """``/api/v1`` on ``server_url``, with ``session``'s bearer token."""

    def __init__(self, server_url: str, session: Session, http: httpx.Client) -> None:
        self.base = server_url.rstrip("/")
        self.session = session
        self.http = http

    # -- plumbing ---------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: BaseModel | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        bearer = self.session.bearer()
        content = None if body is None else body.model_dump_json().encode("utf-8")
        response: httpx.Response | None = None
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
            if content is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self.http.request(
                    method,
                    f"{self.base}{path}",
                    params=dict(params) if params else None,
                    content=content,
                    headers=headers,
                    timeout=timeout or httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.HTTPError as exc:
                raise ApiUnavailable(f"the server did not answer ({type(exc).__name__})") from exc
            self.http.cookies.clear()
            if response.status_code == 401 and attempt == 1:
                bearer = self.session.after_unauthorized(bearer)
                continue
            break
        assert response is not None
        if response.is_redirect:
            raise SignInRequired(
                "the server's door asked for a browser sign-in instead of accepting the token"
            )
        if response.status_code == 401:
            raise SignInRequired("the server refused the sign-in; sign in again")
        if response.status_code >= 400:
            raise self._refusal(response)
        return response

    @staticmethod
    def _refusal(response: httpx.Response) -> ApiError:
        status = response.status_code
        try:
            refusal: ApiRefusal | None = ApiRefusal.model_validate_json(response.content)
        except ValidationError:
            refusal = None
        code = refusal.error if refusal else f"http_{status}"
        detail = refusal.detail if refusal else f"HTTP {status}"
        if status >= 500 or status in (408, 429):
            return ApiUnavailable(
                f"the server is unavailable ({code}: {detail})", status=status, code=code
            )
        return ApiRefused(f"{detail} ({code})", status=status, code=code)

    def _get[ModelT: BaseModel](
        self, model: type[ModelT], path: str, params: Mapping[str, str] | None = None
    ) -> ModelT:
        return self._read(model, self._request("GET", path, params=params))

    def _post[ModelT: BaseModel](
        self, model: type[ModelT], path: str, body: BaseModel | None = None
    ) -> ModelT:
        return self._read(model, self._request("POST", path, body=body))

    @staticmethod
    def _read[ModelT: BaseModel](model: type[ModelT], response: httpx.Response) -> ModelT:
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise ApiError(f"the server answered with an invalid {model.__name__}") from exc

    @staticmethod
    def _id(value: str) -> str:
        return quote(value, safe="")

    # -- resources --------------------------------------------------------------------------

    def meta(self) -> ApiMeta:
        return self._get(ApiMeta, "/api/v1/meta")

    def summary(self) -> JourneySummary:
        return self._get(JourneySummary, "/api/v1/summary")

    def watch(self) -> WatchStatus:
        return self._get(WatchStatus, "/api/v1/watch")

    def recordings(self, params: Mapping[str, str]) -> JourneyRecordings:
        return self._get(JourneyRecordings, "/api/v1/recordings", params)

    def recording(self, recording_id: str) -> JourneyRecording:
        return self._get(JourneyRecording, f"/api/v1/recordings/{self._id(recording_id)}")

    def transcript(self, recording_id: str) -> JourneyTranscript:
        path = f"/api/v1/recordings/{self._id(recording_id)}/transcript"
        return self._get(JourneyTranscript, path)

    def features(self, recording_id: str) -> JourneyFeatureDetail:
        path = f"/api/v1/recordings/{self._id(recording_id)}/features"
        return self._get(JourneyFeatureDetail, path)

    def audio(self, recording_id: str) -> bytes:
        """The recording's bytes, whole, into memory (never onto disk)."""
        return self.audio_url(f"/api/v1/recordings/{self._id(recording_id)}/audio")

    def audio_url(self, path: str) -> bytes:
        """The bytes of an audio path the server named (a referee side's ``audio_url``)."""
        if not path.startswith("/api/v1/recordings/") or not path.endswith("/audio"):
            raise ApiError("the server named an audio path outside /api/v1/recordings")
        response = self._request("GET", path, timeout=AUDIO_TIMEOUT)
        declared = response.headers.get("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise ApiError("the recording is too large to play here")
        if not response.headers.get("Content-Type", "").startswith("audio/"):
            raise ApiError("the server's answer is not audio")
        return response.content

    def review_queue(self) -> JourneyReviewQueue:
        return self._get(JourneyReviewQueue, "/api/v1/review/queue")

    def review_verdict(self, request: ReviewVerdictRequest) -> ReviewVerdictAnswer:
        return self._post(ReviewVerdictAnswer, "/api/v1/review/verdicts", request)

    def document(self, kind: DocumentKind) -> JourneyDocument:
        return self._get(JourneyDocument, f"/api/v1/{kind}")

    def create_goal(self, request: GoalRequest) -> GoalCreated:
        return self._post(GoalCreated, "/api/v1/goals", request)

    def abandon_goal(self, goal_id: str) -> GoalAbandoned:
        return self._post(GoalAbandoned, f"/api/v1/goals/{self._id(goal_id)}/abandon")

    def referee_next(self) -> RefereeNext:
        return self._get(RefereeNext, "/api/v1/referee/next")

    def referee_verdict(self, request: RefereeVerdictRequest) -> RefereeVerdictAnswer:
        return self._post(RefereeVerdictAnswer, "/api/v1/referee/verdicts", request)

    def referee_results(self) -> RefereeResults:
        return self._get(RefereeResults, "/api/v1/referee/results")
