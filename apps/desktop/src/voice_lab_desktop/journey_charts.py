"""The Journey screen's numbers and charts, as plain data (checklist J).

@canonical journey_charts -- the port of voice-journey's SPA for the Journey page: the
selectors of ``web/src/state/TrendsManager.js`` and ``JourneysManager.js``, the math of
``web/src/components/charts/chartUtils.js``, and every chart of
``web/src/components/charts/`` rebuilt as a ``SuiChart`` spec (the stand-in's API: panels
stacked on a shared x axis, layers in data coordinates; the chart owns the pixels). The SPA
drew in pixels; here the same geometry is said in data units: a quarter is one slot of
the Range River, a month one unit of Cause & Effect, a quarter's middle month the x of
the quarterly panels. Tooltips are the SPA's texts, with its ``<br/>`` as a line break and
its bold dropped (plain text only).

Nothing here touches Qt or the network; ``screens/journey.py`` feeds it the ``trends`` and
``journeys`` documents of ``/api/v1`` and hands the results to QML.

Colours are SuiTheme tones: the SPA's brass is ``bucket5``, its light blue ``bucket1``,
its mid-register blue ``info``, its grey ``muted``, its green ``ok``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

type Json = dict[str, Any]
type Spec = dict[str, Any]

NOTE_NAMES: Final = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MONTH_NAMES: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
BRASS: Final = "bucket5"
BLUE: Final = "bucket1"
MID: Final = "info"
LOW: Final = "muted"
GREEN: Final = "ok"
ZONE: Final = "info"

SUBJECT_TABS: Final = (
    ("verdict", "The Verdict"),
    ("range", "Range River"),
    ("practice", "Cause & Effect"),
    ("vibrato", "Vibrato Story"),
    ("trust", "What Not To Trust"),
    ("songs", "Song Scorecards"),
    ("threads", "Repertoire Threads"),
)
SUBJECTS: Final = tuple(key for key, _ in SUBJECT_TABS)
VERDICT_LABELS: Final = {
    "improving": "improving",
    "declining": "declining",
    "flat": "flat",
    "trending_up": "trending up",
    "trending_down": "trending down",
    "insufficient_data": "insufficient data",
}
VERDICT_TONES: Final = {
    "improving": "ok",
    "declining": "danger",
    "flat": "info",
    "trending_up": "ok",
    "trending_down": "warn",
    "insufficient_data": "neutral",
}


# ---- JavaScript's number rules -------------------------------------------------------------


def js_round(value: float) -> int:
    """``Math.round``: halves go up (``Math.round(-2.5) === -2``)."""
    return math.floor(value + 0.5)


def js_num(value: Any) -> str:
    """``String(n)`` for the numbers these documents hold."""
    if isinstance(value, bool) or value is None:
        return "" if value is None else str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    return str(value)


def js_fixed(value: float, digits: int) -> str:
    """``Number.prototype.toFixed``: the exact binary value, halves away from zero."""
    quantum = Decimal(1).scaleb(-digits)
    exact = Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)
    text = f"{exact:f}"
    return "0" + text[2:] if text.startswith("-0") and exact == 0 else text


def thousands(value: Any) -> str:
    """``toLocaleString()`` of a count (en-US grouping)."""
    return f"{int(value):,}" if isinstance(value, int | float) else js_num(value)


def truthy(value: Any) -> bool:
    """JavaScript truthiness for the values of a JSON document."""
    if value is None or value is False:
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


# ---- chartUtils.js -------------------------------------------------------------------------


def note_name(hz: Any) -> str:
    if not truthy(hz) or hz <= 0:
        return ""
    midi = js_round(69 + 12 * math.log2(hz / 440))
    return NOTE_NAMES[midi % 12] + str(midi // 12 - 1)


def midi_hz(midi: int) -> float:
    return float(440 * 2 ** ((midi - 69) / 12))


def month_idx(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def month_short(month: str) -> str:
    return MONTH_NAMES[int(month[5:7]) - 1]


def quarter_mid_idx(quarter: str) -> int:
    return int(quarter[:4]) * 12 + (int(quarter[6:7]) - 1) * 3 + 1


def era_label(era: Mapping[str, Any]) -> str:
    start, end = str(era["start"]), str(era["end"])
    y1, y2 = start[:4], end[:4]
    if y1 == y2:
        return f"{month_short(start)}–{month_short(end)} {y1}"
    return f"{month_short(start)} {y1} – {month_short(end)} {y2}"


def domain_of(
    values: Sequence[Any], pad_share: float, floor: float | None = None
) -> tuple[float, float]:
    present_values = [float(v) for v in values if v is not None]
    if not present_values:
        return (0.0, 1.0)
    lo, hi = min(present_values), max(present_values)
    pad = max((hi - lo) * pad_share, 0.001)
    lo, hi = lo - pad, hi + pad
    if floor is not None:
        lo = max(lo, floor)
    return (lo, hi)


def pct(share: Any) -> str:
    return "" if share is None else f"{js_round(share * 100)}%"


def year_shade(year: Any, min_year: int, max_year: int) -> float:
    """``yearColor``'s position in the brass gradient (0 the oldest year, 1 the newest)."""
    if max_year == min_year:
        return 1.0
    t = (float(year) - min_year) / (max_year - min_year)
    return max(0.0, min(1.0, t))


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


