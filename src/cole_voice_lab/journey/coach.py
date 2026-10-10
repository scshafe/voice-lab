"""The coaching layer (port of voice-journey's ``coach.mjs``).

Turns the repo-safe manifests into practice recommendations:

- due for a take: spaced re-recording for the same-song engine, ranked by how much
  evidence the next take buys (staleness times history, span extension, eligibility
  proximity);
- the frontier: songs one measured step from a threshold, each flag carrying the
  numbers that say so.

Pure functions over aggregates only: no audio, no transcript text, and every
suggestion states its reasons as data ("why" chips). Evidence-thin findings are framed
as "add data", never as verdict-chasing.

Same payloads as the JavaScript: times are epoch milliseconds, ``Date.parse`` and
``Math.log2`` follow V8 (``jscompat``), ``toFixed`` and ``Math.round`` follow
JavaScript. ``now`` defaults to the current time in milliseconds.
"""

from __future__ import annotations

from typing import Any

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey.jsonio import format_number, js_round, to_fixed

Json = dict[str, Any]

MS_PER_DAY = 86_400_000
DAYS_PER_YEAR = 365.25

DUE_AFTER_DAYS = 90
MIN_RECURRING_TAKES = 3
ELIGIBLE_CLEAN_TAKES = 6
ELIGIBLE_SPAN_YEARS = 1.5
RECENT_TAKES_WINDOW = 10
VIBRATO_SETTLING_BAND = (4.5, 5.2)
SETTLED_ZONE_HZ = 5.5


def _to_time(value: Any) -> float | None:
    return js.date_parse("" if js.nullish(value) else value)


