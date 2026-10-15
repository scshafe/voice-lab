"""Corpus (checklist C-01 to C-37, A-04, A-06): the table, filters, detail, playback and
verdicts, driven offscreen over a real /api/v1 on synthetic manifests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AppRig, Window, make_rig, open_window
from PySide6.QtCore import Q_ARG, QMetaObject, QObject, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from synthetic import TRANSCRIPT_TEXT, audio_root, big_root, config_for, takes
from voice_lab_desktop.api import ApiClient, ApiRefused, ApiUnavailable
from voice_lab_desktop.formatting import (
    bucket_tone,
    fmt_bytes,
    fmt_captured,
    fmt_confidence,
    fmt_seconds,
    labelize,
    text,
)
from voice_lab_desktop.screens.corpus import (
    NO_PLAYBACK,
    PLAY_TOOLTIP,
    detail_entries,
    play_label,
    table_row,
)
from voice_lab_desktop.tablemodel import TableRows

ROW = TableRows.ROW_ROLE


# ---- helpers ------------------------------------------------------------------------------


def call(item: QObject, method: str, *args: Any) -> None:
    """Call a QML function of ``item`` (its arguments are QVariants)."""
    params = [Q_ARG("QVariant", value) for value in args]
    assert QMetaObject.invokeMethod(item, method, *params)


def items(root: QQuickItem) -> list[QQuickItem]:
    """``root`` and every item under it (the visual tree: a Repeater's delegates are not
    QObject children of the window)."""
    found = [root]
    for child in root.childItems():
        found.extend(items(child))
    return found


def all_named(window: Window, name: str, under: Any = None) -> list[QQuickItem]:
    root = under if under is not None else window.root.property("contentItem")
    found = [item for item in items(root) if item.objectName() == name]
    sheet = window.find("rowDetail")  # a popup's items live in the window's overlay
    if under is None and sheet.property("contentItem") is not None:
        overlay = sheet.property("contentItem").parentItem()
        found += [i for i in items(overlay) if i.objectName() == name and i not in found]
    return found


def named(window: Window, name: str, under: Any = None) -> QQuickItem:
    found = all_named(window, name, under)
    assert found, f"no item named {name!r}"
    return found[0]


def open_corpus(qtbot: Any, window: Window) -> Any:
    corpus = window.model.corpus
    window.model.navigate("corpus")
    settled(qtbot, corpus)
    return corpus


def settled(qtbot: Any, corpus: Any) -> None:
    qtbot.waitUntil(lambda: corpus.state == "loaded" and corpus.totalFiltered >= 0, timeout=10000)
    qtbot.waitUntil(lambda: not corpus.loading, timeout=10000)


def shown(corpus: Any) -> list[dict[str, Any]]:
    """The table's rows as it draws them."""
    model = corpus.rows
    return [model.data(model.index(i), ROW) for i in range(model.rowCount())]


def raw(corpus: Any) -> list[dict[str, Any]]:
    return corpus.rows.rows


def server_rows(window: Window) -> dict[str, dict[str, Any]]:
    """Every full row the synthetic server serves, by id."""
    store = window.rig.served.store
    return {row["recordingId"]: row for row in store.current().rows_payload["rows"]}


def wait_reload(qtbot: Any, corpus: Any, change: Any) -> None:
    """Run ``change`` and wait until the first page of the new query is in."""
    change()
    qtbot.waitUntil(lambda: corpus.totalFiltered >= 0 and not corpus.loading, timeout=10000)


def table(window: Window) -> Any:
    return window.find("corpusTable")


def delegates(window: Window) -> list[Any]:
    view = named(window, "rows", table(window))
    content = view.property("contentItem")
    return [item for item in content.childItems() if item.property("row") and item.isVisible()]


def open_detail(qtbot: Any, corpus: Any, recording: str) -> None:
    corpus.openDetail(recording)
    qtbot.waitUntil(lambda: corpus.detailState in ("loaded", "failed"), timeout=10000)


def take(index: int) -> Any:
    return takes()[index]


@pytest.fixture
def quiet_rig(tmp_path: Path, synthetic_root: Path) -> Iterator[AppRig]:
    """Playback and transcript text off (the server's gates closed)."""
    config = config_for(synthetic_root, playback=False, transcript_dir=None)
    with make_rig(tmp_path / "quiet", config) as made:
        yield made


@pytest.fixture
def quiet_window(qtbot: Any, quiet_rig: AppRig) -> Iterator[Window]:
    with open_window(quiet_rig) as opened:
        yield opened


@pytest.fixture
def big_window(qtbot: Any, tmp_path: Path) -> Iterator[Window]:
    with make_rig(tmp_path, config_for(big_root(tmp_path))) as made, open_window(made) as opened:
        yield opened


# ---- A: the status line and the boot -------------------------------------------------------