# ---- the selectors (TrendsManager.js, JourneysManager.js) ---------------------------------


def active_quarters(trends: Mapping[str, Any] | None, include_flagged: bool) -> list[Json]:
    """``selectActiveQuarters``: reliable-only by default, the all-takes rollup when asked."""
    if not trends or not trends.get("available"):
        return []
    chosen = trends.get("quartersAllTakes") if include_flagged else None
    return list(chosen or trends.get("quarters") or [])


def year_span(trends: Mapping[str, Any] | None) -> tuple[int, int]:
    """``selectYearSpan``: the years the (reliable) quarters cover."""
    years = [int(q["quarter"][:4]) for q in (trends or {}).get("quarters") or []]
    return (min(years), max(years)) if years else (0, 0)


def fingerprint_takes(trends: Mapping[str, Any] | None, include_flagged: bool) -> list[Json]:
    return [t for t in (trends or {}).get("vibratoTakes") or [] if include_flagged or t["reliable"]]


def fingerprint_years(trends: Mapping[str, Any] | None) -> list[str]:
    return sorted({str(t["year"]) for t in (trends or {}).get("vibratoTakes") or []})


def tuning_pseudo_quarters(journeys: Mapping[str, Any] | None) -> list[Json]:
    """``selectTuningPseudoQuarters``: per-quarter median and IQR of note-core error (n>=3)."""
    takes = _get(journeys, "noteCore", "takes") or []
    by_quarter: dict[str, list[float]] = {}
    for take in takes:
        if not take.get("quarter") or take.get("centErrorMedian") is None:
            continue
        by_quarter.setdefault(take["quarter"], []).append(take["centErrorMedian"])
    out: list[Json] = []
    for quarter in sorted(by_quarter):
        values = sorted(by_quarter[quarter])

        def at(p: float, values: list[float] = values) -> float:
            return values[math.floor((len(values) - 1) * p)]

        spread = [at(0.25), at(0.75)] if len(values) >= 3 else [None, None]
        out.append(
            {"quarter": quarter, "nc": at(0.5), "nReliable": len(values), "spread": {"nc": spread}}
        )
    return [q for q in out if q["nReliable"] >= 3]


def note_core_overall(journeys: Mapping[str, Any] | None) -> Json:
    takes = _get(journeys, "noteCore", "takes") or []
    values = sorted(t["centErrorMedian"] for t in takes if t.get("centErrorMedian") is not None)
    if not values:
        return {"takeCount": len(takes), "median": None}
    return {"takeCount": len(takes), "median": values[math.floor((len(values) - 1) / 2)]}


def song_scorecards(journeys: Mapping[str, Any] | None) -> list[Json]:
    """``selectSongScorecards``: the ten songs with most takes."""
    found = list((journeys or {}).get("journeys") or [])
    return sorted(found, key=lambda j: -j["takesUsed"])[:10]


def tab_availability(
    trends: Mapping[str, Any], journeys: Mapping[str, Any] | None
) -> dict[str, bool]:
    return {
        "verdict": bool(journeys),
        "range": True,
        "practice": len(trends.get("monthlyCadence") or []) > 0,
        "vibrato": True,
        "trust": True,
        "songs": bool(journeys),
        "threads": len(trends.get("clusters") or []) > 0,
    }


def active_subject(requested: str, availability: Mapping[str, bool]) -> str:
    if availability.get(requested):
        return requested
    return next(key for key in SUBJECTS if availability.get(key))


# ---- the stat band and the verdict ---------------------------------------------------------


def stat_tiles(trends: Mapping[str, Any]) -> list[Json]:
    head = trends["headline"]
    tiles = [
        {"value": thousands(head["takesAnalyzed"]), "label": "takes analyzed"},
        {"value": f"{js_num(head['hoursAnalyzed'])} h", "label": "singing measured"},
    ]
    top = head.get("workingTop")
    if top:
        tiles.append(
            {
                "value": f"+{js_num(top['gainSemitones'])} st",
                "label": f"working top, {top['baselineYear']} → {top['peakYear']}",
            }
        )
    tiles.append({"value": thousands(head["reliableTakes"]), "label": "reliable takes"})
    tiles.append({"value": thousands(head["flaggedTakes"]), "label": "flagged for listening"})
    return tiles


def verdict_rows(journeys: Mapping[str, Any]) -> list[Json]:
    rows = []
    for dim in journeys.get("improvementIndex") or []:
        slope = dim.get("medianSlopePerYear")
        ci = dim.get("ci95")
        verdict = str(dim.get("verdict"))
        rows.append(
            {
                "key": dim.get("key") or dim.get("label"),
                "label": dim.get("label"),
                "slope": "—" if slope is None else f"{js_num(slope)}/yr",
                "ci": f"CI {js_num(ci[0])} … {js_num(ci[1])}" if ci else "CI —",
                "songs": f"{dim.get('clustersUsed')} songs",
                "verdict": VERDICT_LABELS.get(verdict, verdict),
                "tone": VERDICT_TONES.get(verdict, "neutral"),
            }
        )
    return rows


