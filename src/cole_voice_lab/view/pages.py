"""The web view's pages, rendered from the journey store (``journey/browser.py``).

Each page reads the store's served state (the repo-safe manifests: recording index,
filter results, transcript and lyric *indicator* manifests, voice features, song
journeys) or the small state files (goals, referee judgments). None reads a
recording, a transcript or lyric text, feature detail or anything under ``raw/``,
``derived/`` or ``memos/``, and none links to a route that serves them (D-025). The
values shown are dates, durations, buckets, counts, aggregates and the generic cluster
labels (``recurring_lyric_cluster_001``).
"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..journey import jscompat as js
from ..journey.browser import (
    JOURNEYS_UNAVAILABLE,
    TRENDS_UNAVAILABLE,
    JourneyConfig,
    JourneyStore,
    filter_rows,
    sort_rows,
)
from ..journey.jsonio import iso_timestamp
from . import report as evidence
from .charts import ColumnValue, LinePoint, RangeBand, column_chart, line_chart, range_chart
from .render import (
    DASH,
    bucket_label,
    card,
    chip,
    date_of,
    duration,
    esc,
    fmt,
    fmt_signed,
    href,
    is_number,
    page,
    table,
    tiles,
)

Json = dict[str, Any]
PAGE_SIZE: Final = 50
NO_AUDIO_NOTE: Final = (
    '<p class="note">This view shows summaries only: no recording, transcript or lyric is '
    "served here. Listening and review happen in the desktop app.</p>"
)
_VERDICT_LABELS: Final = {
    "improving": "improving",
    "declining": "declining",
    "flat": "flat",
    "trending_up": "trending up",
    "trending_down": "trending down",
    "insufficient_data": "not enough data",
    "achieved": "achieved",
    "missed": "missed",
    "active": "active",
    "abandoned": "abandoned",
}


def _first(params: Sequence[tuple[str, str]], name: str) -> str | None:
    return next((value for key, value in params if key == name), None)


def _verdict_chip(verdict: Any) -> str:
    text = js.to_string(verdict)
    return chip(esc(_VERDICT_LABELS.get(text, text.replace("_", " "))), text)


def _years(rows: Sequence[Json]) -> list[str]:
    return sorted({js.to_string(row["year"]) for row in rows if row["year"] not in (None, "")})


# ---- summary -----------------------------------------------------------------------------


def summary_page(store: JourneyStore) -> str:
    served = store.current()
    payload = served.rows_payload
    totals = payload["totals"]
    rows = payload["rows"]
    known_years = [year for year in _years(rows) if year != "unknown"]
    span = f"{esc(known_years[0])}–{esc(known_years[-1])}" if known_years else DASH
    entries = [
        (fmt(totals["rows"], 0), "recordings"),
        (span, "years"),
        (fmt(totals["reviewed"], 0), "reviewed"),
        (fmt(totals["reviewQueueSelected"], 0), "in the review queue"),
    ]
    trends = served.voice_trends or {}
    headline = trends.get("headline") if trends.get("available") else None
    if isinstance(headline, Mapping):
        entries.append((fmt(headline.get("takesAnalyzed"), 0), "takes measured"))
        entries.append((fmt(headline.get("hoursAnalyzed"), 1, " h"), "singing analyzed"))
        top = headline.get("workingTop")
        if isinstance(top, Mapping):
            entries.append(
                (
                    fmt_signed(top.get("gainSemitones"), 1, " st"),
                    f"working top, {esc(top.get('baselineYear'))} → {esc(top.get('peakYear'))}",
                )
            )
    buckets: Mapping[str, Any] = totals["buckets"]
    largest = max((count for count in buckets.values() if is_number(count)), default=1) or 1
    bucket_rows = [
        [
            f'<a href="{href("/view/recordings", [("bucket", name)])}">{bucket_label(name)}</a>',
            fmt(count, 0),
            f'<div class="track"><div class="bar" style="width:{_percent(count, largest)}%">'
            "</div></div>",
        ]
        for name, count in sorted(buckets.items(), key=lambda item: -_count(item[1]))
    ]
    years: Mapping[str, Any] = totals["years"]
    year_columns = [
        ColumnValue(
            label=year, value=_count(years[year]), tick=year[2:] if len(years) > 8 else year
        )
        for year in sorted(years)
    ]
    body = card("At a glance", tiles(entries) + NO_AUDIO_NOTE)
    body += card(
        "Classification",
        table([("Bucket", False), ("Recordings", True), ("", False)], bucket_rows)
        if bucket_rows
        else '<p class="note">No recordings are indexed yet.</p>',
    )
    if year_columns:
        body += card(
            "Recordings per year",
            column_chart("Recordings per year", year_columns)
            + table(
                [("Year", False), ("Recordings", True)],
                [[esc(column.label), fmt(column.value, 0)] for column in year_columns],
            ),
        )
    source = payload["source"]
    body += card(
        "Sources",
        f'<p class="meta">Rows built {esc(payload["generatedAt"])} from the recording index '
        f"({fmt(source['index']['recordingCount'], 0)} recordings) and the filter results "
        f"({fmt(source['results']['recordingCount'], 0)}); "
        f"{fmt(source['state']['verdictCount'], 0)} local review verdicts.</p>",
    )
    return page("Summary", "summary", body)


def _count(value: Any) -> float:
    return float(value) if is_number(value) else 0.0


def _percent(value: Any, largest: float) -> str:
    return f"{max(_count(value) / largest * 100, 0):.1f}"


# ---- recordings --------------------------------------------------------------------------


def _row_labels(row: Json) -> str:
    labels = []
    if row["reviewQueueSelected"]:
        labels.append(chip("review queue"))
    if row["spotCheckStatus"] == "reviewed" and row["humanVerdict"]:
        labels.append(chip(f"reviewed: {bucket_label(row['humanVerdict'])}"))
    if row["lyricMatchMarked"]:
        labels.append(chip("lyric match"))
    if row["wordlessVocaliseMarked"]:
        labels.append(chip("wordless"))
    return "".join(labels) or DASH


def recordings_page(store: JourneyStore, params: Sequence[tuple[str, str]]) -> str:
    rows = store.current().rows_payload["rows"]
    bucket = _first(params, "bucket") or None
    year = _first(params, "year") or None
    filtered = sort_rows(filter_rows(rows, {"bucket": bucket, "year": year}), "capturedAt", "desc")
    pages = max(math.ceil(len(filtered) / PAGE_SIZE), 1)
    requested = js.parse_int(_first(params, "page") or "1")
    number = int(min(max(requested, 1), pages)) if math.isfinite(requested) else 1
    shown = filtered[(number - 1) * PAGE_SIZE : number * PAGE_SIZE]
    table_rows = [
        [
            date_of(row["capturedAt"]),
            duration(row["durationSeconds"]),
            bucket_label(row["bucket"]),
            _row_labels(row),
            esc(js.coalesce(row["lyricMatchClusterLabel"], row["lyricMatchCluster"], DASH)),
        ]
        for row in shown
    ]
    filters = [("bucket", bucket), ("year", year)]
    bucket_links = " ".join(
        f'<a href="{href("/view/recordings", [("bucket", name), ("year", year)])}">'
        f"{bucket_label(name)}</a>"
        for name in sorted({js.to_string(row["bucket"]) for row in rows})
    )
    year_links = " ".join(
        f'<a href="{href("/view/recordings", [("bucket", bucket), ("year", value)])}">'
        f"{esc(value)}</a>"
        for value in _years(rows)
    )
    active = []
    if bucket is not None:
        active.append(f"bucket {bucket_label(bucket)}")
    if year is not None:
        active.append(f"year {esc(year)}")
    heading = (
        f'<p class="meta">{fmt(len(filtered), 0)} recordings'
        + (f' · {" · ".join(active)} · <a href="/view/recordings">all</a>' if active else "")
        + f" · page {number} of {pages}</p>"
    )
    pager = ['<div class="pager">']
    pager.append(
        f'<a href="{href("/view/recordings", [*filters, ("page", number - 1)])}">← newer</a>'
        if number > 1
        else "<span></span>"
    )
    pager.append(
        f'<a href="{href("/view/recordings", [*filters, ("page", number + 1)])}">older →</a>'
        if number < pages
        else "<span></span>"
    )
    pager.append("</div>")
    body = card(
        "Recordings",
        heading
        + table(
            [
                ("Date", False),
                ("Duration", True),
                ("Bucket", False),
                ("Labels", False),
                ("Cluster", False),
            ],
            table_rows,
        )
        + "".join(pager)
        + NO_AUDIO_NOTE,
    )
    body += card(
        "Filter",
        f'<p class="note">Bucket: {bucket_links or DASH}</p>'
        f'<p class="note">Year: {year_links or DASH}</p>',
    )
    return page("Recordings", "recordings", body)


# ---- journeys ----------------------------------------------------------------------------


def _unavailable(document: Mapping[str, Any]) -> str:
    return (
        f'<p class="note">Not computed yet ({esc(document.get("reason", "unavailable"))}). '
        "It appears once the pipeline has written its manifest.</p>"
    )


def journeys_page(store: JourneyStore) -> str:
    document = store.current().song_journeys
    if document is None:
        return page(
            "Journeys", "journeys", card("Same-song journeys", _unavailable(JOURNEYS_UNAVAILABLE))
        )
    totals = document.get("totals") or {}
    entries = [
        (fmt(totals.get("clustersEligible"), 0), "recurring songs"),
        (fmt(totals.get("reliableTakes"), 0), "reliable takes"),
        (fmt(totals.get("takesWithNoteCore"), 0), "takes with note-core tuning"),
    ]
    index_rows = []
    for dimension in document.get("improvementIndex") or []:
        ci = dimension.get("ci95")
        ci_text = (
            f"{fmt(ci[0], 2)} … {fmt(ci[1], 2)}"
            if isinstance(ci, list | tuple) and len(ci) == 2
            else DASH
        )
        index_rows.append(
            [
                esc(dimension.get("label")),
                fmt_signed(dimension.get("medianSlopePerYear"), 3),
                ci_text,
                fmt(dimension.get("clustersUsed"), 0),
                _verdict_chip(dimension.get("verdict")),
            ]
        )
    journeys = sorted(
        document.get("journeys") or [], key=lambda item: -_count(js.get(item, "takesUsed"))
    )
    journey_rows = []
    for journey in journeys:
        per_year = journey.get("perYear") or {}
        years = sorted(per_year)
        slopes = journey.get("slopesPerYear") or {}
        first = per_year[years[0]] if years else {}
        last = per_year[years[-1]] if years else {}
        journey_rows.append(
            [
                esc(js.coalesce(journey.get("label"), journey.get("clusterId"), DASH)),
                fmt(journey.get("takesUsed"), 0),
                f"{esc(journey.get('firstYear', DASH))}–{esc(journey.get('lastYear', DASH))}",
                f"{fmt(js.get(first, 'noteCoreCentError'))} → "
                f"{fmt(js.get(last, 'noteCoreCentError'))}",
                fmt_signed(slopes.get("noteCoreCentError"), 2),
                fmt_signed(slopes.get("cpps"), 3),
            ]
        )
    body = card(
        "Same-song journeys",
        tiles(entries)
        + '<p class="note">Each recurring song is compared only against itself: slopes per '
        "year inside a song (Theil–Sen), then the median across songs with a bootstrap 95% "
        "interval.</p>",
    )
    body += card(
        "Improvement index",
        table(
            [
                ("Dimension", False),
                ("Slope / yr", True),
                ("95% CI", True),
                ("Songs", True),
                ("Verdict", False),
            ],
            index_rows,
        )
        if index_rows
        else '<p class="note">No dimension has enough songs yet.</p>',
    )
    body += card(
        "Songs",
        table(
            [
                ("Song", False),
                ("Takes", True),
                ("Span", False),
                ("Tuning error, first → last year (c)", True),
                ("Tuning slope / yr", True),
                ("CPPS slope / yr", True),
            ],
            journey_rows,
        )
        if journey_rows
        else '<p class="note">No song has enough takes yet.</p>',
    )
    return page("Journeys", "journeys", body)


# ---- trends ------------------------------------------------------------------------------


def _pair(value: Any) -> tuple[float, float] | None:
    low, high = js.get(value, "loHz"), js.get(value, "hiHz")
    return (float(low), float(high)) if is_number(low) and is_number(high) else None


def _number_or_none(value: Any) -> float | None:
    return float(value) if is_number(value) else None


def _quarter_tick(quarter: str) -> str:
    return quarter[:4] if quarter.endswith("Q1") else ""


def trends_page(store: JourneyStore) -> str:
    trends: Mapping[str, Any] = store.current().voice_trends or TRENDS_UNAVAILABLE
    cadence = [
        ColumnValue(
            label=js.to_string(entry["month"]),
            value=_count(entry["count"]),
            tick=js.to_string(entry["month"])[:4]
            if js.to_string(entry["month"]).endswith("-01")
            else "",
        )
        for entry in trends.get("monthlyCadence") or []
    ]
    cadence_card = (
        card("Recordings per month", column_chart("Recordings per month", cadence))
        if cadence
        else ""
    )
    if not trends.get("available"):
        body = card("Voice trends", _unavailable(trends)) + cadence_card
        return page("Trends", "trends", body)
    headline = trends.get("headline") or {}
    entries = [
        (fmt(headline.get("takesAnalyzed"), 0), "takes measured"),
        (fmt(headline.get("reliableTakes"), 0), "reliable takes"),
        (fmt(headline.get("flaggedTakes"), 0), "flagged (left out)"),
        (fmt(headline.get("hoursAnalyzed"), 1, " h"), "singing analyzed"),
    ]
    top = headline.get("workingTop")
    if isinstance(top, Mapping):
        entries.append(
            (
                fmt_signed(top.get("gainSemitones"), 1, " st"),
                f"working top, {esc(top.get('baselineYear'))} → {esc(top.get('peakYear'))}",
            )
        )
    quarters = [quarter for quarter in trends.get("quarters") or [] if isinstance(quarter, Mapping)]
    bands = [
        RangeBand(
            label=js.to_string(quarter.get("quarter")),
            tick=_quarter_tick(js.to_string(quarter.get("quarter"))),
            outer=_pair(quarter.get("envelope")),
            inner=_pair(quarter.get("typical")),
            median=_number_or_none(quarter.get("medianHz")),
        )
        for quarter in quarters
    ]

    def series(*keys: str) -> list[LinePoint]:
        points = []
        for quarter in quarters:
            value: Any = quarter
            for key in keys:
                value = js.get(value, key)
            name = js.to_string(quarter.get("quarter"))
            points.append(LinePoint(name, _number_or_none(value), _quarter_tick(name)))
        return points

    body = card("Voice trends", tiles(entries) + NO_AUDIO_NOTE)
    pitch = range_chart("Pitch range by quarter (Hz, log scale)", bands)
    if pitch:
        body += card(
            "Pitch range by quarter",
            pitch + '<p class="note">Light band: the quarter\'s envelope (5th–95th percentile of '
            "take extremes). Dark bar: the typical range. Line: the median.</p>",
        )
    for title, keys, unit, digits in (
        ("Longest sustained note (s)", ("sustainSeconds",), "s", 1),
        ("Voice clarity, CPPS (dB)", ("cpps",), "", 1),
        ("Vibrato rate (Hz)", ("vibrato", "rateHz"), "", 2),
    ):
        chart = line_chart(
            f"{title} by quarter",
            series(*keys),
            format_value=_formatter(digits, unit),
        )
        if chart:
            body += card(f"{esc(title)} by quarter", chart)
    body += cadence_card
    tops = trends.get("yearlyTops") or []
    if tops:
        body += card(
            "Working top by year",
            table(
                [("Year", False), ("Reliable takes", True), ("Top (Hz)", True), ("Note", False)],
                [
                    [
                        esc(entry.get("year")),
                        fmt(entry.get("n"), 0),
                        fmt(entry.get("topHz")),
                        esc(evidence.note_name(entry.get("topHz"))),
                    ]
                    for entry in tops
                ],
            ),
        )
    eras = trends.get("eras") or []
    if eras:
        body += card(
            "Eras",
            table(
                [("From", False), ("To", False), ("Takes", True)],
                [
                    [esc(era.get("start")), esc(era.get("end")), fmt(era.get("totalTakes"), 0)]
                    for era in eras
                ],
            ),
        )
    threads = trends.get("clusters") or []
    if threads:
        body += card(
            "Recurring songs over time",
            table(
                [("Song", False), ("Takes", True), ("First", False), ("Last", False)],
                [
                    [
                        esc(thread.get("label")),
                        fmt(thread.get("size"), 0),
                        esc(js.coalesce(thread.get("first"), DASH)),
                        esc(js.coalesce(thread.get("last"), DASH)),
                    ]
                    for thread in threads
                ],
            ),
        )
    if quarters:
        body += card(
            "By quarter",
            table(
                [
                    ("Quarter", False),
                    ("Takes", True),
                    ("Reliable", True),
                    ("Median (Hz)", True),
                    ("Typical (Hz)", True),
                    ("Sustain (s)", True),
                    ("CPPS", True),
                ],
                [
                    [
                        esc(quarter.get("quarter")),
                        fmt(quarter.get("n"), 0),
                        fmt(quarter.get("nReliable"), 0),
                        fmt(quarter.get("medianHz")),
                        _range_text(_pair(quarter.get("typical"))),
                        fmt(quarter.get("sustainSeconds"), 2),
                        fmt(quarter.get("cpps"), 2),
                    ]
                    for quarter in quarters
                ],
            ),
        )
    return page("Trends", "trends", body)


def _formatter(digits: int, unit: str) -> Callable[[float], str]:
    def format_value(value: float) -> str:
        return fmt(value, digits, unit)

    return format_value


def _range_text(pair: tuple[float, float] | None) -> str:
    return DASH if pair is None else f"{fmt(pair[0])}–{fmt(pair[1])}"


# ---- goals -------------------------------------------------------------------------------


def goals_page(store: JourneyStore) -> str:
    goals = store.goals()["goals"]
    rows = []
    for goal in goals:
        progress = goal.get("progress") or {}
        sign = "≥" if goal.get("direction") == "at_least" else "≤"
        counted = fmt(progress.get("takesCounted"), 0)
        required = fmt(js.coalesce(progress.get("takesRequired"), goal.get("takesRequired")), 0)
        rows.append(
            [
                esc(js.coalesce(goal.get("title"), goal.get("goalId"), DASH)),
                esc(js.coalesce(goal.get("metricLabel"), goal.get("metric"), DASH)),
                f"{sign} {fmt(goal.get('target'), 2)}",
                f"{counted} / {required}",
                fmt(progress.get("runningMedian"), 2),
                date_of(goal.get("createdAt")),
                _verdict_chip(goal.get("derivedStatus")),
            ]
        )
    explain = (
        '<p class="note">Status only. Goals are created and abandoned through the API (the '
        "desktop app); a goal counts the first takes recorded after it was set.</p>"
    )
    body = card(
        "Goals",
        (
            table(
                [
                    ("Goal", False),
                    ("Metric", False),
                    ("Target", True),
                    ("Takes", True),
                    ("Running median", True),
                    ("Set", False),
                    ("Status", False),
                ],
                rows,
            )
            if rows
            else '<p class="note">No goals yet.</p>'
        )
        + explain,
    )
    return page("Goals", "goals", body)


# ---- the evidence report -----------------------------------------------------------------

type _Stamp = tuple[tuple[int, int, int] | None, ...]


def _stamp(paths: Sequence[Path]) -> _Stamp:
    stamps: list[tuple[int, int, int] | None] = []
    for path in paths:
        try:
            metadata = os.stat(path)
        except FileNotFoundError:
            stamps.append(None)
        else:
            stamps.append((metadata.st_mtime_ns, metadata.st_size, metadata.st_ino))
    return tuple(stamps)


@dataclass(frozen=True)
class _Rendered:
    stamp: _Stamp
    sections: str
    sources: str
    rendered_at: str


class ReportCache:
    """The evidence report's sections, rendered live from the manifests the generator
    reads and re-rendered only when one of them (or the referee state) changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rendered: _Rendered | None = None

    @staticmethod
    def inputs(config: JourneyConfig) -> tuple[Path, ...]:
        return (
            config.results,
            config.features,
            config.lyrics,
            config.journeys,
            config.referee_state,
        )

    def sections(self, config: JourneyConfig) -> _Rendered:
        paths = self.inputs(config)
        stamp = _stamp(paths)
        with self._lock:
            if self._rendered is None or self._rendered.stamp != stamp:
                results, features, lyrics, journeys, referee = paths
                inputs = evidence.read_inputs(
                    results=results,
                    features=features,
                    lyrics=lyrics,
                    journeys=journeys,
                    referee_state=referee,
                )
                self._rendered = _Rendered(
                    stamp=stamp,
                    sections=evidence.report_sections(inputs.model(), inputs.journeys_manifest),
                    sources=evidence.report_sources(
                        inputs.features_manifest, inputs.journeys_manifest, inputs.referee_state
                    ),
                    rendered_at=iso_timestamp(),
                )
            return self._rendered


def report_page(store: JourneyStore, cache: ReportCache) -> str:
    try:
        rendered = cache.sections(store.config)
    except FileNotFoundError:
        body = card(
            "Evidence report",
            '<p class="note">The report needs the filter results manifest, which is not there '
            "yet.</p>",
        )
        return page("Evidence report", "report", body)
    meta = card(
        "Evidence report",
        f'<p class="meta">Rendered {esc(rendered.rendered_at)} from the manifests · '
        f"{esc(rendered.sources)}</p>"
        '<p class="note">Numeric aggregates and generic labels only: no audio, lyric text or '
        "transcript text.</p>",
    )
    return page(
        "Evidence report", "report", meta + f'<div class="report">\n{rendered.sections}\n</div>'
    )
