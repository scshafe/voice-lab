"""The corpus browser's data layer (port of voice-journey's ``corpus-browser.mjs``).

Pure functions over the repo-safe manifests, plus the small state files the API writes,
and no HTTP (the routes are ``api/journey_routes.py``):

- rows: :func:`build_corpus_rows` joins the recording index, the filter results, the
  transcript and lyric manifests and the local verdict state into
  ``voice-journey.corpus-browser-rows.v1``; :func:`select_review_queue`,
  :func:`filter_rows`, :func:`sort_rows`, :func:`query_rows`, :func:`paginate_rows`,
  :func:`project_rows`, :func:`summarize_rows`, :func:`filter_options_for`;
- analysis: :func:`build_voice_trends`, :func:`build_referee_pairs` (seeded,
  ``mulberry32`` bit for bit), :func:`loudness_match_gains_db`,
  :func:`bradley_terry_by_year`, the coach and goals payloads (``coach.py`` and
  ``goals.py`` keep their rules);
- state: :func:`read_state`, :func:`read_referee_state`, :func:`read_goals_state`,
  :func:`read_watch_status` and the writes, which go to the state directory only
  (``VOICE_JOURNEY_STATE_DIR``; on the PC ``/data/voice-lab/state``), never next to
  the manifests, which the API reads only;
- :class:`JourneyStore`: the loaded manifests (the JS "serve state"), reloaded when any
  input manifest's modification time, size or inode changes. The watcher is a separate
  process now (design §5.3), so nothing calls a reload hook: the store checks the files
  on each read, which costs a ``stat`` per manifest;
- :func:`open_recording_audio`: the read-only seam for playback, under the phone corpus
  root and the measurement directory (see its docstring);
- ``merge-verdicts``: the command line (``python -m cole_voice_lab.journey.browser``).

Outputs are the JavaScript's, byte for byte where the JS wrote JSON
(``jsonio.stringify``). Differences from the JS:

- Every row says its source (D-051, Recording v1 step 7): ``sourceType``, after
  ``filename``, is the index row's ``source.type`` (``sources.source_type``: ``voice-memos``
  for a row of an index written before the source port). It is an exact filter like
  ``bucket`` (``sourceType=measurement``) and a column of the ``fields=list`` projection.
- ``serve`` is ``python -m cole_voice_lab.api serve --journey``; this command line keeps
  ``merge-verdicts`` (and ``help``), with ``--results``, ``--state`` and ``--out``.
  Its default state file is in the state directory (the JS used the artifacts
  directory, which is the state directory's default).
- Playback reads a recording's file directly through :func:`open_recording_audio`
  instead of spawning ``host-access.mjs read-handle``: the file is the one the index row
  names, and only when the row's id is the id of that name (``host-access``'s rule), so
  an index cannot point the API at another file. The JS listed the whole drop for each
  request. Measurement sessions are readable too.
- The state files are replaced atomically (``jsonio.write_json``); same bytes.
- ``localeCompare`` is :func:`jscompat.locale_compare_key` (exact on the ASCII ids,
  file names and timestamps here). ``String.prototype.slice`` on timestamps counts code
  points (timestamps are ASCII). ``Math.log``/``Math.exp`` in the Bradley-Terry fit
  are the platform's; V8 uses fdlibm, which can differ in the last bit; the strengths
  are rounded to three places, so the outputs agree except at an exact rounding tie.
- A value whose JavaScript coercion would have thrown (a manifest number where a string
  is expected, ``null`` as a request body) raises a ``TypeError`` or is read as an empty
  object; both are server errors or refusals, as in the JS.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import IO, Any, Final

from . import jscompat as js
from .coach import build_coach_payload
from .corpus import ALLOWED_EXTENSIONS, recording_id
from .goals import (
    GOALS_STATE_SCHEMA_VERSION,
    create_goal,
    goal_context,
    goals_payload,
    validate_goal_spec,
)
from .jsonio import (
    MISSING,
    iso_timestamp,
    js_object,
    js_round,
    read_json,
    stringify,
    to_fixed,
    write_json,
)
from .paths import JourneyPaths, resolve_paths
from .sources import source_type

Json = dict[str, Any]
SearchParams = Sequence[tuple[str, str]]

ROWS_SCHEMA_VERSION: Final = "voice-journey.corpus-browser-rows.v1"
STATE_SCHEMA_VERSION: Final = "voice-journey.corpus-browser-state.v1"
REFEREE_STATE_SCHEMA_VERSION: Final = "voice-journey.referee-state.v1"
TRENDS_SCHEMA_VERSION: Final = "voice-journey.voice-trends.v1"
WATCH_STATE_VERSION: Final = "voice-journey.watchd-state.v1"

STATE_NAME: Final = "corpus-browser-state.json"
REFEREE_STATE_NAME: Final = "referee-state.json"
GOALS_STATE_NAME: Final = "goals-state.json"
WATCH_STATE_NAME: Final = "watchd-state.json"

INDEX_MANIFEST: Final = "recording-index.json"
RESULTS_MANIFEST: Final = "full-corpus-filter-results.json"
TRANSCRIPTS_MANIFEST: Final = "local-stt-transcripts.json"
LYRICS_MANIFEST: Final = "local-lyric-indicators.json"
FEATURES_MANIFEST: Final = "local-voice-features.json"
JOURNEYS_MANIFEST: Final = "song-journeys.json"

RELIABLE_REJECTION_MAX: Final = 0.10
DEFAULT_QUEUE_SIZE: Final = 56
DEFAULT_REFEREE_SEED: Final = 2026
REFEREE_PAIRS_PER_CLUSTER: Final = 30
REFEREE_CHOICES: Final = ("a", "b", "too_close", "not_same_song", "skip")
LOUDNESS_MATCH_CAP_DB: Final = 12
ALLOWED_VERDICTS: Final = (
    "clean_singing",
    "noise_contaminated_singing",
    "music_contaminated_singing",
    "non_singing",
    "still_uncertain",
)
SORTABLE_FIELDS: Final = frozenset(
    {
        "filename",
        "capturedAt",
        "year",
        "durationSeconds",
        "sizeBytes",
        "bucket",
        "confidence",
        "spotCheckStatus",
        "transcriptStatus",
        "lyricMatchStatus",
        "reviewQueueOrder",
    }
)
ROWS_PAGE_LIMIT_MAX: Final = 500
ROWS_PAGE_LIMIT_DEFAULT: Final = 100
_MAX_SAFE_INTEGER: Final = 2**53 - 1
# The lean projection behind `fields=list`: the scalar columns the corpus table renders.
LIST_PROJECTION_FIELDS: Final = (
    "recordingId",
    "filename",
    "sourceType",
    "capturedAt",
    "year",
    "durationSeconds",
    "bucket",
    "confidence",
    "spotCheckRecommended",
    "spotCheckStatus",
    "humanVerdict",
    "transcriptStatus",
    "transcriptHasWords",
    "transcriptWordCount",
    "lyricMatchCluster",
    "lyricMatchClusterLabel",
    "reviewQueueSelected",
    "reviewQueueOrder",
)
_AUDIO_TYPES: Final = {".m4a": "audio/mp4", ".wav": "audio/wav"}
_MEASUREMENT_AUDIO: Final = frozenset({".wav"})


class BrowserError(Exception):
    """A refused request, with the HTTP status the JS answered (``httpError``)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---- small JavaScript rules -----------------------------------------------------------


def _text(value: Any, what: str = "value") -> str:
    """A string the JS called a string method on (``.slice``): anything else threw."""

    if not isinstance(value, str):
        raise TypeError(f"{what}.slice is not a function")
    return value


def _div(left: float, right: float) -> float:
    """JavaScript's ``/``: division by zero is an infinity or NaN, never an error."""

    if right == 0:
        if left == 0 or left != left:
            return math.nan
        return math.copysign(math.inf, left) * math.copysign(1.0, right)
    return left / right


def _default_sort_key(value: Any) -> bytes:
    """``Array.prototype.sort()`` without a comparator: ``String(v)`` by UTF-16 units."""

    return js.utf16_key(js.to_string(value))


def year_for(captured_at: Any) -> Any:
    return _text(captured_at, "capturedAt")[:4] if js.truthy(captured_at) else "unknown"


def strict_equal(left: Any, right: Any) -> bool:
    """``left === right`` for JSON values (objects by identity)."""

    if js.is_number(left) and js.is_number(right):
        return bool(left == right)
    if isinstance(left, str | bool) and type(left) is type(right):
        return bool(left == right)
    return left is right


def compare_values(left: Any, right: Any) -> float:
    """``compareValues``: equal is 0, null and undefined sort last, numbers by
    difference, anything else by ``localeCompare`` of its string form."""

    if strict_equal(left, right):
        return 0
    if js.nullish(left):
        return 1
    if js.nullish(right):
        return -1
    if js.is_number(left) and js.is_number(right):
        return float(left) - float(right)
    return js.locale_compare(js.to_string(left), js.to_string(right))


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


# ---- state files ----------------------------------------------------------------------


def empty_state() -> Json:
    return {"schemaVersion": STATE_SCHEMA_VERSION, "updatedAt": None, "verdicts": []}


