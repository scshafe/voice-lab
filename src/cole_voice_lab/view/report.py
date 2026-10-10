"""The evidence report (port of voice-journey's ``evidence-report.mjs``).

@canonical view.report -- the regenerable "did I get better" document: the executive
verdict, the same-song improvement index, the blind listening test (Bradley-Terry by
year), the yearly transformation charts, note-core tuning, song scorecards, limitations
and the method appendix. Static HTML with inline SVG and no scripts, built from the
repo-safe manifests (filter results, voice features, lyric indicators, song journeys)
and the optional local referee state. It holds numeric aggregates and generic labels
only: no audio, no transcript or lyric text, no contour arrays (the clusters it names
are ids such as ``lyric-cluster-001``).

The data layer is ``journey/browser.py`` (``build_voice_trends``,
``bradley_terry_by_year``, ``read_referee_state``). Two consumers:

- the command line, ``python -m cole_voice_lab.view.report generate [--dry-run]
  [--results PATH] [--features PATH] [--lyrics PATH] [--journeys PATH]
  [--referee-state PATH] [--out PATH] [--generated-at ISO]``, which writes the JS's
  document (:func:`render_report`) byte for byte for the same inputs;
- the web view's ``/view/report`` page, which renders :func:`report_sections` (the
  same sections) live inside the view's own page (``view/pages.py``).

Differences from the JS:

- The referee state's default is in the state directory (``VOICE_JOURNEY_STATE_DIR``,
  whose default is the artifacts directory the JS used).
- Every value from a manifest is escaped, and ``"`` is escaped too (``&quot;``). The JS
  escaped ``&``, ``<`` and ``>`` in most places but interpolated the executive verdict's
  years and labels, the scorecards' numbers and the improvement index's numbers raw.
  The output is the same bytes unless a manifest value holds ``&``, ``<``, ``>`` or
  ``"`` in one of those places (the pipeline writes numbers, years and generic labels
  there).
- ``Math.log2`` is :func:`jscompat.log2` (V8's, bit for bit). ``Math.pow(2, x)`` is
  Python's ``2.0 ** x`` (the C library's ``pow``); it agreed with V8 on every value
  checked (the 141 MIDI notes 0-140 and 200 000 exponents in [5, 11]); a last-bit
  difference would move a grid line's coordinate in its seventeenth digit.
- A file that cannot be read or a manifest of the wrong shape exits 1 with the error,
  as the JS did; the messages are Python's.
"""

from __future__ import annotations

import html
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Final

from ..journey import jscompat as js
from ..journey.browser import (
    FEATURES_MANIFEST,
    JOURNEYS_MANIFEST,
    LYRICS_MANIFEST,
    REFEREE_STATE_NAME,
    RESULTS_MANIFEST,
    bradley_terry_by_year,
    build_voice_trends,
    read_optional_json,
    read_referee_state,
)
from ..journey.jsonio import iso_timestamp, js_round, read_json, stringify, to_fixed
from ..journey.paths import resolve_paths

Json = dict[str, Any]

MIN_JUDGMENTS_FOR_EAR_VERDICT: Final = 30
NOTE_NAMES: Final = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_VERDICT_LABELS: Final = {
    "improving": "improving",
    "declining": "declining",
    "flat": "flat",
    "trending_up": "trending up",
    "trending_down": "trending down",
    "insufficient_data": "insufficient data",
}

HELP: Final = """Voice Journey evidence report generator.

Usage:
  voice-journey-report generate --dry-run
  voice-journey-report generate [--results PATH] [--features PATH] [--lyrics PATH] \
[--journeys PATH] [--referee-state PATH] [--out PATH] [--generated-at ISO]

Renders the regenerable "did I get better" document from the committed
manifests plus the optional local referee judgments: executive verdict,
same-song improvement index, perceived-quality curve, yearly transformation
charts, note-core tuning, song scorecards, limitations, and method appendix.
Self-contained static HTML (inline SVG, no scripts); numeric aggregates and
generic labels only. Default output is gitignored (repo = code + manifests).
"""


class EvidenceReportError(Exception):
    """A refused command line (exit 1)."""


# ---- small JavaScript rules --------------------------------------------------------------


def esc(value: Any) -> str:
    """``escapeHtml(String(value))``, with ``"`` escaped as well (see the docstring)."""

    return html.escape(js.to_string(value), quote=False).replace('"', "&quot;")


def _s(value: Any) -> str:
    """A number (or any value) as a template literal interpolates it, escaped."""

    return esc(value)


def _n(value: float) -> str:
    """A computed coordinate as ``String(number)``."""

    return js.to_string(value)


def _num(value: Any) -> float:
    return js.to_number(value)