def median(values: list[Any]) -> Any:
    """The lower median of the finite numbers (``null`` when there are none)."""

    ordered = sorted(value for value in values if js.is_finite(value))
    if not ordered:
        return None
    return ordered[(len(ordered) - 1) // 2]


def cluster_rollup(rows: list[Json] | None) -> list[Json]:
    """Rows to a per-cluster rollup (id, privacy-generic label, take counts, first/last)."""

    clusters: dict[Any, Json] = {}
    for row in rows or []:
        cluster_id = js.get(row, "lyricMatchCluster")
        if not js.truthy(cluster_id):
            continue
        cluster = clusters.setdefault(
            cluster_id,
            {
                "clusterId": cluster_id,
                "label": None,
                "takeCount": 0,
                "cleanCount": 0,
                "firstAt": None,
                "lastAt": None,
                "recordingIds": [],
            },
        )
        cluster["takeCount"] += 1
        if js.get(row, "bucket") == "clean_singing":
            cluster["cleanCount"] += 1
        label = cluster["label"]
        if label is None:
            label = js.coalesce(js.get(row, "lyricMatchClusterLabel"), None)
        cluster["label"] = label
        cluster["recordingIds"].append(js.get(row, "recordingId"))
        at = _to_time(js.get(row, "capturedAt"))
        if at is not None:
            if cluster["firstAt"] is None or at < cluster["firstAt"]:
                cluster["firstAt"] = at
            if cluster["lastAt"] is None or at > cluster["lastAt"]:
                cluster["lastAt"] = at
    return list(clusters.values())


def _journeys_by_id(journeys_manifest: Any) -> dict[Any, Any]:
    journeys = js.coalesce(js.get(journeys_manifest, "journeys"), [])
    return {js.get(journey, "clusterId"): journey for journey in journeys}


def build_due_songs(
    *,
    rows: list[Json] | None = None,
    journeys_manifest: Any = None,
    now: float,
    due_after_days: float = DUE_AFTER_DAYS,
    limit: int = 12,
) -> list[Json]:
    journeys_by_id = _journeys_by_id(journeys_manifest)
    due: list[Json] = []
    for cluster in cluster_rollup(rows):
        first_at, last_at = cluster["firstAt"], cluster["lastAt"]
        if cluster["takeCount"] < MIN_RECURRING_TAKES or last_at is None or first_at is None:
            continue
        days_since_last = (now - last_at) / MS_PER_DAY
        if days_since_last < due_after_days:
            continue

        journey = journeys_by_id.get(cluster["clusterId"])
        span_gain_years = days_since_last / DAYS_PER_YEAR
        span_including_today_years = (now - first_at) / MS_PER_DAY / DAYS_PER_YEAR
        clean_short = max(0, ELIGIBLE_CLEAN_TAKES - cluster["cleanCount"])
        near_eligible = (
            journey is None
            and 0 < clean_short <= 2
            and span_including_today_years >= ELIGIBLE_SPAN_YEARS
        )

        # Evidence-gain heuristic: history depth times staleness, plus a boost when a
        # take today either extends a judged song's span or completes eligibility.
        score = (
            js.log2(1 + cluster["takeCount"])
            * js.js_min(0.5 + days_since_last / DAYS_PER_YEAR, 2.5)
            + (js.js_min(span_gain_years, 1.5) if journey is not None else 0)
            + (3 if near_eligible else 0)
        )

        first_year = js.utc_year(first_at)
        last_year = js.utc_year(last_at)
        why = [
            f"last sung {format_number(js_round(days_since_last))} d ago",
            f"{cluster['takeCount']} takes, {first_year}–{last_year}",
        ]
        if journey is not None:
            why.append(
                "in the same-song index "
                f"({js.to_string(js.get(journey, 'takesUsed'))} takes used) — a take today "
                f"extends its span {js.to_fixed_string(span_gain_years, 1)} y"
            )
        if near_eligible:
            plural = "" if clean_short == 1 else "s"
            why.append(f"{clean_short} clean take{plural} short of same-song eligibility")

        due.append(
            {
                "clusterId": cluster["clusterId"],
                "label": cluster["label"] if cluster["label"] is not None else cluster["clusterId"],
                "score": to_fixed(score, 2),
                "daysSinceLast": js_round(days_since_last),
                "takeCount": cluster["takeCount"],
                "firstYear": str(first_year),
                "lastYear": str(last_year),
                "inSameSongIndex": journey is not None,
                "why": why,
            }
        )
    due.sort(key=lambda entry: -entry["score"])
    return due[:limit]


def _completed_feature_rows(features_manifest: Any) -> list[Json]:
    rows = js.coalesce(js.get(features_manifest, "results"), [])
    return [
        row
        for row in rows
        if js.get(row, "status") == "completed" and js.truthy(js.get(row, "features"))
    ]


def _latest_per_year_entry(journey: Any) -> Json | None:
    per_year = js.coalesce(js.get(journey, "perYear"), {})
    years = sorted(per_year.keys(), key=js.utf16_key) if isinstance(per_year, dict) else []
    if not years:
        return None
    year = years[-1]
    entry = per_year[year]
    return {"year": year, **(entry if isinstance(entry, dict) else {})}


def _path(value: Any, *keys: str) -> Any:
    for key in keys:
        value = js.get(value, key)
    return value


def _sustain(row: Json) -> Any:
    return _path(row, "features", "phrasing", "longestSustainedSeconds")


def _best_sustain(rows: list[Json]) -> float:
    best: float = 0
    for row in rows:
        sustain = _sustain(row)
        if js.is_finite(sustain) and sustain > best:
            best = sustain
    return best


def _time_or_zero(row: Json) -> float:
    at = _to_time(js.get(row, "capturedAt"))
    return 0 if at is None else at


def build_frontier(
    *,
    rows: list[Json] | None = None,
    features_manifest: Any = None,
    journeys_manifest: Any = None,
    working_top_hz: Any = None,
    limit: int = 12,
) -> list[Json]:
    feature_rows = _completed_feature_rows(features_manifest)
    cluster_by_recording: dict[Any, Any] = {}
    label_by_cluster: dict[Any, Any] = {}
    for row in rows or []:
        cluster_id = js.get(row, "lyricMatchCluster")
        if not js.truthy(cluster_id):
            continue
        cluster_by_recording[js.get(row, "recordingId")] = cluster_id
        label = js.get(row, "lyricMatchClusterLabel")
        if js.truthy(label) and cluster_id not in label_by_cluster:
            label_by_cluster[cluster_id] = label

    personal_best_sustain = _best_sustain(feature_rows)

    takes_by_cluster: dict[Any, list[Json]] = {}
    for row in feature_rows:
        cluster_id = cluster_by_recording.get(js.get(row, "recordingId"))
        if not js.truthy(cluster_id):
            continue
        takes_by_cluster.setdefault(cluster_id, []).append(row)

    journeys_by_id = _journeys_by_id(journeys_manifest)
    entries: list[Json] = []
    for cluster_id, takes in takes_by_cluster.items():
        if len(takes) < MIN_RECURRING_TAKES:
            continue
        recent = sorted(takes, key=lambda row: -_time_or_zero(row))[:RECENT_TAKES_WINDOW]
        flags: list[Json] = []

        # Range stretch: the song's top notes sit just under the corpus working top.
        if js.is_finite(working_top_hz):
            p95 = median([_path(row, "features", "pitch", "f0Hz", "p95") for row in recent])
            if p95 is not None and p95 > 0:
                semitones_under = 12 * js.log2(working_top_hz / p95)
                if 0.5 <= semitones_under <= 2:
                    flags.append(
                        {
                            "rule": "range-stretch",
                            "detail": f"top notes sit {js.to_fixed_string(semitones_under, 1)} "
                            "st under your working top — a stretch, not a leap",
                        }
                    )

        # Vibrato settling: recent takes hover just below the settled zone.
        vibrato_takes = [
            row
            for row in recent
            if js.is_finite(_path(row, "features", "vibrato", "meanRateHz"))
            and js.to_number(
                js.coalesce(_path(row, "features", "vibrato", "vibratoSegmentCount"), 0)
            )
            > 0
        ]
        if len(vibrato_takes) >= 3:
            low, high = VIBRATO_SETTLING_BAND
            in_band = [
                rate
                for row in vibrato_takes
                if low <= (rate := _path(row, "features", "vibrato", "meanRateHz")) <= high
            ]
            share = len(in_band) / len(vibrato_takes)
            if share >= 0.3:
                rate = median(in_band)
                flags.append(
                    {
                        "rule": "vibrato-settling",
                        "detail": f"{format_number(js_round(share * 100))}% of recent takes at "
                        f"{js.to_fixed_string(rate, 1)} Hz — one nudge from the settled "
                        f"{format_number(SETTLED_ZONE_HZ)} Hz zone",
                    }
                )

        # Sustain personal best: within striking distance of the corpus record.
        best_recent = _best_sustain(recent)
        if (
            personal_best_sustain > 0
            and best_recent >= 0.85 * personal_best_sustain
            and best_recent < personal_best_sustain
        ):
            within = js.js_max(1, js_round((1 - best_recent / personal_best_sustain) * 100))
            flags.append(
                {
                    "rule": "sustain-pb",
                    "detail": f"longest recent sustain {js.to_fixed_string(best_recent, 1)} s — "
                    f"within {format_number(within)}% of your "
                    f"{js.to_fixed_string(personal_best_sustain, 1)} s best",
                }
            )

        journey = journeys_by_id.get(cluster_id)
        if journey is not None:
            # Nearly in tune: latest-year note cores sit just outside the ±25 c gate.
            latest = _latest_per_year_entry(journey)
            error = js.get(latest, "noteCoreCentError")
            if latest is not None and js.is_finite(error) and 25 <= error <= 35:
                flags.append(
                    {
                        "rule": "tuning-near",
                        "detail": f"note cores at {format_number(error)} c in "
                        f"{js.to_string(latest['year'])} — nearly in tune, worth targeted "
                        "drilling",
                    }
                )
            # Evidence-thin clarity: promising slope, not enough takes to trust it.
            cpps_slope = _path(journey, "slopesPerYear", "cpps")
            takes_used = js.get(journey, "takesUsed")
            if js.is_finite(cpps_slope) and cpps_slope > 0.15 and js.to_number(takes_used) < 12:
                flags.append(
                    {
                        "rule": "clarity-thin",
                        "detail": f"clarity trending +{format_number(cpps_slope)}/yr on only "
                        f"{js.to_string(takes_used)} takes — promising but evidence-thin; "
                        "add data",
                    }
                )

        if flags:
            entries.append(
                {
                    "clusterId": cluster_id,
                    "label": label_by_cluster.get(cluster_id, cluster_id),
                    "featureTakes": len(takes),
                    "flags": flags,
                }
            )
    entries.sort(key=lambda entry: (-len(entry["flags"]), -entry["featureTakes"]))
    return entries[:limit]


def build_coach_payload(
    *,
    rows: list[Json] | None = None,
    features_manifest: Any = None,
    journeys_manifest: Any = None,
    voice_trends: Any = None,
    now: float | None = None,
) -> Json:
    moment = js.now_ms() if now is None else now
    working_top = js.coalesce(_path(voice_trends, "headline", "workingTop"), None)
    working_top_hz = js.coalesce(js.get(working_top, "peakTopHz"), None)
    return {
        "generatedAt": js.iso_from_ms(moment),
        "params": {
            "dueAfterDays": DUE_AFTER_DAYS,
            "vibratoSettlingBandHz": list(VIBRATO_SETTLING_BAND),
            "eligibility": {"cleanTakes": ELIGIBLE_CLEAN_TAKES, "spanYears": ELIGIBLE_SPAN_YEARS},
        },
        "workingTop": working_top,
        "dueSongs": build_due_songs(rows=rows, journeys_manifest=journeys_manifest, now=moment),
        "frontier": build_frontier(
            rows=rows,
            features_manifest=features_manifest,
            journeys_manifest=journeys_manifest,
            working_top_hz=working_top_hz,
        ),
        "readScope": {
            "committedManifestsOnly": True,
            "countsAndAggregatesOnly": True,
            "transcriptText": False,
            "audioBytes": False,
        },
    }