def empty_referee_state() -> Json:
    return {"schemaVersion": REFEREE_STATE_SCHEMA_VERSION, "updatedAt": None, "judgments": []}


def _read_versioned(
    path: Path | str, key: str, version: str, items: str, empty: Callable[[], Json]
) -> Json:
    try:
        state = read_json(path)
    except FileNotFoundError:
        return empty()
    if js.get(state, key) != version or not isinstance(js.get(state, items), list):
        return empty()
    return dict(state)


def read_state(path: Path | str) -> Json:
    """The local verdict state (``corpus-browser-state.v1``); empty when absent or foreign."""

    return _read_versioned(path, "schemaVersion", STATE_SCHEMA_VERSION, "verdicts", empty_state)


def read_referee_state(path: Path | str) -> Json:
    return _read_versioned(
        path,
        "schemaVersion",
        REFEREE_STATE_SCHEMA_VERSION,
        "judgments",
        empty_referee_state,
    )


def read_goals_state(path: Path | str) -> Json:
    return _read_versioned(
        path,
        "version",
        GOALS_STATE_SCHEMA_VERSION,
        "goals",
        lambda: {"version": GOALS_STATE_SCHEMA_VERSION, "goals": []},
    )


def read_optional_json(path: Path | str) -> Any:
    try:
        return read_json(path)
    except FileNotFoundError:
        return None


def verdict_map_for(state: Any) -> dict[Any, Json]:
    found: dict[Any, Json] = {}
    for verdict in js.coalesce(js.get(state, "verdicts"), []):
        recording = js.get(verdict, "recordingId")
        if js.truthy(recording):
            found[recording] = verdict
    return found


def _upsert(entries: Iterable[Json], entry: Json, key: str) -> list[Json]:
    kept = [item for item in entries if js.get(item, key) != entry[key]]
    kept.append(entry)
    return sorted(kept, key=lambda item: js.locale_compare_key(_text(item[key], key)))


def upsert_verdict(state: Json, verdict: Json) -> Json:
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "updatedAt": verdict["reviewedAt"],
        "verdicts": _upsert(js.coalesce(js.get(state, "verdicts"), []), verdict, "recordingId"),
    }


def upsert_referee_judgment(state: Json, judgment: Json) -> Json:
    return {
        "schemaVersion": REFEREE_STATE_SCHEMA_VERSION,
        "updatedAt": judgment["judgedAt"],
        "judgments": _upsert(js.coalesce(js.get(state, "judgments"), []), judgment, "pairId"),
    }


def read_watch_status(path: Path | str) -> Json:
    """``GET /api/watch/status`` from the job runner's watch state file.

    The watcher no longer runs inside the API (design §5.3): what the API can say is
    what the job runner persisted. Absent (or of another version) it is the JS's
    "not enabled" answer; otherwise the persisted part of the JS status snapshot
    (``lastRun``, the last five ``recentRuns``, ``nextDigestAt`` as an ISO time)."""

    state = read_optional_json(path)
    if js.get(state, "version") != WATCH_STATE_VERSION:
        return {"enabled": False, "reason": "watch_not_enabled"}
    next_digest = js.get(state, "nextDigestAt")
    recent = js.coalesce(js.get(state, "recentRuns"), [])
    return {
        "enabled": True,
        "source": "job_runner_state_file",
        "lastRun": js.coalesce(js.get(state, "lastRun"), None),
        "recentRuns": list(recent)[-5:] if isinstance(recent, list) else [],
        "nextDigestAt": js.iso_from_ms(js.to_number(next_digest))
        if js.truthy(next_digest)
        else None,
    }


# ---- rows -----------------------------------------------------------------------------


def manifest_results_by_recording_id(manifest: Any) -> dict[Any, Any]:
    found: dict[Any, Any] = {}
    for result in js.coalesce(js.get(manifest, "results"), []):
        recording = js.get(result, "recordingId")
        if js.truthy(recording):
            found[recording] = result
    return found


def manifest_source_summary(manifest: Any) -> Json:
    if not js.truthy(manifest):
        return {"available": False, "schemaVersion": None, "recordingCount": None}
    return {
        "available": True,
        "schemaVersion": js.coalesce(js.get(manifest, "schemaVersion"), None),
        "recordingCount": js.coalesce(
            js.length(js.get(manifest, "results")),
            js.get(js.get(manifest, "totals"), "recordings"),
            js.get(js.get(manifest, "sourceTranscripts"), "recordingCount"),
            None,
        ),
    }


def _path(value: Any, *keys: str) -> Any:
    for key in keys:
        value = js.get(value, key)
    return value


def _greater_than_zero(value: Any) -> bool:
    return js.to_number(value) > 0


def _row_for(
    recording: Any,
    result_by_id: Mapping[Any, Any],
    state_by_id: Mapping[Any, Json],
    transcript_by_id: Mapping[Any, Any],
    lyric_by_id: Mapping[Any, Any],
) -> Json:
    recording_key = js.get(recording, "recordingId")
    result = result_by_id.get(recording_key)
    classification = js.coalesce(js.get(result, "classification"), None)
    spot_check = js.coalesce(js.get(result, "humanSpotCheck"), None)
    local = state_by_id.get(recording_key)
    transcript = transcript_by_id.get(recording_key)
    lyric = js.coalesce(js.get(lyric_by_id.get(recording_key), "lyricMatch"), None)
    wordless = js.coalesce(_path(lyric, "signals", "wordlessVocalise"), None)
    word_count = js.coalesce(_path(transcript, "counts", "words"), None)
    captured_at = js.coalesce(js.get(result, "capturedAt"), js.get(recording, "capturedAt"), None)
    has_words = js.coalesce(
        _path(transcript, "derivedFeatures", "hasTranscribedWords"),
        None if word_count is None else _greater_than_zero(word_count),
    )
    zero_words = js.is_number(word_count) and word_count == 0
    return {
        "recordingId": recording_key,
        "filename": js.get(recording, "filename"),
        "sourceType": source_type(recording),
        "capturedAt": captured_at,
        "year": js.coalesce(js.get(result, "year"), year_for(captured_at)),
        "durationSeconds": js.coalesce(
            _path(result, "features", "durationSeconds"),
            js.get(recording, "durationSeconds"),
            None,
        ),
        "sizeBytes": js.coalesce(_path(recording, "file", "sizeBytes"), None),
        "bucket": js.coalesce(js.get(classification, "finalBucket"), "unclassified"),
        "confidence": js.coalesce(js.get(classification, "confidence"), None),
        "rationale": js.coalesce(js.get(classification, "rationale"), None),
        "spotCheckRecommended": js.coalesce(js.get(spot_check, "recommended"), False),
        "spotCheckStatus": "reviewed"
        if local is not None
        else js.coalesce(js.get(spot_check, "status"), "not_reviewed"),
        "humanVerdict": js.coalesce(js.get(local, "verdict"), js.get(spot_check, "verdict"), None),
        "reviewedBy": js.coalesce(
            js.get(local, "reviewedBy"), js.get(spot_check, "reviewedBy"), None
        ),
        "reviewedAt": js.coalesce(
            js.get(local, "reviewedAt"), js.get(spot_check, "reviewedAt"), None
        ),
        "transcriptStatus": js.coalesce(js.get(transcript, "status"), "not_available"),
        "transcriptLanguage": js.coalesce(_path(transcript, "language", "detected"), None),
        "transcriptWordCount": word_count,
        "transcriptSegmentCount": js.coalesce(_path(transcript, "counts", "segments"), None),
        "transcriptCharacterCount": js.coalesce(_path(transcript, "counts", "characters"), None),
        "transcriptHasWords": has_words,
        "transcriptTextLocalOnly": js.truthy(
            _path(transcript, "localArtifacts", "transcriptTextPath")
        )
        and _path(transcript, "localArtifacts", "textIncludedInManifest") is False,
        "lyricMatchStatus": js.coalesce(js.get(lyric, "status"), "not_available"),
        "lyricMatchMarked": js.truthy(js.get(lyric, "marked")),
        "lyricMatchMethod": js.coalesce(js.get(lyric, "method"), None),
        "lyricMatchConfidence": js.coalesce(js.get(lyric, "confidence"), None),
        "lyricMatchCluster": js.coalesce(js.get(lyric, "matchedCluster"), None),
        "lyricMatchClusterLabel": js.coalesce(js.get(lyric, "matchedClusterLabel"), None),
        "wordlessVocaliseMarked": js.truthy(
            js.coalesce(
                js.get(wordless, "marked"),
                js.get(transcript, "status") == "completed" and zero_words,
            )
        ),
        "wordlessVocaliseConfidence": js.coalesce(js.get(wordless, "confidence"), None),
        "wordlessVocaliseLanguage": js.coalesce(
            js.get(wordless, "detectedLanguage"),
            _path(transcript, "language", "detected"),
            None,
        ),
        "reviewQueueSelected": False,
        "reviewQueueOrder": None,
        "reviewQueueReason": None,
    }


