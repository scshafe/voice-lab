"""Strict request and response models of the journey routes of ``/api/v1``.

@canonical api.journey_contracts -- the envelopes of the corpus browser's resources
(design §3.4: recordings, review, analysis, goals, referee) and the request bodies they
read. Envelope fields are snake_case like the rest of ``/api/v1``; what is derived from
the voice-journey manifests (a recording row, a trends or coach document, a goal) is
carried as typed JSON inside the envelope with the manifests' own (camelCase) keys, so
the row schema stays the manifests' (``voice-journey.corpus-browser-rows.v1``). JSON
Schemas are generated into ``schemas/`` (``api-v1-journey-*``) by ``cole_voice_lab.schema``.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field, JsonValue, StringConstraints

from ..journey.goals import TAKES_REQUIRED_MAX, TAKES_REQUIRED_MIN
from ..models import ContractModel

type JsonObject = dict[str, JsonValue]
type RecordingId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
type ShortText = Annotated[str, StringConstraints(max_length=200)]
type Timestamp = Annotated[str, StringConstraints(min_length=1, max_length=64)]

ReviewVerdictName = Literal[
    "clean_singing",
    "noise_contaminated_singing",
    "music_contaminated_singing",
    "non_singing",
    "still_uncertain",
]
RefereeChoice = Literal["a", "b", "too_close", "not_same_song", "skip"]
GoalMetricName = Literal[
    "vibratoRateHz",
    "vibratoExtentCents",
    "cpps",
    "longestSustainSeconds",
    "pitchP95Hz",
    "tuningMedianCentError",
]
GoalDirection = Literal["at_least", "at_most"]


class RowsPage(ContractModel):
    """Which slice of the filtered, sorted rows a page holds."""

    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    returned: int = Field(ge=0)
    total_filtered: int = Field(ge=0)


class RowTotals(ContractModel):
    """Counts over a set of rows (the filtered set, or the whole corpus)."""

    rows: int = Field(ge=0)
    buckets: dict[str, int]
    years: dict[str, int]
    review_queue_selected: int = Field(ge=0)
    reviewed: int = Field(ge=0)


class JourneyRecordings(ContractModel):
    """``GET /api/v1/recordings``: one page of rows (always paged: ``limit`` defaults to
    100, at most 500). Query: ``bucket``, ``year``, ``spotCheckStatus``,
    ``transcriptStatus``, ``lyricMatchStatus``, ``sourceType``, ``reviewQueue=selected``,
    ``q``, ``sort``, ``direction``, ``limit``, ``offset``, ``fields=list``. Every row says
    its ``sourceType`` (``voice-memos``, ``measurement``; D-051), also under
    ``fields=list``."""

    schema_version: Literal["cole-voice-lab.api.journey-recordings/v1"] = (
        "cole-voice-lab.api.journey-recordings/v1"
    )
    rows_schema_version: str
    generated_at: str
    page: RowsPage
    totals: RowTotals
    rows: tuple[JsonObject, ...]


class JourneyRecording(ContractModel):
    """``GET /api/v1/recordings/{id}``: one row, every column."""

    schema_version: Literal["cole-voice-lab.api.journey-recording/v1"] = (
        "cole-voice-lab.api.journey-recording/v1"
    )
    rows_schema_version: str
    row: JsonObject


class JourneyTranscript(ContractModel):
    """``GET /api/v1/recordings/{id}/transcript``: local transcript text, on request."""

    schema_version: Literal["cole-voice-lab.api.journey-transcript/v1"] = (
        "cole-voice-lab.api.journey-transcript/v1"
    )
    recording_id: RecordingId
    status: JsonValue
    language: JsonValue
    word_count: JsonValue
    text: str
    local_only: Literal[True] = True


class JourneyFeatureDetail(ContractModel):
    """``GET /api/v1/recordings/{id}/features``: the local per-take feature detail."""

    schema_version: Literal["cole-voice-lab.api.journey-feature-detail/v1"] = (
        "cole-voice-lab.api.journey-feature-detail/v1"
    )
    recording_id: RecordingId
    filename: JsonValue
    captured_at: JsonValue
    year: JsonValue
    bucket: JsonValue
    method_id: JsonValue
    summary: JsonValue
    detail: JsonValue
    local_only: Literal[True] = True


class JourneyReviewQueue(ContractModel):
    """``GET /api/v1/review/queue``: the selected rows in queue order."""

    schema_version: Literal["cole-voice-lab.api.journey-review-queue/v1"] = (
        "cole-voice-lab.api.journey-review-queue/v1"
    )
    strategy: str
    rows: tuple[JsonObject, ...]


class ReviewVerdictRequest(ContractModel):
    """``POST /api/v1/review/verdicts``: a spot-check verdict, kept in local state only
    (``merge-verdicts`` folds it into the results manifest later, on the host)."""

    recording_id: RecordingId
    verdict: ReviewVerdictName
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None
    reviewed_by: ShortText | None = None
    reviewed_at: Timestamp | None = None


class ReviewVerdict(ContractModel):
    recording_id: RecordingId
    verdict: ReviewVerdictName
    note: str | None
    reviewed_by: str
    reviewed_at: str
    source: Literal["corpus-browser"] = "corpus-browser"


class ReviewVerdictAnswer(ContractModel):
    schema_version: Literal["cole-voice-lab.api.journey-review-verdict/v1"] = (
        "cole-voice-lab.api.journey-review-verdict/v1"
    )
    verdict: ReviewVerdict
    totals: RowTotals


class ReviewQueueInfo(ContractModel):
    strategy: str
    target_size: int
    selected_count: int = Field(ge=0)
    recording_ids: tuple[JsonValue, ...]


class FilterOptions(ContractModel):
    buckets: tuple[JsonValue, ...]
    years: tuple[JsonValue, ...]
    spot_check_statuses: tuple[JsonValue, ...]
    transcript_statuses: tuple[JsonValue, ...]
    lyric_match_statuses: tuple[JsonValue, ...]


class JourneySummary(ContractModel):
    """``GET /api/v1/summary``: what is loaded, which gates are open, the totals."""

    schema_version: Literal["cole-voice-lab.api.journey-summary/v1"] = (
        "cole-voice-lab.api.journey-summary/v1"
    )
    rows_schema_version: str
    generated_at: str
    playback_enabled: bool
    transcript_text_enabled: bool
    feature_detail_enabled: bool
    source: JsonObject
    review_queue: ReviewQueueInfo
    totals: RowTotals
    filter_options: FilterOptions


class JourneyDocument(ContractModel):
    """``GET /api/v1/{trends,journeys,coach,goals}``: a manifest-derived document.
    ``available`` is false (and ``document`` says why) when its manifest is not
    loaded."""

    schema_version: Literal["cole-voice-lab.api.journey-document/v1"] = (
        "cole-voice-lab.api.journey-document/v1"
    )
    kind: Literal["trends", "journeys", "coach", "goals"]
    available: bool
    document: JsonObject


class GoalRequest(ContractModel):
    """``POST /api/v1/goals``: a pre-registered n=1 experiment (``goals.py`` rules)."""

    metric: GoalMetricName
    direction: GoalDirection
    target: float = Field(allow_inf_nan=False)
    takes_required: int = Field(default=10, ge=TAKES_REQUIRED_MIN, le=TAKES_REQUIRED_MAX)
    cluster_id: ShortText | None = None
    title: Annotated[str, StringConstraints(max_length=500)] | None = None


class GoalCreated(ContractModel):
    schema_version: Literal["cole-voice-lab.api.journey-goal/v1"] = (
        "cole-voice-lab.api.journey-goal/v1"
    )
    goal: JsonObject


class GoalAbandoned(ContractModel):
    schema_version: Literal["cole-voice-lab.api.journey-goal-abandoned/v1"] = (
        "cole-voice-lab.api.journey-goal-abandoned/v1"
    )
    goal_id: str
    abandoned_at: str


class RefereeSide(ContractModel):
    audio_url: str
    gain_db: float


class RefereePair(ContractModel):
    """A blind trial: no year, date or cluster until the verdict reveals them."""

    pair_id: str
    a: RefereeSide
    b: RefereeSide


class RefereeNext(ContractModel):
    """``GET /api/v1/referee/next`` (needs the playback approval)."""

    schema_version: Literal["cole-voice-lab.api.journey-referee-next/v1"] = (
        "cole-voice-lab.api.journey-referee-next/v1"
    )
    judged: int = Field(ge=0)
    pool_size: int = Field(ge=0)
    pair: RefereePair | None


class RefereeVerdictRequest(ContractModel):
    """``POST /api/v1/referee/verdicts`` (needs the playback approval)."""

    pair_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    choice: RefereeChoice
    judged_at: Timestamp | None = None


class RefereeReveal(ContractModel):
    a_year: JsonValue
    b_year: JsonValue
    a_captured_at: JsonValue
    b_captured_at: JsonValue
    cluster_id: JsonValue
    cluster_label: JsonValue


class RefereeVerdictAnswer(ContractModel):
    schema_version: Literal["cole-voice-lab.api.journey-referee-verdict/v1"] = (
        "cole-voice-lab.api.journey-referee-verdict/v1"
    )
    reveal: RefereeReveal
    judged: int = Field(ge=0)
    pool_size: int = Field(ge=0)


class RefereeResults(ContractModel):
    """``GET /api/v1/referee/results``: the Bradley-Terry fit by year."""

    schema_version: Literal["cole-voice-lab.api.journey-referee-results/v1"] = (
        "cole-voice-lab.api.journey-referee-results/v1"
    )
    enabled: bool
    pool_size: int = Field(ge=0)
    fit: JsonObject


class WatchStatus(ContractModel):
    """``GET /api/v1/watch``: the job runner's watcher, as its state file says."""

    schema_version: Literal["cole-voice-lab.api.journey-watch/v1"] = (
        "cole-voice-lab.api.journey-watch/v1"
    )
    enabled: bool
    reason: str | None = None
    state: JsonObject | None = None


JOURNEY_API_SCHEMA_MODELS: Final[dict[str, type[BaseModel]]] = {
    "api-v1-journey-recordings": JourneyRecordings,
    "api-v1-journey-recording": JourneyRecording,
    "api-v1-journey-transcript": JourneyTranscript,
    "api-v1-journey-feature-detail": JourneyFeatureDetail,
    "api-v1-journey-review-queue": JourneyReviewQueue,
    "api-v1-journey-review-verdict-request": ReviewVerdictRequest,
    "api-v1-journey-review-verdict": ReviewVerdictAnswer,
    "api-v1-journey-summary": JourneySummary,
    "api-v1-journey-document": JourneyDocument,
    "api-v1-journey-goal-request": GoalRequest,
    "api-v1-journey-goal": GoalCreated,
    "api-v1-journey-goal-abandoned": GoalAbandoned,
    "api-v1-journey-referee-next": RefereeNext,
    "api-v1-journey-referee-verdict-request": RefereeVerdictRequest,
    "api-v1-journey-referee-verdict": RefereeVerdictAnswer,
    "api-v1-journey-referee-results": RefereeResults,
    "api-v1-journey-watch": WatchStatus,
}
