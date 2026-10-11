"""Referee (checklist R-01 to R-11): blind A/B trials over /api/v1, matched playback, the
reveal and the next pair, the gate, and the Bradley–Terry results by year."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import AppRig, Window, make_rig, open_window
from synthetic import audio_root, config_for, tone_m4a
from voice_lab_desktop.api import ApiClient, ApiRefused
from voice_lab_desktop.formatting import text as js_text
from voice_lab_desktop.playback import REFEREE_LOWPASS_HZ, decode, shape
from voice_lab_desktop.screens.referee import (
    REVEAL_ADVANCE_MS,
    bradley_terry_spec,
    choices_text,
    flags_text,
    gate_text,
    year_rows,
)


def open_referee(qtbot: Any, window: Window) -> Any:
    referee = window.model.referee
    window.model.navigate("referee")
    qtbot.waitUntil(lambda: referee.state == "loaded", timeout=10000)
    return referee


def seed(rig: AppRig, choices: list[str]) -> list[dict[str, Any]]:
    """Judge the pool's first pairs (one a choice) on the server, as earlier sessions did."""
    store = rig.served.store
    pairs = store.current().referee_pool[: len(choices)]
    for pair, choice in zip(pairs, choices, strict=True):
        store.record_judgment(pair, choice, None)
    return pairs


@pytest.fixture
def toasts(window: Window) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    window.model.toast.connect(lambda text, tone: seen.append((text, tone)))
    return seen


@pytest.mark.checklist("R-01")
def test_referee_states(qtbot: Any, tmp_path: Path) -> None:
    # The server runs without the journey routes: both requests are refused (503).
    with make_rig(tmp_path, None) as rig, open_window(rig) as window:
        referee = window.model.referee
        states: list[str] = []
        referee.changed.connect(lambda: states.append(referee.state))
        window.model.navigate("referee")
        qtbot.waitUntil(lambda: referee.state == "failed", timeout=10000)
        assert states[0] == "loading"
        assert "journey_unavailable" in referee.error
        assert window.find("refereeError").property("visible")
        assert not window.find("trialCard").property("visible")
        assert not window.find("resultsCard").property("visible")
        window.model.navigate("status")
        window.model.navigate("referee")  # a failed screen tries again when shown
        qtbot.waitUntil(lambda: "loading" in states[1:], timeout=10000)


def test_signed_out_the_referee_waits(qtbot: Any, tmp_path: Path, synthetic_root: Path) -> None:
    config = config_for(synthetic_root)
    with make_rig(tmp_path, config, signed_in=False) as rig, open_window(rig) as window:
        window.model.navigate("referee")
        referee = window.model.referee
        assert referee.state == "idle" and not referee.enabled and not referee.gated
        needs = [o for o in window.find_all("needsServer") if o.property("visible")]
        assert len(needs) == 1 and "Sign in to see the referee" in needs[0].property("title")


@pytest.mark.checklist("R-02")
def test_referee_gate_notice(qtbot: Any, tmp_path: Path, synthetic_root: Path) -> None:
    corpus = audio_root(tmp_path / "journey")
    config = config_for(synthetic_root, corpus=corpus, playback=False)
    with make_rig(tmp_path, config) as rig, open_window(rig) as window:
        seed(rig, ["a", "b"])
        pool = len(rig.served.store.current().referee_pool)
        referee = open_referee(qtbot, window)
        qtbot.waitUntil(lambda: referee.hasResults, timeout=10000)
        assert referee.gated and not referee.enabled and not referee.hasPair
        assert referee.gateText == gate_text(pool)
        assert f"{pool:,} blind pairs are waiting" in referee.gateText
        assert "Existing results still show below." in referee.gateText
        gate = window.find("refereeGate")
        assert gate.property("visible") and gate.property("text") == referee.gateText
        assert not window.find("trialCard").property("visible")
        assert window.find("resultsCard").property("visible")  # existing results still show
        referee.judge("a")  # nothing to judge without a pair
        assert not referee.submitting