def build_corpus_rows(
    index_manifest: Any,
    results_manifest: Any,
    *,
    state: Json | None = None,
    queue_size: int = DEFAULT_QUEUE_SIZE,
    transcripts_manifest: Any = None,
    lyrics_manifest: Any = None,
) -> Json:
    """``buildCorpusRows``: one row per indexed recording, the review queue marked."""

    state = empty_state() if state is None else state
    result_by_id = manifest_results_by_recording_id(results_manifest)
    state_by_id = verdict_map_for(state)
    transcript_by_id = manifest_results_by_recording_id(transcripts_manifest)
    lyric_by_id = manifest_results_by_recording_id(lyrics_manifest)
    rows = [
        _row_for(recording, result_by_id, state_by_id, transcript_by_id, lyric_by_id)
        for recording in js.coalesce(js.get(index_manifest, "recordings"), [])
    ]
    queue = select_review_queue(rows, queue_size)
    for order, (row, reason) in enumerate(queue, start=1):
        row["reviewQueueSelected"] = True
        row["reviewQueueOrder"] = order
        row["reviewQueueReason"] = reason
    return {
        "schemaVersion": ROWS_SCHEMA_VERSION,
        "generatedAt": iso_timestamp(),
        "source": {
            "index": {
                "schemaVersion": js.get(index_manifest, "schemaVersion"),
                "recordingCount": js.coalesce(
                    _path(index_manifest, "totals", "recordings"),
                    js.length(js.get(index_manifest, "recordings")),
                    None,
                ),
            },
            "results": {
                "schemaVersion": js.get(results_manifest, "schemaVersion"),
                "recordingCount": js.coalesce(js.length(js.get(results_manifest, "results")), None),
            },
            "transcripts": manifest_source_summary(transcripts_manifest),
            "lyrics": manifest_source_summary(lyrics_manifest),
            "state": {
                "schemaVersion": js.get(state, "schemaVersion"),
                "verdictCount": js.coalesce(js.length(js.get(state, "verdicts")), 0),
            },
            "readScope": {
                "committedManifestsOnly": True,
                "audioBytes": False,
                "transcriptText": False,
                "verdictWrites": True,
                "upload": False,
                "corpusMutation": False,
            },
        },
        "reviewQueue": {
            "strategy": "uncertain_year_confidence_stratified_with_bucket_controls.v1",
            "targetSize": queue_size,
            "selectedCount": len(queue),
            "recordingIds": [row["recordingId"] for row, _ in queue],
        },
        "totals": summarize_rows(rows),
        "rows": rows,
    }


def _compare_queue_rows(left: Json, right: Json) -> float:
    return (
        compare_values(left["year"], right["year"])
        or compare_values(left["confidence"], right["confidence"])
        or js.locale_compare(
            _text(left["recordingId"], "recordingId"), _text(right["recordingId"], "recordingId")
        )
    )


def _queue_order(left: Json, right: Json) -> int:
    return _sign(_compare_queue_rows(left, right))


_QUEUE_ORDER = cmp_to_key(_queue_order)


def stratified_pick(rows: Sequence[Json], count: int) -> list[Json]:
    if count <= 0 or not rows:
        return []
    ordered = sorted(rows, key=_QUEUE_ORDER)
    if len(ordered) <= count:
        return ordered
    picked: list[Json] = []
    seen: set[Any] = set()
    for index in range(count):
        position = int(js_round((index * (len(ordered) - 1)) / ((count - 1) or 1)))
        row = ordered[position]
        if row["recordingId"] not in seen:
            seen.add(row["recordingId"])
            picked.append(row)
    for row in ordered:
        if len(picked) >= count:
            break
        if row["recordingId"] not in seen:
            seen.add(row["recordingId"])
            picked.append(row)
    return picked


def select_review_queue(
    rows: Sequence[Json], target_size: int = DEFAULT_QUEUE_SIZE
) -> list[tuple[Json, str]]:
    """``selectReviewQueue``: bucket controls, then uncertain rows stratified by year and
    confidence, then a stratified backfill; ``(row, reason)`` in queue order."""

    selected: list[tuple[Json, str]] = []
    seen: set[Any] = set()

    def add(candidates: Iterable[Json], count: int, reason: str) -> None:
        fresh = [row for row in candidates if row["recordingId"] not in seen]
        for row in stratified_pick(fresh, count):
            seen.add(row["recordingId"])
            selected.append((row, reason))

    controls = (
        ("music_contaminated_singing", 1),
        ("noise_contaminated_singing", 4),
        ("non_singing", 4),
        ("clean_singing", 6),
    )
    control_budget = min(15, max(0, target_size - 40))
    used = 0
    for bucket, preferred in controls:
        count = min(preferred, max(0, control_budget - used))
        if count > 0:
            add((row for row in rows if row["bucket"] == bucket), count, f"{bucket}_control")
            used += count
    add(
        (row for row in rows if row["bucket"] == "uncertain_manual_review"),
        target_size - len(selected),
        "uncertain_year_confidence_stratified",
    )
    if len(selected) < target_size:
        add(rows, target_size - len(selected), "backfill_stratified")
    selected.sort(key=lambda entry: _QUEUE_ORDER(entry[0]))
    return selected[:target_size]


def summarize_rows(rows: Iterable[Json]) -> Json:
    buckets: dict[str, int] = {}
    years: dict[str, int] = {}
    count = selected = reviewed = 0
    for row in rows:
        count += 1
        bucket = js.to_string(row["bucket"])
        buckets[bucket] = buckets.get(bucket, 0) + 1
        year = js.to_string(js.coalesce(row["year"], "unknown"))
        years[year] = years.get(year, 0) + 1
        if js.truthy(row["reviewQueueSelected"]):
            selected += 1
        if row["spotCheckStatus"] == "reviewed":
            reviewed += 1
    return {
        "rows": count,
        "buckets": js_object(buckets.items()),
        "years": js_object(years.items()),
        "reviewQueueSelected": selected,
        "reviewed": reviewed,
    }


def _normalize(value: Any) -> str:
    return js.trim(js.to_string(js.coalesce(value, ""))).lower()


def _first(params: SearchParams, name: str) -> str | None:
    """``URLSearchParams.get``: the first value, ``None`` when absent."""

    return next((value for key, value in params if key == name), None)


def filter_rows(rows: Sequence[Json], filters: Mapping[str, Any] | None = None) -> list[Json]:
    """``filterRows``: exact matches on the table axes, then ``q`` as a substring of the
    searchable columns (case-folded)."""

    filters = filters or {}
    query = _normalize(filters.get("q"))
    exact = (
        "bucket",
        "year",
        "spotCheckStatus",
        "transcriptStatus",
        "lyricMatchStatus",
        "sourceType",
    )
    searchable = (
        "filename",
        "recordingId",
        "bucket",
        "rationale",
        "transcriptLanguage",
        "lyricMatchCluster",
        "lyricMatchClusterLabel",
    )

    def keep(row: Json) -> bool:
        for name in exact:
            wanted = filters.get(name)
            if js.truthy(wanted) and row.get(name, MISSING) != wanted:
                return False
        if filters.get("reviewQueue") == "selected" and not js.truthy(row["reviewQueueSelected"]):
            return False
        if not query:
            return True
        return any(query in _normalize(row.get(name, MISSING)) for name in searchable)

    return [row for row in rows if keep(row)]


def sort_rows(
    rows: Sequence[Json], field_name: str = "capturedAt", direction: str = "asc"
) -> list[Json]:
    sort_field = field_name if field_name in SORTABLE_FIELDS else "capturedAt"
    multiplier = -1 if direction == "desc" else 1

    def order(left: Json, right: Json) -> int:
        return _sign(
            compare_values(left.get(sort_field, MISSING), right.get(sort_field, MISSING))
            * multiplier
        )

    return sorted(rows, key=cmp_to_key(order))


def query_rows(rows: Sequence[Json], params: SearchParams) -> list[Json]:
    """``queryRows``: the filters (``Object.fromEntries``: the last value of a repeated
    name), then ``sort``/``direction`` (``get``: the first)."""

    filtered = filter_rows(rows, dict(params))
    return sort_rows(
        filtered, _first(params, "sort") or "capturedAt", _first(params, "direction") or "asc"
    )


def _clamp_integer(value: str | None, fallback: int, low: int, high: int) -> int:
    parsed = js.parse_int(js.coalesce(value, ""))
    if not math.isfinite(parsed):
        return fallback
    return int(min(high, max(low, parsed)))


def paginate_rows(rows: Sequence[Json], params: SearchParams) -> tuple[list[Json], Json | None]:
    """``paginateRows``: a page only when ``limit`` or ``offset`` is given; bad values
    fall back, out-of-range ones are clamped."""

    limit_param, offset_param = _first(params, "limit"), _first(params, "offset")
    if limit_param is None and offset_param is None:
        return list(rows), None
    limit = _clamp_integer(limit_param, ROWS_PAGE_LIMIT_DEFAULT, 1, ROWS_PAGE_LIMIT_MAX)
    offset = _clamp_integer(offset_param, 0, 0, _MAX_SAFE_INTEGER)
    page_rows = list(rows[offset : offset + limit])
    return page_rows, {
        "offset": offset,
        "limit": limit,
        "returned": len(page_rows),
        "totalFiltered": len(rows),
    }


