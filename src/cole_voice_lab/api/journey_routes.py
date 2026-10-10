"""The corpus browser's HTTP surface: ``/api/v1`` resources and voice-journey's old paths.

@canonical api.journey_routes -- the routes of voice-journey's ``corpus-browser.mjs``,
served by the API (an ``api.routing`` route of ``ApiServer``), over the data layer in
``journey/browser.py``:

=======================================  ==========================================  ===
``/api/v1`` (strict envelopes)            voice-journey path (the JS's exact JSON)    m
=======================================  ==========================================  ===
``/api/v1/recordings``                    ``/api/rows``                               GET
``/api/v1/recordings/{id}``               ``/api/row/{id}``                           GET
``/api/v1/recordings/{id}/audio``         ``/api/audio/{id}`` (HTTP ``Range``)        GET
``/api/v1/recordings/{id}/transcript``    ``/api/transcript/{id}``                    GET
``/api/v1/recordings/{id}/features``      ``/api/feature-detail/{id}``                GET
``/api/v1/review/queue``                  ``/api/review-queue``                       GET
``/api/v1/review/verdicts``               ``/api/verdicts``                           POST
``/api/v1/summary``                       ``/api/summary``                            GET
``/api/v1/trends``                        ``/api/trends``                             GET
``/api/v1/journeys``                      ``/api/journeys``                           GET
``/api/v1/coach``                         ``/api/coach``                              GET
``/api/v1/goals``                         ``/api/goals``                              GET
``/api/v1/goals``                         ``/api/goals``                              POST
``/api/v1/goals/{id}/abandon``            ``/api/goals/abandon`` (``goalId`` body)    POST
``/api/v1/referee/next``                  ``/api/referee/next``                       GET
``/api/v1/referee/verdicts``              ``/api/referee/verdicts``                   POST
``/api/v1/referee/results``               ``/api/referee/results``                    GET
``/api/v1/watch``                         ``/api/watch/status``                       GET
``/api/v1/chains``                        ``/api/chains`` (voice-journey PR #1)       GET
=======================================  ==========================================  ===

The voice-journey paths answer during phase 3 only (design §3.4), as thin aliases of the
same handlers, with the JS's status codes and JSON (``JSON.stringify(p, null, 2)``)
including its ``{"error": ...}`` refusals. The SPA's page routes (``/``, ``/journey``,
``/referee``, ``/practice``, ``/report``, ``/assets/*``) are not served from the PC
(D-035): they answer ``308`` to ``/view/``.

**Auth (the defence-in-depth change).** The JS trusted the door's forwarded identity
headers and checked nothing itself. Every ``/api/`` route here is behind the API's own
bearer verification (issuer, audience, lifetime, signature, the one allowed subject;
``auth.py``), applied by ``ApiServer`` before a route sees the request, exactly as for
``/api/v1/meta`` (design §3.5). The 308 redirects carry nothing and need no identity.

**Gates.** Without ``serve --journey`` the routes answer ``503 journey_unavailable``.
The chain registry (``api/chains.py``) is read on each request; a missing file is
``503 chain_registry_unavailable``.
Playback (audio and the blind referee) needs ``--playback-approval`` (design §4.2), local
transcript text a transcript directory, feature detail a feature directory. The
``/api/v1`` referee refuses without the approval (``403 playback_not_approved``); the
old paths keep the JS's answers (``next`` says ``enabled: false``, verdicts are
accepted). Every write goes to the state directory (``JourneyStore``).

Differences on the old paths: a body that is not JSON is ``400 {"error":
"invalid_json"}`` (the JS answered 500 with the parser's message); a recording whose
file is missing is ``404 {"error": "audio_not_found"}`` (500 in the JS); the
``corpus_access_denied`` hint names the PC's drop, not macOS's Full Disk Access; an
unknown ``/api/`` path or method answers the server's refusal (``ApiRefusal``).
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, NoReturn
from urllib.parse import parse_qsl, quote, unquote

from pydantic import BaseModel, ValidationError

from ..canonical import canonical_json_bytes
from ..journey import jscompat as js
from ..journey.browser import (
    ALLOWED_VERDICTS,
    DEFAULT_QUEUE_SIZE,
    DEFAULT_REFEREE_SEED,
    JOURNEYS_UNAVAILABLE,
    REFEREE_CHOICES,
    TRENDS_UNAVAILABLE,
    AudioHandle,
    BrowserError,
    CorpusAccessDenied,
    JourneyConfig,
    JourneyStore,
    SeamMismatch,
    ServeState,
    coach_payload,
    feature_detail_path,
    make_verdict,
    open_recording_audio,
    paginate_rows,
    parse_range,
    playback_summary,
    project_rows,
    query_rows,
    review_queue_rows,
    summarize_rows,
    summary_payload,
    transcript_path,
    transcript_text_summary,
)
from ..journey.goals import GoalError
from ..journey.jsonio import MISSING, parse_json, read_json, read_text, stringify
from ..journey.paths import assert_bind_allowed, resolve_paths
from .chains import ChainRegistryError, read_chain_registry, registry_view
from .contracts import ApiErrorCode
from .journey_contracts import (
    FilterOptions,
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
    RefereePair,
    RefereeResults,
    RefereeReveal,
    RefereeSide,
    RefereeVerdictAnswer,
    RefereeVerdictRequest,
    ReviewQueueInfo,
    ReviewVerdict,
    ReviewVerdictAnswer,
    ReviewVerdictRequest,
    RowsPage,
    RowTotals,
    WatchStatus,
)
from .routing import RouteRefusal, RouteRequest, RouteResponse

Json = dict[str, Any]
type _Handler = Callable[[JourneyStore, RouteRequest], RouteResponse]
type _DocumentKind = Literal["trends", "journeys", "coach", "goals"]

# The JS read at most 64 KiB of a request body.
MAX_BODY_BYTES: Final = 64 * 1024
VIEW_PATH: Final = "/view/"
_SPA_PAGES: Final = frozenset({"/", "/journey", "/referee", "/practice", "/report"})
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_V1_RECORDING = re.compile(
    r"/api/v1/recordings/(?P<id>[^/]+)(?:/(?P<part>audio|transcript|features))?"
)
_V1_ABANDON = re.compile(r"/api/v1/goals/(?P<id>[^/]+)/abandon")
_ALIAS_PREFIXES: Final = ("/api/row/", "/api/audio/", "/api/transcript/", "/api/feature-detail/")
_ALIAS_PATHS: Final = frozenset(
    {
        "/api/rows",
        "/api/review-queue",
        "/api/verdicts",
        "/api/summary",
        "/api/trends",
        "/api/journeys",
        "/api/coach",
        "/api/goals",
        "/api/goals/abandon",
        "/api/referee/next",
        "/api/referee/verdicts",
        "/api/referee/results",
        "/api/watch/status",
        "/api/chains",
    }
)
ACCESS_DENIED_HINT: Final = (
    "The service cannot read the corpus root. Check that the API can read the phone "
    "drop (its owner, mode and the read-only mount), then retry."
)


class _JsAnswer(Exception):
    """An old-path answer the JS gave as ``sendJson(status, payload)``."""

    def __init__(self, status: int, payload: Json) -> None:
        super().__init__(payload.get("error", "answer"))
        self.status = status
        self.payload = payload


def _refuse(status: int, code: ApiErrorCode, detail: str) -> NoReturn:
    raise RouteRefusal(status, code, detail)


def _plain(value: Any) -> Any:
    """A JSON value as ``JSON.stringify`` would send it: ``undefined`` members dropped,
    ``undefined`` array elements ``null``."""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items() if item is not MISSING}
    if isinstance(value, list | tuple):
        return [None if item is MISSING else _plain(item) for item in value]
    return None if value is MISSING else value


def _js(
    status: int, route: str, payload: Any, headers: tuple[tuple[str, str], ...] = ()
) -> RouteResponse:
    return RouteResponse(
        status=status,
        route=route,
        body=(stringify(payload) + "\n").encode("utf-8"),
        headers=headers,
    )


def _model(status: int, route: str, model: BaseModel) -> RouteResponse:
    return RouteResponse(status=status, route=route, body=canonical_json_bytes(model) + b"\n")


def _decode_component(text: str) -> str:
    """``decodeURIComponent``: a malformed escape or invalid UTF-8 is a ``ValueError``."""

    if _BAD_ESCAPE.search(text):
        raise ValueError("URI malformed")
    try:
        return unquote(text, errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("URI malformed") from error


def _encode_component(text: Any) -> str:
    """``encodeURIComponent(String(text))``."""

    return quote(js.to_string(text), safe="!*'()")


def _params(query: str) -> list[tuple[str, str]]:
    return parse_qsl(query, keep_blank_values=True)


def _totals(totals: Mapping[str, Any]) -> RowTotals:
    return RowTotals(
        rows=totals["rows"],
        buckets=dict(totals["buckets"]),
        years=dict(totals["years"]),
        review_queue_selected=totals["reviewQueueSelected"],
        reviewed=totals["reviewed"],
    )


@dataclass(frozen=True, slots=True)
class _Local:
    """One recording's local file, located and gated (shared by both path families):
    ``payload`` (the JS's answer), or the JS's ``status`` and ``error``."""

    status: int
    error: str
    payload: Json | None = None

    @property
    def code(self) -> ApiErrorCode:
        return _V1_CODES[self.error]


_V1_CODES: Final[dict[str, ApiErrorCode]] = {
    "recording_not_indexed": "recording_not_found",
    "transcript_dir_not_configured": "transcript_dir_not_configured",
    "transcript_not_available": "transcript_not_available",
    "invalid_transcript_path": "invalid_transcript_path",
    "transcript_text_missing": "transcript_text_missing",
    "feature_dir_not_configured": "feature_dir_not_configured",
    "invalid_feature_detail_path": "invalid_feature_detail_path",
    "feature_detail_missing": "feature_detail_missing",
}


class JourneyRoutes:
    """The journey route of ``ApiServer`` (``api.routing.Route``).

    ``store`` is ``None`` when the server runs without ``--journey``: the journey paths
    then answer ``503 journey_unavailable``."""

    def __init__(self, store: JourneyStore | None) -> None:
        self.store = store

    def describe(self) -> Json | None:
        """The startup summary the JS printed (rows, queue, gates, stores)."""

        if self.store is None:
            return None
        served = self.store.current()
        payload = served.rows_payload
        return {
            "rows": len(payload["rows"]),
            "reviewQueue": payload["reviewQueue"],
            **self.store.config.describe(),
            "voiceTrends": {
                "available": bool((served.voice_trends or {}).get("available")),
                "quarters": len((served.voice_trends or {}).get("quarters", [])),
            },
            "songJourneys": {
                "available": served.song_journeys is not None,
                "clusters": len((served.song_journeys or {}).get("journeys") or []),
            },
            "refereePoolSize": len(served.referee_pool),
        }

    # -- dispatch -------------------------------------------------------------------------

    def __call__(self, request: RouteRequest) -> RouteResponse | None:
        path = request.path
        if request.method in {"GET", "HEAD"} and (
            path in _SPA_PAGES or path.startswith("/assets/")
        ):
            return RouteResponse(
                status=308,
                route="journey.spa",
                content_type="text/plain; charset=utf-8",
                headers=(("Location", VIEW_PATH),),
            )
        if path.startswith("/api/v1/"):
            handler = self._v1_handler(request)
            if handler is None:
                return None
            return handler(self._require_store(), request)
        if path in _ALIAS_PATHS or path.startswith(_ALIAS_PREFIXES):
            return self._alias(request)
        return None

    def _require_store(self) -> JourneyStore:
        if self.store is None:
            _refuse(503, "journey_unavailable", "the server runs without the journey routes")
        return self.store

    def _v1_handler(self, request: RouteRequest) -> _Handler | None:
        path, method = request.path, request.method
        methods = _V1_ROUTES.get(path)
        if methods is None:
            if _V1_RECORDING.fullmatch(path):
                methods = {"GET": _v1_recording}
            elif _V1_ABANDON.fullmatch(path):
                methods = {"POST": _v1_goal_abandon}
            else:
                return None
        handler = methods.get(method)
        if handler is None:
            _refuse(405, "method_not_allowed", f"this route answers {', '.join(methods)}")
        return handler

    def _alias(self, request: RouteRequest) -> RouteResponse:
        store = self._require_store()
        try:
            return _alias_dispatch(store, request)
        except _JsAnswer as answer:
            return _js(answer.status, "journey.alias", answer.payload)
        except BrowserError as error:
            return _js(error.status_code, "journey.alias", {"error": str(error)})


# ---- shared handlers ------------------------------------------------------------------


def _row_or_none(served: ServeState, recording: str) -> Json | None:
    return served.rows_by_id.get(recording)


def _transcript(store: JourneyStore, served: ServeState, recording: str) -> _Local:
    row = _row_or_none(served, recording)
    config = store.config
    if row is None:
        return _Local(404, "recording_not_indexed")
    if config.transcript_dir is None:
        return _Local(403, "transcript_dir_not_configured")
    if row.get("transcriptStatus") != "completed" or not js.truthy(
        row.get("transcriptTextLocalOnly")
    ):
        return _Local(404, "transcript_not_available")
    path = transcript_path(config.transcript_dir, recording)
    if path is None:
        return _Local(400, "invalid_transcript_path")
    try:
        text = read_text(path)
    except FileNotFoundError:
        return _Local(404, "transcript_text_missing")
    return _Local(
        200,
        "",
        {
            "recordingId": recording,
            "status": row["transcriptStatus"],
            "language": row.get("transcriptLanguage"),
            "wordCount": row.get("transcriptWordCount"),
            "text": text,
            "localOnly": True,
            "repoSafe": {
                "transcriptTextCommittedToRepo": False,
                "audioBytes": False,
                "upload": False,
            },
        },
    )


def _feature_detail(store: JourneyStore, served: ServeState, recording: str) -> _Local:
    row = _row_or_none(served, recording)
    config = store.config
    if row is None:
        return _Local(404, "recording_not_indexed")
    if config.feature_dir is None:
        return _Local(403, "feature_dir_not_configured")
    path = feature_detail_path(config.feature_dir, recording)
    if path is None:
        return _Local(400, "invalid_feature_detail_path")
    try:
        detail = read_json(path)
    except FileNotFoundError:
        return _Local(404, "feature_detail_missing")
    return _Local(
        200,
        "",
        {
            "recordingId": recording,
            "filename": row.get("filename", MISSING),
            "capturedAt": row.get("capturedAt", MISSING),
            "year": row.get("year", MISSING),
            "bucket": row.get("bucket", MISSING),
            "methodId": js.coalesce(js.get(detail, "methodId"), None),
            "summary": js.coalesce(js.get(detail, "summary"), None),
            "detail": js.coalesce(js.get(detail, "detail"), None),
            "localOnly": True,
            "repoSafe": {
                "featureArraysCommittedToRepo": False,
                "audioBytes": False,
                "transcriptText": False,
                "upload": False,
            },
        },
    )


def _open_audio(store: JourneyStore, row: Json) -> AudioHandle:
    config = store.config
    return open_recording_audio(
        row,
        corpus_root=config.corpus_root,
        measurement_dir=config.measurement_dir,
        approval=config.playback_approval,
    )


def _audio_response(route: str, audio: AudioHandle, range_header: str | None) -> RouteResponse:
    span = parse_range(range_header, audio.size)
    headers: tuple[tuple[str, str], ...] = (("Accept-Ranges", "bytes"),)
    if span is None:
        return RouteResponse(
            status=200,
            route=route,
            content_type=audio.content_type,
            headers=headers,
            stream=audio.handle,
            stream_length=audio.size,
        )
    start, end = span
    audio.handle.seek(start)
    return RouteResponse(
        status=206,
        route=route,
        content_type=audio.content_type,
        headers=(*headers, ("Content-Range", f"bytes {start}-{end}/{audio.size}")),
        stream=audio.handle,
        stream_length=end - start + 1,
    )


# ---- /api/v1 --------------------------------------------------------------------------


def _v1_id(match: re.Match[str] | None) -> str:
    assert match is not None
    try:
        return _decode_component(match["id"])
    except ValueError:
        _refuse(404, "recording_not_found", "that is not an indexed recording")


def _v1_body[ModelT: BaseModel](request: RouteRequest, model: type[ModelT]) -> ModelT:
    if request.content_length is None:
        _refuse(411, "content_length_required", "a JSON body needs Content-Length")
    try:
        return model.model_validate_json(request.read_body(MAX_BODY_BYTES))
    except ValidationError as error:
        raise RouteRefusal(
            422, "invalid_request", f"the body is not a valid {model.__name__}"
        ) from error


def _v1_recordings(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    served = store.current()
    params = _params(request.query)
    filtered = query_rows(served.rows_payload["rows"], params)
    paging = (
        params
        if any(key in {"limit", "offset"} for key, _ in params)
        else [
            *params,
            ("limit", "100"),
        ]
    )
    page_rows, page = paginate_rows(filtered, paging)
    assert page is not None
    fields = next((value for key, value in params if key == "fields"), None)
    try:
        rows = project_rows(page_rows, fields)
    except BrowserError:
        _refuse(400, "unsupported_fields_projection", "fields is list or absent")
    payload = served.rows_payload
    return _model(
        200,
        "journey.recordings",
        JourneyRecordings(
            rows_schema_version=payload["schemaVersion"],
            generated_at=payload["generatedAt"],
            page=RowsPage(
                offset=page["offset"],
                limit=page["limit"],
                returned=page["returned"],
                total_filtered=page["totalFiltered"],
            ),
            totals=_totals(summarize_rows(filtered)),
            rows=tuple(_plain(row) for row in rows),
        ),
    )


def _v1_recording(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    match = _V1_RECORDING.fullmatch(request.path)
    recording = _v1_id(match)
    assert match is not None
    served = store.current()
    row = _row_or_none(served, recording)
    if row is None:
        _refuse(404, "recording_not_found", "that is not an indexed recording")
    part = match["part"]
    if part == "audio":
        return _v1_audio(store, request, row)
    if part in {"transcript", "features"}:
        local = (
            _transcript(store, served, recording)
            if part == "transcript"
            else _feature_detail(store, served, recording)
        )
        if local.payload is None:
            _refuse(local.status, local.code, f"the {part} of that recording is not available")
        found = _plain(local.payload)
        if part == "transcript":
            return _model(
                200,
                "journey.transcript",
                JourneyTranscript(
                    recording_id=recording,
                    status=found["status"],
                    language=found["language"],
                    word_count=found["wordCount"],
                    text=found["text"],
                ),
            )
        return _model(
            200,
            "journey.features",
            JourneyFeatureDetail(
                recording_id=recording,
                filename=found.get("filename"),
                captured_at=found.get("capturedAt"),
                year=found.get("year"),
                bucket=found.get("bucket"),
                method_id=found["methodId"],
                summary=found["summary"],
                detail=found["detail"],
            ),
        )
    return _model(
        200,
        "journey.recording",
        JourneyRecording(rows_schema_version=served.rows_payload["schemaVersion"], row=_plain(row)),
    )


def _v1_audio(store: JourneyStore, request: RouteRequest, row: Json) -> RouteResponse:
    if not store.config.playback_enabled:
        _refuse(403, "playback_not_approved", "playback needs the recorded playback approval")
    try:
        audio = _open_audio(store, row)
    except CorpusAccessDenied:
        _refuse(503, "corpus_access_denied", ACCESS_DENIED_HINT)
    except SeamMismatch:
        _refuse(409, "seam_handle_mismatch", "the index row does not name its recording")
    except FileNotFoundError:
        _refuse(404, "audio_not_found", "the recording's file is not in the corpus")
    return _audio_response("journey.audio", audio, request.header("Range"))


def _v1_review_queue(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    served = store.current()
    return _model(
        200,
        "journey.review_queue",
        JourneyReviewQueue(
            strategy=served.rows_payload["reviewQueue"]["strategy"],
            rows=tuple(_plain(row) for row in review_queue_rows(served)),
        ),
    )


def _v1_review_verdict(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    body = _v1_body(request, ReviewVerdictRequest)
    if body.recording_id not in store.current().rows_by_id:
        _refuse(404, "recording_not_found", "that is not an indexed recording")
    verdict = make_verdict(
        body.recording_id,
        body.verdict,
        note=body.note,
        reviewed_by=body.reviewed_by,
        reviewed_at=body.reviewed_at,
    )
    totals = store.record_verdict(verdict)
    return _model(
        200,
        "journey.verdict",
        ReviewVerdictAnswer(
            verdict=ReviewVerdict(
                recording_id=verdict["recordingId"],
                verdict=body.verdict,
                note=verdict["note"],
                reviewed_by=verdict["reviewedBy"],
                reviewed_at=verdict["reviewedAt"],
            ),
            totals=_totals(totals),
        ),
    )


def _v1_summary(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    served = store.current()
    summary = summary_payload(served, store.config)
    queue, options = summary["reviewQueue"], summary["filterOptions"]
    return _model(
        200,
        "journey.summary",
        JourneySummary(
            rows_schema_version=summary["schemaVersion"],
            generated_at=served.rows_payload["generatedAt"],
            playback_enabled=summary["playback"]["enabled"],
            transcript_text_enabled=summary["transcriptText"]["enabled"],
            feature_detail_enabled=summary["featureDetail"]["enabled"],
            source=_plain(summary["source"]),
            review_queue=ReviewQueueInfo(
                strategy=queue["strategy"],
                target_size=queue["targetSize"],
                selected_count=queue["selectedCount"],
                recording_ids=tuple(_plain(queue["recordingIds"])),
            ),
            totals=_totals(summary["totals"]),
            filter_options=FilterOptions(
                buckets=tuple(_plain(options["buckets"])),
                years=tuple(_plain(options["years"])),
                spot_check_statuses=tuple(_plain(options["spotCheckStatuses"])),
                transcript_statuses=tuple(_plain(options["transcriptStatuses"])),
                lyric_match_statuses=tuple(_plain(options["lyricMatchStatuses"])),
            ),
        ),
    )


def _document(kind: _DocumentKind, document: Mapping[str, Any]) -> RouteResponse:
    return _model(
        200,
        f"journey.{kind}",
        JourneyDocument(
            kind=kind,
            available=document.get("available", True) is not False,
            document=_plain(document),
        ),
    )


def _v1_trends(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    return _document("trends", store.current().voice_trends or TRENDS_UNAVAILABLE)


def _v1_journeys(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    return _document("journeys", store.current().song_journeys or JOURNEYS_UNAVAILABLE)


def _v1_coach(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    return _document("coach", coach_payload(store.current()))


def _v1_goals(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    return _document("goals", store.goals())


def _v1_goal_create(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    body = _v1_body(request, GoalRequest)
    try:
        goal = store.create_goal(
            {
                "metric": body.metric,
                "direction": body.direction,
                "target": body.target,
                "takesRequired": body.takes_required,
                "clusterId": body.cluster_id,
                "title": body.title,
            }
        )
    except GoalError as error:
        _refuse(400, "invalid_goal", str(error))
    return _model(201, "journey.goal", GoalCreated(goal=_plain(goal)))


def _v1_goal_abandon(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    match = _V1_ABANDON.fullmatch(request.path)
    assert match is not None
    try:
        goal_id = _decode_component(match["id"])
    except ValueError:
        _refuse(404, "goal_not_found", "no goal has that id")
    goal = store.abandon_goal(goal_id)
    if goal is None:
        _refuse(404, "goal_not_found", "no goal has that id")
    return _model(
        200,
        "journey.goal_abandon",
        GoalAbandoned(goal_id=goal_id, abandoned_at=goal["abandonedAt"]),
    )


def _v1_playback(store: JourneyStore) -> None:
    if not store.config.playback_enabled:
        _refuse(403, "playback_not_approved", "blind trials need the recorded playback approval")


def _v1_referee_next(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    _v1_playback(store)
    judged, pool_size, pair = store.referee_next()

    def side(recording: Any, gain: Any) -> RefereeSide:
        return RefereeSide(
            audio_url=f"/api/v1/recordings/{_encode_component(recording)}/audio",
            gain_db=float(js.coalesce(gain, 0)),
        )

    return _model(
        200,
        "journey.referee_next",
        RefereeNext(
            judged=judged,
            pool_size=pool_size,
            pair=None
            if pair is None
            else RefereePair(
                pair_id=pair["pairId"],
                a=side(pair["aId"], pair.get("aGainDb")),
                b=side(pair["bId"], pair.get("bGainDb")),
            ),
        ),
    )


def _v1_referee_verdict(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    _v1_playback(store)
    body = _v1_body(request, RefereeVerdictRequest)
    pair = store.find_pair(body.pair_id)
    if pair is None:
        _refuse(404, "pair_not_found", "no pair of the pool has that id")
    _, judged = store.record_judgment(pair, body.choice, body.judged_at)
    reveal = _plain(
        {
            "a_year": pair["aYear"],
            "b_year": pair["bYear"],
            "a_captured_at": pair["aCapturedAt"],
            "b_captured_at": pair["bCapturedAt"],
            "cluster_id": pair["clusterId"],
            "cluster_label": pair["clusterLabel"],
        }
    )
    return _model(
        200,
        "journey.referee_verdict",
        RefereeVerdictAnswer(
            reveal=RefereeReveal(**reveal),
            judged=judged,
            pool_size=len(store.current().referee_pool),
        ),
    )


def _v1_referee_results(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    return _model(
        200,
        "journey.referee_results",
        RefereeResults(
            enabled=store.config.playback_enabled,
            pool_size=len(store.current().referee_pool),
            fit=_plain(store.referee_fit()),
        ),
    )


def _v1_watch(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    status = store.watch_status()
    if not status["enabled"]:
        answer = WatchStatus(enabled=False, reason=status["reason"])
    else:
        answer = WatchStatus(
            enabled=True, state=_plain({k: v for k, v in status.items() if k != "enabled"})
        )
    return _model(200, "journey.watch", answer)


def _v1_chains(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    try:
        projection = read_chain_registry(store.config.chains)
        if projection is None:
            _refuse(503, "chain_registry_unavailable", "the chain registry file is missing")
        answer = registry_view(projection)
    except ChainRegistryError as error:
        _refuse(500, "chain_registry_invalid", str(error))
    return _model(200, "journey.chains", answer)


_V1_ROUTES: Final[dict[str, dict[str, _Handler]]] = {
    "/api/v1/recordings": {"GET": _v1_recordings},
    "/api/v1/review/queue": {"GET": _v1_review_queue},
    "/api/v1/review/verdicts": {"POST": _v1_review_verdict},
    "/api/v1/summary": {"GET": _v1_summary},
    "/api/v1/trends": {"GET": _v1_trends},
    "/api/v1/journeys": {"GET": _v1_journeys},
    "/api/v1/coach": {"GET": _v1_coach},
    "/api/v1/goals": {"GET": _v1_goals, "POST": _v1_goal_create},
    "/api/v1/referee/next": {"GET": _v1_referee_next},
    "/api/v1/referee/verdicts": {"POST": _v1_referee_verdict},
    "/api/v1/referee/results": {"GET": _v1_referee_results},
    "/api/v1/watch": {"GET": _v1_watch},
    "/api/v1/chains": {"GET": _v1_chains},
}


# ---- voice-journey's paths (phase 3) --------------------------------------------------


def _alias_body(request: RouteRequest) -> Any:
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise _JsAnswer(413, {"error": "request_too_large"})
    text = request.read_body(MAX_BODY_BYTES).decode("utf-8", errors="replace")
    try:
        return parse_json(text or "{}")
    except ValueError as error:
        raise _JsAnswer(400, {"error": "invalid_json"}) from error


def _alias_id(path: str, prefix: str) -> str:
    try:
        return _decode_component(path.removeprefix(prefix))
    except ValueError as error:
        raise _JsAnswer(500, {"error": "URI malformed"}) from error


def _alias_dispatch(store: JourneyStore, request: RouteRequest) -> RouteResponse:
    path, method = request.path, request.method
    route = "journey.alias"
    get, post = method == "GET", method == "POST"
    if get and path == "/api/trends":
        return _js(200, route, store.current().voice_trends or TRENDS_UNAVAILABLE)
    if get and path == "/api/journeys":
        return _js(200, route, store.current().song_journeys or JOURNEYS_UNAVAILABLE)
    if get and path == "/api/referee/next":
        return _js(200, route, _alias_referee_next(store))
    if post and path == "/api/referee/verdicts":
        return _js(200, route, _alias_referee_verdict(store, _alias_body(request)))
    if get and path == "/api/referee/results":
        fit = store.referee_fit()
        pool = len(store.current().referee_pool)
        return _js(200, route, {"enabled": store.config.playback_enabled, "poolSize": pool, **fit})
    if get and path == "/api/rows":
        return _js(200, route, _alias_rows(store, request.query))
    if get and path.startswith("/api/row/"):
        recording = _alias_id(path, "/api/row/")
        served = store.current()
        row = _row_or_none(served, recording)
        if row is None:
            raise _JsAnswer(404, {"error": "row_not_found"})
        return _js(200, route, {"schemaVersion": served.rows_payload["schemaVersion"], "row": row})
    if get and path == "/api/review-queue":
        served = store.current()
        strategy = served.rows_payload["reviewQueue"]["strategy"]
        return _js(200, route, {"strategy": strategy, "rows": review_queue_rows(served)})
    if get and path == "/api/summary":
        return _js(200, route, summary_payload(store.current(), store.config))
    if get and path == "/api/goals":
        return _js(200, route, store.goals())
    if post and path == "/api/goals":
        body = _alias_body(request)
        try:
            goal = store.create_goal(body)
        except GoalError as error:
            raise _JsAnswer(error.status_code, {"error": str(error)}) from error
        return _js(200, route, {"ok": True, "goal": goal})
    if post and path == "/api/goals/abandon":
        body = _alias_body(request)
        goal_id = js.to_string(js.coalesce(js.get(body, "goalId"), ""))
        if store.abandon_goal(goal_id) is None:
            raise _JsAnswer(404, {"error": "goal_not_found"})
        return _js(200, route, {"ok": True})
    if get and path == "/api/coach":
        return _js(200, route, coach_payload(store.current()))
    if get and path == "/api/watch/status":
        return _js(200, route, store.watch_status())
    if get and path == "/api/chains":
        status, payload = _alias_chains(store)
        return _js(status, route, payload)
    if get and path.startswith("/api/audio/"):
        return _alias_audio(store, request, _alias_id(path, "/api/audio/"))
    if get and path.startswith("/api/transcript/"):
        recording = _alias_id(path, "/api/transcript/")
        return _alias_local(_transcript(store, store.current(), recording), recording)
    if get and path.startswith("/api/feature-detail/"):
        recording = _alias_id(path, "/api/feature-detail/")
        return _alias_local(_feature_detail(store, store.current(), recording), recording)
    if post and path == "/api/verdicts":
        return _js(200, route, _alias_verdict(store, _alias_body(request)))
    if not (get or post):
        raise _JsAnswer(405, {"error": "method_not_allowed"})
    raise _JsAnswer(404, {"error": "not_found"})


def _alias_chains(store: JourneyStore) -> tuple[int, Json]:
    try:
        projection = read_chain_registry(store.config.chains)
    except ChainRegistryError as error:
        return 500, {"error": str(error)}
    if projection is None:
        return 503, {"error": "chain_registry_unavailable"}
    return 200, projection


def _alias_rows(store: JourneyStore, query: str) -> Json:
    served = store.current()
    payload = served.rows_payload
    params = _params(query)
    filtered = query_rows(payload["rows"], params)
    page_rows, page = paginate_rows(filtered, params)
    rows = project_rows(page_rows, next((value for key, value in params if key == "fields"), None))
    return {
        **payload,
        "playback": playback_summary(store.config.playback_enabled),
        "transcriptText": transcript_text_summary(store.config.transcript_dir is not None),
        "totals": summarize_rows(filtered),
        **({"page": page} if page is not None else {}),
        "rows": rows,
    }


def _alias_local(local: _Local, recording: str) -> RouteResponse:
    if local.payload is not None:
        return _js(200, "journey.alias", local.payload)
    if local.error == "recording_not_indexed":
        return _js(local.status, "journey.alias", {"error": local.error})
    return _js(local.status, "journey.alias", {"error": local.error, "recordingId": recording})


def _alias_audio(store: JourneyStore, request: RouteRequest, recording: str) -> RouteResponse:
    row = _row_or_none(store.current(), recording)
    if row is None:
        raise _JsAnswer(404, {"error": "recording_not_indexed"})
    if not store.config.playback_enabled:
        raise _JsAnswer(
            403, {"error": "playback_requires_release_gate_approval", "recordingId": recording}
        )
    try:
        audio = _open_audio(store, row)
    except CorpusAccessDenied as error:
        raise _JsAnswer(
            503,
            {"error": "corpus_access_denied", "recordingId": recording, "hint": ACCESS_DENIED_HINT},
        ) from error
    except SeamMismatch as error:
        raise _JsAnswer(409, {"error": "seam_handle_mismatch", "recordingId": recording}) from error
    except FileNotFoundError as error:
        raise _JsAnswer(404, {"error": "audio_not_found", "recordingId": recording}) from error
    return _audio_response("journey.alias", audio, request.header("Range"))


def _alias_referee_next(store: JourneyStore) -> Json:
    if not store.config.playback_enabled:
        return {
            "enabled": False,
            "reason": "playback_requires_release_gate_approval",
            "poolSize": len(store.current().referee_pool),
        }
    judged, pool_size, pair = store.referee_next()

    def side(recording: Any, gain: Any) -> Json:
        return {"audioUrl": f"/api/audio/{_encode_component(recording)}", "gainDb": gain}

    return {
        "enabled": True,
        "progress": {"judged": judged, "poolSize": pool_size},
        "pair": None
        if pair is None
        else {
            "pairId": pair["pairId"],
            "a": side(pair["aId"], js.coalesce(pair.get("aGainDb", MISSING), 0)),
            "b": side(pair["bId"], js.coalesce(pair.get("bGainDb", MISSING), 0)),
        },
    }


def _alias_referee_verdict(store: JourneyStore, body: Any) -> Json:
    pair = store.find_pair(js.to_string(js.coalesce(js.get(body, "pairId"), "")))
    if pair is None:
        raise _JsAnswer(404, {"error": "pair_not_found"})
    choice = js.to_string(js.coalesce(js.get(body, "choice"), ""))
    if choice not in REFEREE_CHOICES:
        raise _JsAnswer(400, {"error": "invalid_choice"})
    _, judged = store.record_judgment(pair, choice, js.get(body, "judgedAt"))
    return {
        "ok": True,
        "reveal": {
            "aYear": pair["aYear"],
            "bYear": pair["bYear"],
            "aCapturedAt": pair["aCapturedAt"],
            "bCapturedAt": pair["bCapturedAt"],
            "clusterId": pair["clusterId"],
            "clusterLabel": pair["clusterLabel"],
        },
        "progress": {"judged": judged, "poolSize": len(store.current().referee_pool)},
    }


def _alias_verdict(store: JourneyStore, body: Any) -> Json:
    recording = js.to_string(js.coalesce(js.get(body, "recordingId"), ""))
    verdict_name = js.to_string(js.coalesce(js.get(body, "verdict"), ""))
    if recording not in store.current().rows_by_id:
        raise _JsAnswer(404, {"error": "recording_not_indexed"})
    if verdict_name not in ALLOWED_VERDICTS:
        raise _JsAnswer(400, {"error": "invalid_verdict"})
    verdict = make_verdict(
        recording,
        verdict_name,
        note=js.get(body, "note"),
        reviewed_by=js.get(body, "reviewedBy"),
        reviewed_at=js.get(body, "reviewedAt"),
    )
    totals = store.record_verdict(verdict)
    return {"ok": True, "verdict": verdict, "totals": totals}


# ---- configuration (serve) ------------------------------------------------------------


def _positive_integer(value: str) -> int:
    number = js.to_number(value)
    if not float(number).is_integer() or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(number)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The ``serve`` options of the journey routes (all off by default)."""

    group = parser.add_argument_group(
        "journey",
        "the corpus browser's routes; their locations come from the environment "
        "(VOICE_JOURNEY_DATA, VOICE_JOURNEY_CORPUS_ROOT, VOICE_JOURNEY_STATE_DIR, ...; "
        "journey/paths.py)",
    )
    group.add_argument("--journey", action="store_true", help="serve the journey routes")
    group.add_argument(
        "--playback-approval",
        metavar="TEXT",
        help="the recorded operator approval for playback (audio and blind referee trials)",
    )
    group.add_argument(
        "--transcript-dir", help="local transcript text (default: under the data root)"
    )
    group.add_argument("--feature-dir", help="local feature detail (default: under the data root)")
    group.add_argument("--queue-size", type=_positive_integer, default=DEFAULT_QUEUE_SIZE)
    group.add_argument("--referee-seed", type=_positive_integer, default=DEFAULT_REFEREE_SEED)


def routes_from_options(
    options: argparse.Namespace, env: Mapping[str, str] | None = None
) -> JourneyRoutes:
    """The journey route for parsed ``serve`` options: disabled without ``--journey``.

    Raises: ``ValueError`` for a non-loopback bind outside the container (the JS's bind
    check, which moved to the API's configuration) or an unreadable manifest;
    ``OSError`` for a missing manifest without a data root."""

    if not options.journey:
        return JourneyRoutes(None)
    assert_bind_allowed(options.host, env)
    paths = resolve_paths(env)
    config = JourneyConfig.from_paths(
        paths,
        playback_approval=options.playback_approval,
        transcript_dir=_path_or_none(options.transcript_dir),
        feature_dir=_path_or_none(options.feature_dir),
        queue_size=options.queue_size,
        referee_seed=options.referee_seed,
    )
    return JourneyRoutes(JourneyStore(config))


def _path_or_none(value: str | None) -> Path | None:
    return Path(value) if value else None