def round_to(value: Any, places: int = 3) -> float | None:
    return to_fixed(float(value), places) if js.is_finite(value) else None


def median_of(values: Sequence[Any]) -> Any:
    ordered = sorted(value for value in values if js.is_finite(value))
    if not ordered:
        return None
    return ordered[math.floor((len(ordered) - 1) * 0.5)]


def note_name(hz: Any) -> str:
    if not js.truthy(hz) or _num(hz) <= 0:
        return ""
    midi = js_round(69 + 12 * js.log2(_num(hz) / 440))
    return NOTE_NAMES[int(midi % 12)] + js.to_string(math.floor(midi / 12) - 1)


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


# ---- static SVG builders (no client scripts) ---------------------------------------------


def svg_open(width: float, height: float, label: str) -> str:
    return (
        f'<svg width="{_n(width)}" height="{_n(height)}" viewBox="0 0 {_n(width)} '
        f'{_n(height)}" role="img" aria-label="{esc(label)}">'
    )


@dataclass(frozen=True)
class Point:
    label: Any
    value: Any


def line_chart_svg(
    *,
    label: str,
    points: Sequence[Point],
    width: float = 560,
    height: float = 190,
    format: Callable[[Any], str] = js.to_string,  # noqa: A002 (the JS option's name)
    color: str = "#d9ad45",
    zero_line: bool = False,
) -> str:
    usable = [point for point in points if not js.nullish(point.value)]
    if len(usable) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 56, 18, 14, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    values = [_num(point.value) for point in usable]
    lo = js.js_min(*values, 0 if zero_line else math.inf)
    hi = js.js_max(*values, 0 if zero_line else -math.inf)
    pad = js.js_max((hi - lo) * 0.15, 0.001)
    lo -= pad
    hi += pad

    def x(index: int) -> float:
        return pad_l + (index / (len(usable) - 1)) * plot_w

    def y(value: float) -> float:
        return pad_t + plot_h - ((value - lo) / (hi - lo)) * plot_h

    body = svg_open(width, height, label)
    if zero_line and lo < 0 < hi:
        body += (
            f'<line x1="{_n(pad_l)}" x2="{_n(width - pad_r)}" y1="{_n(y(0))}" '
            f'y2="{_n(y(0))}" stroke="#3a4254" stroke-width="1"/>'
        )
        body += f'<text x="{_n(pad_l - 8)}" y="{_n(y(0) + 3)}" text-anchor="end">0</text>'
    body += (
        f'<line x1="{_n(pad_l)}" x2="{_n(width - pad_r)}" y1="{_n(pad_t + plot_h)}" '
        f'y2="{_n(pad_t + plot_h)}" stroke="#3a4254" stroke-width="1"/>'
    )
    coordinates = " ".join(f"{_n(x(index))},{_n(y(value))}" for index, value in enumerate(values))
    body += (
        f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    for index, point in enumerate(usable):
        value = values[index]
        body += (
            f'<circle cx="{_n(x(index))}" cy="{_n(y(value))}" r="3" fill="{color}" '
            'stroke="#151a24" stroke-width="2"/>'
        )
        body += (
            f'<text x="{_n(x(index))}" y="{_n(height - 8)}" text-anchor="middle">'
            f"{esc(point.label)}</text>"
        )
        body += (
            f'<text x="{_n(x(index))}" y="{_n(y(value) - 10)}" text-anchor="middle" '
            f'class="num">{esc(format(point.value))}</text>'
        )
    return f"{body}</svg>"


@dataclass(frozen=True)
class Column:
    value: Any
    tick: str | None


def columns_svg(
    *,
    label: str,
    columns: Sequence[Column],
    width: float = 760,
    height: float = 130,
    color: str = "#d9ad45",
) -> str:
    if not columns:
        return ""
    pad_l, pad_r, pad_t, pad_b = 8, 8, 8, 22
    plot_h = height - pad_t - pad_b
    slot = (width - pad_l - pad_r) / len(columns)
    top = js.js_max(*(_num(column.value) for column in columns), 1)
    body = svg_open(width, height, label)
    body += (
        f'<line x1="{_n(pad_l)}" x2="{_n(width - pad_r)}" y1="{_n(pad_t + plot_h)}" '
        f'y2="{_n(pad_t + plot_h)}" stroke="#3a4254" stroke-width="1"/>'
    )
    for index, column in enumerate(columns):
        h = (_num(column.value) / top) * plot_h
        if h > 0:
            body += (
                f'<rect x="{_n(pad_l + index * slot + 1)}" y="{_n(pad_t + plot_h - h)}" '
                f'width="{_n(js.js_max(slot - 2, 1))}" height="{_n(h)}" fill="{color}" '
                'opacity="0.75"/>'
            )
        if column.tick:
            body += (
                f'<text x="{_n(pad_l + index * slot)}" y="{_n(height - 6)}" '
                f'text-anchor="start">{esc(column.tick)}</text>'
            )
    return f"{body}</svg>"


def range_bars_svg(
    *, label: str, years: Sequence[Any], width: float = 560, height: float = 210
) -> str:
    usable = [year for year in years if js.truthy(js.get(year, "topHz"))]
    if len(usable) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 56, 60, 12, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    tops = [_num(js.get(year, "topHz")) for year in usable]
    log_lo = js.log2(js.js_min(*tops) * 0.85)
    log_hi = js.log2(js.js_max(*tops) * 1.08)

    def y(hz: float) -> float:
        return pad_t + plot_h - ((js.log2(hz) - log_lo) / (log_hi - log_lo)) * plot_h

    slot = plot_w / len(usable)
    body = svg_open(width, height, label)
    first = math.ceil(69 + 12 * js.log2(2.0**log_lo / 440))
    last = math.floor(69 + 12 * js.log2(2.0**log_hi / 440))
    for midi in range(first, last + 1):
        name = NOTE_NAMES[midi % 12]
        if name not in ("C", "G"):
            continue
        hz = 440 * 2.0 ** ((midi - 69) / 12)
        body += (
            f'<line x1="{_n(pad_l)}" x2="{_n(width - pad_r)}" y1="{_n(y(hz))}" '
            f'y2="{_n(y(hz))}" stroke="#242b38" stroke-width="1"/>'
        )
        body += (
            f'<text x="{_n(pad_l - 8)}" y="{_n(y(hz) + 3)}" text-anchor="end" class="num">'
            f"{name}{_n(math.floor(midi / 12) - 1)}</text>"
        )
    for index, year in enumerate(usable):
        cx = pad_l + index * slot + slot / 2
        top = y(tops[index])
        body += (
            f'<line x1="{_n(cx)}" x2="{_n(cx)}" y1="{_n(pad_t + plot_h)}" y2="{_n(top)}" '
            'stroke="#d9ad45" stroke-width="6" stroke-linecap="round" opacity="0.75"/>'
        )
        body += (
            f'<text x="{_n(cx)}" y="{_n(top - 8)}" text-anchor="middle" class="num">'
            f"{esc(note_name(js.get(year, 'topHz')))}</text>"
        )
        body += (
            f'<text x="{_n(cx)}" y="{_n(height - 8)}" text-anchor="middle">'
            f"{esc(js.get(year, 'year'))}</text>"
        )
    return f"{body}</svg>"


# ---- the model ---------------------------------------------------------------------------


@dataclass
class ReportModel:
    """``buildReportModel``: the trends, the listening-test fit, note-core by year and
    the bucket counts."""

    trends: Json | None
    bt: Json | None
    note_core_by_year: list[Json] = field(default_factory=list)
    buckets: dict[str, int] = field(default_factory=dict)


def build_report_model(
    *,
    results_manifest: Any,
    features_manifest: Any,
    lyrics_manifest: Any,
    journeys_manifest: Any,
    referee_state: Any,
) -> ReportModel:
    trends = (
        build_voice_trends(
            features_manifest,
            js.coalesce(js.get(results_manifest, "results"), []),
            lyrics_manifest=lyrics_manifest,
        )
        if js.truthy(features_manifest)
        else None
    )
    bt = (
        bradley_terry_by_year(js.coalesce(js.get(referee_state, "judgments"), []))
        if js.truthy(referee_state)
        else None
    )
    note_core_by_year: list[Json] = []
    takes = js.get(js.get(journeys_manifest, "noteCore"), "takes")
    if js.truthy(takes):
        by_year: dict[Any, list[Any]] = {}
        for take in takes:
            by_year.setdefault(js.get(take, "year"), []).append(js.get(take, "centErrorMedian"))
        for year in sorted(by_year, key=lambda key: js.utf16_key(js.to_string(key))):
            note_core_by_year.append(
                {
                    "year": year,
                    "centError": round_to(median_of(by_year[year]), 1),
                    "n": len(by_year[year]),
                }
            )
    buckets: dict[str, int] = {}
    for row in js.coalesce(js.get(results_manifest, "results"), []):
        bucket = js.to_string(
            js.coalesce(js.get(js.get(row, "classification"), "finalBucket"), "unclassified")
        )
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return ReportModel(trends, bt, note_core_by_year, buckets)


# ---- report assembly ---------------------------------------------------------------------


def verdict_chip_html(verdict: Any) -> str:
    text = js.to_string(verdict)
    label = _VERDICT_LABELS.get(text, text)
    return f'<span class="chip {esc(verdict)}">{esc(label)}</span>'


def _join(values: Sequence[Any]) -> str:
    """``values.join(", ")`` (null and undefined as empty), escaped."""

    return ", ".join("" if js.nullish(value) else esc(value) for value in values)


def _signed(value: Any) -> str:
    return f"{'+' if _num(value) >= 0 else ''}{_s(value)}"


def executive_verdict(*, trends: Any, journeys: Any, bt: Any) -> list[str]:
    """``executiveVerdict``: the lede sentences (HTML, values escaped)."""

    sentences: list[str] = []
    working_top = js.get(js.get(trends, "headline"), "workingTop")
    if js.truthy(working_top):
        sentences.append(
            f"Between {_s(working_top['baselineYear'])} and {_s(working_top['peakYear'])} "
            f"your working top expanded from {esc(note_name(working_top['baselineTopHz']))} "
            f"to {esc(note_name(working_top['peakTopHz']))} — "
            f"{_signed(working_top['gainSemitones'])} semitones of reach — and that "
            "territory has stayed accessible since."
        )
    if js.truthy(journeys):
        index = js.coalesce(js.get(journeys, "improvementIndex"), [])
        improving = [
            js.get(item, "label") for item in index if js.get(item, "verdict") == "improving"
        ]
        declining = [
            js.get(item, "label") for item in index if js.get(item, "verdict") == "declining"
        ]
        if not improving and not declining:
            eligible = js.coalesce(
                js.get(js.get(journeys, "totals"), "clustersEligible"), "the eligible"
            )
            sentences.append(
                f"On unchanged material — {_s(eligible)} recurring songs compared only "
                "against themselves — execution has held steady: no measured dimension "
                "moves once repertoire is controlled. The transformation this archive "
                "records is what you sing, more than how the same song is sung."
            )
        else:
            if improving:
                sentences.append(f"Same-song evidence shows improvement in {_join(improving)}.")
            if declining:
                sentences.append(f"Same-song evidence shows decline in {_join(declining)}.")
    years = js.coalesce(js.get(bt, "years"), [])
    if (
        js.truthy(bt)
        and _num(bt["judgedTotal"]) >= MIN_JUDGMENTS_FOR_EAR_VERDICT
        and len(years) >= 2
    ):
        total = _s(bt["judgedTotal"])
        latest = years[-1]["strengthLog2"]
        if _num(latest) > 0.3:
            sentences.append(
                "Your blind ear disagrees with the flat metrics in the best way: across "
                f"{total} judgments, recent years win the listening test (log2 strength "
                f"{_signed(latest)} vs the earliest year)."
            )
        elif _num(latest) < -0.3:
            sentences.append(
                f"Across {total} blind judgments, earlier years currently win the listening "
                "test — worth sitting with."
            )
        else:
            sentences.append(
                f"Across {total} blind judgments, your ear agrees with the metrics: no strong "
                "drift between eras."
            )
    else:
        sentences.append(
            "The blind listening experiment is still open — its verdict will be added here as "
            "judgments accumulate."
        )
    return sentences


def _quarter_label(quarter: Any) -> str:
    text = js.to_string(quarter)
    return text[:4] if text.endswith("Q1") else ""


def _index_rows(journeys_manifest: Any) -> str:
    rows = []
    for dimension in js.coalesce(js.get(journeys_manifest, "improvementIndex"), []):
        slope = js.get(dimension, "medianSlopePerYear")
        ci95 = js.get(dimension, "ci95")
        slope_text = "—" if slope is None else f"{_s(slope)}/yr"
        ci_text = f"{_s(ci95[0])} … {_s(ci95[1])}" if js.truthy(ci95) else "—"
        rows.append(
            "\n      "
            f'<div class="vrow"><span>{esc(js.get(dimension, "label"))}</span>\n'
            f'        <span class="num">{slope_text}</span>\n'
            f'        <span class="num"><small>CI</small> {ci_text}</span>\n'
            f"        <span><small>{_s(js.get(dimension, 'clustersUsed'))} songs</small></span>\n"
            f"        {verdict_chip_html(js.get(dimension, 'verdict'))}</div>"
        )
    return "".join(rows)


def _song_rows(journeys_manifest: Any) -> str:
    journeys = list(js.coalesce(js.get(journeys_manifest, "journeys"), []))
    journeys.sort(
        key=cmp_to_key(
            lambda left, right: _sign(
                _num(js.get(right, "takesUsed")) - _num(js.get(left, "takesUsed"))
            )
        )
    )
    rows = []
    for journey in journeys[:8]:
        per_year = journey["perYear"]
        years = sorted(per_year, key=js.utf16_key)
        first = per_year.get(years[0]) if years else None
        last = per_year.get(years[-1]) if years else None
        slopes = journey["slopesPerYear"]
        first_error = js.coalesce(js.get(first, "noteCoreCentError"), "—")
        last_error = js.coalesce(js.get(last, "noteCoreCentError"), "—")
        rows.append(
            f"<tr><td>{esc(js.get(journey, 'clusterId'))}</td>"
            f'<td class="num">{_s(js.get(journey, "takesUsed"))}</td>'
            f'<td class="num">{esc(js.get(journey, "firstYear"))}–'
            f"{esc(js.get(journey, 'lastYear'))}</td>"
            f'<td class="num">{_s(first_error)} → {_s(last_error)}</td>'
            f'<td class="num">{_s(js.coalesce(js.get(slopes, "noteCoreCentError"), "—"))}</td>'
            f'<td class="num">{_s(js.coalesce(js.get(slopes, "cpps"), "—"))}</td></tr>'
        )
    return "".join(rows)


def _bt_section(bt: Any, trends: Any) -> str:
    if js.truthy(bt) and _num(bt["judgedTotal"]) > 0:
        by_choice = bt["byChoice"]
        flags = bt["notSameSongFlags"]
        chart = line_chart_svg(
            label="Perceived quality by year",
            points=[Point(row["year"], row["strengthLog2"]) for row in bt["years"]],
            format=lambda v: f"+{js.to_string(v)}" if _num(v) >= 0 else js.to_string(v),
            color="#8fd9a0",
            zero_line=True,
        )
        flag_note = (
            '<p class="note">Chained-cluster flags from listening: '
            + ", ".join(f"{esc(flag['clusterId'])} ×{_s(flag['count'])}" for flag in flags)
            + ".</p>"
            if flags
            else ""
        )
        return (
            f"<p>{_s(bt['judgedTotal'])} blind judgments so far "
            f"({_s(by_choice['a'] + by_choice['b'])} decisive, {_s(by_choice['too_close'])} "
            f"too close, {_s(by_choice['not_same_song'])} not-same-song flags).</p>\n"
            f"       {chart}\n"
            f"       {flag_note}"
        )
    return (
        '<p class="note">No judgments yet. The Referee page serves '
        f"{'blind same-song pairs' if js.truthy(trends) else 'the experiment'} whenever the "
        "browser runs with release-gated playback; this section regenerates as verdicts "
        "accumulate.</p>"
    )


def _stat_tiles(headline: Any, buckets: Mapping[str, int]) -> list[tuple[str, str]]:
    tiles: list[tuple[str, str]] = []
    if js.truthy(headline):
        tiles.append((js.to_string(headline["takesAnalyzed"]), "takes measured"))
        tiles.append((f"{js.to_string(headline['hoursAnalyzed'])} h", "singing analyzed"))
        working_top = headline["workingTop"]
        if js.truthy(working_top):
            tiles.append(
                (
                    f"+{js.to_string(working_top['gainSemitones'])} st",
                    f"working top, {js.to_string(working_top['baselineYear'])} → "
                    f"{js.to_string(working_top['peakYear'])}",
                )
            )
        tiles.append((js.to_string(headline["reliableTakes"]), "reliable takes"))
    singing = (
        buckets.get("clean_singing", 0)
        + buckets.get("noise_contaminated_singing", 0)
        + buckets.get("music_contaminated_singing", 0)
    )
    tiles.append((js.to_string(singing), "classified singing"))
    return tiles


def report_sections(model: ReportModel, journeys_manifest: Any) -> str:
    """The report's ``<main>`` content (its seven cards), shared by the generated
    document and ``/view/report``."""

    trends, bt = model.trends, model.bt
    headline = js.coalesce(js.get(trends, "headline"), None)
    sentences = executive_verdict(trends=trends, journeys=journeys_manifest, bt=bt)
    tiles = _stat_tiles(headline, model.buckets)
    index_rows = _index_rows(journeys_manifest)
    song_rows = _song_rows(journeys_manifest)
    quarterly = js.coalesce(js.get(trends, "quarters"), [])
    sustain_chart = line_chart_svg(
        label="Longest sustained note by quarter",
        points=[
            Point(_quarter_label(q["quarter"]), q["sustainSeconds"])
            for q in quarterly
            if q["sustainSeconds"] is not None
        ],
        format=lambda v: f"{js.to_string(v)}s",
        width=760,
    )
    cpps_chart = line_chart_svg(
        label="CPPS by quarter",
        points=[
            Point(_quarter_label(q["quarter"]), q["cpps"])
            for q in quarterly
            if q["cpps"] is not None
        ],
        width=760,
    )
    cadence_chart = columns_svg(
        label="Recordings per month",
        columns=[
            Column(
                entry["count"],
                entry["month"][:4] if js.to_string(entry["month"]).endswith("-01") else None,
            )
            for entry in js.coalesce(js.get(trends, "monthlyCadence"), [])
        ],
    )
    cent_chart = line_chart_svg(
        label="Note-core tuning error by year",
        points=[Point(row["year"], row["centError"]) for row in model.note_core_by_year],
        format=lambda v: f"{js.to_string(v)}c",
        color="#8fd9a0",
    )
    method = js.get(journeys_manifest, "method")
    slopes_id = js.coalesce(js.get(js.get(method, "slopes"), "id"), "theil_sen_per_cluster.v1")
    bootstrap = js.get(method, "bootstrap")
    resamples = js.coalesce(js.get(bootstrap, "resamples"), 1000)
    seed = js.coalesce(js.get(bootstrap, "seed"), "—")
    bt_method = js.get(bt, "method")
    lede = "\n    ".join(f'<p class="lede">{sentence}</p>' for sentence in sentences)
    tile_html = "".join(
        f'<div class="tile"><div class="v num">{esc(value)}</div>'
        f'<div class="l">{esc(label)}</div></div>'
        for value, label in tiles
    )
    no_index = (
        '<p class="note">Run <code>npm run journeys -- analyze</code> to populate this section.</p>'
    )
    return f"""  <section class="card">
    <h2>Did I get better, and in what ways?</h2>
    {lede}
    <div class="stats">{tile_html}</div>
  </section>

  <section class="card">
    <h2>The verdict — same-song controlled</h2>
    <p class="note">Slopes per year inside recurring songs (Theil–Sen per song; bootstrap 95% \
CI across songs). The strictest lens the archive allows.</p>
    {index_rows or no_index}
  </section>

  <section class="card">
    <h2>The listening test — your ear as referee</h2>
    {_bt_section(bt, trends)}
  </section>

  <section class="card">
    <h2>The yearly transformation</h2>
    <p class="note">Uncontrolled for repertoire — this is the story of where your voice went, \
not same-song execution.</p>
    {
        range_bars_svg(
            label="Working top by year (95th percentile of take tops)",
            years=js.coalesce(js.get(trends, "yearlyTops"), []),
        )
    }
    {cent_chart}
    <div class="scroller">{sustain_chart}</div>
    <div class="scroller">{cpps_chart}</div>
    <div class="scroller">{cadence_chart}</div>
  </section>

  <section class="card">
    <h2>Song scorecards</h2>
    <div class="scroller"><table>
      <tr><th>Cluster</th><th>Takes</th><th>Span</th><th>Tuning error, first → last year (c)\
</th><th>Tuning slope /yr</th><th>CPPS slope /yr</th></tr>
      {song_rows}
    </table></div>
  </section>

  <section class="card">
    <h2>Read honestly — limitations</h2>
    <ul>
      <li>Recording conditions drifted for seven years (phones, rooms, distance): HNR falls \
monotonically for that reason, absolute loudness is untrustworthy, and CPPS is the preferred \
clarity measure.</li>
      <li>Song clusters come from transcript similarity; some are chained across distinct \
songs. Not-same-song verdicts from the Referee accumulate as repair evidence; an acoustic \
verification pass is the planned fix.</li>
      <li>Note-core tuning is measured on stored 50 ms contours against each take's own \
inferred reference — robust to a-cappella drift, but its noise floor is a few cents; small \
true changes can hide inside it.</li>
      <li>Same-song eligibility (&ge;6 takes over &ge;1.5 years) favors songs you kept \
returning to; songs mastered-and-retired are underrepresented by construction.</li>
      <li>114 takes carry high f0-rejection diagnostics and are excluded from every aggregate \
here.</li>
    </ul>
  </section>

  <section class="card">
    <h2>Method appendix</h2>
    <ul>
      <li>Features: praat-parselmouth + librosa in a pinned local venv (Praat-ac f0, \
two-layer outlier suppression); versions and thresholds ride in each manifest's config \
fingerprint.</li>
      <li>Reliability rule: takes with total f0 frame rejection &le; 10% enter aggregates.</li>
      <li>Improvement index: {esc(slopes_id)} with {esc(resamples)} bootstrap resamples, seed \
{esc(seed)}.</li>
      <li>Listening test: {esc(js.coalesce(js.get(bt_method, "id"), "bradley_terry_by_year.v1"))}; \
{esc(js.coalesce(js.get(bt_method, "ties"), "ties as half-wins"))}; \
{esc(js.coalesce(js.get(bt_method, "regularization"), "+0.25 pseudo-wins"))}.</li>
      <li>Privacy: everything in this report is a numeric aggregate or generic label; no \
audio, lyric text, or transcript text — and nothing ever leaves the machine.</li>
    </ul>
  </section>"""


REPORT_STYLE: Final = """  :root { color-scheme: dark; font-family: ui-sans-serif, system-ui, \
-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  body { margin: 0; background: #10131a; color: #f5f7fb; }
  header { padding: 32px 24px; background: linear-gradient(135deg, #182033, #3d2c17); \
border-bottom: 1px solid #30384a; }
  h1 { margin: 0 0 6px; font-size: clamp(28px, 4vw, 42px); letter-spacing: -0.04em; }
  h2 { margin: 0 0 4px; font-size: 1.25rem; }
  .meta { color: #9aa5b8; font-size: 0.82rem; }
  main { padding: 24px; display: grid; gap: 20px; max-width: 900px; margin: 0 auto; }
  .card { background: #151a24; border: 1px solid #2a3141; border-radius: 12px; \
padding: 18px 20px; }
  .card p { color: #c5ccda; line-height: 1.55; }
  .card p.note { color: #9aa5b8; font-size: 0.85rem; }
  .lede { font-size: 1.02rem; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); \
gap: 10px; margin-top: 14px; }
  .tile { background: rgb(255 255 255 / 0.05); border: 1px solid #30384a; \
border-radius: 10px; padding: 10px 14px; }
  .tile .v { font-size: 1.4rem; font-weight: 650; }
  .tile .l { font-size: 0.72rem; color: #aeb8ca; margin-top: 2px; }
  .vrow { display: grid; grid-template-columns: minmax(14rem, 2fr) minmax(6rem, 1fr) \
minmax(9rem, 1fr) minmax(5rem, 1fr) auto; gap: 10px; align-items: center; \
background: rgb(255 255 255 / 0.03); border: 1px solid #2a3141; border-radius: 9px; \
padding: 8px 12px; font-size: 0.85rem; color: #c5ccda; margin-bottom: 6px; }
  .vrow small, .note small { color: #8b93a5; }
  .num { font-variant-numeric: tabular-nums; }
  .chip { display: inline-block; font-size: 0.72rem; font-weight: 650; padding: 3px 10px; \
border-radius: 99px; border: 1px solid #3a4254; }
  .chip.improving { color: #8fd9a0; border-color: #3d6a4a; }
  .chip.declining { color: #e08585; border-color: #6d4040; }
  .chip.flat, .chip.trending_up, .chip.trending_down { color: #aeb8ca; }
  table { border-collapse: collapse; font-size: 0.84rem; }
  th, td { border: 1px solid #2a3141; padding: 5px 10px; text-align: right; color: #c5ccda; }
  th { text-align: left; color: #aeb8ca; }
  td:first-child { text-align: left; }
  svg { max-width: 100%; height: auto; }
  svg text { font: 11px ui-sans-serif, system-ui, sans-serif; fill: #9aa5b8; }
  ul { color: #c5ccda; line-height: 1.55; }
  .scroller { overflow-x: auto; }
  @media print { body { background: #ffffff; } }"""


def render_report(
    *, generated_at: str, model: ReportModel, journeys_manifest: Any, sources: str
) -> str:
    """``renderReport``: the whole self-contained document."""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Journey — Evidence Report</title>
<style>
{REPORT_STYLE}
</style>
</head>
<body>
<header>
  <h1>Voice Journey — Evidence Report</h1>
  <div class="meta">Generated {esc(generated_at)} · {esc(sources)} · regenerate any time with \
<code>npm run report</code></div>
</header>
<main>
{report_sections(model, journeys_manifest)}
</main>
</body>
</html>"""


def report_sources(features_manifest: Any, journeys_manifest: Any, referee_state: Json) -> str:
    features = js.length(js.get(features_manifest, "results"))
    journeys = js.length(js.get(journeys_manifest, "journeys"))
    return (
        f"{js.to_string(js.coalesce(features, 0))} feature takes · "
        f"{js.to_string(js.coalesce(journeys, 0))} song journeys · "
        f"{js.to_string(len(referee_state['judgments']))} listening judgments"
    )


# ---- inputs, dry run and the command line ------------------------------------------------


@dataclass
class ReportInputs:
    """The manifests and the referee state one report is built from."""

    results_manifest: Any
    features_manifest: Any
    lyrics_manifest: Any
    journeys_manifest: Any
    referee_state: Json

    def model(self) -> ReportModel:
        return build_report_model(
            results_manifest=self.results_manifest,
            features_manifest=self.features_manifest,
            lyrics_manifest=self.lyrics_manifest,
            journeys_manifest=self.journeys_manifest,
            referee_state=self.referee_state,
        )


def read_inputs(
    *,
    results: Path | str,
    features: Path | str,
    lyrics: Path | str,
    journeys: Path | str,
    referee_state: Path | str,
) -> ReportInputs:
    """The JS's reads: the results manifest is required, the others optional."""

    return ReportInputs(
        results_manifest=read_json(results),
        features_manifest=read_optional_json(features),
        lyrics_manifest=read_optional_json(lyrics),
        journeys_manifest=read_optional_json(journeys),
        referee_state=read_referee_state(referee_state),
    )


def build_dry_run(out: str, model: ReportModel, inputs: ReportInputs) -> Json:
    judgments = js.length(js.get(inputs.referee_state, "judgments"))
    return {
        "dryRun": True,
        "operation": "evidence-report-generation",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "reads": [
                "filter-results-manifest",
                "voice-feature-manifest",
                "lyric-indicator-manifest",
                "song-journeys-manifest",
                "referee-state (optional, local)",
            ],
            "writes": ["evidence-report-html (gitignored by default)"],
        },
        "outputPath": out,
        "inputs": {
            "trendsAvailable": js.truthy(js.get(model.trends, "available")),
            "journeysAvailable": js.truthy(inputs.journeys_manifest),
            "refereeJudgments": js.coalesce(judgments, 0),
            "noteCoreYears": len(model.note_core_by_year),
        },
        "readScope": {
            "committedManifests": True,
            "localRefereeState": True,
            "audioBytes": False,
            "transcriptText": False,
            "contourArrays": False,
            "upload": False,
        },
    }