def project_rows(rows: Sequence[Json], fields_param: str | None) -> list[Json]:
    if fields_param is None:
        return list(rows)
    if fields_param != "list":
        raise BrowserError(400, "unsupported_fields_projection")
    return [{name: row.get(name, MISSING) for name in LIST_PROJECTION_FIELDS} for row in rows]


def _unique_sorted(values: Iterable[Any]) -> list[Any]:
    unique: list[Any] = []
    for value in values:
        if js.truthy(value) and value not in unique:
            unique.append(value)
    return sorted(unique, key=_default_sort_key)


def filter_options_for(rows: Sequence[Json]) -> Json:
    def column(name: str) -> list[Any]:
        return _unique_sorted(row.get(name, MISSING) for row in rows)

    return {
        "buckets": column("bucket"),
        "years": column("year"),
        "spotCheckStatuses": column("spotCheckStatus"),
        "transcriptStatuses": column("transcriptStatus"),
        "lyricMatchStatuses": column("lyricMatchStatus"),
    }


def apply_verdict_to_row(row: Json, verdict: Json) -> Json:
    """The row as the JS's ``applyVerdictToRows`` leaves it (a new dict, same key order)."""

    return {
        **row,
        "spotCheckStatus": "reviewed",
        "humanVerdict": verdict["verdict"],
        "reviewedBy": verdict["reviewedBy"],
        "reviewedAt": verdict["reviewedAt"],
    }


def make_verdict(
    recording: str, verdict: str, *, note: Any, reviewed_by: Any, reviewed_at: Any
) -> Json:
    """``parseVerdictRequest``'s record: falsy optional fields take their defaults."""

    return {
        "recordingId": recording,
        "verdict": verdict,
        "note": js.to_string(note) if js.truthy(note) else None,
        "reviewedBy": js.to_string(reviewed_by) if js.truthy(reviewed_by) else "operator",
        "reviewedAt": js.to_string(reviewed_at) if js.truthy(reviewed_at) else iso_timestamp(),
        "source": "corpus-browser",
    }


def merge_verdicts_into_results(results_manifest: Json, state: Json) -> Json:
    """``mergeVerdictsIntoResults``: repo-safe ``humanSpotCheck`` metadata only."""

    state_by_id = verdict_map_for(state)
    merged = 0
    results = []
    for result in js.coalesce(js.get(results_manifest, "results"), []):
        verdict = state_by_id.get(js.get(result, "recordingId"))
        if verdict is None:
            results.append(result)
            continue
        merged += 1
        existing = js.get(result, "humanSpotCheck")
        base = dict(existing) if isinstance(existing, Mapping) else {}
        results.append(
            {
                **result,
                "humanSpotCheck": {
                    **base,
                    "recommended": js.coalesce(js.get(existing, "recommended"), True),
                    "status": "reviewed",
                    "verdict": js.get(verdict, "verdict"),
                    "reviewedBy": js.get(verdict, "reviewedBy"),
                    "reviewedAt": js.get(verdict, "reviewedAt"),
                    "notes": js.get(verdict, "note"),
                    "source": js.get(verdict, "source"),
                },
            }
        )
    return {
        **results_manifest,
        "updatedAt": iso_timestamp(),
        "humanSpotCheckMerge": {
            "sourceStateSchemaVersion": js.get(state, "schemaVersion"),
            "mergedCount": merged,
            "mergedAt": iso_timestamp(),
            "repoSafe": True,
            "audioBytes": False,
            "transcriptText": False,
        },
        "results": results,
    }


# ---- voice trends ---------------------------------------------------------------------


def quantile_of(values: Iterable[Any], p: float) -> Any:
    ordered = sorted(value for value in values if js.is_finite(value))
    if not ordered:
        return None
    return ordered[math.floor((len(ordered) - 1) * p)]


def median_of(values: Iterable[Any]) -> Any:
    return quantile_of(values, 0.5)


def round_to(value: Any, places: int = 3) -> float | None:
    return to_fixed(float(value), places) if js.is_finite(value) else None


def quarter_for(captured_at: Any) -> str | None:
    if not js.truthy(captured_at) or js.to_number(js.length(captured_at)) < 7:
        return None
    text = _text(captured_at, "capturedAt")
    month = js.to_number(text[5:7])
    if not float(month).is_integer() or month < 1 or month > 12:
        return None
    return f"{text[:4]}-Q{math.ceil(month / 3)}"


def total_rejection_share_for(feature_row: Any) -> Any:
    voicing = js.coalesce(_path(feature_row, "features", "voicing"), {})
    return js.coalesce(js.get(voicing, "rejectedOutlierShare"), 0) + js.coalesce(
        js.get(voicing, "rejectedGlobalOutlierShare"), 0
    )


def _features(row: Any) -> Any:
    return js.get(row, "features")


def _quarter_rollups(source: Sequence[Any], completed_by_quarter: Mapping[str, int]) -> list[Json]:
    by_quarter: dict[str, list[Any]] = {}
    for row in source:
        quarter = quarter_for(js.get(row, "capturedAt"))
        if quarter is None:
            continue
        by_quarter.setdefault(quarter, []).append(row)
    rollups = []
    for quarter in sorted(by_quarter, key=js.utf16_key):
        group = by_quarter[quarter]

        def pitch(selector: str, group: list[Any] = group) -> list[Any]:
            return [
                js.coalesce(_path(_features(row), "pitch", "f0Hz", selector), None) for row in group
            ]

        def series(*keys: str, group: list[Any] = group) -> list[Any]:
            return [js.coalesce(_path(_features(row), *keys), None) for row in group]

        def iqr(values: list[Any]) -> list[Any]:
            return [round_to(quantile_of(values, 0.25), 2), round_to(quantile_of(values, 0.75), 2)]

        medians = [value for value in pitch("p50") if value is not None]
        register = {"low": 0, "mid": 0, "high": 0}
        for value in medians:
            if value < 200:
                register["low"] += 1
            elif value < 300:
                register["mid"] += 1
            else:
                register["high"] += 1
        register_total = max(len(medians), 1)
        rates = series("vibrato", "meanRateHz")
        extents = series("vibrato", "meanExtentCents")
        time_shares = series("vibrato", "vibratoTimeShare")
        sustains = series("phrasing", "longestSustainedSeconds")
        cpps_values = series("quality", "cpps")
        hnr_values = series("quality", "meanHnrDb")
        rollups.append(
            {
                "quarter": quarter,
                "n": completed_by_quarter.get(quarter, len(group)),
                "nReliable": len(group),
                "envelope": {
                    "loHz": round_to(quantile_of(pitch("p05"), 0.05), 1),
                    "hiHz": round_to(quantile_of(pitch("p95"), 0.95), 1),
                },
                "typical": {
                    "loHz": round_to(median_of(pitch("p25")), 1),
                    "hiHz": round_to(median_of(pitch("p75")), 1),
                },
                "medianHz": round_to(median_of(pitch("p50")), 1),
                "registerShares": {
                    name: round_to(register[name] / register_total) for name in register
                },
                "vibrato": {
                    "rateHz": round_to(median_of(rates), 2),
                    "extentCents": round_to(median_of(extents), 1),
                    "timeShare": round_to(median_of(time_shares)),
                },
                "sustainSeconds": round_to(median_of(sustains), 2),
                "cpps": round_to(median_of(cpps_values), 2),
                "hnr": round_to(median_of(hnr_values), 2),
                "spread": {
                    "rate": iqr(rates),
                    "extent": iqr(extents),
                    "timeShare": iqr(time_shares),
                    "sustain": iqr(sustains),
                    "cpps": iqr(cpps_values),
                    "hnr": iqr(hnr_values),
                },
            }
        )
    return rollups


def _month_index(month: str) -> float:
    return js.to_number(month[:4]) * 12 + js.to_number(month[5:7]) - 1


def _month_at(index: float) -> str:
    year = js.to_string(math.floor(index / 12))
    return f"{year}-{js.to_string(math.fmod(index, 12) + 1).rjust(2, '0')}"


def build_eras(monthly_cadence: Sequence[Json], reliable: Sequence[Any]) -> list[Json]:
    month_min, gap_max_months, total_min = 40, 2, 90
    count_by_index = {_month_index(entry["month"]): entry for entry in monthly_cadence}
    qualifying = sorted(
        _month_index(entry["month"]) for entry in monthly_cadence if entry["count"] >= month_min
    )
    runs: list[list[float]] = []
    for index in qualifying:
        if runs and index - runs[-1][1] <= gap_max_months + 1:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    eras: list[Json] = []
    for start_index, end_index in runs:
        total = 0
        cursor = start_index
        while cursor <= end_index:
            entry = count_by_index.get(cursor)
            total += entry["count"] if entry is not None else 0
            cursor += 1
        start, end = _month_at(start_index), _month_at(end_index)

        def in_era(row: Any, start: str = start, end: str = end) -> bool:
            captured = js.get(row, "capturedAt")
            if not js.truthy(captured):
                return False
            month = js.utf16_key(_text(captured, "capturedAt")[:7])
            return bool(month) and js.utf16_key(start) <= month <= js.utf16_key(end)

        members = sorted(
            (row for row in reliable if in_era(row)),
            key=lambda row: js.locale_compare_key(js.to_string(js.get(row, "capturedAt"))),
        )
        samples: list[Any] = []
        picks = min(3, len(members))
        for pick in range(picks):
            position = int(js_round((pick * (len(members) - 1)) / ((picks - 1) or 1)))
            identifier = js.get(members[position], "recordingId")
            if identifier not in samples:
                samples.append(identifier)
        eras.append(
            {"start": start, "end": end, "totalTakes": total, "sampleRecordingIds": samples}
        )
    return [era for era in eras if era["totalTakes"] >= total_min]


