"""Inline SVG charts for the web view: one series each, themed by the page's CSS.

Marks carry classes (``series``, ``dot``, ``band``, ``core``, ``median``, ``col``,
``grid``), never colours, so the stylesheet themes them for light and dark. Each mark
has a ``<title>`` (the value, as a native tooltip) and each chart an ``aria-label``;
the pages put a table next to the charts that carry numbers. Only the first and last
points are labelled directly. No scripts.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from .render import esc, fmt

WIDTH: Final = 420
NOTE_NAMES: Final = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def _c(value: float) -> str:
    """A coordinate, to two decimals."""

    return f"{value:.2f}".rstrip("0").rstrip(".")


def _open(width: float, height: float, label: str) -> str:
    return (
        f'<svg width="{_c(width)}" height="{_c(height)}" viewBox="0 0 {_c(width)} {_c(height)}" '
        f'role="img" aria-label="{esc(label)}"><title>{esc(label)}</title>'
    )


def note_name(hz: float) -> str:
    midi = round(69 + 12 * math.log2(hz / 440))
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


@dataclass(frozen=True)
class LinePoint:
    label: str
    value: float | None
    tick: str = ""


def line_chart(
    label: str,
    points: Sequence[LinePoint],
    *,
    format_value: Callable[[float], str] = fmt,
    height: float = 170,
) -> str:
    """A line over evenly spaced categories (quarters, years); gaps for missing values.
    Empty when fewer than two points have a value."""

    valued = [point.value for point in points if point.value is not None]
    if len(valued) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 14, 16, 24
    plot_w, plot_h = WIDTH - pad_l - pad_r, height - pad_t - pad_b
    low, high = min(valued), max(valued)
    ticks = [low] if high == low else [low, (low + high) / 2, high]
    pad = max((high - low) * 0.12, 0.001)
    lo, hi = low - pad, high + pad
    step = plot_w / max(len(points) - 1, 1)

    def x(index: int) -> float:
        return pad_l + index * step

    def y(value: float) -> float:
        return pad_t + plot_h - (value - lo) / (hi - lo) * plot_h

    body = _open(WIDTH, height, label)
    for tick in ticks:
        body += (
            f'<line class="grid" x1="{_c(pad_l)}" x2="{_c(WIDTH - pad_r)}" '
            f'y1="{_c(y(tick))}" y2="{_c(y(tick))}"/>'
            f'<text x="{_c(pad_l - 6)}" y="{_c(y(tick) + 4)}" text-anchor="end" class="num">'
            f"{format_value(tick)}</text>"
        )
    runs: list[list[str]] = [[]]
    for index, point in enumerate(points):
        if point.value is None:
            runs.append([])
        else:
            runs[-1].append(f"{_c(x(index))},{_c(y(point.value))}")
    for run in runs:
        if len(run) > 1:
            body += f'<polyline class="series" points="{" ".join(run)}"/>'
    labelled = [index for index, point in enumerate(points) if point.value is not None]
    ends = {labelled[0], labelled[-1]}
    for index, point in enumerate(points):
        if point.tick:
            body += (
                f'<text x="{_c(x(index))}" y="{_c(height - 6)}" text-anchor="middle">'
                f"{esc(point.tick)}</text>"
            )
        if point.value is None:
            continue
        shown = format_value(point.value)
        body += (
            f'<circle class="dot" cx="{_c(x(index))}" cy="{_c(y(point.value))}" r="4">'
            f"<title>{esc(point.label)}: {shown}</title></circle>"
        )
        if index in ends:
            anchor = "start" if index == labelled[0] else "end"
            body += (
                f'<text x="{_c(x(index))}" y="{_c(y(point.value) - 9)}" '
                f'text-anchor="{anchor}" class="num">{shown}</text>'
            )
    return f"{body}</svg>"


@dataclass(frozen=True)
class RangeBand:
    """One category's pitch range: the outer envelope, the typical (inner) range and
    the median, in Hz (any may be missing)."""

    label: str
    tick: str
    outer: tuple[float, float] | None
    inner: tuple[float, float] | None
    median: float | None


def range_chart(label: str, bands: Sequence[RangeBand], *, height: float = 230) -> str:
    """Vertical ranges on a log-frequency axis with note names at each C and G. Empty
    when no band has a value."""

    values = [
        value
        for band in bands
        for pair in (band.outer, band.inner)
        if pair is not None
        for value in pair
        if value > 0
    ] + [band.median for band in bands if band.median is not None and band.median > 0]
    if not values:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 10, 10, 24
    plot_w, plot_h = WIDTH - pad_l - pad_r, height - pad_t - pad_b
    log_lo, log_hi = math.log2(min(values) * 0.92), math.log2(max(values) * 1.08)

    def y(hz: float) -> float:
        return pad_t + plot_h - (math.log2(hz) - log_lo) / (log_hi - log_lo) * plot_h

    slot = plot_w / len(bands)
    width = max(min(slot * 0.6, 18), 2)
    body = _open(WIDTH, height, label)
    first = math.ceil(69 + 12 * (log_lo - math.log2(440)))
    last = math.floor(69 + 12 * (log_hi - math.log2(440)))
    for midi in range(first, last + 1):
        if midi % 12 not in (0, 7):
            continue
        hz = 440 * 2 ** ((midi - 69) / 12)
        body += (
            f'<line class="grid" x1="{_c(pad_l)}" x2="{_c(WIDTH - pad_r)}" '
            f'y1="{_c(y(hz))}" y2="{_c(y(hz))}"/>'
            f'<text x="{_c(pad_l - 6)}" y="{_c(y(hz) + 4)}" text-anchor="end">'
            f"{note_name(hz)}</text>"
        )
    for index, band in enumerate(bands):
        cx = pad_l + index * slot + slot / 2
        if band.outer is not None and min(band.outer) > 0:
            low, high = band.outer
            body += (
                f'<rect class="band" x="{_c(cx - width / 2)}" y="{_c(y(high))}" '
                f'width="{_c(width)}" height="{_c(max(y(low) - y(high), 1))}" rx="3">'
                f"<title>{esc(band.label)}: envelope {fmt(low)}–{fmt(high)} Hz</title></rect>"
            )
        if band.inner is not None and min(band.inner) > 0:
            low, high = band.inner
            body += (
                f'<rect class="core" x="{_c(cx - width / 4)}" y="{_c(y(high))}" '
                f'width="{_c(width / 2)}" height="{_c(max(y(low) - y(high), 1))}" rx="2">'
                f"<title>{esc(band.label)}: typical {fmt(low)}–{fmt(high)} Hz</title></rect>"
            )
        if band.median is not None and band.median > 0:
            body += (
                f'<line class="median" x1="{_c(cx - width / 2)}" x2="{_c(cx + width / 2)}" '
                f'y1="{_c(y(band.median))}" y2="{_c(y(band.median))}">'
                f"<title>{esc(band.label)}: median {fmt(band.median)} Hz "
                f"({note_name(band.median)})</title></line>"
            )
        if band.tick:
            body += (
                f'<text x="{_c(cx)}" y="{_c(height - 6)}" text-anchor="middle">'
                f"{esc(band.tick)}</text>"
            )
    return f"{body}</svg>"


@dataclass(frozen=True)
class ColumnValue:
    label: str
    value: float
    tick: str = ""


def column_chart(label: str, columns: Sequence[ColumnValue], *, height: float = 130) -> str:
    """Counts as columns (recordings per month or year). Empty without columns."""

    if not columns:
        return ""
    pad_l, pad_r, pad_t, pad_b = 8, 8, 16, 22
    plot_w, plot_h = WIDTH - pad_l - pad_r, height - pad_t - pad_b
    top = max(max(column.value for column in columns), 1)
    slot = plot_w / len(columns)
    gap = 2 if slot > 4 else 0
    body = _open(WIDTH, height, label)
    body += (
        f'<line class="grid" x1="{_c(pad_l)}" x2="{_c(WIDTH - pad_r)}" '
        f'y1="{_c(pad_t + plot_h)}" y2="{_c(pad_t + plot_h)}"/>'
    )
    peak = max(range(len(columns)), key=lambda index: columns[index].value)
    for index, column in enumerate(columns):
        h = column.value / top * plot_h
        x = pad_l + index * slot
        if h > 0:
            body += (
                f'<rect class="col" x="{_c(x + gap / 2)}" y="{_c(pad_t + plot_h - h)}" '
                f'width="{_c(max(slot - gap, 1))}" height="{_c(h)}" rx="{_c(min(2, slot / 4))}">'
                f"<title>{esc(column.label)}: {fmt(column.value, 0)}</title></rect>"
            )
        if index == peak and h > 0:
            body += (
                f'<text x="{_c(x + slot / 2)}" y="{_c(pad_t + plot_h - h - 4)}" '
                f'text-anchor="middle" class="num">{fmt(column.value, 0)}</text>'
            )
        if column.tick:
            body += (
                f'<text x="{_c(x)}" y="{_c(height - 6)}" text-anchor="start">'
                f"{esc(column.tick)}</text>"
            )
    return f"{body}</svg>"
