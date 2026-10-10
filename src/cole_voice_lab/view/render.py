"""The web view's page shell and value formatting: one stylesheet, no scripts.

Every value that reaches a page goes through :func:`esc` (``html.escape`` with quotes),
and every query parameter in a link through :func:`href` (percent-encoded, then
escaped). The stylesheet is inline (the page's Content-Security-Policy allows inline
styles and nothing else) and themes light and dark with ``prefers-color-scheme``.
"""

from __future__ import annotations

import html
import math
from collections.abc import Iterable, Sequence
from typing import Any, Final
from urllib.parse import urlencode

from ..journey import jscompat as js

ROOT: Final = "/view/"
PAGES: Final = (
    ("summary", "/view/", "Summary"),
    ("recordings", "/view/recordings", "Recordings"),
    ("journeys", "/view/journeys", "Journeys"),
    ("trends", "/view/trends", "Trends"),
    ("goals", "/view/goals", "Goals"),
    ("report", "/view/report", "Report"),
)
DASH: Final = "—"

BUCKET_LABELS: Final = {
    "clean_singing": "clean singing",
    "noise_contaminated_singing": "singing + noise",
    "music_contaminated_singing": "singing + music",
    "non_singing": "not singing",
    "uncertain_manual_review": "uncertain",
    "still_uncertain": "still uncertain",
    "unclassified": "unclassified",
}

STYLE: Final = """
:root {
  color-scheme: light dark;
  --bg: #f6f6f4; --card: #fcfcfb; --ink: #0b0b0b; --muted: #52514e; --line: #d9d8d3;
  --accent: #2a78d6; --accent-soft: #b7d3f6; --good: #008300; --bad: #e34948;
  --chip: #eeede9;
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121211; --card: #1a1a19; --ink: #ffffff; --muted: #c3c2b7; --line: #383835;
    --accent: #3987e5; --accent-soft: #184f95; --good: #3fa34d; --bad: #e66767;
    --chip: #262624;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); line-height: 1.45; }
header.top { padding: 14px 16px 0; max-width: 900px; margin: 0 auto; }
header.top h1 { margin: 0; font-size: 1.15rem; letter-spacing: -0.01em; }
nav { display: flex; flex-wrap: wrap; gap: 4px 6px; margin: 10px 0 0; }
nav a { color: var(--muted); text-decoration: none; font-size: 0.9rem; padding: 4px 10px;
  border-radius: 99px; border: 1px solid var(--line); }
nav a[aria-current="page"] { color: var(--ink); border-color: var(--accent); }
main { padding: 14px 16px 32px; display: grid; gap: 14px; max-width: 900px; margin: 0 auto; }
h2 { margin: 0 0 6px; font-size: 1.05rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px 16px; min-width: 0; }
.card p { margin: 6px 0; }
.note, .meta { color: var(--muted); font-size: 0.85rem; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(8.5rem, 1fr));
  gap: 8px; margin-top: 10px; }
.tile { border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; }
.tile .v { font-size: 1.3rem; font-weight: 650; }
.tile .l { font-size: 0.75rem; color: var(--muted); }
.num { font-variant-numeric: tabular-nums; }
.scroller { overflow-x: auto; }
table { border-collapse: collapse; font-size: 0.85rem; width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left;
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
td:first-child { white-space: nowrap; }
td.num, th.num { text-align: right; }
.chip { display: inline-block; font-size: 0.72rem; font-weight: 600; padding: 2px 8px;
  border-radius: 99px; border: 1px solid var(--line); background: var(--chip);
  color: var(--ink); margin: 1px 2px 1px 0; }
.chip.improving, .chip.achieved { border-color: var(--good); }
.chip.declining, .chip.missed { border-color: var(--bad); }
.track { width: 7rem; margin-top: 5px; }
.bar { height: 8px; border-radius: 4px; background: var(--accent); min-width: 2px; }
.pager { display: flex; justify-content: space-between; gap: 8px; margin-top: 10px; }
a { color: var(--accent); }
svg { max-width: 100%; height: auto; display: block; }
svg text { font: 11px ui-sans-serif, system-ui, sans-serif; fill: var(--muted); }
svg .grid { stroke: var(--line); stroke-width: 1; }
svg .series { stroke: var(--accent); stroke-width: 2; fill: none;
  stroke-linejoin: round; stroke-linecap: round; }
svg .dot { fill: var(--accent); stroke: var(--card); stroke-width: 2; }
svg .band { fill: var(--accent-soft); }
svg .core { fill: var(--accent); }
svg .median { stroke: var(--ink); stroke-width: 2; }
svg .col { fill: var(--accent); }
.report { display: grid; gap: 14px; }
.report .card { overflow-x: auto; }
.report svg { min-width: 520px; }
.report .lede { font-size: 1rem; }
.report .vrow { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: center;
  border: 1px solid var(--line); border-radius: 9px; padding: 8px 10px; font-size: 0.85rem;
  margin-bottom: 6px; }
.report .vrow > span:first-child { flex: 1 1 100%; }
.report small { color: var(--muted); }
.report table { width: auto; }
.report th, .report td { border: 1px solid var(--line); }
.report ul { padding-left: 1.2rem; }
.report svg polyline { stroke: var(--accent); }
.report svg circle { fill: var(--accent); stroke: var(--card); }
.report svg rect { fill: var(--accent); }
.report svg line { stroke: var(--line); }
.report svg line[stroke-width="6"] { stroke: var(--accent); }
"""