def verdict_summary(journeys: Mapping[str, Any]) -> str:
    counts = {"improving": 0, "declining": 0, "flat": 0}
    for dim in journeys.get("improvementIndex") or []:
        if dim.get("verdict") in counts:
            counts[dim["verdict"]] += 1
    eligible = _get(journeys, "totals", "clustersEligible")
    if counts["improving"] == 0 and counts["declining"] == 0:
        return (
            f"Across {js_num(eligible)} recurring songs, execution on unchanged material has "
            "held steady — no dimension moves once repertoire is controlled. The "
            "transformation this archive records lives in the other tabs: what you sing "
            "(range, register, repertoire) changed far more than how the same song is sung. "
            "Individual songs still moved both ways — see the scorecards."
        )
    return (
        f"{counts['improving']} improving · {counts['flat']} flat · {counts['declining']} "
        f"declining across {js_num(eligible)} songs. Individual songs vary — see the scorecards."
    )


# ---- shared chart pieces (chartPrimitives.jsx) ---------------------------------------------


def two_ticks(lo: float, hi: float, fmt: Callable[[float], str]) -> list[Json]:
    """``AxisTicks``: the domain's two ends."""
    return [{"value": lo, "label": fmt(lo)}, {"value": hi, "label": fmt(hi)}]


def quarter_series(
    quarters: Sequence[Mapping[str, Any]],
    pick: Callable[[Mapping[str, Any]], Any],
    spread_key: str,
    spread_scale: float = 1.0,
) -> list[Json]:
    """``quarterSeries`` in data units: x is the quarter's middle month (+0.5)."""
    out: list[Json] = []
    for q in quarters:
        spread = _get(q, "spread", spread_key) or [None, None]
        lo = None if spread[0] is None else spread[0] * spread_scale
        hi = None if spread[1] is None else spread[1] * spread_scale
        out.append(
            {"x": quarter_mid_idx(q["quarter"]) + 0.5, "q": q, "value": pick(q), "lo": lo, "hi": hi}
        )
    return out


def band_and_line(points: Sequence[Mapping[str, Any]], tone: str) -> list[Json]:
    """``BandAndLine``: the spread band (when two quarters have one) and the median line."""
    layers: list[Json] = []
    banded = [
        [p["x"], p["lo"], p["hi"]] for p in points if p["lo"] is not None and p["hi"] is not None
    ]
    lined = [[p["x"], p["value"]] for p in points if p["value"] is not None]
    if len(banded) >= 2:
        layers.append({"kind": "band", "tone": tone, "opacity": 0.16, "points": banded})
    if len(lined) >= 2:
        layers.append({"kind": "line", "tone": tone, "width": 2, "points": lined})
    return layers


def _domain_with_spread(
    quarters: Sequence[Mapping[str, Any]],
    pick: Callable[[Mapping[str, Any]], Any],
    key: str,
    scale: float = 1.0,
    extra: Sequence[float] = (),
) -> tuple[float, float]:
    values: list[Any] = [pick(q) for q in quarters]
    for q in quarters:
        spread = _get(q, "spread", key) or [None, None]
        values += [None if s is None else s * scale for s in spread]
    return domain_of([*values, *extra], 0.12, 0)


def _quarter_points(
    series: Sequence[Mapping[str, Any]],
    tone: str,
    radius: float,
    tip: Callable[[Mapping[str, Any]], str],
) -> Json:
    return {
        "kind": "points",
        "tone": tone,
        "radius": radius,
        "points": [
            {"x": p["x"], "y": p["value"], "tooltip": tip(p)}
            for p in series
            if p["value"] is not None
        ],
    }


# ---- the Range River (RangeRiverChart.jsx) -------------------------------------------------

RIVER_HEIGHT: Final = 300
STRIP_HEIGHT: Final = 22


def _usable(q: Mapping[str, Any]) -> bool:
    return bool(
        q["nReliable"] >= 3
        and truthy(_get(q, "envelope", "loHz"))
        and truthy(_get(q, "envelope", "hiHz"))
        and truthy(_get(q, "typical", "loHz"))
        and truthy(_get(q, "typical", "hiHz"))
        and truthy(q.get("medianHz"))
    )


def river_tooltip(q: Mapping[str, Any]) -> str:
    lines = [f"{q['quarter']} · {q['nReliable']} reliable of {q['n']} takes"]
    lo, hi = _get(q, "envelope", "loHz"), _get(q, "envelope", "hiHz")
    if truthy(lo):
        lines.append(f"reach {note_name(lo)}–{note_name(hi)} ({js_num(lo)}–{js_num(hi)} Hz)")
    else:
        lines.append("sparse quarter")
    if truthy(q.get("medianHz")):
        high = _get(q, "registerShares", "high")
        lines.append(f"median {note_name(q['medianHz'])} · high-register {pct(high)}")
    return "\n".join(lines)