def cluster_threads(lyrics_manifest: Any, limit: int = 8) -> list[Json]:
    clusters = js.get(lyrics_manifest, "clusters")
    if not js.truthy(lyrics_manifest) or not isinstance(clusters, list):
        return []
    captured_by_id = {
        js.get(row, "recordingId"): js.coalesce(js.get(row, "capturedAt"), None)
        for row in js.coalesce(js.get(lyrics_manifest, "results"), [])
    }
    threads = []
    for cluster in clusters:
        recording_ids = js.coalesce(js.get(cluster, "recordingIds"), [])
        dates = sorted(
            (
                date
                for date in (
                    captured_by_id.get(identifier, MISSING) for identifier in recording_ids
                )
                if js.truthy(date)
            ),
            key=_default_sort_key,
        )
        per_year: dict[str, int] = {}
        for date in dates:
            year = _text(date, "date")[:4]
            per_year[year] = per_year.get(year, 0) + 1
        cluster_id = js.get(cluster, "clusterId")
        threads.append(
            {
                "id": cluster_id,
                "label": js.coalesce(js.get(cluster, "label"), cluster_id),
                "size": js.coalesce(js.length(js.get(cluster, "recordingIds")), 0),
                "first": _text(dates[0], "date")[:7] if dates else None,
                "last": _text(dates[-1], "date")[:7] if dates else None,
                "perYear": js_object(per_year.items()),
            }
        )
    kept = [thread for thread in threads if thread["size"] >= 2 and js.truthy(thread["first"])]
    kept.sort(
        key=cmp_to_key(
            lambda left, right: (
                _sign(right["size"] - left["size"])
                or js.locale_compare(_text(left["id"], "id"), _text(right["id"], "id"))
            )
        )
    )
    return kept[:limit]


def build_voice_trends(
    features_manifest: Any, rows: Sequence[Any] = (), *, lyrics_manifest: Any = None
) -> Json:
    """``buildVoiceTrends``: quarterly summaries, the working-top headline, eras and
    lyric-cluster threads from the repo-safe features manifest (aggregates only)."""

    cadence_by_month: dict[str, int] = {}
    for row in rows:
        captured = js.get(row, "capturedAt")
        month = _text(captured, "capturedAt")[:7] if js.truthy(captured) else None
        if month:
            cadence_by_month[month] = cadence_by_month.get(month, 0) + 1
    monthly_cadence = [
        {"month": month, "count": cadence_by_month[month]}
        for month in sorted(cadence_by_month, key=js.utf16_key)
    ]
    results = js.get(features_manifest, "results")
    if not js.truthy(features_manifest) or not isinstance(results, list):
        return {
            "available": False,
            "reason": "features_manifest_not_loaded",
            "launchHint": "relaunch with --features manifests/local-voice-features.json",
            "monthlyCadence": monthly_cadence,
        }
    completed = [
        row
        for row in results
        if js.get(row, "status") == "completed" and js.truthy(js.get(row, "features"))
    ]
    reliable = [
        row for row in completed if total_rejection_share_for(row) <= RELIABLE_REJECTION_MAX
    ]
    completed_by_quarter: dict[str, int] = {}
    for row in completed:
        quarter = quarter_for(js.get(row, "capturedAt"))
        if quarter is not None:
            completed_by_quarter[quarter] = completed_by_quarter.get(quarter, 0) + 1

    by_year: dict[Any, list[Any]] = {}
    for row in reliable:
        year = js.coalesce(js.get(row, "year"), year_for(js.get(row, "capturedAt")))
        by_year.setdefault(year, []).append(row)
    yearly_tops = []
    for year in sorted(by_year, key=_default_sort_key):
        group = by_year[year]
        if len(group) < 10:
            continue
        top = round_to(
            quantile_of(
                (js.coalesce(_path(_features(row), "pitch", "f0Hz", "p95"), None) for row in group),
                0.95,
            ),
            1,
        )
        if top is not None:
            yearly_tops.append({"year": year, "n": len(group), "topHz": top})
    working_top = None
    if len(yearly_tops) >= 2:
        baseline = yearly_tops[0]
        peak = baseline
        for candidate in yearly_tops:
            if candidate["topHz"] > peak["topHz"]:
                peak = candidate
        working_top = {
            "baselineYear": baseline["year"],
            "baselineTopHz": baseline["topHz"],
            "peakYear": peak["year"],
            "peakTopHz": peak["topHz"],
            "gainSemitones": round_to(12 * js.log2(_div(peak["topHz"], baseline["topHz"])), 1),
        }

    vibrato_takes = []
    for row in completed:
        vibrato = _path(_features(row), "vibrato")
        captured = js.get(row, "capturedAt")
        if (
            js.nullish(js.get(vibrato, "meanRateHz"))
            or js.nullish(js.get(vibrato, "meanExtentCents"))
            or not js.truthy(captured)
        ):
            continue
        vibrato_takes.append(
            {
                "id": js.get(row, "recordingId"),
                "date": _text(captured, "capturedAt")[:10],
                "year": js.coalesce(js.get(row, "year"), year_for(captured)),
                "rate": js.get(vibrato, "meanRateHz"),
                "extent": js.get(vibrato, "meanExtentCents"),
                "reliable": total_rejection_share_for(row) <= RELIABLE_REJECTION_MAX,
            }
        )

    hours = 0.0
    for row in completed:
        hours = hours + js.coalesce(_path(_features(row), "durationSeconds"), 0)
    return {
        "available": True,
        "schemaVersion": TRENDS_SCHEMA_VERSION,
        "source": {
            "features": {
                "schemaVersion": js.coalesce(js.get(features_manifest, "schemaVersion"), None),
                "generatedAt": js.coalesce(js.get(features_manifest, "generatedAt"), None),
                "updatedAt": js.coalesce(js.get(features_manifest, "updatedAt"), None),
                "recordingCount": len(results),
            },
            "reliability": {
                "rule": f"total f0 frame rejection <= {js.to_string(RELIABLE_REJECTION_MAX)}",
                "reliableCount": len(reliable),
                "flaggedCount": len(completed) - len(reliable),
            },
        },
        "headline": {
            "takesAnalyzed": len(completed),
            "reliableTakes": len(reliable),
            "flaggedTakes": len(completed) - len(reliable),
            "hoursAnalyzed": round_to(hours / 3600, 1),
            "workingTop": working_top,
        },
        "quarters": _quarter_rollups(reliable, completed_by_quarter),
        "quartersAllTakes": _quarter_rollups(completed, completed_by_quarter),
        "yearlyTops": yearly_tops,
        "monthlyCadence": monthly_cadence,
        "eras": build_eras(monthly_cadence, reliable),
        "vibratoTakes": vibrato_takes,
        "clusters": cluster_threads(lyrics_manifest),
        "readScope": {
            "committedManifestsOnly": True,
            "summaryAggregatesOnly": True,
            "featureArrays": False,
            "audioBytes": False,
            "transcriptText": False,
            "upload": False,
            "corpusMutation": False,
        },
    }


# ---- the referee ----------------------------------------------------------------------