def esc(value: Any) -> str:
    """Any value as page text: ``String(value)`` for JSON values, HTML-escaped (quotes
    too, so it is safe in an attribute)."""

    text = value if isinstance(value, str) else js.to_string(value)
    return html.escape(text, quote=True)


def href(path: str, params: Iterable[tuple[str, Any]] = ()) -> str:
    """A link to ``path`` with ``params`` (``None`` values dropped), escaped for an
    attribute."""

    kept = [(name, js.to_string(value)) for name, value in params if value is not None]
    query = urlencode(kept)
    return esc(f"{path}?{query}" if query else path)


def is_number(value: Any) -> bool:
    return js.is_number(value) and math.isfinite(float(value))


def fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    """A number with at most ``digits`` decimals (trailing zeros dropped), escaped;
    ``—`` for anything that is not a finite number."""

    if not is_number(value):
        return DASH
    text = f"{float(value):,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return esc(f"{'0' if text == '-0' else text}{suffix}")


def fmt_signed(value: Any, digits: int = 2, suffix: str = "") -> str:
    text = fmt(value, digits, suffix)
    return f"+{text}" if is_number(value) and float(value) > 0 else text


def duration(seconds: Any) -> str:
    """``m:ss`` (or ``h:mm:ss``), ``—`` when unknown."""

    if not is_number(seconds) or float(seconds) < 0:
        return DASH
    total = int(round(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def date_of(value: Any) -> str:
    """The ``YYYY-MM-DD`` of an ISO timestamp, escaped; ``—`` when absent."""

    if not isinstance(value, str) or not value:
        return DASH
    return esc(value[:10])


def bucket_label(bucket: Any) -> str:
    text = js.to_string(bucket)
    return esc(BUCKET_LABELS.get(text, text.replace("_", " ")))


def chip(text: str, kind: Any = "") -> str:
    """A labelled chip; ``kind`` is a class (escaped), ``text`` is already HTML."""

    classes = f"chip {esc(kind)}" if kind else "chip"
    return f'<span class="{classes}">{text}</span>'


def tiles(entries: Sequence[tuple[str, str]]) -> str:
    """Stat tiles; each ``(value, label)`` is already HTML."""

    body = "".join(
        f'<div class="tile"><div class="v num">{value}</div><div class="l">{label}</div></div>'
        for value, label in entries
    )
    return f'<div class="stats">{body}</div>'


def table(head: Sequence[tuple[str, bool]], rows: Sequence[Sequence[str]]) -> str:
    """A scrollable table; ``head`` is ``(label, numeric)``; cells are already HTML."""

    numeric = [is_num for _, is_num in head]
    header = "".join(
        f'<th{' class="num"' if is_num else ""} scope="col">{label}</th>' for label, is_num in head
    )
    body = "".join(
        "<tr>"
        + "".join(
            f"<td{' class="num"' if numeric[index] else ''}>{cell}</td>"
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<div class="scroller"><table><thead><tr>{header}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def card(title: str, body: str, *, extra_class: str = "") -> str:
    classes = f"card {extra_class}" if extra_class else "card"
    return f'<section class="{classes}"><h2>{title}</h2>{body}</section>'


def page(title: str, active: str | None, body: str) -> str:
    """The whole document: the shell, the navigation and ``body`` (HTML)."""

    links = "".join(
        f'<a href="{esc(path)}"{' aria-current="page"' if key == active else ""}>{esc(label)}</a>'
        for key, path, label in PAGES
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{esc(title)} · Voice Lab</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n"
        f'<header class="top"><h1>Voice Lab</h1><nav aria-label="Pages">{links}</nav></header>\n'
        f"<main>\n{body}\n</main>\n</body>\n</html>\n"
    )


def message_page(title: str, text: str) -> str:
    """A page that says one thing (a refusal or an empty state); ``text`` is plain."""

    return page(title, None, card(esc(title), f"<p>{esc(text)}</p>"))