@pytest.mark.checklist("A-06")
def test_corpus_loads_summary_and_first_page(
    qtbot: Any, window: Window, tmp_path: Path, synthetic_root: Path
) -> None:
    # A model that has not shown the Corpus has asked for nothing.
    with make_rig(tmp_path / "unshown", config_for(synthetic_root)) as unshown:
        assert unshown.model.corpus.state == "idle"
        assert unshown.model.corpus.rows.rowCount() == 0
    # Signed in, the app opens on the Corpus, which loads the summary and a page.
    assert window.model.screen == "corpus"
    corpus = window.model.corpus
    open_corpus(qtbot, window)
    assert corpus.playbackEnabled and corpus.transcriptTextEnabled
    assert corpus.totalFiltered == 72 and corpus.rows.rowCount() == 72
    options = {c["key"]: [o["value"] for o in c["options"]] for c in corpus.filterControls}
    assert options["bucket"][0] == "" and "clean_singing" in options["bucket"]
    corpus.activate()  # a second show does not reload
    assert not corpus.loading


@pytest.mark.checklist("A-04")
def test_corpus_status_line(qtbot: Any, window: Window, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = window.model.corpus
    line = window.find("corpusStatusLine")
    assert corpus.statusLine in ("Loading rows…", "Showing 72 of 72 takes")
    open_corpus(qtbot, window)
    assert corpus.statusLine == "Showing 72 of 72 takes"
    assert line.property("text") == "Showing 72 of 72 takes"

    def down(self: ApiClient, params: Any) -> Any:
        raise ApiUnavailable("the server did not answer (ConnectError)")

    monkeypatch.setattr(ApiClient, "recordings", down)
    corpus.sortBy("year")
    qtbot.waitUntil(lambda: corpus.statusLine == "Row fetch failed", timeout=10000)
    assert line.property("text") == "Row fetch failed"
    assert "did not answer" in corpus.error
    assert window.find("corpusError").property("visible")


# ---- C-01 to C-08: filters ----------------------------------------------------------------


@pytest.mark.checklist("C-01")
def test_corpus_search_filters_rows(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    search = window.find("corpusSearch")
    assert search.property("placeholderText") == "filename, bucket, rationale"
    wait_reload(qtbot, corpus, lambda: corpus.setSearch("non_singing rationale"))
    qtbot.waitUntil(lambda: corpus.searchText == "non_singing rationale", timeout=5000)
    qtbot.waitUntil(lambda: not corpus.loading and corpus.totalFiltered == 9, timeout=5000)
    assert all(row["bucket"] == "non_singing" for row in raw(corpus))
    assert corpus.anyFilter
    # Typing in the field: the search applies once typing pauses.
    search.setProperty("text", "-071")
    search.textEdited.emit()
    qtbot.waitUntil(lambda: corpus.totalFiltered == 1 and not corpus.loading, timeout=5000)
    assert raw(corpus)[0]["filename"] == take(71).filename
    # Escape (or the clear button) clears it.
    call(search, "clear")
    qtbot.waitUntil(lambda: corpus.totalFiltered == 72 and not corpus.loading, timeout=5000)
    assert corpus.searchText == "" and not corpus.anyFilter


@pytest.mark.checklist("C-02", "C-03", "C-04", "C-05", "C-06", "C-07")
def test_corpus_select_filters(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    summary = window.rig.served.store
    controls = {c["key"]: c for c in corpus.filterControls}
    assert [c["label"] for c in corpus.filterControls] == [
        "Bucket",
        "Year",
        "Queue",
        "Spot-check",
        "Transcript",
        "Lyric mark",
    ]
    assert controls["reviewQueue"]["options"] == [
        {"value": "", "label": "All"},
        {"value": "selected", "label": "Spot-check sample"},
    ]
    assert [o["value"] for o in controls["year"]["options"]] == ["", *map(str, range(2019, 2025))]
    rows_by_id = server_rows(window)
    rows = list(rows_by_id.values())
    cases = {
        "bucket": ("non_singing", "bucket"),
        "year": ("2021", "year"),
        "reviewQueue": ("selected", "reviewQueueSelected"),
        "spotCheckStatus": ("reviewed", "spotCheckStatus"),
        "transcriptStatus": ("failed", "transcriptStatus"),
        "lyricMatchStatus": ("marked_indicator", "lyricMatchStatus"),
    }
    for key, (value, field) in cases.items():
        select = named(window, f"filter-{key}")
        assert select.property("label") == controls[key]["label"]
        wait_reload(qtbot, corpus, lambda select=select, value=value: call(select, "select", value))
        assert corpus.filters[key] == value
        wanted = True if field == "reviewQueueSelected" else value
        expected = sorted(r["recordingId"] for r in rows if r.get(field) == wanted)
        assert expected and sorted(r["recordingId"] for r in raw(corpus)) == expected, key
        assert {rows_by_id[r["recordingId"]][field] for r in raw(corpus)} == {wanted}, key
        assert named(window, f"filter-{key}").property("value") == value
        wait_reload(qtbot, corpus, lambda key=key: corpus.setFilter(key, ""))
    assert summary is not None


@pytest.mark.checklist("C-08")
def test_corpus_clear_filters(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    clear = window.find("clearFilters")
    assert not corpus.anyFilter and not clear.property("visible")
    wait_reload(qtbot, corpus, lambda: corpus.setFilter("year", "2020"))
    wait_reload(qtbot, corpus, lambda: corpus.setFilter("q", "clean"))
    assert corpus.anyFilter and clear.property("visible")
    assert corpus.totalFiltered < 72
    wait_reload(qtbot, corpus, lambda: clear.clicked.emit())
    assert not corpus.anyFilter and corpus.totalFiltered == 72
    assert all(not c["value"] for c in corpus.filterControls) and corpus.searchText == ""


# ---- C-09 to C-23: the table --------------------------------------------------------------


@pytest.mark.checklist("C-09", "C-10", "C-11", "C-12", "C-13", "C-14", "C-15", "C-16", "C-17")
def test_corpus_columns_render_the_list_fields(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    columns = corpus.columns
    assert [c["title"] for c in columns] == [
        "Queue",
        "Filename",
        "Captured",
        "Year",
        "Duration",
        "Bucket",
        "Conf.",
        "Spot-check",
        "Transcript",
        "Lyric cluster",
        "Listen / review",
    ]
    assert table(window).property("pinnedColumns") == 2  # the queue and the filename stay
    full = server_rows(window)
    for row, cells in zip(raw(corpus), shown(corpus), strict=True):
        source = full[row["recordingId"]]
        order = source["reviewQueueOrder"]
        assert cells["queue"] == (f"#{order}" if order else "")
        assert cells["filename"] == source["filename"]
        assert cells["captured"] == fmt_captured(source["capturedAt"])
        assert cells["year"] == source["year"]
        assert cells["duration"] == f"{round(source['durationSeconds'])}s"
        assert cells["bucket"] == {
            "text": labelize(source["bucket"]),
            "tone": bucket_tone(source["bucket"]),
        }
        assert cells["confidence"] == f"{source['confidence']:.2f}"
        assert cells["spotCheck"]["primary"] == labelize(source["spotCheckStatus"])
        assert cells["spotCheck"]["muted"] == (source["spotCheckStatus"] == "not_reviewed")
        words = source["transcriptWordCount"]
        assert cells["transcript"]["secondary"] == (f"{words} words" if words is not None else "")
    first = shown(corpus)[0]
    assert first["captured"] == "2019-01-05 20:00" and first["duration"] == "30s"
    assert first["bucket"] == {"text": "clean singing", "tone": "ok"}
    assert first["confidence"] == "0.30"
    assert first["transcript"] == {"primary": "completed", "secondary": "20 words", "muted": False}
    # The delegates draw what the model holds (the virtualised rows on screen).
    drawn = {d.property("row")["_key"] for d in delegates(window)}
    assert raw(corpus)[0]["recordingId"] in drawn


@pytest.mark.checklist("C-11", "C-20")
def test_corpus_sorts_on_header(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    captured = named(window, "header-captured")
    assert captured.property("contentItem").property("text") == "Captured ▲"
    assert [r["capturedAt"] for r in raw(corpus)] == sorted(r["capturedAt"] for r in raw(corpus))
    duration = named(window, "header-duration")
    wait_reload(qtbot, corpus, lambda: duration.clicked.emit())
    assert (corpus.sortKey, corpus.sortDescending) == ("durationSeconds", False)
    assert duration.property("contentItem").property("text") == "Duration ▲"
    assert captured.property("contentItem").property("text") == "Captured"
    values = [r["durationSeconds"] for r in raw(corpus)]
    assert values == sorted(values)
    wait_reload(qtbot, corpus, lambda: duration.clicked.emit())
    assert corpus.sortDescending
    assert duration.property("contentItem").property("text") == "Duration ▼"
    values = [r["durationSeconds"] for r in raw(corpus)]
    assert values == sorted(values, reverse=True)
    # The lyric cluster and the actions are not sortable.
    cluster = named(window, "header-cluster")
    assert not cluster.property("enabled")


@pytest.mark.checklist("C-18")
def test_corpus_cluster_click_filters(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    plain = next(i for i, row in enumerate(raw(corpus)) if not row.get("lyricMatchCluster"))
    assert shown(corpus)[plain]["cluster"] == ""
    corpus.cellActivated(plain, "cluster")  # no cluster: nothing to filter on
    corpus.cellActivated(plain, "year")  # not a link column
    assert corpus.searchText == "" and corpus.detailId == ""
    index = next(i for i, row in enumerate(raw(corpus)) if row.get("lyricMatchCluster"))
    assert shown(corpus)[index]["cluster"] == "recurring_lyric_cluster_001"
    wait_reload(qtbot, corpus, lambda: table(window).cellActivated.emit(index, "cluster"))
    assert corpus.searchText == "lyric-cluster-001"
    assert window.find("corpusSearch").property("text") == "lyric-cluster-001"
    assert {row["lyricMatchCluster"] for row in raw(corpus)} == {"lyric-cluster-001"}
    assert corpus.totalFiltered == 18
    corpus.cellActivated(999, "cluster")  # out of range: nothing


@pytest.mark.checklist("C-19")
def test_corpus_play_is_gated(qtbot: Any, quiet_window: Window, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    play = shown(corpus)[0]["actions"][0]
    assert play == {"id": "play", "text": "Play", "enabled": True, "tooltip": PLAY_TOOLTIP}
    assert shown(corpus)[0]["actions"][1]["text"] == "Review"
    quiet = open_corpus(qtbot, quiet_window)
    assert not quiet.playbackEnabled
    play = shown(quiet)[0]["actions"][0]
    assert play == {"id": "play", "text": "Play", "enabled": False, "tooltip": NO_PLAYBACK}
    buttons = [b for b in all_named(quiet_window, "action-play") if b.property("visible")]
    assert buttons and not any(b.property("enabled") for b in buttons)
    quiet.play(raw(quiet)[0]["recordingId"])  # refused without playback
    assert quiet_window.rig.output.played == []


@pytest.mark.checklist("C-10", "C-19")
def test_corpus_row_opens_detail(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    first = raw(corpus)[0]
    table(window).cellActivated.emit(0, "filename")
    qtbot.waitUntil(lambda: corpus.detailState == "loaded", timeout=10000)
    assert corpus.detailId == first["recordingId"]
    sheet = window.find("rowDetail")
    qtbot.waitUntil(lambda: sheet.property("visible"), timeout=5000)
    corpus.closeDetail()
    table(window).actionTriggered.emit(1, "review")
    qtbot.waitUntil(lambda: corpus.detailId == raw(corpus)[1]["recordingId"], timeout=5000)
    corpus.closeDetail()
    table(window).activated.emit(2)  # Enter, or a double click
    qtbot.waitUntil(lambda: corpus.detailId == raw(corpus)[2]["recordingId"], timeout=5000)
    corpus.actionTriggered(-1, "review")  # out of range: nothing
    corpus.actionTriggered(0, "unknown")
    assert corpus.detailId == raw(corpus)[2]["recordingId"]


@pytest.mark.checklist("C-21")
def test_corpus_highlights_the_review_sample(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    highlighted = [cells["_key"] for cells in shown(corpus) if cells["_highlight"]]
    selected = [r["recordingId"] for r in raw(corpus) if r["reviewQueueSelected"]]
    assert highlighted == selected and len(selected) == 6
    orders = sorted(int(cells["queue"][1:]) for cells in shown(corpus) if cells["queue"])
    assert orders == [1, 2, 3, 4, 5, 6]


@pytest.mark.checklist("C-22")
def test_corpus_pages_at_the_end(qtbot: Any, big_window: Window) -> None:
    corpus = open_corpus(qtbot, big_window)
    assert corpus.rows.rowCount() == 100 and corpus.totalFiltered == 7000 and corpus.hasMore
    assert corpus.statusLine == "Showing 100 of 7,000 takes"
    call(table(big_window), "positionAtRow", 99)
    qtbot.waitUntil(lambda: corpus.rows.rowCount() == 200 and not corpus.loading, timeout=10000)
    assert corpus.statusLine == "Showing 200 of 7,000 takes"
    ids = [r["recordingId"] for r in raw(corpus)]
    assert len(set(ids)) == 200  # the second page follows the first, nothing twice


@pytest.mark.checklist("C-22")
def test_corpus_table_handles_7000_rows(qtbot: Any, big_window: Window) -> None:
    corpus = open_corpus(qtbot, big_window)
    while corpus.hasMore:
        count = corpus.rows.rowCount()
        call(table(big_window), "positionAtRow", count - 1)
        qtbot.waitUntil(
            lambda count=count: corpus.rows.rowCount() > count and not corpus.loading, timeout=10000
        )
    assert corpus.rows.rowCount() == 7000 and not corpus.hasMore
    assert corpus.statusLine == "Showing 7,000 of 7,000 takes"
    assert len({r["recordingId"] for r in raw(corpus)}) == 7000
    # Virtualised: only the rows in view (and a small cache) have delegates.
    drawn = delegates(big_window)
    assert 0 < len(drawn) < 100  # delegates (on screen, cached, pooled) for 7,000 rows
    view = named(big_window, "rows", table(big_window))
    top = view.property("contentY")
    bottom = top + view.property("height")
    on_screen = [d for d in drawn if d.y() + d.height() > top and d.y() < bottom]
    tail = {r["recordingId"] for r in raw(corpus)[-200:]}  # the view is where the end was
    assert on_screen and {item.property("row")["_key"] for item in on_screen} <= tail
    corpus.loadMore()  # nothing left: no request
    assert not corpus.loading


@pytest.mark.checklist("C-23")
def test_corpus_one_player_at_a_time(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    player, output = window.model.player, window.rig.output
    first, second = raw(corpus)[0]["recordingId"], raw(corpus)[1]["recordingId"]
    table(window).actionTriggered.emit(0, "play")
    qtbot.waitUntil(lambda: player.state == "playing", timeout=10000)
    assert player.activeKey == f"corpus:{first}"
    assert shown(corpus)[0]["actions"][0]["text"] == "Pause"
    table(window).actionTriggered.emit(0, "play")  # the same row pauses it
    assert player.state == "paused" and shown(corpus)[0]["actions"][0]["text"] == "Resume"
    table(window).actionTriggered.emit(1, "play")
    qtbot.waitUntil(
        lambda: player.activeKey == f"corpus:{second}" and player.state == "playing", timeout=10000
    )
    assert not output.running or len(output.played) == 2
    assert len(output.played) == 2
    assert shown(corpus)[0]["actions"][0]["text"] == "Play"
    assert shown(corpus)[1]["actions"][0]["text"] == "Pause"
    assert [play_label(s) for s in ("idle", "loading", "playing", "paused")] == [
        "Play",
        "Loading…",
        "Pause",
        "Resume",
    ]


# ---- C-24 to C-37: the detail -------------------------------------------------------------


@pytest.mark.checklist("C-24", "C-25", "C-26", "C-27", "C-28", "C-29", "C-30", "C-31")
def test_corpus_detail_fields(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    zero = take(0)
    open_detail(qtbot, corpus, zero.recording_id)
    assert corpus.detailTitle == zero.filename
    assert corpus.detailDescription == "clean singing · 2019"
    sheet = window.find("rowDetail")
    qtbot.waitUntil(lambda: sheet.property("visible"), timeout=5000)
    assert sheet.property("title") == zero.filename
    assert sheet.property("description") == "clean singing · 2019"
    entries = {e["label"]: e["value"] for e in corpus.detailEntries}
    assert entries == {
        "Captured": "2019-01-05 20:00",
        "Duration": "30s",
        "Size": "1.00 MB",
        "Bucket": "clean singing",
        "Confidence": "0.30",
        "Spot-check": "not reviewed",
        "Transcript": "completed · 20 words · en",
        "Lyric cluster": "recurring_lyric_cluster_001",
        "Lyric confidence": "0.91",
    }  # no verdict, reviewer, queue or vocalise: empty values are left out (C-30)
    assert corpus.detailRationale == "clean_singing rationale 0"
    assert window.find("detailRationale").property("text") == "clean_singing rationale 0"
    shown_entries = [obj.property("text") for obj in all_named(window, "detail-Captured")]
    assert "2019-01-05 20:00" in shown_entries

    full = server_rows(window)
    queued = next(r for r in full.values() if r["reviewQueueOrder"] == 1)
    open_detail(qtbot, corpus, queued["recordingId"])
    entries = {e["label"]: e["value"] for e in corpus.detailEntries}
    assert entries["Queue"] == f"#1 — {queued['reviewQueueReason']}"
    wordless = take(2)  # index % 9 == 2: a wordless vocalise
    open_detail(qtbot, corpus, wordless.recording_id)
    entries = {e["label"]: e["value"] for e in corpus.detailEntries}
    assert entries["Wordless vocalise"] == "marked"
    assert "Lyric cluster" not in entries
    reviewed = {
        **full[take(0).recording_id],
        "humanVerdict": "non_singing",
        "reviewedBy": "operator",
        "reviewedAt": "2026-10-11T08:00:00.000Z",
        "transcriptWordCount": None,
        "transcriptLanguage": None,
    }
    entries = {e["label"]: e["value"] for e in detail_entries(reviewed)}
    assert entries["Human verdict"] == "non singing" and entries["Reviewed by"] == "operator"
    assert entries["Reviewed at"] == "2026-10-11 08:00"
    assert entries["Transcript"] == "completed"


@pytest.mark.checklist("C-32")
def test_corpus_detail_listen(qtbot: Any, window: Window, quiet_window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    open_detail(qtbot, corpus, take(0).recording_id)
    bar = window.find("detailPlayer")
    assert bar.property("key") == f"corpus:{take(0).recording_id}"
    bar.findChild(QObject, "playButton").clicked.emit()
    player = window.model.player
    qtbot.waitUntil(lambda: player.state == "playing", timeout=10000)
    assert player.activeKey == f"corpus:{take(0).recording_id}"
    assert window.rig.output.played  # the decoded take went to the device
    # A take with no audio on the server: the player says why.
    corpus.closeDetail()
    open_detail(qtbot, corpus, take(9).recording_id)
    window.find("detailPlayer").findChild(QObject, "playButton").clicked.emit()
    qtbot.waitUntil(lambda: player.state == "failed", timeout=10000)
    assert "not in the corpus" in player.error

    quiet = open_corpus(qtbot, quiet_window)
    open_detail(qtbot, quiet, take(0).recording_id)
    bar = quiet_window.find("detailPlayer")
    assert not bar.findChild(QObject, "playButton").property("enabled")
    unavailable = bar.findChild(QObject, "playerUnavailable")
    assert unavailable.property("visible")
    assert unavailable.property("text") == "Playback requires the release-gated launch."


@pytest.mark.checklist("C-33")
def test_corpus_detail_transcript_on_request(
    qtbot: Any, window: Window, quiet_window: Window, synthetic_root: Path
) -> None:
    corpus = open_corpus(qtbot, window)
    open_detail(qtbot, corpus, take(0).recording_id)
    assert corpus.transcriptAllowed and corpus.transcriptState == "idle"
    assert corpus.transcriptText == ""  # nothing is fetched before the click
    view = window.find("viewTranscript")
    assert view.property("visible") and view.property("text") == "View local text"
    view.clicked.emit()
    qtbot.waitUntil(lambda: corpus.transcriptState == "loaded", timeout=10000)
    assert corpus.transcriptText == TRANSCRIPT_TEXT
    assert window.find("transcriptText").property("text") == TRANSCRIPT_TEXT
    # Another row drops it; closing the sheet drops it.
    open_detail(qtbot, corpus, take(3).recording_id)
    assert corpus.transcriptState == "idle" and corpus.transcriptText == ""
    corpus.loadTranscript()
    qtbot.waitUntil(lambda: corpus.transcriptState == "loaded", timeout=10000)
    corpus.closeDetail()
    assert corpus.transcriptState == "idle" and corpus.transcriptText == ""
    # Not completed: no button, the SPA's sentence.
    open_detail(qtbot, corpus, take(1).recording_id)  # its transcript failed
    assert not corpus.transcriptAllowed
    assert corpus.transcriptMessage == "No completed transcript."
    assert window.find("transcriptMessage").property("visible")
    corpus.loadTranscript()  # refused without the rule
    assert corpus.transcriptState == "idle"
    # The text file is gone on the server: the error shows.
    (synthetic_root / "artifacts" / "stt-transcripts" / f"{take(6).recording_id}.txt").unlink()
    open_detail(qtbot, corpus, take(6).recording_id)
    corpus.loadTranscript()
    qtbot.waitUntil(lambda: corpus.transcriptState == "failed", timeout=10000)
    assert "not available" in corpus.transcriptMessage
    # Text off on the server: the other sentence.
    quiet = open_corpus(qtbot, quiet_window)
    open_detail(qtbot, quiet, take(0).recording_id)
    assert not quiet.transcriptAllowed
    assert quiet.transcriptMessage == "Local transcript text is not enabled for this launch."
    # The answer of a closed sheet is dropped.
    open_detail(qtbot, corpus, take(9).recording_id)
    corpus.loadTranscript()
    corpus.closeDetail()
    qtbot.wait(200)
    assert corpus.transcriptState == "idle" and corpus.transcriptText == ""


@pytest.mark.checklist("C-34", "C-35")
def test_corpus_verdict_is_recorded(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    toasts: list[tuple[str, str]] = []
    window.model.toast.connect(lambda message, tone: toasts.append((message, tone)))
    zero = take(0)
    open_detail(qtbot, corpus, zero.recording_id)
    assert [c["label"] for c in corpus.verdictChoices] == [
        "clean singing",
        "noise contaminated singing",
        "music contaminated singing",
        "non singing",
        "still uncertain",
    ]
    reviewed_before = corpus.totals["reviewed"]
    window.find("verdictNote").setProperty("text", "  a synthetic note ")
    button = named(window, "verdict-non_singing")
    assert button.property("variant") == "secondary"
    button.clicked.emit()
    qtbot.waitUntil(lambda: toasts == [("Verdict recorded: non singing", "ok")], timeout=10000)
    assert corpus.detailVerdict == "non_singing"
    assert named(window, "verdict-non_singing").property("variant") == "primary"
    entries = {e["label"]: e["value"] for e in corpus.detailEntries}
    assert entries["Spot-check"] == "reviewed" and entries["Human verdict"] == "non singing"
    assert entries["Reviewed by"] == "operator" and "Reviewed at" in entries
    row = shown(corpus)[0]
    assert row["_key"] == zero.recording_id
    assert row["spotCheck"] == {"primary": "reviewed", "secondary": "non singing", "muted": False}
    assert corpus.totals["reviewed"] == reviewed_before + 1
    state = window.rig.served.store.current().rows_by_id[zero.recording_id]
    assert state["humanVerdict"] == "non_singing"
    corpus.submitVerdict("not_a_verdict", "")  # not one of the five: nothing sent
    assert len(toasts) == 1
    # A verdict on a row the table no longer shows, answered after the sheet closed: the
    # server records it; nothing on screen is touched.
    wait_reload(qtbot, corpus, lambda: corpus.setFilter("q", "lyric-cluster-002"))
    corpus.submitVerdict("still_uncertain", "")
    corpus.closeDetail()
    qtbot.waitUntil(lambda: len(toasts) == 2, timeout=10000)
    assert toasts[-1] == ("Verdict recorded: still uncertain", "ok")
    state = window.rig.served.store.current().rows_by_id[zero.recording_id]
    assert state["humanVerdict"] == "still_uncertain"


@pytest.mark.checklist("C-35")
def test_corpus_verdict_failure_toasts(
    qtbot: Any, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = open_corpus(qtbot, window)
    toasts: list[tuple[str, str]] = []
    window.model.toast.connect(lambda message, tone: toasts.append((message, tone)))
    open_detail(qtbot, corpus, take(0).recording_id)

    def refuse(self: ApiClient, request: Any) -> Any:
        raise ApiRefused("that is not an indexed recording (recording_not_found)", status=404)

    monkeypatch.setattr(ApiClient, "review_verdict", refuse)
    corpus.submitVerdict("clean_singing", "")
    qtbot.waitUntil(lambda: bool(toasts), timeout=10000)
    assert toasts == [
        ("Verdict failed: that is not an indexed recording (recording_not_found)", "danger")
    ]
    assert corpus.detailVerdict == "" and not corpus.verdictBusy
    assert shown(corpus)[0]["spotCheck"]["primary"] == "not reviewed"


@pytest.mark.checklist("C-36", "C-37")
def test_corpus_detail_states(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    sheet = window.find("rowDetail")
    corpus.openDetail("no-such-take")
    assert corpus.detailState == "loading" and corpus.detailTitle == "no-such-take"
    qtbot.waitUntil(lambda: sheet.property("visible"), timeout=5000)
    assert window.find("detailLoading").property("visible")
    qtbot.waitUntil(lambda: corpus.detailState == "failed", timeout=10000)
    assert "not an indexed recording" in corpus.detailError
    assert window.find("detailError").property("text") == corpus.detailError
    # Close.
    window.find("closeDetail").clicked.emit()
    qtbot.waitUntil(lambda: not sheet.property("visible"), timeout=5000)
    assert not corpus.detailOpen and corpus.detailId == ""
    # Escape closes it too.
    open_detail(qtbot, corpus, take(0).recording_id)
    qtbot.waitUntil(lambda: sheet.property("opened"), timeout=5000)
    QTest.keyClick(window.root, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not corpus.detailOpen, timeout=5000)
    # A late answer for a row that is no longer open is dropped.
    corpus.openDetail(take(0).recording_id)
    corpus.openDetail(take(1).recording_id)
    qtbot.waitUntil(lambda: corpus.detailState == "loaded", timeout=10000)
    qtbot.wait(100)
    assert corpus.detailTitle == take(1).filename


@pytest.mark.checklist("C-10")
def test_a_row_opened_as_the_sheet_closes_is_shown(qtbot: Any, window: Window) -> None:
    """A closing sheet stays visible through its exit transition. A row opened then (a quick
    click; a test, in one turn of the event loop) is shown, and the transition's end does
    not close it again (the parallel suite's lost details, under load)."""
    corpus = open_corpus(qtbot, window)
    sheet = window.find("rowDetail")
    open_detail(qtbot, corpus, take(0).recording_id)
    qtbot.waitUntil(lambda: sheet.property("opened"), timeout=5000)
    corpus.closeDetail()
    assert sheet.property("visible") and not sheet.property("opened")  # its exit runs
    open_detail(qtbot, corpus, take(3).recording_id)
    qtbot.waitUntil(lambda: sheet.property("opened"), timeout=5000)
    qtbot.wait(400)  # longer than any transition
    assert corpus.detailOpen and corpus.detailId == take(3).recording_id
    assert corpus.detailState == "loaded" and sheet.property("visible")
    # Closed here: the model closes as the sheet starts to, and a row opened during the
    # exit is shown too.
    QTest.keyClick(window.root, Qt.Key.Key_Escape)
    assert not corpus.detailOpen and sheet.property("visible")
    open_detail(qtbot, corpus, take(6).recording_id)
    qtbot.waitUntil(lambda: sheet.property("opened"), timeout=5000)
    qtbot.wait(400)
    assert corpus.detailOpen and corpus.detailId == take(6).recording_id
    assert sheet.property("visible")


# ---- show_query (Journey and Practice click-throughs) and the model's edges --------------


def test_show_query_opens_on_one_song(qtbot: Any, window: Window) -> None:
    corpus = window.model.corpus
    window.model.open_corpus_with_query("lyric-cluster-002")
    assert window.model.screen == "corpus"
    settled(qtbot, corpus)
    assert corpus.searchText == "lyric-cluster-002"
    assert {row["lyricMatchCluster"] for row in raw(corpus)} == {"lyric-cluster-002"}
    assert window.find("corpusSearch").property("text") == "lyric-cluster-002"
    wait_reload(qtbot, corpus, lambda: corpus.setFilter("year", "2019"))
    # A take playing that the table does not show: the table is left alone.
    corpus.play(take(0).recording_id)  # lyric-cluster-001, filtered out
    qtbot.waitUntil(lambda: window.model.player.state == "playing", timeout=10000)
    window.model.player.stop()
    wait_reload(qtbot, corpus, lambda: corpus.show_query("lyric-cluster-001"))
    assert corpus.filters["year"] == "" and corpus.searchText == "lyric-cluster-001"
    # Before the Corpus has loaded (or after a failure), a click-through loads it.
    corpus.forget()
    corpus.show_query("lyric-cluster-002")
    settled(qtbot, corpus)
    assert {row["lyricMatchCluster"] for row in raw(corpus)} == {"lyric-cluster-002"}


def test_signing_out_forgets_the_corpus(qtbot: Any, window: Window) -> None:
    corpus = open_corpus(qtbot, window)
    open_detail(qtbot, corpus, take(0).recording_id)
    window.model.signOut()
    assert corpus.state == "idle" and corpus.rows.rowCount() == 0
    assert not corpus.detailOpen and corpus.statusLine == "Loading rows…"
    corpus.refresh()  # signed out: nothing is asked
    corpus.show_query("x")
    corpus.loadMore()
    assert corpus.state == "idle" and corpus.searchText == "x"
    corpus.setFilter("unknown", "x")  # not a filter: ignored
    assert "unknown" not in corpus.filters
    corpus.setFilter("year", "2019")  # remembered; nothing is asked while signed out
    assert corpus.filters["year"] == "2019" and not corpus.loading


def test_a_failed_summary_shows_and_retries(
    qtbot: Any, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = window.model.corpus
    original = ApiClient.summary

    def down(self: ApiClient) -> Any:
        raise ApiUnavailable("the server is unavailable (internal_error: boom)")

    settled(qtbot, corpus)
    monkeypatch.setattr(ApiClient, "summary", down)
    corpus.forget()
    corpus.activate()
    qtbot.waitUntil(lambda: corpus.state == "failed", timeout=10000)
    assert named(window, "corpusError").property("visible")
    assert "unavailable" in corpus.error
    monkeypatch.setattr(ApiClient, "summary", original)
    corpus.activate()
    qtbot.waitUntil(lambda: corpus.state == "loaded", timeout=10000)


def test_the_select_is_controlled(qtbot: Any) -> None:
    """SuiSelect never assigns its own ``value``: a choice is only ``valueSelected``; the
    owner's binding carries it back, or a choice the owner does not take changes nothing."""
    import scshafe_qt
    from PySide6.QtQml import QQmlApplicationEngine
    from voice_lab_desktop.cli import qml_dir, teardown

    engine = QQmlApplicationEngine()
    scshafe_qt.register(engine)
    engine.addImportPath(str(qml_dir()))
    engine.loadData(
        b"import QtQuick\nimport VoiceLab.StandIn\n"
        b"Window { id: w; width: 400; height: 100; visible: true\n"
        b'  property string chosen: ""\n  property bool accept: true\n'
        b"  property var picked: []\n"
        b'  SuiSelect { objectName: "select"; label: "Bucket"; value: w.chosen\n'
        b'    options: [{value: "", label: "All"}, {value: "a", label: "A"},'
        b' {value: "b", label: "B"}]\n'
        b"    onValueSelected: (v) => { w.picked = w.picked.concat([v]);"
        b" if (w.accept) w.chosen = v } }\n}"
    )
    root = engine.rootObjects()[0]
    try:
        select = root.findChild(QObject, "select")
        combo = select.findChild(QObject, "combo")
        call(select, "select", "a")
        assert root.property("chosen") == "a" and select.property("value") == "a"
        assert select.property("currentLabel") == "A"
        root.setProperty("chosen", "")  # the owner's model resets: the select follows it
        assert select.property("value") == "" and select.property("currentLabel") == "All"
        assert combo.property("currentIndex") == 0
        root.setProperty("accept", False)
        call(select, "select", "b")  # the owner does not take it
        assert select.property("value") == "" and select.property("currentLabel") == "All"
        combo.setProperty("currentIndex", 2)  # what a pick in the popup does to the combo
        combo.activated.emit(2)
        assert root.property("picked").toVariant()[-1] == "b"
        assert combo.property("currentIndex") == 0  # given back to `value`
        root.setProperty("chosen", "a")
        assert combo.property("currentIndex") == 1 and select.property("currentLabel") == "A"
        call(select, "select", "nope")  # not an option
        call(select, "select", "a")  # already the value
        assert list(root.property("picked").toVariant()) == ["a", "b", "b"]
    finally:
        teardown(engine, lambda: None)


def test_formatting_follows_fmt_js() -> None:
    assert fmt_captured("2019-01-05T20:00:00Z") == "2019-01-05 20:00"
    assert fmt_captured("2019-01-05T20:00:00.123Z") == "2019-01-05 20:00"
    assert fmt_captured(None) == "" and fmt_captured("") == ""
    assert fmt_seconds(29.5) == "30s" and fmt_seconds(None) == ""
    assert fmt_bytes(1048576) == "1.00 MB" and fmt_bytes(None) == ""
    assert fmt_confidence(0.305) == "0.31" and fmt_confidence(None) == ""
    assert [bucket_tone(b) for b in ("clean_singing", "non_singing", "something")] == [
        "ok",
        "danger",
        "info",
    ]
    assert labelize("still_uncertain") == "still uncertain" and labelize(None) == ""
    assert text(1.0) == "1" and text(None) == ""


def test_table_row_without_optional_fields() -> None:
    row = table_row({"recordingId": "r", "spotCheckStatus": "reviewed"}, playback=True)
    assert row["queue"] == "" and row["cluster"] == "" and row["transcript"]["secondary"] == ""
    assert row["spotCheck"] == {"primary": "reviewed", "secondary": "", "muted": False}
    model = TableRows(lambda r: r)
    assert model.data(model.index(0), ROW) is None
    model.append([])
    model.refresh()  # empty: nothing to re-read
    model.append([{"recordingId": "r"}])
    assert model.data(model.index(0), ROW) == {"recordingId": "r"}
    assert model.data(model.index(0), int(Qt.ItemDataRole.DisplayRole)) is None
    assert model.index_of("recordingId", "missing") == -1
    assert audio_root is not None