def _defaults() -> Json:
    paths = resolve_paths()
    return {
        "results": str(paths.manifest(RESULTS_MANIFEST)),
        "features": str(paths.manifest(FEATURES_MANIFEST)),
        "lyrics": str(paths.manifest(LYRICS_MANIFEST)),
        "journeys": str(paths.manifest(JOURNEYS_MANIFEST)),
        "referee_state": str(paths.state_dir / REFEREE_STATE_NAME),
        "out": str(paths.artifact("evidence-report.html")),
    }


_VALUE_FLAGS: Final = {
    "--features": "features",
    "--generated-at": "generated_at",
    "--journeys": "journeys",
    "--lyrics": "lyrics",
    "--out": "out",
    "--referee-state": "referee_state",
    "--results": "results",
}


def parse_args(argv: Sequence[str]) -> Json:
    args = list(argv)
    has_command = bool(args) and not args[0].startswith("--") and args[0] != "help"
    command = args.pop(0) if has_command else "generate"
    options: Json = {
        "command": command,
        "dry_run": False,
        "generated_at": None,
        "help": False,
        **_defaults(),
    }
    while args:
        flag = args.pop(0)
        if flag == "--dry-run":
            options["dry_run"] = True
        elif flag in _VALUE_FLAGS:
            value = args.pop(0) if args else None
            if not value or value.startswith("--"):
                raise EvidenceReportError(f"{flag} requires a value")
            options[_VALUE_FLAGS[flag]] = value
        elif flag in {"--help", "help"}:
            options["help"] = True
        else:
            raise EvidenceReportError(f"unsupported argument: {flag}")
    return options