def range_river(quarters: Sequence[Mapping[str, Any]]) -> Spec | None:
    """The river (reach and typical bands, the median, broken at sparse quarters) and its
    register strip, one slot per quarter."""
    if not quarters:
        return None
    los = [q["envelope"]["loHz"] for q in quarters if truthy(_get(q, "envelope", "loHz"))]
    his = [q["envelope"]["hiHz"] for q in quarters if truthy(_get(q, "envelope", "hiHz"))]
    y_min = max(55.0, min(los) * 0.93) if los else 55.0
    y_max = max(his) * 1.07 if his else 1000.0
    grid = []
    midi = math.ceil(69 + 12 * math.log2(y_min / 440))
    while midi <= math.floor(69 + 12 * math.log2(y_max / 440)):
        name = NOTE_NAMES[midi % 12]
        if name in ("C", "G"):
            grid.append((midi, name + str(midi // 12 - 1), midi_hz(midi)))
        midi += 1
    segments: list[list[tuple[int, Mapping[str, Any]]]] = []
    current: list[tuple[int, Mapping[str, Any]]] | None = None
    for i, q in enumerate(quarters):
        if _usable(q):
            if current is None:
                current = []
                segments.append(current)
            current.append((i, q))
        else:
            current = None
    layers: list[Json] = []
    for segment in segments:
        if len(segment) < 2:
            continue
        layers.append(
            {
                "kind": "band",
                "tone": BRASS,
                "opacity": 0.14,
                "points": [[i, q["envelope"]["loHz"], q["envelope"]["hiHz"]] for i, q in segment],
            }
        )
        layers.append(
            {
                "kind": "band",
                "tone": BRASS,
                "opacity": 0.32,
                "points": [[i, q["typical"]["loHz"], q["typical"]["hiHz"]] for i, q in segment],
            }
        )
        layers.append(
            {
                "kind": "line",
                "tone": BRASS,
                "width": 2,
                "points": [[i, q["medianHz"]] for i, q in segment],
            }
        )
    sparse = [
        {"x": i, "y": q["medianHz"], "opacity": 0.35}
        for i, q in enumerate(quarters)
        if q["nReliable"] < 3 and truthy(q.get("medianHz"))
    ]
    if sparse:
        layers.append({"kind": "points", "tone": BRASS, "radius": 3, "points": sparse})
    regions = {
        "kind": "regions",
        "regions": [
            {"x0": i - 0.5, "x1": i + 0.5, "tooltip": river_tooltip(q)}
            for i, q in enumerate(quarters)
        ],
    }
    layers.append(regions)
    strip: list[Json] = []
    for i, q in enumerate(quarters):
        shares = q.get("registerShares")
        if not shares:
            continue
        offset = 0.0
        for key, tone in (("high", BRASS), ("mid", MID), ("low", LOW)):
            share = shares.get(key) or 0
            if share * STRIP_HEIGHT > 0.5:
                strip.append(
                    {
                        "x0": i - 0.38,
                        "x1": i + 0.38,
                        "y0": 1 - offset - share,
                        "y1": 1 - offset,
                        "tone": tone,
                    }
                )
            offset += share
    years: list[Json] = []
    last_year = ""
    for i, q in enumerate(quarters):
        year = q["quarter"][:4]
        if year != last_year:
            years.append({"value": i, "label": year})
            last_year = year
    return {
        "label": "Pitch range per quarter on a log-frequency axis labeled in note names",
        "xMin": -0.5,
        "xMax": len(quarters) - 0.5,
        "xTicks": years,
        "padLeft": 56,
        "padRight": 64,
        "panels": [
            {
                "height": RIVER_HEIGHT,
                "title": "",
                "yScale": "log2",
                "yMin": y_min,
                "yMax": y_max,
                "grid": True,
                "yTicks": [{"value": hz, "label": label} for _, label, hz in grid],
                "yTicksRight": [{"value": hz, "label": f"{js_round(hz)} Hz"} for _, _, hz in grid],
                "layers": layers,
            },
            {
                "height": STRIP_HEIGHT,
                "title": "",
                "yMin": 0,
                "yMax": 1,
                "layers": [{"kind": "rects", "tone": BRASS, "rects": strip}, regions],
            },
        ],
    }


def range_headline(trends: Mapping[str, Any]) -> str:
    top = _get(trends, "headline", "workingTop")
    if not top:
        return "Where your voice lives, quarter by quarter."
    return (
        f"Working top: {note_name(top['baselineTopHz'])} ({top['baselineYear']}) → "
        f"{note_name(top['peakTopHz'])} ({top['peakYear']}) · "
        f"+{js_num(top['gainSemitones'])} semitones of reach, held since."
    )


RANGE_LEGEND: Final = (
    {"text": "reach (p05–p95 envelope)", "tone": BRASS, "opacity": 0.14},
    {"text": "typical (p25–p75)", "tone": BRASS, "opacity": 0.32},
    {"text": "median", "tone": BRASS, "opacity": 1.0},
    {"text": "register strip: high ≥300 Hz · mid · low <200 Hz", "tone": MID, "opacity": 1.0},
)

RANGE_COLUMNS: Final = (
    {"key": "quarter", "title": "Quarter", "width": 90},
    {"key": "reliable", "title": "Reliable", "width": 80, "kind": "number"},
    {"key": "total", "title": "Total", "width": 70, "kind": "number"},
    {"key": "floor", "title": "Floor", "width": 120},
    {"key": "top", "title": "Top", "width": 120},
    {"key": "median", "title": "Median", "width": 80},
    {"key": "high", "title": "High share", "width": 90, "kind": "number"},
)


def range_table(quarters: Sequence[Mapping[str, Any]]) -> list[Json]:
    rows = []
    for q in quarters:
        lo, hi = _get(q, "envelope", "loHz"), _get(q, "envelope", "hiHz")
        rows.append(
            {
                "_key": q["quarter"],
                "quarter": q["quarter"],
                "reliable": js_num(q["nReliable"]),
                "total": js_num(q["n"]),
                "floor": f"{note_name(lo)} ({js_num(lo)})" if truthy(lo) else "",
                "top": f"{note_name(hi)} ({js_num(hi)})" if truthy(hi) else "",
                "median": note_name(q.get("medianHz")) if truthy(q.get("medianHz")) else "",
                "high": pct(_get(q, "registerShares", "high")),
            }
        )
    return rows


# ---- Cause & Effect (CauseEffectChart.jsx) -------------------------------------------------


def cause_effect(
    months: Sequence[Mapping[str, Any]],
    quarters: Sequence[Mapping[str, Any]],
    eras: Sequence[Mapping[str, Any]],
) -> Spec | None:
    if not months or not quarters:
        return None
    min_i = month_idx(months[0]["month"])
    max_i = month_idx(months[-1]["month"])
    max_count = max(m["count"] for m in months)
    era_rects = {
        "kind": "rects",
        "tone": BRASS,
        "opacity": 0.07,
        "rects": [
            {"x0": month_idx(e["start"]), "x1": month_idx(e["end"]) + 1, "y0": None, "y1": None}
            for e in eras
        ],
    }
    bars = [
        {
            "x0": month_idx(m["month"]) + 0.1,
            "x1": month_idx(m["month"]) + 0.9,
            "y0": 0,
            "y1": m["count"],
            "tooltip": f"{month_short(m['month'])} {m['month'][:4]} · {m['count']} recordings",
        }
        for m in months
        if m["count"] > 0
    ]
    panels: list[Json] = [
        {
            "height": 104,
            "title": "recordings per month",
            "yMin": 0,
            "yMax": max_count,
            "yTicks": two_ticks(0, max_count, lambda v: str(js_round(v))),
            "layers": [era_rects, {"kind": "rects", "tone": BRASS, "opacity": 0.75, "rects": bars}],
        }
    ]
    specs = (
        (
            "sustain",
            lambda q: q.get("sustainSeconds"),
            lambda v: f"{js_fixed(v, 1)}s",
            BLUE,
            "longest sustained note (s, quarterly median)",
        ),
        (
            "cpps",
            lambda q: q.get("cpps"),
            lambda v: js_fixed(v, 1),
            BRASS,
            "voice clarity — CPPS (dB, quarterly median)",
        ),
    )
    for key, pick, fmt, tone, title in specs:
        lo, hi = _domain_with_spread(quarters, pick, key)
        series = quarter_series(quarters, pick, key)

        def tip(p: Mapping[str, Any], fmt: Callable[[float], str] = fmt) -> str:
            return f"{p['q']['quarter']} · {fmt(p['value'])} · {p['q']['nReliable']} takes"

        panels.append(
            {
                "height": 82,
                "title": title,
                "yMin": lo,
                "yMax": hi,
                "yTicks": two_ticks(lo, hi, fmt),
                "layers": [
                    era_rects,
                    *band_and_line(series, tone),
                    _quarter_points(series, tone, 3, tip),
                ],
            }
        )
    return {
        "label": "Practice cadence, longest sustained note, and CPPS voice clarity on one shared "
        "time axis",
        "xMin": min_i,
        "xMax": max_i + 1,
        "xTicks": [
            {"value": month_idx(m["month"]), "label": m["month"][:4]}
            for m in months
            if m["month"][5:7] == "01"
        ],
        "panels": panels,
    }


def era_buttons(eras: Sequence[Mapping[str, Any]], playback: bool) -> list[Json]:
    return [
        {
            "index": n,
            "text": f"▶ {era_label(era)} · {js_num(era['totalTakes'])} takes",
            "enabled": playback and len(era.get("sampleRecordingIds") or []) > 0,
            "tooltip": f"Hear {len(era.get('sampleRecordingIds') or [])} takes from this era"
            if playback
            else "Playback needs the release-gated launch (corpus root + approval)",
        }
        for n, era in enumerate(eras)
    ]


CAUSE_COLUMNS: Final = (
    {"key": "quarter", "title": "Quarter", "width": 90},
    {"key": "takes", "title": "Takes", "width": 70, "kind": "number"},
    {"key": "sustain", "title": "Sustain s", "width": 90, "kind": "number"},
    {"key": "cpps", "title": "CPPS", "width": 80, "kind": "number"},
    {"key": "hnr", "title": "HNR", "width": 80, "kind": "number"},
)


def cause_table(quarters: Sequence[Mapping[str, Any]]) -> list[Json]:
    def cell(value: Any) -> str:
        return "" if value is None else js_num(value)

    return [
        {
            "_key": q["quarter"],
            "quarter": q["quarter"],
            "takes": js_num(q["nReliable"]),
            "sustain": cell(q.get("sustainSeconds")),
            "cpps": cell(q.get("cpps")),
            "hnr": cell(q.get("hnr")),
        }
        for q in quarters
    ]


# ---- the Vibrato Story (VibratoCharts.jsx) -------------------------------------------------


def _quarter_x_range(quarters: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    return quarter_mid_idx(quarters[0]["quarter"]) - 1, quarter_mid_idx(quarters[-1]["quarter"]) + 2


def _q1_ticks(quarters: Sequence[Mapping[str, Any]], label: Callable[[str], str]) -> list[Json]:
    return [
        {"value": quarter_mid_idx(q["quarter"]) + 0.5, "label": label(q["quarter"])}
        for q in quarters
        if q["quarter"].endswith("Q1")
    ]


def vibrato_triptych(quarters: Sequence[Mapping[str, Any]]) -> Spec | None:
    if not quarters:
        return None
    x_min, x_max = _quarter_x_range(quarters)

    def share(q: Mapping[str, Any]) -> Any:
        value = _get(q, "vibrato", "timeShare")
        return None if value is None else value * 100

    specs: tuple[
        tuple[str, str, Callable[[Mapping[str, Any]], Any], Callable[[float], str], bool, float],
        ...,
    ] = (
        (
            "rate (Hz)",
            "rate",
            lambda q: _get(q, "vibrato", "rateHz"),
            lambda v: js_fixed(v, 1),
            True,
            1,
        ),
        (
            "extent (cents)",
            "extent",
            lambda q: _get(q, "vibrato", "extentCents"),
            lambda v: f"{js_round(v)}c",
            False,
            1,
        ),
        ("share of sustained time", "timeShare", share, lambda v: f"{js_round(v)}%", False, 100),
    )
    panels = []
    for title, key, pick, fmt, zone, scale in specs:
        lo, hi = _domain_with_spread(quarters, pick, key, scale, (6.4,) if zone else ())
        series = quarter_series(quarters, pick, key, scale)

        def tip(p: Mapping[str, Any], fmt: Callable[[float], str] = fmt) -> str:
            return f"{p['q']['quarter']} · {fmt(p['value'])} · {p['q']['nReliable']} takes"

        layers: list[Json] = []
        if zone:
            layers.append(
                {
                    "kind": "zone",
                    "y0": 5,
                    "y1": 6,
                    "tone": ZONE,
                    "opacity": 0.12,
                    "label": "settled 5–6 Hz",
                }
            )
        layers += [*band_and_line(series, BRASS), _quarter_points(series, BRASS, 3, tip)]
        panels.append(
            {
                "height": 84,
                "title": title,
                "yMin": lo,
                "yMax": hi,
                "yTicks": two_ticks(lo, hi, fmt),
                "layers": layers,
            }
        )
    return {
        "label": "Vibrato rate, extent, and time share per quarter with interquartile bands",
        "xMin": x_min,
        "xMax": x_max,
        "padRight": 96,
        "xTicks": _q1_ticks(quarters, lambda quarter: quarter[:4]),
        "panels": panels,
    }


def fingerprint(takes: Sequence[Mapping[str, Any]], span: tuple[int, int]) -> Spec | None:
    """Every take's extent against its rate, brass by year, hollow when flagged; a dot's id
    is its recording (the take inspector)."""
    if not takes:
        return None
    x_hi = min(150.0, max(t["extent"] for t in takes) * 1.04)
    y_lo, y_hi = 2.5, 8.5
    points = []
    for t in takes:
        if t["rate"] < y_lo or t["rate"] > y_hi or t["extent"] > x_hi:
            continue
        flagged = "" if t["reliable"] else " · flagged"
        points.append(
            {
                "x": t["extent"],
                "y": t["rate"],
                "r": 3.2,
                "shade": year_shade(t["year"], span[0], span[1]),
                "hollow": not t["reliable"],
                "id": t["id"],
                "tooltip": f"{t['date']} · {js_num(t['rate'])} Hz · {js_round(t['extent'])}c"
                f"{flagged}\nclick to inspect",
            }
        )
    return {
        "label": "Scatter of vibrato extent versus rate for every take, colored by year",
        "xMin": 0,
        "xMax": x_hi,
        "xTitle": "extent (cents)",
        "xTicks": [{"value": v, "label": str(v)} for v in (0, 50, 100, 150) if v <= x_hi],
        "panels": [
            {
                "height": 280,
                "title": "rate (Hz)",
                "yMin": y_lo,
                "yMax": y_hi,
                "grid": True,
                "yTicks": [{"value": v, "label": str(v)} for v in (3, 4, 5, 6, 7, 8)],
                "layers": [
                    {"kind": "zone", "y0": 5, "y1": 6, "tone": ZONE, "opacity": 0.10},
                    {"kind": "points", "tone": BRASS, "radius": 3.2, "points": points},
                ],
            }
        ],
    }


def year_legend(years: Sequence[str], span: tuple[int, int]) -> list[Json]:
    return [{"year": y, "shade": year_shade(y, span[0], span[1]), "tone": BRASS} for y in years]


# ---- What Not To Trust (MiniLinePanel.jsx) -------------------------------------------------


def mini_line_panel(
    title: str,
    quarters: Sequence[Mapping[str, Any]],
    series_key: str,
    pick: Callable[[Mapping[str, Any]], Any],
    tone: str,
    fmt: Callable[[float], str],
) -> Spec | None:
    if not quarters:
        return None
    x_min, x_max = _quarter_x_range(quarters)
    lo, hi = _domain_with_spread(quarters, pick, series_key)
    series = quarter_series(quarters, pick, series_key)

    def tip(p: Mapping[str, Any]) -> str:
        return f"{p['q']['quarter']} · {fmt(p['value'])}"

    return {
        "label": title,
        "xMin": x_min,
        "xMax": x_max,
        "padLeft": 46,
        "padRight": 14,
        "xTicks": _q1_ticks(quarters, lambda quarter: quarter[2:4]),
        "panels": [
            {
                "height": 110,
                "title": title,
                "yMin": lo,
                "yMax": hi,
                "yTicks": two_ticks(lo, hi, fmt),
                "layers": [*band_and_line(series, tone), _quarter_points(series, tone, 2.6, tip)],
            }
        ],
    }


def trust_panels(
    quarters: Sequence[Mapping[str, Any]],
    tuning_quarters: Sequence[Mapping[str, Any]],
    journeys: Mapping[str, Any] | None,
) -> list[Json]:
    panels = [
        mini_line_panel(
            "CPPS (dB) — tracks the voice",
            quarters,
            "cpps",
            lambda q: q.get("cpps"),
            BRASS,
            lambda v: js_fixed(v, 1),
        ),
        mini_line_panel(
            "HNR (dB) — tracks the microphones",
            quarters,
            "hnr",
            lambda q: q.get("hnr"),
            BLUE,
            lambda v: js_fixed(v, 1),
        ),
    ]
    if journeys:
        panels.append(
            mini_line_panel(
                "note-core tuning error (cents) — now measured",
                tuning_quarters,
                "nc",
                lambda q: q.get("nc"),
                GREEN,
                lambda v: f"{js_fixed(v, 1)}c",
            )
        )
    return [{"title": p["label"], "spec": p} for p in panels if p is not None]


def tuning_tile(journeys: Mapping[str, Any] | None, overall: Mapping[str, Any]) -> Json:
    if journeys and overall.get("median") is not None:
        return {
            "title": "Tuning — now measured on note cores.",
            "text": f"{thousands(overall['takeCount'])} takes scored on stable note medians only "
            "(slides and melisma excluded by construction): median error "
            f"{js_fixed(overall['median'], 1)} cents, steady across seven years. The green "
            "panel above is the real instrument; the old every-frame metric is retired.",
        }
    return {
        "title": "Tuning accuracy — not yet meaningful.",
        "text": "Note-core scoring arrives with the same-song analysis (the journeys stage of "
        "the PC's pipeline).",
    }


# ---- Song Scorecards (SmallCharts.jsx: SparklineChart) -------------------------------------


def song_name(cluster_id: str, clusters: Sequence[Mapping[str, Any]]) -> str:
    for index, cluster in enumerate(clusters):
        if cluster.get("id") == cluster_id:
            return f"Song {chr(65 + index)}"
    return cluster_id.replace("lyric-", "", 1)


def sparkline(per_year: Mapping[str, Any], value_key: str) -> Spec | None:
    years = sorted(per_year)
    values = [per_year[y].get(value_key) for y in years if per_year[y].get(value_key) is not None]
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    if hi - lo < 0.001:
        lo, hi = lo - 1, hi + 1
    return {
        "label": "note-core tuning error by year",
        "compact": True,
        "xMin": 0,
        "xMax": len(values) - 1,
        "padLeft": 4,
        "padRight": 4,
        "panels": [
            {
                "height": 26,
                "yMin": lo,
                "yMax": hi,
                "layers": [
                    {
                        "kind": "line",
                        "tone": BRASS,
                        "width": 2,
                        "points": [[i, v] for i, v in enumerate(values)],
                    }
                ],
            }
        ],
    }


def slope_arrow(value: Any, down_good: bool) -> Json:
    """``SlopeArrow``: ▼/▲/▬ and the slope, toned good, bad, or steady (|v| < 0.05)."""
    if value is None:
        return {"text": "—", "tone": "muted"}
    good = value < 0 if down_good else value > 0
    arrow = "▼" if value < 0 else "▲" if value > 0 else "▬"
    tone = "muted" if abs(value) < 0.05 else "ok" if good else "danger"
    return {"text": f"{arrow} {js_num(value)}/yr", "tone": tone}


def scorecard_rows(
    journeys: Mapping[str, Any], clusters: Sequence[Mapping[str, Any]]
) -> list[Json]:
    rows = []
    for journey in song_scorecards(journeys):
        slopes = journey.get("slopesPerYear") or {}
        rows.append(
            {
                "clusterId": journey["clusterId"],
                "name": song_name(journey["clusterId"], clusters),
                "takes": f"{js_num(journey['takesUsed'])} takes",
                "years": f"{journey.get('firstYear')}–{journey.get('lastYear')}",
                "spark": sparkline(journey.get("perYear") or {}, "noteCoreCentError"),
                "tuning": slope_arrow(slopes.get("noteCoreCentError"), True),
                "clarity": slope_arrow(slopes.get("cpps"), False),
            }
        )
    return rows


# ---- Repertoire Threads (ThreadsChart.jsx) -------------------------------------------------


def threads(clusters: Sequence[Mapping[str, Any]]) -> Spec | None:
    """One row per song (Song A at the top), its span, a dot per year sized by takes; a dot's
    id is its cluster (the Corpus click-through)."""
    if not clusters:
        return None
    min_i = min(month_idx(t["first"]) for t in clusters) - 2
    max_i = max(month_idx(t["last"]) for t in clusters) + 2
    rows = len(clusters)
    layers: list[Json] = [
        {"kind": "vrules", "xs": [year * 12 for year in range(-(-min_i // 12), max_i // 12 + 1)]}
    ]
    dots = []
    labels, sizes = [], []
    for index, thread in enumerate(clusters):
        y = rows - 1 - index
        song = f"Song {chr(65 + index)}"
        labels.append({"value": y, "label": song})
        sizes.append(
            {
                "value": y,
                "label": f"{js_num(thread['size'])} takes · "
                f"{thread['first'][:4]}–{thread['last'][2:4]}",
            }
        )
        layers.append(
            {
                "kind": "line",
                "tone": "line",
                "width": 2,
                "points": [[month_idx(thread["first"]), y], [month_idx(thread["last"]), y]],
            }
        )
        for year in sorted(thread.get("perYear") or {}):
            count = thread["perYear"][year]
            plural = "" if count == 1 else "s"
            dots.append(
                {
                    "x": int(year) * 12 + 5.5,
                    "y": y,
                    "r": max(3.5, math.sqrt(count) * 1.9),
                    "id": thread["id"],
                    "tooltip": f"{song} · {year} · {count} take{plural}\nclick to open in Corpus",
                }
            )
    layers.append({"kind": "points", "tone": BRASS, "points": dots})
    return {
        "label": "Recurring song clusters as timeline threads with takes per year",
        "xMin": min_i,
        "xMax": max_i + 1,
        "padLeft": 78,
        "padRight": 150,
        "xTicks": [
            {"value": year * 12, "label": str(year)}
            for year in range(-(-min_i // 12), max_i // 12 + 1)
        ],
        "panels": [
            {
                "height": rows * 40,
                "title": "",
                "yMin": -0.5,
                "yMax": rows - 0.5,
                "yTicks": labels,
                "yTicksRight": sizes,
                "layers": layers,
            }
        ],
    }


# ---- the take inspector (TakeInspectorSheet, F0ContourChart) -------------------------------


def inspector_title(detail: Mapping[str, Any] | None, recording_id: str) -> str:
    if not detail:
        return f"Take {recording_id}"
    captured = str(detail.get("captured_at") or "")
    return f"{detail.get('filename')} · {captured[:10]}"


def inspector_tiles(detail: Mapping[str, Any]) -> list[Json]:
    summary = detail.get("summary") or {}
    pitch = summary.get("pitch") or {}
    vibrato = summary.get("vibrato") or {}
    quality = summary.get("quality") or {}
    phrasing = summary.get("phrasing") or {}
    p50 = _get(pitch, "f0Hz", "p50")
    span = pitch.get("rangeSemitonesP05P95")
    rate = vibrato.get("meanRateHz")
    cpps = quality.get("cpps")
    sustain = phrasing.get("longestSustainedSeconds")
    return [
        {
            "label": "median pitch",
            "value": f"{note_name(p50)} ({js_num(p50)} Hz)" if truthy(p50) else "—",
        },
        {"label": "range p05–p95", "value": "—" if span is None else js_num(span)},
        {
            "label": "vibrato",
            "value": f"{js_num(rate)} Hz · {js_round(vibrato.get('meanExtentCents') or 0)}c"
            if truthy(rate)
            else "—",
        },
        {"label": "CPPS", "value": "—" if cpps is None else f"{js_num(cpps)} dB"},
        {"label": "longest sustain", "value": f"{js_num(sustain)} s" if truthy(sustain) else "—"},
        {
            "label": "bucket",
            "value": "—" if detail.get("bucket") is None else str(detail["bucket"]),
        },
    ]


def f0_contour(contour: Any) -> Spec | None:
    """The take's voiced f0 every 50 ms, on a log-frequency axis with C and G gridlines."""
    if not isinstance(contour, list) or len(contour) < 2:
        return None
    times = [float(p[0]) for p in contour]
    freqs = [float(p[1]) for p in contour]
    t_hi = max(times)
    f_lo, f_hi = min(freqs) * 0.94, max(freqs) * 1.06
    grid = []
    midi = math.ceil(69 + 12 * math.log2(f_lo / 440))
    while midi <= math.floor(69 + 12 * math.log2(f_hi / 440)):
        name = NOTE_NAMES[midi % 12]
        if name in ("C", "G"):
            grid.append({"value": midi_hz(midi), "label": name + str(midi // 12 - 1)})
        midi += 1
    return {
        "label": "f0 contour of this take",
        "xMin": 0,
        "xMax": t_hi,
        "padLeft": 44,
        "padRight": 12,
        "xTicks": [{"value": t_hi, "label": f"{js_round(t_hi)} s"}],
        "panels": [
            {
                "height": 160,
                "title": "",
                "yScale": "log2",
                "yMin": f_lo,
                "yMax": f_hi,
                "grid": True,
                "yTicks": grid,
                "layers": [
                    {
                        "kind": "points",
                        "tone": BRASS,
                        "radius": 1.6,
                        "points": [
                            {"x": t, "y": f, "opacity": 1.0}
                            for t, f in zip(times, freqs, strict=True)
                        ],
                    }
                ],
            }
        ],
    }
