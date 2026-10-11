"""Analysis → Journey (checklist J-01 to J-22) over a real /api/v1 on synthetic manifests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import Window, make_rig, open_window
from journey_browser_fixtures import MANIFEST_NAMES, feature_row
from PySide6 import QtQuick  # noqa: F401 - converts QQuickItem* properties
from synthetic import audio_root, config_for, data_root, takes, tone_m4a
from voice_lab_desktop import journey_charts as charts
from voice_lab_desktop.education import EDUCATION, card, plain
from voice_lab_desktop.screens.journey import FEATURE_STORE_MISSING, TRENDS_UNAVAILABLE

from cole_voice_lab.journey.corpus import recording_id

Json = dict[str, Any]


# ---- helpers -------------------------------------------------------------------------------


def call(item: Any, name: str, *args: Any) -> Any:
    """A QML function of ``item`` (untyped JS functions take and return QVariant)."""
    from PySide6.QtCore import Q_ARG, Q_RETURN_ARG, QMetaObject, Qt

    return QMetaObject.invokeMethod(
        item,
        name,
        Qt.ConnectionType.DirectConnection,
        Q_RETURN_ARG("QVariant"),
        *[Q_ARG("QVariant", arg) for arg in args],
    )


def named(window: Window, name: str) -> list[Any]:
    """Every item of the window's visual tree named ``name`` (Repeater delegates are not
    QObject children of the window, so ``findChildren`` misses them)."""
    from PySide6.QtQuick import QQuickItem

    found: list[Any] = []
    stack: list[QQuickItem] = [window.root.property("contentItem")]
    while stack:
        item = stack.pop()
        if item.objectName() == name:
            found.append(item)
        stack.extend(reversed(item.childItems()))
    return found


def texts(window: Window, name: str) -> list[str]:
    return [item.property("text") for item in named(window, name)]


def one(window: Window, name: str) -> Any:
    found = named(window, name)
    assert len(found) == 1, (name, len(found))
    return found[0]


def loaded(qtbot: Any, window: Window) -> Any:
    model = window.model.journey
    window.model.navigate("journey")
    qtbot.waitUntil(lambda: model.state == "loaded", timeout=15000)
    return model


def show(qtbot: Any, window: Window, subject: str, chart: str) -> Any:
    window.model.journey.selectSubject(subject)
    item = window.find(chart)
    qtbot.waitUntil(lambda: item.property("width") > 100 and item.property("paintCount") > 0)
    qtbot.wait(50)  # let the layout settle (text wrapping above the chart moves it)
    return item


def scroll_to(qtbot: Any, window: Window, item: Any) -> None:
    """Scroll the screen so ``item``'s top is near the window's top."""
    from PySide6.QtCore import QPointF

    flick = window.find("journeyScreen")
    top = item.mapToItem(flick.property("contentItem"), QPointF(0, 0)).y()
    flick.setProperty("contentY", max(0.0, top - 40))
    qtbot.wait(50)


def at_scene(item: Any, x: float, y: float) -> Any:
    from PySide6.QtCore import QPoint, QPointF

    scene = item.mapToScene(QPointF(x, y))
    return QPoint(round(scene.x()), round(scene.y()))


def hover(qtbot: Any, window: Window, item: Any, x: float, y: float) -> None:
    from PySide6.QtTest import QTest

    QTest.mouseMove(window.root, at_scene(item, x, y))
    qtbot.wait(20)


def click(qtbot: Any, window: Window, item: Any, x: float, y: float) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(
        window.root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at_scene(item, x, y)
    )


def manifests(data: Path) -> dict[str, Json]:
    return {
        key: json.loads((data / "manifests" / name).read_text(encoding="utf-8"))
        for key, name in MANIFEST_NAMES.items()
        if (data / "manifests" / name).exists()
    }


def write(data: Path, key: str, payload: Json) -> None:
    (data / "manifests" / MANIFEST_NAMES[key]).write_text(json.dumps(payload), encoding="utf-8")


def era_root(root: Path) -> tuple[Path, Path, list[str]]:
    """The synthetic corpus plus a burst of 100 takes in May and June 2022 (two months of
    50: a high-practice era by the server's rule), with audio for each."""
    data = data_root(root)
    corpus = audio_root(root)
    found = manifests(data)
    tone = tone_m4a()
    for take in takes():  # an era's samples may be any reliable take of its months
        (corpus / take.filename).write_bytes(tone)
    burst = []
    for i in range(100):
        month = 5 + i // 50
        filename = f"2022{month:02d}15 {i:06d}-era.m4a"
        identifier = recording_id(filename)
        captured = f"2022-{month:02d}-15T10:{i % 60:02d}:{i // 60:02d}Z"
        burst.append(identifier)
        found["index"]["recordings"].append(
            {
                "recordingId": identifier,
                "filename": filename,
                "capturedAt": captured,
                "durationSeconds": 40,
                "file": {"sizeBytes": 2000},
            }
        )
        found["results"]["results"].append(
            {
                "recordingId": identifier,
                "filename": filename,
                "capturedAt": captured,
                "year": "2022",
                "features": {"durationSeconds": 40},
                "classification": {
                    "finalBucket": "clean_singing",
                    "confidence": 0.9,
                    "rationale": "x",
                },
                "humanSpotCheck": {"recommended": False, "status": "not_reviewed"},
            }
        )
        row = feature_row(
            identifier, captured, {"p05": 110, "p25": 170, "p50": 230, "p75": 290, "p95": 340}
        )
        row["year"] = "2022"
        found["features"]["results"].append(row)
        (corpus / filename).write_bytes(tone)
    for key in ("index", "results", "features"):
        write(data, key, found[key])
    return data, corpus, burst


@pytest.fixture
def journey_window(qtbot: Any, window: Window) -> Window:
    loaded(qtbot, window)
    return window


@contextmanager
def variant_window(
    qtbot: Any, tmp_path: Path, mutate: Any = None, **changes: Any
) -> Iterator[Window]:
    data = data_root(tmp_path / "journey")
    corpus = audio_root(tmp_path / "journey")
    if mutate is not None:
        mutate(data)
    with (
        make_rig(tmp_path, config_for(data, corpus=corpus, **changes)) as rig,
        open_window(rig) as window,
    ):
        yield window


# ---- J-01: states ---------------------------------------------------------------------------


@pytest.mark.checklist("J-01")
def test_journey_states(qtbot: Any, tmp_path: Path, window: Window) -> None:
    model = window.model.journey
    window.model.navigate("journey")
    assert model.state == "loading"
    assert window.find("journeyLoading").property("visible")
    qtbot.waitUntil(lambda: model.state == "loaded", timeout=15000)
    assert model.available and not window.find("journeyLoading").property("visible")
    assert window.find("statBand").property("visible")

    def no_features(data: Path) -> None:
        (data / "manifests" / MANIFEST_NAMES["features"]).unlink()

    with variant_window(qtbot, tmp_path / "nofeatures", no_features) as variant:
        bare = loaded(qtbot, variant)
        assert not bare.available and bare.notice == TRENDS_UNAVAILABLE
        assert variant.find("journeyNotice").property("visible")
        assert not variant.find("statBand").property("visible")
        assert bare.tabs == [] and bare.rangeSpec is None

    with make_rig(tmp_path / "off", None) as rig, open_window(rig) as off:
        off.model.navigate("journey")
        qtbot.waitUntil(lambda: off.model.journey.state == "failed", timeout=15000)
        assert "journey_unavailable" in off.model.journey.error
        assert off.find("journeyError").property("visible")


@pytest.mark.checklist("J-01")
def test_journey_needs_a_sign_in(qtbot: Any, tmp_path: Path, synthetic_root: Path) -> None:
    with (
        make_rig(tmp_path, config_for(synthetic_root), signed_in=False) as rig,
        open_window(rig) as w,
    ):
        w.model.navigate("journey")
        assert w.model.journey.state == "idle"
        from PySide6.QtCore import QObject

        needs = w.find("journeyScreen").findChild(QObject, "needsServer")
        assert needs.property("visible")
        assert not w.find("statBand").property("visible")


# ---- J-02, J-03: the stat band and include-flagged -----------------------------------------


@pytest.mark.checklist("J-02")
def test_journey_stat_tiles(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    assert model.statTiles == [
        {"value": "72", "label": "takes analyzed"},
        {"value": "1.2 h", "label": "singing measured"},
        {"value": "+3.9 st", "label": "working top, 2019 → 2024"},
        {"value": "66", "label": "reliable takes"},
        {"value": "6", "label": "flagged for listening"},
    ]
    values = texts(journey_window, "statValue")
    assert values == ["72", "1.2 h", "+3.9 st", "66", "6"]
    head = {
        "takesAnalyzed": 2433,
        "hoursAnalyzed": 41.0,
        "reliableTakes": 2100,
        "flaggedTakes": 333,
    }
    assert [t["value"] for t in charts.stat_tiles({"headline": head})] == [
        "2,433",
        "41 h",
        "2,100",
        "333",
    ]


@pytest.mark.checklist("J-03")
def test_journey_include_flagged(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    trends = model.trends
    model.selectSubject("vibrato")
    reliable_dots = model.fingerprintSpec["panels"][0]["layers"][1]["points"]
    assert len(reliable_dots) == 66 and not any(p["hollow"] for p in reliable_dots)
    box = journey_window.find("includeFlagged")
    assert box.property("checked") is False
    box.setProperty("checked", True)
    box.toggled.emit()
    qtbot.waitUntil(lambda: model.includeFlagged)
    dots = model.fingerprintSpec["panels"][0]["layers"][1]["points"]
    assert len(dots) == 72 and sum(p["hollow"] for p in dots) == 6
    assert charts.active_quarters(trends, True) == trends["quartersAllTakes"]
    assert charts.active_quarters(trends, False) == trends["quarters"]
    assert model.rangeSpec["panels"][0]["layers"][-1]["regions"][0]["tooltip"].startswith(
        "2019-Q1 · 3 reliable of 3 takes"
    )
    model.setIncludeFlagged(False)
    assert len(model.fingerprintSpec["panels"][0]["layers"][1]["points"]) == 66


# ---- J-04: the tabs -------------------------------------------------------------------------


@pytest.mark.checklist("J-04")
def test_journey_tabs_follow_availability(
    qtbot: Any, tmp_path: Path, journey_window: Window
) -> None:
    model = journey_window.model.journey
    keys = [t["key"] for t in model.tabs]
    assert keys == ["verdict", "range", "practice", "vibrato", "trust", "songs", "threads"]
    assert [t["label"] for t in model.tabs][:3] == ["The Verdict", "Range River", "Cause & Effect"]
    assert model.subject == "verdict" and journey_window.find("verdictCard").property("visible")
    one(journey_window, "tab-threads").clicked.emit()
    assert model.subject == "threads" and journey_window.find("threadsCard").property("visible")
    assert not journey_window.find("verdictCard").property("visible")
    # The tab is kept for the session: leave the screen and come back.
    journey_window.model.navigate("status")
    journey_window.model.navigate("journey")
    qtbot.waitUntil(lambda: model.state == "loaded")
    assert model.subject == "threads"

    def no_journeys(data: Path) -> None:
        (data / "manifests" / MANIFEST_NAMES["journeys"]).unlink()

    with variant_window(qtbot, tmp_path / "nojourneys", no_journeys) as variant:
        bare = loaded(qtbot, variant)
        assert [t["key"] for t in bare.tabs] == ["range", "practice", "vibrato", "trust", "threads"]
        assert bare.subject == "range"  # "verdict" was asked for; the first available shows
        assert bare.verdictRows == [] and bare.scorecards == []
    availability = charts.tab_availability({"monthlyCadence": [], "clusters": []}, None)
    assert [k for k, on in availability.items() if on] == ["range", "vibrato", "trust"]
    model.selectSubject("nonsense")
    assert model.subject == "threads"


# ---- J-05: the verdict ----------------------------------------------------------------------


@pytest.mark.checklist("J-05")
def test_journey_verdict(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    assert model.verdictRows == [
        {
            "key": "noteCoreCentError",
            "label": "note-core tuning error (cents)",
            "slope": "-0.45/yr",
            "ci": "CI -0.8 … -0.1",
            "songs": "2 songs",
            "verdict": "improving",
            "tone": "ok",
        },
        {
            "key": "cpps",
            "label": "voice clarity (CPPS dB)",
            "slope": "0.05/yr",
            "ci": "CI -0.2 … 0.3",
            "songs": "2 songs",
            "verdict": "flat",
            "tone": "info",
        },
    ]
    assert model.verdictSummary == (
        "1 improving · 1 flat · 0 declining across 2 songs. Individual songs vary — see the "
        "scorecards."
    )
    chips = texts(journey_window, "verdictChip")
    assert chips == ["improving", "flat"]
    steady = {
        "totals": {"clustersEligible": 5},
        "improvementIndex": [
            {
                "label": "x",
                "medianSlopePerYear": None,
                "ci95": None,
                "clustersUsed": 0,
                "verdict": "insufficient_data",
            },
            {
                "label": "y",
                "medianSlopePerYear": 0.0,
                "ci95": [-1, 1],
                "clustersUsed": 5,
                "verdict": "flat",
            },
        ],
    }
    assert charts.verdict_summary(steady).startswith(
        "Across 5 recurring songs, execution on unchanged material has held steady"
    )
    rows = charts.verdict_rows(steady)
    assert (rows[0]["slope"], rows[0]["ci"], rows[0]["verdict"]) == (
        "—",
        "CI —",
        "insufficient data",
    )
    assert rows[1]["slope"] == "0/yr"


# ---- J-06 to J-10, J-22: the Range River ----------------------------------------------------


@pytest.mark.checklist("J-06", "J-07", "J-08", "J-09", "J-22")
def test_journey_range_river_layers(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    chart = show(qtbot, journey_window, "range", "rangeChart")
    spec = model.rangeSpec
    assert chart.property("spec")["panels"][0]["yScale"] == "log2"
    river, strip = spec["panels"]
    assert spec["xMin"] == -0.5 and spec["xMax"] == 23.5
    assert [t["label"] for t in spec["xTicks"]] == ["2019", "2020", "2021", "2022", "2023", "2024"]
    assert [t["label"] for t in river["yTicks"]] == ["G2", "C3", "G3", "C4", "G4"]
    right = ["98 Hz", "131 Hz", "196 Hz", "262 Hz", "392 Hz"]
    assert [t["label"] for t in river["yTicksRight"]] == right
    # Each year's Q3 has a flagged take, so 2 reliable: the river breaks there (fewer than
    # 3), the lone Q4 of 2024 draws no segment, and the six Q3 medians are faint dots.
    kinds = [layer["kind"] for layer in river["layers"]]
    assert kinds == ["band", "band", "line"] * 6 + ["points", "regions"]
    reach, typical, median = river["layers"][:3]
    assert reach["opacity"] == 0.14 and typical["opacity"] == 0.32
    assert reach["points"][0] == [0, 100.0, 300.0] and typical["points"][0] == [0, 150.0, 260.0]
    assert median["points"] == [[0, 201.0], [1, 201.0]]
    assert [p["x"] for p in river["layers"][-2]["points"]] == [2, 6, 10, 14, 18, 22]
    assert {p["opacity"] for p in river["layers"][-2]["points"]} == {0.35}
    regions = river["layers"][-1]["regions"]
    assert len(regions) == 24
    assert regions[0] == {
        "x0": -0.5,
        "x1": 0.5,
        "tooltip": "2019-Q1 · 3 reliable of 3 takes\nreach G2–D4 (100–300 Hz)\nmedian G3 · "
        "high-register 0%",
    }
    assert regions[2]["tooltip"].startswith("2019-Q3 · 2 reliable of 3 takes\n")
    assert strip["layers"][0]["rects"][0] == {
        "x0": -0.38,
        "x1": 0.38,
        "y0": 0.0,
        "y1": 1.0,
        "tone": "info",
    }
    assert model.rangeHeadline == (
        "Working top: D4 (2019) → F#4 (2024) · +3.9 semitones of reach, held since."
    )
    assert [item["text"] for item in model.rangeLegend][0] == "reach (p05–p95 envelope)"
    assert len(named(journey_window, "rangeLegendItem")) == 4

    # Hovering a quarter shows its tooltip (J-07, J-22).
    pos = call(chart, "pointPosition", 0, 0, 201.0)
    assert call(chart, "tooltipAt", pos.x(), pos.y()) == regions[0]["tooltip"]
    scroll_to(qtbot, journey_window, chart)
    hover(qtbot, journey_window, chart, pos.x(), pos.y())
    qtbot.waitUntil(lambda: chart.property("hoverText") == regions[0]["tooltip"])
    assert chart.findChild(type(chart), "tooltip").property("visible")

    # With every quarter reliable the river is one unbroken segment.
    quarters = [{**q, "nReliable": 3} for q in model.trends["quarters"]]
    whole = charts.range_river(quarters)
    assert whole is not None
    layers = whole["panels"][0]["layers"]
    assert [layer["kind"] for layer in layers] == ["band", "band", "line", "regions"]
    assert len(layers[2]["points"]) == 24
    sparse = {**quarters[0], "envelope": {"loHz": None, "hiHz": None}, "medianHz": None}
    assert charts.river_tooltip(sparse) == "2019-Q1 · 3 reliable of 3 takes\nsparse quarter"
    assert charts.range_river([]) is None
    no_top = {"headline": {"workingTop": None}}
    assert charts.range_headline(no_top) == "Where your voice lives, quarter by quarter."


@pytest.mark.checklist("J-10", "J-13")
def test_journey_data_tables(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    range_rows = model.rangeTable.rows
    assert range_rows[0] == {
        "_key": "2019-Q1",
        "quarter": "2019-Q1",
        "reliable": "3",
        "total": "3",
        "floor": "G2 (100)",
        "top": "D4 (300)",
        "median": "G3",
        "high": "0%",
    }
    assert [c["title"] for c in model.rangeColumns] == [
        "Quarter",
        "Reliable",
        "Total",
        "Floor",
        "Top",
        "Median",
        "High share",
    ]
    cause_rows = model.causeTable.rows
    assert cause_rows[0] == {
        "_key": "2019-Q1",
        "quarter": "2019-Q1",
        "takes": "3",
        "sustain": "2",
        "cpps": "10",
        "hnr": "18",
    }
    assert [c["title"] for c in model.causeColumns] == [
        "Quarter",
        "Takes",
        "Sustain s",
        "CPPS",
        "HNR",
    ]
    model.selectSubject("range")
    table = journey_window.find("rangeTable")
    from PySide6.QtCore import QObject

    toggle = table.findChild(QObject, "dataTableToggle")
    grid = table.findChild(QObject, "dataTable")
    assert not grid.property("visible")
    toggle.clicked.emit()
    assert grid.property("visible") and grid.property("count") == 24
    blank = {"quarter": "2020-Q2", "nReliable": 0, "n": 1, "envelope": {}, "typical": {}}
    assert charts.range_table([blank])[0]["floor"] == ""
    assert charts.cause_table([blank])[0]["sustain"] == ""


# ---- J-11 to J-13: Cause & Effect -----------------------------------------------------------


@pytest.mark.checklist("J-11", "J-22")
def test_journey_cause_effect_layers(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    chart = show(qtbot, journey_window, "practice", "causeChart")
    spec = model.causeSpec
    cadence, sustain, cpps = spec["panels"]
    assert cadence["title"] == "recordings per month"
    assert sustain["title"] == "longest sustained note (s, quarterly median)"
    assert cpps["title"] == "voice clarity — CPPS (dB, quarterly median)"
    bars = cadence["layers"][1]["rects"]
    assert len(bars) == 72 and bars[0]["tooltip"] == "Jan 2019 · 1 recordings"
    assert cadence["yTicks"] == [{"value": 0, "label": "0"}, {"value": 1, "label": "1"}]
    point = sustain["layers"][-1]["points"][0]
    assert point["tooltip"] == "2019-Q1 · 2.0s · 3 takes"
    assert cpps["layers"][-1]["points"][0]["tooltip"] == "2019-Q1 · 10.0 · 3 takes"
    assert sustain["yTicks"][0]["label"] == "1.9s"
    assert spec["xTicks"][0] == {"value": 2019 * 12, "label": "2019"}
    pos = call(chart, "pointPosition", 1, point["x"], point["y"])
    assert call(chart, "tooltipAt", pos.x(), pos.y()) == point["tooltip"]
    bar_pos = call(chart, "pointPosition", 0, bars[0]["x0"] + 0.4, 0.5)
    assert call(chart, "tooltipAt", bar_pos.x(), bar_pos.y()) == "Jan 2019 · 1 recordings"
    assert model.eras == []  # the synthetic corpus has no high-practice era
    assert charts.cause_effect([], [], []) is None


@pytest.mark.checklist("J-12")
def test_journey_era_playback(qtbot: Any, tmp_path: Path) -> None:
    data, corpus, burst = era_root(tmp_path / "journey")
    with make_rig(tmp_path, config_for(data, corpus=corpus)) as rig, open_window(rig) as window:
        model = loaded(qtbot, window)
        assert len(model.eras) == 1
        era = model.eras[0]
        assert era["text"] == "▶ May–Jun 2022 · 102 takes"
        assert era["enabled"] and era["tooltip"] == "Hear 3 takes from this era"
        trends_era = model.trends["eras"][0]
        samples = trends_era["sampleRecordingIds"]
        spec = model.causeSpec
        band = spec["panels"][0]["layers"][0]["rects"][0]
        assert (band["x0"], band["x1"]) == (2022 * 12 + 4, 2022 * 12 + 6)
        model.selectSubject("practice")
        button = one(window, "era-0")
        assert button.property("enabled")
        played: list[str] = []
        player = rig.model.player
        player.finished.connect(played.append)
        button.clicked.emit()
        for n in range(len(samples)):
            qtbot.waitUntil(
                lambda n=n: player.state == "playing" and len(rig.output.played) == n + 1,
                timeout=10000,
            )
            assert player.activeKey == "era:0"
            rig.output.finish()
        qtbot.waitUntil(lambda: played == ["era:0"] * len(samples) and player.state == "idle")
        assert set(samples) <= set(burst) | {t.recording_id for t in takes()}
        model.playEra(7)  # no such era: nothing plays
        assert player.state == "idle"
    assert charts.era_buttons([trends_era], False)[0] == {
        "index": 0,
        "text": "▶ May–Jun 2022 · 102 takes",
        "enabled": False,
        "tooltip": "Playback needs the release-gated launch (corpus root + approval)",
    }
    assert charts.era_label({"start": "2021-11", "end": "2022-02"}) == "Nov 2021 – Feb 2022"


@pytest.mark.checklist("J-12")
def test_journey_eras_are_silent_without_playback(qtbot: Any, tmp_path: Path) -> None:
    data, corpus, _ = era_root(tmp_path / "journey")
    with (
        make_rig(tmp_path, config_for(data, corpus=corpus, playback=False)) as rig,
        open_window(rig) as w,
    ):
        model = loaded(qtbot, w)
        assert not model.playbackEnabled and model.eras[0]["enabled"] is False
        model.playEra(0)
        assert rig.model.player.state == "idle" and rig.output.played == []


# ---- J-14 to J-17: the Vibrato Story and the take inspector ---------------------------------


@pytest.mark.checklist("J-14", "J-15", "J-16")
def test_journey_vibrato_layers(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    chart = show(qtbot, journey_window, "vibrato", "vibratoChart")
    rate, extent, share = model.vibratoSpec["panels"]
    assert [p["title"] for p in model.vibratoSpec["panels"]] == [
        "rate (Hz)",
        "extent (cents)",
        "share of sustained time",
    ]
    assert rate["layers"][0] == {
        "kind": "zone",
        "y0": 5,
        "y1": 6,
        "tone": "info",
        "opacity": 0.12,
        "label": "settled 5–6 Hz",
    }
    assert rate["yMax"] > 6.4
    assert rate["layers"][-1]["points"][0]["tooltip"] == "2019-Q1 · 4.1 · 3 takes"
    assert extent["layers"][-1]["points"][0]["tooltip"] == "2019-Q1 · 61c · 3 takes"
    assert share["layers"][-1]["points"][0]["tooltip"] == "2019-Q1 · 30% · 3 takes"
    assert chart.property("spec")["panels"][2]["title"] == "share of sustained time"
    finger = model.fingerprintSpec
    assert finger["xTitle"] == "extent (cents)" and finger["xMax"] == pytest.approx(62 * 1.04)
    dot = finger["panels"][0]["layers"][1]["points"][0]
    assert dot["tooltip"] == "2019-01-05 · 4 Hz · 60c\nclick to inspect"
    assert dot["shade"] == 0.0 and dot["hollow"] is False and dot["id"] == takes()[0].recording_id
    assert model.yearLegend[0] == {"year": "2019", "shade": 0.0, "tone": "bucket5"}
    assert model.yearLegend[-1]["shade"] == 1.0 and len(model.yearLegend) == 6
    assert len(named(journey_window, "yearLegendItem")) == 6
    flagged = {
        "id": "f",
        "date": "2020-01-01",
        "year": "2020",
        "rate": 5.0,
        "extent": 40,
        "reliable": False,
    }
    spec = charts.fingerprint([flagged, {**flagged, "rate": 9.5}], (2019, 2021))
    assert spec is not None
    points = spec["panels"][0]["layers"][1]["points"]
    assert len(points) == 1 and points[0]["hollow"] and points[0]["shade"] == 0.5
    assert points[0]["tooltip"] == "2020-01-01 · 5 Hz · 40c · flagged\nclick to inspect"
    assert charts.fingerprint([], (0, 0)) is None and charts.vibrato_triptych([]) is None


@pytest.mark.checklist("J-15", "J-17")
def test_journey_take_inspector(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    rig = journey_window.rig
    chart = show(qtbot, journey_window, "vibrato", "fingerprintChart")
    first = takes()[0]
    dot = model.fingerprintSpec["panels"][0]["layers"][1]["points"][0]
    pos = call(chart, "pointPosition", 0, dot["x"], dot["y"])
    assert call(chart, "idAt", pos.x(), pos.y()) == first.recording_id
    scroll_to(qtbot, journey_window, chart)
    click(qtbot, journey_window, chart, pos.x(), pos.y())
    qtbot.waitUntil(lambda: model.inspectorState == "loaded", timeout=10000)
    dialog = journey_window.find("takeInspector")
    assert dialog.property("opened") or dialog.property("visible")
    assert model.inspectorTitle == f"{first.filename} · 2019-01-05"
    assert model.inspectorTiles == [
        {"label": "median pitch", "value": "G3 (200 Hz)"},
        {"label": "range p05–p95", "value": "11"},
        {"label": "vibrato", "value": "4 Hz · 60c"},
        {"label": "CPPS", "value": "10 dB"},
        {"label": "longest sustain", "value": "2 s"},
        {"label": "bucket", "value": "clean_singing"},
    ]
    contour = model.inspectorContour
    assert contour["panels"][0]["yScale"] == "log2" and contour["xTicks"] == [
        {"value": 2.95, "label": "3 s"}
    ]
    assert len(contour["panels"][0]["layers"][0]["points"]) == 60
    bar = journey_window.find("inspectorPlayer")
    assert bar.property("key") == f"inspector:{first.recording_id}"
    from PySide6.QtCore import QObject

    bar.findChild(QObject, "playButton").clicked.emit()
    qtbot.waitUntil(lambda: rig.model.player.state == "playing", timeout=10000)
    journey_window.find("closeInspector").clicked.emit()
    qtbot.waitUntil(lambda: not model.inspectorOpen)
    assert rig.model.player.state == "idle"  # closing the inspector stops its take

    # A take with no feature detail on the server, and an unknown one.
    model.openTake(takes()[1].recording_id)
    qtbot.waitUntil(lambda: model.inspectorState == "failed", timeout=10000)
    assert model.inspectorError.startswith("Detail unavailable: ")
    model.closeTake()
    model.openTake("")
    assert not model.inspectorOpen


@pytest.mark.checklist("J-17")
def test_journey_take_inspector_without_the_feature_store(qtbot: Any, tmp_path: Path) -> None:
    with variant_window(qtbot, tmp_path, feature_dir=None, playback=False) as window:
        model = loaded(qtbot, window)
        model.openTake(takes()[0].recording_id)
        qtbot.waitUntil(lambda: model.inspectorState == "failed", timeout=10000)
        assert model.inspectorError == FEATURE_STORE_MISSING
        assert not window.find("inspectorPlayer").property("visible")
    assert charts.inspector_title(None, "abc") == "Take abc"
    assert charts.inspector_tiles({"summary": None, "bucket": None}) == [
        {"label": "median pitch", "value": "—"},
        {"label": "range p05–p95", "value": "—"},
        {"label": "vibrato", "value": "—"},
        {"label": "CPPS", "value": "—"},
        {"label": "longest sustain", "value": "—"},
        {"label": "bucket", "value": "—"},
    ]
    assert charts.f0_contour([[0, 200]]) is None and charts.f0_contour(None) is None


# ---- J-18 to J-21 ---------------------------------------------------------------------------


@pytest.mark.checklist("J-18")
def test_journey_trust(qtbot: Any, tmp_path: Path, journey_window: Window) -> None:
    model = journey_window.model.journey
    model.selectSubject("trust")
    titles = [p["title"] for p in model.trustPanels]
    assert titles == [
        "CPPS (dB) — tracks the voice",
        "HNR (dB) — tracks the microphones",
        "note-core tuning error (cents) — now measured",
    ]
    cpps, hnr, tuning = (p["spec"] for p in model.trustPanels)
    assert cpps["panels"][0]["layers"][-1]["points"][0]["tooltip"] == "2019-Q1 · 10.0"
    assert hnr["panels"][0]["layers"][-1]["points"][0]["tooltip"] == "2019-Q1 · 18.0"
    assert tuning["panels"][0]["layers"][-1]["points"][0]["tooltip"] == "2019-Q1 · 22.1c"
    assert [t["label"] for t in cpps["xTicks"]] == ["19", "20", "21", "22", "23", "24"]
    assert model.tuningTile["title"] == "Tuning — now measured on note cores."
    assert model.tuningTile["text"].startswith(
        "72 takes scored on stable note medians only (slides and melisma excluded by "
        "construction): median error 20.7 cents"
    )
    assert len(named(journey_window, "trustChart")) == 3
    assert charts.tuning_tile(None, {"median": None})["title"] == (
        "Tuning accuracy — not yet meaningful."
    )
    pseudo = charts.tuning_pseudo_quarters(
        {
            "noteCore": {
                "takes": [{"quarter": "2020-Q1", "centErrorMedian": v} for v in (3, 1, 2)]
                + [{"quarter": "2020-Q2", "centErrorMedian": 5}, {"quarter": None}]
            }
        }
    )
    assert pseudo == [{"quarter": "2020-Q1", "nc": 2, "nReliable": 3, "spread": {"nc": [1, 2]}}]
    assert charts.note_core_overall(None) == {"takeCount": 0, "median": None}
    assert charts.mini_line_panel("t", [], "cpps", lambda q: q, "ok", str) is None


@pytest.mark.checklist("J-19")
def test_journey_scorecards(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    model.selectSubject("songs")
    assert journey_window.find("songsCard").property("visible")
    rows = model.scorecards
    assert [r["name"] for r in rows] == ["Song A", "Song B"]
    first = rows[0]
    assert (first["clusterId"], first["takes"], first["years"]) == (
        "lyric-cluster-001",
        "18 takes",
        "2019–2024",
    )
    assert first["tuning"] == {"text": "▼ -0.4/yr", "tone": "ok"}
    assert first["clarity"] == {"text": "▲ 0.3/yr", "tone": "ok"}
    assert rows[1]["clarity"] == {"text": "▼ -0.2/yr", "tone": "danger"}
    spark = first["spark"]
    assert spark["compact"] and len(spark["panels"][0]["layers"][0]["points"]) == 6
    assert charts.sparkline({"2020": {"x": 1}, "2021": {"x": 1}}, "x")["panels"][0]["yMin"] == 0
    assert charts.sparkline({"2020": {"x": 1}}, "x") is None
    assert charts.slope_arrow(None, True) == {"text": "—", "tone": "muted"}
    assert charts.slope_arrow(0.01, False) == {"text": "▲ 0.01/yr", "tone": "muted"}
    assert charts.slope_arrow(0, False)["text"] == "▬ 0/yr"
    assert charts.song_name("lyric-unknown", []) == "unknown"
    names = named(journey_window, "songName")
    names[0].clicked.emit()
    assert journey_window.model.screen == "corpus"
    many = {"journeys": [{"clusterId": f"c{i}", "takesUsed": i} for i in range(14)]}
    assert [j["clusterId"] for j in charts.song_scorecards(many)][:2] == ["c13", "c12"]
    assert len(charts.song_scorecards(many)) == 10


@pytest.mark.checklist("J-20")
def test_journey_threads(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    opened: list[str] = []
    model.open_corpus = opened.append
    chart = show(qtbot, journey_window, "threads", "threadsChart")
    spec = model.threadsSpec
    panel = spec["panels"][0]
    assert [t["label"] for t in panel["yTicks"]] == ["Song A", "Song B"]
    assert [t["label"] for t in panel["yTicksRight"]] == [
        "18 takes · 2019–24",
        "12 takes · 2019–24",
    ]
    dots = panel["layers"][-1]["points"]
    first = dots[0]
    assert first["tooltip"] == "Song A · 2019 · 3 takes\nclick to open in Corpus"
    assert first["id"] == "lyric-cluster-001" and first["r"] == 3.5  # max(3.5, sqrt(3) * 1.9)
    assert first["x"] == 2019 * 12 + 5.5 and first["y"] == 1
    assert [t["label"] for t in spec["xTicks"]] == ["2019", "2020", "2021", "2022", "2023", "2024"]
    pos = call(chart, "pointPosition", 0, first["x"], first["y"])
    assert call(chart, "idAt", pos.x(), pos.y()) == "lyric-cluster-001"
    scroll_to(qtbot, journey_window, chart)
    click(qtbot, journey_window, chart, pos.x(), pos.y())
    qtbot.waitUntil(lambda: opened == ["lyric-cluster-001"])
    model.open_corpus = journey_window.model.open_corpus_with_query
    model.openCluster("lyric-cluster-002")
    assert journey_window.model.screen == "corpus"
    model.openCluster("")
    single = charts.threads(
        [{"id": "x", "size": 1, "first": "2020-03", "last": "2020-03", "perYear": {"2020": 1}}]
    )
    assert single["panels"][0]["layers"][-1]["points"][0]["tooltip"].startswith(
        "Song A · 2020 · 1 take\n"
    )
    assert charts.threads([]) is None


@pytest.mark.checklist("J-21")
def test_journey_education_cards(qtbot: Any, journey_window: Window) -> None:
    model = journey_window.model.journey
    assert set(EDUCATION) == set(charts.SUBJECTS)
    for key in charts.SUBJECTS:
        model.selectSubject(key)
        assert model.education == card(key)
        assert journey_window.find("educationCard").property("visible")
    model.selectSubject("verdict")
    assert model.education["title"] == "Understanding the Verdict"
    headings = texts(journey_window, "educationHeading")
    assert headings == [
        "Why compare only within the same song",
        "The statistics",
        "How to read a row",
    ]
    statistics = EDUCATION["verdict"].sections[1][1]
    assert "the honest call is<b> flat</b>" in statistics
    assert "the honest call is flat — the data" in plain(statistics)
    river = EDUCATION["range"].sections[2][1]
    assert "low &lt;200 Hz" in river and "low <200 Hz" in plain(river)
    assert EDUCATION["range"].sections[0][1].count("<sup>1/12</sup>") == 1
    assert card("nonsense") is None
    assert sum(len(e.sections) for e in EDUCATION.values()) == 19


# ---- the model's edges ---------------------------------------------------------------------


def test_journey_model_edges(
    qtbot: Any, journey_window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = journey_window.model.journey
    from voice_lab_desktop.api import ApiClient, ApiUnavailable
    from voice_lab_desktop.tablemodel import TableRows

    # A failing journeys document leaves the trends (the SPA's slices failed on their own).
    real = ApiClient.document

    def flaky(self: ApiClient, kind: str) -> Any:
        if kind == "journeys":
            raise ApiUnavailable("the server is unavailable (test)")
        return real(self, kind)

    monkeypatch.setattr(ApiClient, "document", flaky)
    model.refresh()
    model.refresh()  # the first answer arrives after the second request: it is dropped
    qtbot.waitUntil(lambda: model.journeys is None and model.state == "loaded", timeout=10000)
    assert model.available and [t["key"] for t in model.tabs][0] == "range"

    # A take closed before its detail arrives stays closed.
    model.openTake(takes()[0].recording_id)
    model.closeTake()
    qtbot.wait(300)
    assert not model.inspectorOpen and model.inspectorState == "idle"

    # Signing out forgets everything.
    journey_window.model.signOut()
    assert model.state == "idle" and model.tabs == [] and model.trends is None
    model.refresh()
    assert model.state == "idle"

    rows = TableRows()
    rows.replace([{"a": 1}])
    from PySide6.QtCore import QModelIndex, Qt

    assert rows.rowCount() == 1 and rows.rowCount(rows.index(0)) == 0
    assert rows.data(QModelIndex()) is None
    assert rows.data(rows.index(0), Qt.ItemDataRole.DisplayRole) is None
    assert rows.roleNames()[Qt.ItemDataRole.UserRole + 1] == b"row"


def test_journey_chart_edges() -> None:
    single = [{"x": 1, "lo": 1, "hi": 2, "value": 1, "q": {}}]
    assert charts.band_and_line(single, "ok") == []
    no_strip = {
        "quarter": "2020-Q1",
        "n": 3,
        "nReliable": 3,
        "envelope": {"loHz": 100, "hiHz": 200},
        "typical": {"loHz": 120, "hiHz": 180},
        "medianHz": 150,
    }
    spec = charts.range_river([no_strip, {**no_strip, "quarter": "2020-Q2"}])
    assert spec is not None and spec["panels"][1]["layers"][0]["rects"] == []
    thin = {**no_strip, "registerShares": {"high": 0.01, "mid": 0.99, "low": 0}}
    rects = charts.range_river([thin])["panels"][1]["layers"][0]["rects"]  # type: ignore[index]
    assert [r["tone"] for r in rects] == ["info"]  # a sliver under half a pixel is not drawn
    assert charts.domain_of([1, 2], 0.5) == (0.5, 2.5)
    assert charts.truthy("") is False and charts.truthy("x") is True


# ---- the number rules ----------------------------------------------------------------------


def test_journey_number_rules() -> None:
    assert charts.js_round(2.5) == 3 and charts.js_round(-2.5) == -2
    assert charts.js_fixed(0.25, 1) == "0.3" and charts.js_fixed(-0.04, 1) == "0.0"
    assert charts.js_fixed(2.0, 1) == "2.0"
    assert (charts.js_num(3.0), charts.js_num(3.9), charts.js_num(None), charts.js_num(True)) == (
        "3",
        "3.9",
        "",
        "true",
    )
    assert charts.js_num("x") == "x" and charts.js_num(float("inf")) == "inf"
    assert charts.thousands("n/a") == "n/a"
    assert (
        charts.note_name(440) == "A4" and charts.note_name(0) == "" and charts.note_name(None) == ""
    )
    assert charts.note_name(261.63) == "C4" and charts.note_name(27.5) == "A0"
    assert charts.domain_of([], 0.1) == (0.0, 1.0)
    assert charts.domain_of([1, None, 1], 0.1, 0) == (pytest.approx(0.999), pytest.approx(1.001))
    assert charts.pct(None) == "" and charts.pct(0.125) == "13%"
    assert charts.year_shade("2020", 2020, 2020) == 1.0
    assert charts.truthy(float("nan")) is False and charts.truthy([]) is True
    assert charts.year_span(None) == (0, 0) and charts.fingerprint_years(None) == []
    assert charts.active_quarters({"available": False}, True) == []