@pytest.mark.checklist("R-03")
def test_referee_matched_playback(qtbot: Any, window: Window) -> None:
    referee = open_referee(qtbot, window)
    assert referee.hasPair and referee.lowpassHz == REFEREE_LOWPASS_HZ == 8000.0
    player = window.model.player
    output = window.rig.output
    tone = decode(tone_m4a())
    for side, gain in (("A", referee.aGainDb), ("B", referee.bGainDb)):
        bar = window.find(f"player{side}")
        source = referee.aSource if side == "A" else referee.bSource
        assert source.startswith("/api/v1/recordings/") and source.endswith("/audio")
        assert bar.property("source") == source
        assert bar.property("gainDb") == pytest.approx(gain)
        assert bar.property("lowpassHz") == 8000.0
        bar.findChild(type(bar), "playButton").clicked.emit()
        key = f"referee:{side.lower()}"
        qtbot.waitUntil(lambda key=key: player.activeKey == key and player.state == "playing")
        sent = output.played[-1].samples
        expected = shape(tone, gain_db=gain, lowpass_hz=REFEREE_LOWPASS_HZ).samples
        assert np.allclose(sent, expected, atol=1e-6)
        # The 8 kHz low-pass took out what lies above it; the gain set the level.
        spectrum = np.abs(np.fft.rfft(sent[:, 0]))
        freqs = np.fft.rfftfreq(sent.shape[0], 1 / output.played[-1].rate)
        assert float(spectrum[freqs > 8000].max(initial=0.0)) < 1e-3
    gains = {referee.aGainDb, referee.bGainDb}
    assert gains != {0.0}  # the synthetic takes differ in level: matching is not a no-op


@pytest.mark.checklist("R-04", "R-05", "R-06", "R-11")
def test_referee_judgment_and_reveal(qtbot: Any, window: Window) -> None:
    referee = open_referee(qtbot, window)
    store = window.rig.served.store
    pool = len(store.current().referee_pool)
    assert window.find("progressLine").property("text") == f"0 of {pool} pairs judged."
    assert not referee.hasResults
    buttons = {
        c: window.find(f"choice-{c}") for c in ("a", "b", "too_close", "not_same_song", "skip")
    }
    assert [b.property("text") for b in buttons.values()] == [
        "A sounds better",
        "B sounds better",
        "Too close",
        "Not the same song",
        "Skip",
    ]
    assert all(b.property("enabled") for b in buttons.values())
    assert buttons["a"].property("variant") == "primary"
    assert buttons["skip"].property("variant") == "secondary"
    window.find("playerA").findChild(type(buttons["a"]), "playButton").clicked.emit()
    player = window.model.player
    qtbot.waitUntil(lambda: player.state == "playing")

    first = referee.pairId
    pair = store.find_pair(first)
    buttons["a"].clicked.emit()
    assert referee.submitting and referee.lastChoice == "a"
    assert player.state == "idle"  # judging stops playback
    assert not any(b.property("enabled") for b in buttons.values())
    referee.judge("b")  # a second click while sending is ignored
    qtbot.waitUntil(lambda: referee.revealed, timeout=10000)
    expected = (
        f"A was {pair['aCapturedAt'][:10]} · B was {pair['bCapturedAt'][:10]} · {pair['clusterId']}"
    )
    assert referee.revealText == expected
    reveal = window.find("revealLine")
    assert reveal.property("visible") and reveal.property("text") == expected
    assert not any(b.property("enabled") for b in buttons.values())  # revealed
    assert referee.progressText == f"1 of {pool} pairs judged."
    assert store.referee_fit()["judgedTotal"] == 1
    qtbot.waitUntil(lambda: referee.hasResults, timeout=10000)  # refreshed after judging
    assert referee.yearCount == 2 and referee.hasChart
    assert referee.timer.isActive() and referee.timer.interval() == REVEAL_ADVANCE_MS == 1400
    qtbot.waitUntil(lambda: referee.hasPair and referee.pairId != first, timeout=5000)
    assert not referee.revealed and referee.revealText == ""
    assert window.find("progressLine").property("text") == f"1 of {pool} pairs judged."
    assert all(b.property("enabled") for b in buttons.values())

    buttons["skip"].clicked.emit()
    qtbot.waitUntil(lambda: referee.revealed, timeout=10000)
    referee.shutdown()  # the pause before the next pair ends with the app
    assert not referee.timer.isActive()