def generate_report(options: Mapping[str, Any]) -> str | None:
    """``generateReport``: print the dry run, or write the document and print its path."""

    inputs = read_inputs(
        results=options["results"],
        features=options["features"],
        lyrics=options["lyrics"],
        journeys=options["journeys"],
        referee_state=options["referee_state"],
    )
    model = inputs.model()
    if options["dry_run"]:
        sys.stdout.write(f"{stringify(build_dry_run(options['out'], model, inputs))}\n")
        return None
    generated_at = options["generated_at"] or iso_timestamp()
    document = render_report(
        generated_at=generated_at,
        model=model,
        journeys_manifest=inputs.journeys_manifest,
        sources=report_sources(
            inputs.features_manifest, inputs.journeys_manifest, inputs.referee_state
        ),
    )
    out = Path(options["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"{document}\n", encoding="utf-8")
    sys.stdout.write(f"{options['out']}\n")
    return str(options["out"])


def run(argv: Sequence[str]) -> int:
    options = parse_args(argv)
    if options["help"]:
        sys.stdout.write(HELP)
        return 0
    if options["command"] != "generate":
        raise EvidenceReportError(f"unsupported command: {options['command']}")
    generate_report(options)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except (EvidenceReportError, OSError, ValueError, TypeError, KeyError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