def _int32(value: int) -> int:
    return ((value & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


def _imul(left: int, right: int) -> int:
    return _int32(_int32(left) * _int32(right))


def mulberry32(seed: int) -> Callable[[], float]:
    """voice-journey's ``mulberry32``, bit for bit (``Math.imul``, ``>>>``, ``^``)."""

    state = seed & 0xFFFFFFFF

    def random() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = _imul(t ^ (t >> 15), _int32(t | 1))
        t = _int32(t ^ _int32(t + _imul(t ^ ((t & 0xFFFFFFFF) >> 7), _int32(t | 61))))
        return ((t ^ ((t & 0xFFFFFFFF) >> 14)) & 0xFFFFFFFF) / 4294967296

    return random


def seeded_shuffle[T](items: list[T], random: Callable[[], float]) -> list[T]:
    for i in range(len(items) - 1, 0, -1):
        j = math.floor(random() * (i + 1))
        items[i], items[j] = items[j], items[i]
    return items


def loudness_match_gains_db(a_rms_db: Any, b_rms_db: Any) -> Json:
    """Symmetric playback gains that bring both sides of a blind pair to their mutual
    mean RMS, capped at ±12 dB; 0 when either level is unknown."""

    if not js.is_finite(a_rms_db) or not js.is_finite(b_rms_db):
        return {"aGainDb": 0, "bGainDb": 0}
    mean = (a_rms_db + b_rms_db) / 2

    def clamp(value: float) -> float:
        return max(-LOUDNESS_MATCH_CAP_DB, min(LOUDNESS_MATCH_CAP_DB, value))

    return {
        "aGainDb": to_fixed(clamp(mean - a_rms_db), 1),
        "bGainDb": to_fixed(clamp(mean - b_rms_db), 1),
    }


def build_referee_pairs(
    lyrics_manifest: Any,
    features_manifest: Any,
    *,
    seed: int = DEFAULT_REFEREE_SEED,
    pairs_per_cluster: int = REFEREE_PAIRS_PER_CLUSTER,
) -> list[Json]:
    """``buildRefereePairs``: cross-year pairs of reliable takes of the same lyric
    cluster, seeded, at most ``pairs_per_cluster`` per cluster, interleaved."""

    reliable: dict[Any, Json] = {}
    for row in js.coalesce(js.get(features_manifest, "results"), []):
        captured = js.get(row, "capturedAt")
        if (
            js.get(row, "status") != "completed"
            or not js.truthy(js.get(row, "features"))
            or not js.truthy(captured)
        ):
            continue
        if total_rejection_share_for(row) > RELIABLE_REJECTION_MAX:
            continue
        reliable[js.get(row, "recordingId")] = {
            "year": js.coalesce(js.get(row, "year"), _text(captured, "capturedAt")[:4]),
            "capturedAt": captured,
            "rmsDb": js.coalesce(_path(_features(row), "dynamics", "voicedRmsDbP50"), None),
        }
    random = mulberry32(seed)
    per_cluster: list[list[Json]] = []
    for cluster in js.coalesce(js.get(lyrics_manifest, "clusters"), []):
        members = [
            identifier
            for identifier in js.coalesce(js.get(cluster, "recordingIds"), [])
            if identifier in reliable
        ]
        candidates = [
            (members[i], members[j])
            for i in range(len(members))
            for j in range(i + 1, len(members))
            if reliable[members[i]]["year"] != reliable[members[j]]["year"]
        ]
        seeded_shuffle(candidates, random)
        cluster_id = js.get(cluster, "clusterId")
        pairs = []
        for left, right in candidates[:pairs_per_cluster]:
            flip = random() < 0.5
            a_id, b_id = (right, left) if flip else (left, right)
            identity = "+".join(sorted((left, right), key=_default_sort_key))
            digest = hashlib.sha256(js.utf8(f"{js.to_string(cluster_id)}:{identity}"))
            a, b = reliable[a_id], reliable[b_id]
            pairs.append(
                {
                    "pairId": digest.hexdigest()[:16],
                    "aId": a_id,
                    "bId": b_id,
                    "clusterId": cluster_id,
                    "clusterLabel": js.coalesce(js.get(cluster, "label"), cluster_id),
                    "aYear": a["year"],
                    "bYear": b["year"],
                    "aCapturedAt": a["capturedAt"],
                    "bCapturedAt": b["capturedAt"],
                    **loudness_match_gains_db(a["rmsDb"], b["rmsDb"]),
                }
            )
        if pairs:
            per_cluster.append(pairs)
    seeded_shuffle(per_cluster, random)
    pool: list[Json] = []
    index = 0
    while True:
        added = False
        for pairs in per_cluster:
            if index < len(pairs):
                pool.append(pairs[index])
                added = True
        if not added:
            return pool
        index += 1


def bradley_terry_by_year(judgments: Sequence[Any]) -> Json:
    """Bradley-Terry across years (the Zermelo MM iteration, 200 rounds). Ties count as
    half a win each way, every compared year pair gets +0.25 pseudo-wins each way, and
    strengths are log2 relative to the earliest year."""

    by_choice = dict.fromkeys(REFEREE_CHOICES, 0)
    wins: dict[tuple[Any, Any], float] = {}
    per_year: dict[Any, Json] = {}
    flags: dict[Any, int] = {}

    def add_win(winner: Any, loser: Any, amount: float) -> None:
        key = (js.to_string(winner), js.to_string(loser))
        wins[key] = wins.get(key, 0) + amount

    def year_row(year: Any) -> Json:
        if year not in per_year:
            per_year[year] = {"year": year, "wins": 0, "losses": 0, "ties": 0, "trials": 0}
        return per_year[year]

    for judgment in judgments:
        choice = js.get(judgment, "choice")
        if isinstance(choice, str) and choice in by_choice:
            by_choice[choice] += 1
        if choice == "not_same_song":
            cluster = js.get(judgment, "clusterId")
            flags[cluster] = flags.get(cluster, 0) + 1
            continue
        if choice == "skip":
            continue
        a_year, b_year = js.get(judgment, "aYear"), js.get(judgment, "bYear")
        if not js.truthy(a_year) or not js.truthy(b_year) or a_year == b_year:
            continue
        if choice == "a":
            add_win(a_year, b_year, 1)
            year_row(a_year)["wins"] += 1
            year_row(b_year)["losses"] += 1
        elif choice == "b":
            add_win(b_year, a_year, 1)
            year_row(b_year)["wins"] += 1
            year_row(a_year)["losses"] += 1
        elif choice == "too_close":
            add_win(a_year, b_year, 0.5)
            add_win(b_year, a_year, 0.5)
            year_row(a_year)["ties"] += 1
            year_row(b_year)["ties"] += 1
        year_row(a_year)["trials"] += 1
        year_row(b_year)["trials"] += 1

    years = sorted(per_year, key=_default_sort_key)
    names = {year: js.to_string(year) for year in years}
    if len(years) >= 2:
        for i, first in enumerate(years):
            for second in years[i + 1 :]:
                forward = wins.get((names[first], names[second]), 0)
                backward = wins.get((names[second], names[first]), 0)
                if forward + backward > 0:
                    add_win(first, second, 0.25)
                    add_win(second, first, 0.25)
        strengths = dict.fromkeys(years, 1.0)
        for _ in range(200):
            updated: dict[Any, float] = {}
            for year in years:
                total_wins = 0.0
                denominator = 0.0
                for other in years:
                    if other == year:
                        continue
                    won = wins.get((names[year], names[other]), 0)
                    lost = wins.get((names[other], names[year]), 0)
                    pair_total = won + lost
                    if pair_total == 0:
                        continue
                    total_wins += won
                    denominator += pair_total / (strengths[year] + strengths[other])
                updated[year] = (
                    max(total_wins, 1e-6) / denominator if denominator > 0 else strengths[year]
                )
            mean_log = 0.0
            for value in updated.values():
                mean_log = mean_log + math.log(value)
            mean_log = mean_log / len(updated)
            for year in years:
                strengths[year] = updated[year] / math.exp(mean_log)
        baseline = js.log2(strengths[years[0]])
        for year in years:
            per_year[year]["strengthLog2"] = round_to(js.log2(strengths[year]) - baseline, 3)
    else:
        for year in years:
            per_year[year]["strengthLog2"] = 0
    return {
        "method": {
            "id": "bradley_terry_by_year.v1",
            "ties": "counted as half a win each way",
            "regularization": "+0.25 pseudo-win each way per compared year pair",
            "strengthScale": "log2 relative to the earliest judged year",
        },
        "byChoice": by_choice,
        "judgedTotal": len(judgments),
        "years": [per_year[year] for year in years],
        "notSameSongFlags": [
            {"clusterId": cluster, "count": count}
            for cluster, count in sorted(flags.items(), key=lambda item: _flag_key(item))
        ],
    }


def _flag_key(item: tuple[Any, int]) -> bytes:
    """``[...map.entries()].sort()``: each ``[k, v]`` compares as ``String([k, v])``."""

    return _default_sort_key([item[0], item[1]])


# ---- payloads the routes serve (the JS shapes) ----------------------------------------


def playback_summary(enabled: bool) -> Json:
    return {
        "enabled": enabled,
        "releaseGateRequired": not enabled,
        "readScope": {
            "operatorClickedRecordingsOnly": True,
            "bulkRead": False,
            "retainedAudioBytes": False,
            "audibleDerivedArtifacts": False,
            "upload": False,
            "corpusMutation": False,
        },
    }


def transcript_text_summary(enabled: bool) -> Json:
    return {
        "enabled": enabled,
        "releaseGateRequired": False,
        "readScope": {
            "localTranscriptText": enabled,
            "explicitRecordingRequestsOnly": True,
            "bulkRead": False,
            "transcriptTextInRowJson": False,
            "transcriptTextCommittedToRepo": False,
            "audioBytes": False,
            "upload": False,
            "corpusMutation": False,
        },
    }


def feature_detail_summary(enabled: bool) -> Json:
    return {
        "enabled": enabled,
        "releaseGateRequired": False,
        "readScope": {
            "localFeatureArrays": enabled,
            "explicitRecordingRequestsOnly": True,
            "bulkRead": False,
            "featureArraysCommittedToRepo": False,
            "audioBytes": False,
            "transcriptText": False,
            "upload": False,
            "corpusMutation": False,
        },
    }


TRENDS_UNAVAILABLE: Final = {"available": False, "reason": "trends_not_computed"}
JOURNEYS_UNAVAILABLE: Final = {
    "available": False,
    "reason": "song_journeys_manifest_not_loaded",
    "launchHint": (
        "run npm run journeys -- analyze on the host, or relaunch with "
        "--journeys manifests/song-journeys.json"
    ),
}


# ---- configuration and the serve state ------------------------------------------------


@dataclass(frozen=True)
class JourneyConfig:
    """Where the corpus browser reads and writes, and its gates.

    The manifests are read only. Every write (verdicts, goals, referee judgments) goes
    to ``state_dir``. Playback needs ``playback_approval`` (design §4.2: the approval
    carries over as configuration the API refuses to run without); transcript text and
    feature detail need their directories."""

    index: Path
    results: Path
    transcripts: Path
    lyrics: Path
    features: Path
    journeys: Path
    state_dir: Path
    corpus_root: Path | None = None
    measurement_dir: Path | None = None
    playback_approval: str | None = None
    transcript_dir: Path | None = None
    feature_dir: Path | None = None
    queue_size: int = DEFAULT_QUEUE_SIZE
    referee_seed: int = DEFAULT_REFEREE_SEED
    referee_pairs_per_cluster: int = REFEREE_PAIRS_PER_CLUSTER
    allow_empty_manifests: bool = False

    @classmethod
    def from_paths(
        cls,
        paths: JourneyPaths | None = None,
        *,
        playback_approval: str | None = None,
        transcript_dir: Path | None = None,
        feature_dir: Path | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        referee_seed: int = DEFAULT_REFEREE_SEED,
    ) -> JourneyConfig:
        """The JS's defaults and its ``applyEnvDefaults``: with a data root the
        transcript and feature directories default to their places under it and a
        missing index or results manifest is an empty corpus (a fresh data volume)."""

        resolved = resolve_paths() if paths is None else paths
        rooted = resolved.data_root is not None
        return cls(
            index=resolved.manifest(INDEX_MANIFEST),
            results=resolved.manifest(RESULTS_MANIFEST),
            transcripts=resolved.manifest(TRANSCRIPTS_MANIFEST),
            lyrics=resolved.manifest(LYRICS_MANIFEST),
            features=resolved.manifest(FEATURES_MANIFEST),
            journeys=resolved.manifest(JOURNEYS_MANIFEST),
            state_dir=resolved.state_dir,
            corpus_root=resolved.phone_corpus_root,
            measurement_dir=resolved.measurement_dir,
            playback_approval=playback_approval or None,
            transcript_dir=transcript_dir
            or (resolved.artifact("stt-transcripts") if rooted else None),
            feature_dir=feature_dir or (resolved.artifact("voice-features") if rooted else None),
            queue_size=queue_size,
            referee_seed=referee_seed,
            allow_empty_manifests=rooted,
        )

    @property
    def state(self) -> Path:
        return self.state_dir / STATE_NAME

    @property
    def referee_state(self) -> Path:
        return self.state_dir / REFEREE_STATE_NAME

    @property
    def goals_state(self) -> Path:
        return self.state_dir / GOALS_STATE_NAME

    @property
    def watch_state(self) -> Path:
        return self.state_dir / WATCH_STATE_NAME

    @property
    def playback_enabled(self) -> bool:
        return self.corpus_root is not None and bool(self.playback_approval)

    @property
    def inputs(self) -> tuple[Path, ...]:
        return (
            self.index,
            self.results,
            self.transcripts,
            self.lyrics,
            self.features,
            self.journeys,
        )

    def describe(self) -> Json:
        """The startup summary the JS printed (stores, gates), without the endpoints."""

        return {
            "playback": playback_summary(self.playback_enabled),
            "transcriptText": transcript_text_summary(self.transcript_dir is not None),
            "featureDetail": feature_detail_summary(self.feature_dir is not None),
            "statePath": str(self.state),
            "referee": {"statePath": str(self.referee_state)},
            "goals": {"statePath": str(self.goals_state)},
            "watch": {"statePath": str(self.watch_state)},
        }


@dataclass
class ServeState:
    """Everything the routes serve, from one load of the manifests."""

    rows_payload: Json
    rows_by_id: dict[Any, Json]
    voice_trends: Json | None = None
    song_journeys: Json | None = None
    referee_pool: list[Json] = field(default_factory=list)
    features_manifest: Any = None


def build_serve_state(
    rows_payload: Json,
    *,
    voice_trends: Json | None = None,
    song_journeys: Json | None = None,
    referee_pool: list[Json] | None = None,
    features_manifest: Any = None,
) -> ServeState:
    return ServeState(
        rows_payload=rows_payload,
        rows_by_id={row["recordingId"]: row for row in rows_payload["rows"]},
        voice_trends=voice_trends,
        song_journeys=song_journeys,
        referee_pool=referee_pool or [],
        features_manifest=features_manifest,
    )


def _read_manifest(path: Path, empty: Json, allow_empty: bool) -> Any:
    if not allow_empty:
        return read_json(path)
    found = read_optional_json(path)
    return empty if found is None else found


def load_rows_payload(config: JourneyConfig) -> Json:
    return build_corpus_rows(
        _read_manifest(config.index, {"recordings": []}, config.allow_empty_manifests),
        _read_manifest(config.results, {"results": []}, config.allow_empty_manifests),
        state=read_state(config.state),
        queue_size=config.queue_size,
        transcripts_manifest=read_optional_json(config.transcripts),
        lyrics_manifest=read_optional_json(config.lyrics),
    )


def load_serve_state(config: JourneyConfig) -> ServeState:
    """``loadServeState``: the rows, trends, journeys and referee pool, freshly read."""

    rows_payload = load_rows_payload(config)
    features_manifest = read_optional_json(config.features)
    lyrics_manifest = read_optional_json(config.lyrics)
    voice_trends = build_voice_trends(
        features_manifest, rows_payload["rows"], lyrics_manifest=lyrics_manifest
    )
    journeys_manifest = read_optional_json(config.journeys)
    song_journeys = (
        {"available": True, **journeys_manifest} if isinstance(journeys_manifest, Mapping) else None
    )
    referee_pool = build_referee_pairs(
        lyrics_manifest,
        features_manifest,
        seed=config.referee_seed,
        pairs_per_cluster=config.referee_pairs_per_cluster,
    )
    return build_serve_state(
        rows_payload,
        voice_trends=voice_trends,
        song_journeys=song_journeys,
        referee_pool=referee_pool,
        features_manifest=features_manifest,
    )


type _Stamp = tuple[tuple[int, int, int] | None, ...]


def _stamp(paths: Iterable[Path]) -> _Stamp:
    stamps: list[tuple[int, int, int] | None] = []
    for path in paths:
        try:
            metadata = os.stat(path)
        except FileNotFoundError:
            stamps.append(None)
        else:
            stamps.append((metadata.st_mtime_ns, metadata.st_size, metadata.st_ino))
    return tuple(stamps)


class JourneyStore:
    """The loaded manifests, the state files, and the writes, safe across threads.

    :meth:`current` re-reads the manifests when one of them changed since the last load
    (the job runner replaces them atomically). If a reload fails the previous state
    keeps serving and the error is written to stderr. Writes are serialized by one lock
    and go to the state directory only."""

    def __init__(self, config: JourneyConfig, *, log: Callable[[str], None] | None = None) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._log = log or (lambda message: sys.stderr.write(f"{message}\n"))
        self._stamp = _stamp(config.inputs)
        self._state = load_serve_state(config)

    def current(self) -> ServeState:
        stamp = _stamp(self.config.inputs)
        if stamp != self._stamp:
            with self._lock:
                if stamp != self._stamp:
                    try:
                        self._state = load_serve_state(self.config)
                    except (OSError, ValueError, TypeError) as error:
                        self._log(f"journey reload failed: {type(error).__name__}: {error}")
                    else:
                        self._log("journey serve state reloaded")
                    self._stamp = stamp
        return self._state

    # -- verdicts -----------------------------------------------------------------------

    def record_verdict(self, verdict: Json) -> Json:
        """Upsert a verdict into the local state file and the served rows; the new
        totals."""

        with self._lock:
            served = self.current()
            state = upsert_verdict(read_state(self.config.state), verdict)
            write_json(self.config.state, state, announce=False)
            rows = served.rows_payload["rows"]
            for position, row in enumerate(rows):
                if row["recordingId"] == verdict["recordingId"]:
                    updated = apply_verdict_to_row(row, verdict)
                    rows[position] = updated
                    served.rows_by_id[row["recordingId"]] = updated
                    break
            served.rows_payload["source"]["state"]["verdictCount"] = len(state["verdicts"])
            totals = summarize_rows(rows)
            served.rows_payload["totals"] = totals
            return totals

    # -- goals --------------------------------------------------------------------------

    def goals(self) -> Json:
        served = self.current()
        state = read_goals_state(self.config.goals_state)
        return goals_payload(
            state, goal_context(served.rows_payload["rows"], served.features_manifest)
        )

    def create_goal(self, body: Any) -> Json:
        """Validate (``goals.validate_goal_spec``; raises ``GoalError``), store, return."""

        spec = validate_goal_spec(body)
        with self._lock:
            state = read_goals_state(self.config.goals_state)
            goal = create_goal(spec, now=js.now_ms())
            state["goals"] = [*state["goals"], goal]
            write_json(self.config.goals_state, state, announce=False)
            return goal

    def abandon_goal(self, goal_id: str) -> Json | None:
        """Mark a goal abandoned; ``None`` when no goal has that id."""

        with self._lock:
            state = read_goals_state(self.config.goals_state)
            goal = next(
                (item for item in state["goals"] if js.get(item, "goalId") == goal_id), None
            )
            if goal is None:
                return None
            goal["status"] = "abandoned"
            goal["abandonedAt"] = iso_timestamp()
            write_json(self.config.goals_state, state, announce=False)
            return dict(goal)

    # -- the referee --------------------------------------------------------------------

    def referee_next(self) -> tuple[int, int, Json | None]:
        """``(judged, pool size, next unjudged pair or None)``."""

        pool = self.current().referee_pool
        judged = {
            js.get(judgment, "pairId")
            for judgment in read_referee_state(self.config.referee_state)["judgments"]
        }
        pair = next((item for item in pool if item["pairId"] not in judged), None)
        return len(judged), len(pool), pair

    def find_pair(self, pair_id: str) -> Json | None:
        return next(
            (item for item in self.current().referee_pool if item["pairId"] == pair_id), None
        )

    def record_judgment(self, pair: Json, choice: str, judged_at: Any) -> tuple[Json, int]:
        """Store a blind judgment; ``(the stored judgment, judgments in the state)``."""

        judgment = {
            "pairId": pair["pairId"],
            "aId": pair["aId"],
            "bId": pair["bId"],
            "clusterId": pair["clusterId"],
            "aYear": pair["aYear"],
            "bYear": pair["bYear"],
            "choice": choice,
            "judgedAt": js.to_string(judged_at) if js.truthy(judged_at) else iso_timestamp(),
            "source": "referee",
        }
        with self._lock:
            state = upsert_referee_judgment(read_referee_state(self.config.referee_state), judgment)
            write_json(self.config.referee_state, state, announce=False)
        return judgment, len(state["judgments"])

    def referee_fit(self) -> Json:
        return bradley_terry_by_year(read_referee_state(self.config.referee_state)["judgments"])

    def watch_status(self) -> Json:
        return read_watch_status(self.config.watch_state)


def coach_payload(served: ServeState) -> Json:
    return build_coach_payload(
        rows=served.rows_payload["rows"],
        features_manifest=served.features_manifest,
        journeys_manifest=served.song_journeys,
        voice_trends=served.voice_trends,
    )


def summary_payload(served: ServeState, config: JourneyConfig) -> Json:
    payload = served.rows_payload
    return {
        "schemaVersion": payload["schemaVersion"],
        "source": payload["source"],
        "playback": playback_summary(config.playback_enabled),
        "transcriptText": transcript_text_summary(config.transcript_dir is not None),
        "featureDetail": feature_detail_summary(config.feature_dir is not None),
        "reviewQueue": payload["reviewQueue"],
        "totals": payload["totals"],
        "filterOptions": filter_options_for(payload["rows"]),
    }


def review_queue_rows(served: ServeState) -> list[Json]:
    selected = [row for row in served.rows_payload["rows"] if row["reviewQueueSelected"]]
    return sort_rows(selected, "reviewQueueOrder", "asc")


# ---- local files: transcript text, feature detail, audio ------------------------------


def _inside(root: Path, name: str) -> Path | None:
    """``path.resolve(root, name)`` when it stays inside ``root`` (the JS's check)."""

    resolved_root = js.path_resolve(root)
    target = js.path_resolve(name if name.startswith("/") else js.path_join(resolved_root, name))
    relative = os.path.relpath(target, resolved_root)
    if relative.startswith("..") or os.path.isabs(relative):
        return None
    return Path(target)


def transcript_path(transcript_dir: Path, recording: str) -> Path | None:
    return _inside(transcript_dir, f"{recording}.txt")


def feature_detail_path(feature_dir: Path, recording: str) -> Path | None:
    return _inside(feature_dir, f"{recording}.json")


class CorpusAccessDenied(Exception):
    """The operating system refused the corpus root (an operator-fixable state)."""


class SeamMismatch(Exception):
    """The row's file name does not derive the row's id: the index cannot be trusted."""


@dataclass
class AudioHandle:
    """An open, read-only recording: the caller streams it and closes it."""

    handle: IO[bytes]
    size: int
    content_type: str

    def close(self) -> None:
        self.handle.close()


def _audio_location(
    row: Mapping[str, Any], corpus_root: Path | None, measurement_dir: Path | None
) -> tuple[Path, str] | None:
    filename = row.get("filename")
    if not isinstance(filename, str) or not filename:
        return None
    parts = filename.split("/")
    extension = os.path.splitext(filename)[1].lower()
    if any(not part or part.startswith(".") for part in parts):
        return None
    if len(parts) == 1 and extension in ALLOWED_EXTENSIONS and corpus_root is not None:
        if recording_id(filename) != row.get("recordingId"):
            raise SeamMismatch(filename)
        return corpus_root / filename, _AUDIO_TYPES[extension]
    if len(parts) == 2 and extension in _MEASUREMENT_AUDIO and measurement_dir is not None:
        if recording_id(f"measurement/{filename}") != row.get("recordingId"):
            raise SeamMismatch(filename)
        return measurement_dir / parts[0] / parts[1], _AUDIO_TYPES[extension]
    return None


def open_recording_audio(
    row: Mapping[str, Any],
    *,
    corpus_root: Path | None,
    measurement_dir: Path | None,
    approval: str | None,
) -> AudioHandle:
    """The read-only seam: open the file an index row names, for playback.

    A phone recording is ``<corpus_root>/<name>.m4a`` (one path component, not hidden);
    a measurement segment is ``<measurement_dir>/<session>/<name>.wav``. The row's id
    must be the id ``host-access`` derives from that name (``sha256(name)[:16]``, with
    ``measurement/`` before a session path), so the index cannot point elsewhere. The
    file is opened without following a symlink and must be a regular file.

    Raises: ``ValueError`` when no approval is recorded; ``CorpusAccessDenied``
    when the operating system refuses the read; ``SeamMismatch``; ``FileNotFoundError``
    for a file that is not there or not a recording.
    """

    if not approval:
        raise ValueError("playback requires a recorded approval")
    location = _audio_location(row, corpus_root, measurement_dir)
    if location is None:
        raise FileNotFoundError("the row names no readable recording")
    path, content_type = location
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except PermissionError as error:
        raise CorpusAccessDenied(str(error)) from error
    except OSError as error:
        raise FileNotFoundError(str(path)) from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise FileNotFoundError(str(path))
    return AudioHandle(os.fdopen(descriptor, "rb"), metadata.st_size, content_type)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``parseRange``: ``bytes=S-E`` within the file; an empty start is 0 and an empty
    end is the last byte (so ``bytes=-N`` is ``0-N``, as the JS read it); anything else
    is the whole file."""

    if not header or not header.startswith("bytes="):
        return None
    start_text, dash, end_text = header.removeprefix("bytes=").partition("-")
    if not dash or not all(text.isdigit() or text == "" for text in (start_text, end_text)):
        return None
    if not (start_text + end_text).isascii():
        return None
    start = int(start_text) if start_text else 0
    end = int(end_text) if end_text else size - 1
    if start < 0 or end < start or end >= size:
        return None
    return start, end


# ---- the command line -----------------------------------------------------------------

HELP = """Voice Journey corpus browser (data layer).

Usage:
  python -m cole_voice_lab.journey.browser merge-verdicts [--results PATH] [--state PATH] \
[--out PATH]

merge-verdicts writes only repo-safe humanSpotCheck metadata from the local verdict
state into the results manifest (or --out). The browser itself is served by
python -m cole_voice_lab.api serve --journey.
"""


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else None
    if not value or value.startswith("--"):
        raise BrowserError(2, f"{flag} requires a value")
    return value


def _parse_args(argv: Sequence[str]) -> Json:
    args = list(argv)
    command = "serve"
    if args and not args[0].startswith("--") and args[0] != "help":
        command = args.pop(0)
    paths = resolve_paths()
    options: Json = {
        "command": command,
        "results": str(paths.manifest(RESULTS_MANIFEST)),
        "state": str(paths.state_dir / STATE_NAME),
        "out": None,
        "help": False,
    }
    while args:
        flag = args.pop(0)
        if flag in {"--results", "--state", "--out"}:
            options[flag[2:]] = _require_value(args, flag)
        elif flag in {"--help", "help"}:
            options["help"] = True
        else:
            raise BrowserError(2, f"unsupported argument: {flag}")
    return options


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = _parse_args(sys.argv[1:] if argv is None else argv)
        if options["help"] or options["command"] == "help":
            sys.stdout.write(HELP)
            return 0
        if options["command"] != "merge-verdicts":
            raise BrowserError(
                2,
                f"unsupported command: {options['command']} "
                "(serve is python -m cole_voice_lab.api serve --journey)",
            )
        results = read_json(options["results"])
        merged = merge_verdicts_into_results(results, read_state(options["state"]))
        out = options["out"] or options["results"]
        write_json(out, merged, announce=False)
        summary = {"mergedCount": merged["humanSpotCheckMerge"]["mergedCount"], "out": out}
        sys.stdout.write(stringify(summary) + "\n")
        return 0
    except (BrowserError, OSError, ValueError, TypeError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