def test_a_failed_judgment_toasts_and_unlocks(
    qtbot: Any, window: Window, toasts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    referee = open_referee(qtbot, window)

    def refused(self: ApiClient, request: Any) -> Any:
        raise ApiRefused("no pair of the pool has that id (pair_not_found)", status=404)

    monkeypatch.setattr(ApiClient, "referee_verdict", refused)
    referee.judge("too_close")
    qtbot.waitUntil(lambda: not referee.submitting, timeout=10000)
    assert toasts[-1] == (
        "Judgment failed: no pair of the pool has that id (pair_not_found)",
        "danger",
    )
    assert not referee.revealed and referee.lastChoice == "" and referee.hasPair
    referee.judge("nonsense")  # not a choice
    assert not referee.submitting


@pytest.mark.checklist("R-06")
def test_referee_pool_complete(qtbot: Any, tmp_path: Path, synthetic_root: Path) -> None:
    corpus = audio_root(tmp_path / "journey")
    with make_rig(tmp_path, config_for(synthetic_root, corpus=corpus)) as rig:
        pool = rig.served.store.current().referee_pool
        for pair in pool:
            rig.served.store.record_judgment(pair, "skip", None)
        with open_window(rig) as window:
            referee = open_referee(qtbot, window)
            assert referee.enabled and not referee.hasPair
            text = f"Pool complete: {len(pool)} of {len(pool)} pairs judged."
            complete = window.find("poolComplete")
            assert complete.property("visible") and complete.property("text") == text
            assert not window.find("progressLine").property("visible")
            assert not window.find("choice-a").property("visible")


@pytest.mark.checklist("R-07")
def test_referee_audio_error_toasts_once(
    qtbot: Any, window: Window, toasts: list[tuple[str, str]]
) -> None:
    for memo in (window.rig.tmp / "journey" / "corpus").iterdir():
        memo.unlink()  # the server can no longer read any take
    referee = open_referee(qtbot, window)
    player = window.model.player

    def play(side: str) -> None:
        bar = window.find(f"player{side}")
        bar.findChild(type(bar), "playButton").clicked.emit()
        qtbot.waitUntil(lambda: player.state == "failed", timeout=10000)

    play("A")
    play("B")
    failures = [t for t in toasts if t[0].startswith("Track failed to load")]
    assert len(failures) == 1 and failures[0][1] == "danger"
    assert "audio_not_found" in failures[0][0]
    assert "the server's read access to the corpus" in failures[0][0]
    assert "Full Disk Access" not in failures[0][0]
    first = referee.pairId
    referee.judge("skip")
    qtbot.waitUntil(lambda: referee.revealed, timeout=10000)
    referee.advance()  # the next pair now, not after the pause
    qtbot.waitUntil(lambda: referee.hasPair and referee.pairId != first, timeout=10000)
    play("A")
    assert len([t for t in toasts if t[0].startswith("Track failed to load")]) == 2
    player.failed.emit("corpus:x", "elsewhere")  # another screen's take: not the referee's
    assert len([t for t in toasts if t[0].startswith("Track failed to load")]) == 2


@pytest.mark.checklist("R-08", "R-09", "R-10")
def test_referee_results(qtbot: Any, window: Window) -> None:
    seeded = seed(window.rig, ["a", "b", "too_close", "not_same_song", "skip", "a", "b"])
    fit = window.rig.served.store.referee_fit()
    referee = open_referee(qtbot, window)
    qtbot.waitUntil(lambda: referee.hasResults, timeout=10000)
    years = fit["years"]
    assert len(years) >= 2
    spec = referee.chartSpec
    assert spec == bradley_terry_spec(years)
    assert window.find("btChart").property("visible")
    assert [t["label"] for t in spec["xTicks"]] == [str(row["year"]) for row in years]
    panel = spec["panels"][0]
    kinds = [layer["kind"] for layer in panel["layers"]]
    assert kinds == ["rule", "line", "points", "text"]
    rule, line, points, text = panel["layers"]
    assert rule["y"] == 0 and rule["label"] == "0"
    assert line["points"] == [[i, float(row["strengthLog2"])] for i, row in enumerate(years)]
    for point, row in zip(points["points"], years, strict=True):
        assert point["r"] == pytest.approx(max(3, min(7, 2 + math.sqrt(row["trials"]))))
    for label, row in zip(text["items"], years, strict=True):
        sign = "+" if row["strengthLog2"] >= 0 else ""
        assert label["text"] == f"{sign}{js_text(row['strengthLog2'])}"
        assert label["y"] > row["strengthLog2"]  # over its dot
    values = [row["strengthLog2"] for row in years]
    assert panel["yMin"] < min(*values, 0) and panel["yMax"] > max(*values, 0)

    table = window.find("yearTable")
    assert table.property("count") == len(years)
    assert referee.yearRows.rows == year_rows(years)
    assert [c["title"] for c in referee.yearColumns] == [
        "Year",
        "Strength (log2)",
        "Wins",
        "Losses",
        "Ties",
        "Trials",
    ]
    by_choice = fit["byChoice"]
    assert by_choice == {"a": 2, "b": 2, "too_close": 1, "not_same_song": 1, "skip": 1}
    choices = window.find("choicesLine").property("text")
    assert choices == "choices: A 2 · B 2 · too close 1 · not same song 1 · skip 1"
    flagged = seeded[3]["clusterId"]
    flags = window.find("flagsLine")
    assert flags.property("visible")
    assert flags.property("text") == (
        f"Chained-cluster flags: {flagged} ×1 — these feed the cluster repair pass."
    )


def test_results_helpers_follow_the_spa() -> None:
    assert bradley_terry_spec([]) == {} and bradley_terry_spec([{"year": "2019"}]) == {}
    assert flags_text([]) == ""
    assert choices_text({}) == "choices: A 0 · B 0 · too close 0 · not same song 0 · skip 0"
    assert [js_text(v) for v in (2.0, 0.125, 3, None, True)] == ["2", "0.125", "3", "", "true"]
    rows = year_rows([{"year": "2020", "strengthLog2": 0, "wins": 1, "losses": 0}])
    assert rows == [
        {
            "_key": "2020",
            "year": "2020",
            "strength": "0",
            "wins": "1",
            "losses": "0",
            "ties": "",
            "trials": "",
        }
    ]
    spec = bradley_terry_spec(
        [{"year": "2019", "strengthLog2": 0, "trials": 0}, {"year": "2020", "strengthLog2": -0.5}]
    )
    labels = [item["text"] for item in spec["panels"][0]["layers"][3]["items"]]
    assert labels == ["+0", "-0.5"]
    assert spec["panels"][0]["layers"][2]["points"][1]["r"] == 3.0


def test_a_single_judged_year_has_no_chart(qtbot: Any, window: Window) -> None:
    store = window.rig.served.store
    pair = next(p for p in store.current().referee_pool)
    store.record_judgment(pair, "not_same_song", None)  # judged, but no year compared
    referee = open_referee(qtbot, window)
    qtbot.waitUntil(lambda: referee.hasResults, timeout=10000)
    assert referee.yearCount == 0 and not referee.hasChart and referee.chartSpec == {}
    assert not window.find("btChart").property("visible")


def test_another_refusal_is_a_failure_not_the_gate(
    qtbot: Any, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(self: ApiClient) -> Any:
        raise ApiRefused("the server refused (forbidden)", status=403, code="forbidden")

    monkeypatch.setattr(ApiClient, "referee_next", refused)
    referee = window.model.referee
    window.model.navigate("referee")
    qtbot.waitUntil(lambda: referee.state == "failed", timeout=10000)
    assert not referee.gated and referee.error == "the server refused (forbidden)"


def test_a_loaded_referee_is_not_reloaded_and_the_table_model_is_strict(
    qtbot: Any, window: Window
) -> None:
    from PySide6.QtCore import QModelIndex, Qt

    referee = open_referee(qtbot, window)
    first = referee.pairId
    states: list[str] = []
    referee.changed.connect(lambda: states.append(referee.state))
    window.model.navigate("status")
    window.model.navigate("referee")  # shown again: the same pair, no request
    assert states == [] and referee.pairId == first
    model = referee.yearRows
    model.replace([{"_key": "2020", "year": "2020"}])
    model.replace([{"_key": "2020", "year": "2020"}])  # the same rows: no reset
    assert model.rowCount() == 1 and model.roleNames() == {model.ROW_ROLE: b"row"}
    assert model.data(model.index(0, 0), model.ROW_ROLE) == {"_key": "2020", "year": "2020"}
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) is None
    assert model.data(QModelIndex(), model.ROW_ROLE) is None
    assert model.rowCount(model.index(0, 0)) == 0
