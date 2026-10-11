"""Analysis → Practice (checklist P-01 to P-05): the coach and pre-registered goals."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AppRig, Window, make_rig, open_window
from journey_browser_fixtures import write_manifests
from PySide6.QtCore import Q_ARG, QMetaObject
from PySide6.QtQuick import QQuickItem
from synthetic import config_for, data_root, takes
from voice_lab_desktop.api import ApiClient, ApiRefused
from voice_lab_desktop.screens import practice as practice_module
from voice_lab_desktop.screens.practice import (
    CREATED,
    due_empty_text,
    goal_request,
    text,
)

GOALS_STATE = "voice-journey.goals-state.v1"


# ---- helpers ------------------------------------------------------------------------------


def _walk(item: QQuickItem) -> list[QQuickItem]:
    found = [item]
    for child in item.childItems():
        found.extend(_walk(child))
    return found


def items(window: Window) -> list[QQuickItem]:
    """The Practice screen's visual items (Repeater delegates included), in order."""
    screen = [
        i for i in _walk(window.root.property("contentItem")) if i.objectName() == "practiceScreen"
    ]
    assert len(screen) == 1
    return _walk(screen[0])


def named(window: Window, name: str) -> list[Any]:
    return [item for item in items(window) if item.objectName() == name]


def one(window: Window, name: str) -> Any:
    found = named(window, name)
    assert len(found) == 1, (name, len(found))
    return found[0]


def visible_texts(window: Window, name: str) -> list[str]:
    return [i.property("text") for i in named(window, name) if i.isVisible()]


def show_practice(qtbot: Any, window: Window) -> Any:
    practice = window.model.practice
    window.model.navigate("practice")
    qtbot.waitUntil(lambda: practice.state == "loaded" and practice.goalsState == "loaded")
    return practice


def toasts_of(model: Any) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    model.toast.connect(lambda message, tone: seen.append((message, tone)))
    return seen


def type_into(field: Any, value: str) -> None:
    """Edit a text field as typing does (its binding to the model stays)."""
    QMetaObject.invokeMethod(field, "selectAll")
    QMetaObject.invokeMethod(
        field, "remove", Q_ARG(int, 0), Q_ARG(int, len(field.property("text")))
    )
    QMetaObject.invokeMethod(field, "insert", Q_ARG(int, 0), Q_ARG(str, value))


def write_goals(rig: AppRig, goals: list[dict[str, Any]]) -> None:
    path = Path(rig.served.store.config.goals_state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": GOALS_STATE, "goals": goals}), encoding="utf-8")


def goal(goal_id: str, created_at: str, **fields: Any) -> dict[str, Any]:
    return {
        "goalId": goal_id,
        "createdAt": created_at,
        "status": "open",
        "metric": "cpps",
        "direction": "at_least",
        "target": 9,
        "takesRequired": 3,
        "clusterId": None,
        "title": f"goal {goal_id}",
        **fields,
    }


@pytest.fixture
def clusterless_rig(tmp_path: Path) -> Iterator[AppRig]:
    """The synthetic corpus without lyric clusters: nothing is due, nothing is a frontier."""
    data = data_root(tmp_path / "journey")
    lyrics = {
        "schemaVersion": "voice-journey.local-lyric-indicators.v1",
        "totals": {"recordings": 0},
        "clusters": [],
        "results": [],
    }
    write_manifests(data, lyrics=lyrics)
    with make_rig(tmp_path, config_for(data)) as made:
        yield made


# ---- P-01 ---------------------------------------------------------------------------------


@pytest.mark.checklist("P-01")
def test_practice_states(qtbot: Any, window: Window, tmp_path: Path) -> None:
    practice = window.model.practice
    loading = one(window, "practiceLoading")
    assert practice.state == "idle" and loading.property("text") == "Consulting the coach…"
    window.model.navigate("practice")
    assert practice.state == "loading" and loading.isVisible()
    qtbot.waitUntil(lambda: practice.state == "loaded")
    assert not loading.isVisible() and not one(window, "practiceError").isVisible()
    assert one(window, "dueCard").isVisible() and one(window, "goalsCard").isVisible()
    # A second visit asks again, keeping what it shows meanwhile.
    window.model.navigate("corpus")
    window.model.navigate("practice")
    assert practice.state == "loaded"
    # Signing out forgets everything; the screen asks for a sign-in instead.
    window.model.signOut()
    assert practice.state == "idle" and practice.dueSongs == [] and practice.goals == []
    assert one(window, "needsServer").isVisible()
    practice.refresh()  # signed out: asks nothing
    practice.refreshGoals()
    assert practice.state == "idle" and practice.goalsState == "idle"


@pytest.mark.checklist("P-01")
def test_practice_failure_notice(qtbot: Any, tmp_path: Path) -> None:
    with make_rig(tmp_path, None) as rig, open_window(rig) as window:
        practice = rig.model.practice
        rig.model.navigate("practice")
        qtbot.waitUntil(lambda: practice.state == "failed" and practice.goalsState == "failed")
        notice = one(window, "practiceError")
        assert notice.isVisible() and notice.property("title") == "The coach did not answer"
        assert "journey_unavailable" in practice.error
        assert "journey_unavailable" in practice.goalsError
        assert not one(window, "dueCard").isVisible()
        assert not one(window, "goalsCard").isVisible()  # as the SPA: the notice alone


# ---- P-02, P-03 ---------------------------------------------------------------------------


@pytest.mark.checklist("P-02", "P-03")
def test_practice_due_and_frontier(qtbot: Any, window: Window, monkeypatch: Any) -> None:
    practice = show_practice(qtbot, window)
    clusters = {t.cluster for t in takes() if t.cluster}
    due = {row["clusterId"]: row for row in practice.dueSongs}
    assert set(due) == clusters
    first = due["lyric-cluster-001"]
    assert first["label"] == "recurring_lyric_cluster_001"
    assert first["takes"] == "18 takes" and first["years"] == "2019–2024"
    assert first["lastSung"].endswith(" d ago") and ".0" not in first["lastSung"]
    assert first["why"][0] == f"last sung {first['lastSung']}"
    assert first["why"][1] == "18 takes, 2019–2024"
    assert first["why"][2].startswith("in the same-song index (18 takes used)")
    assert not one(window, "dueEmpty").isVisible()
    assert first["why"][0] in visible_texts(window, "whyChip")
    assert "18 takes" in visible_texts(window, "dueTakes")

    frontier = {row["clusterId"]: row for row in practice.frontier}
    assert set(frontier) == clusters
    assert frontier["lyric-cluster-001"]["rules"] == ["range stretch", "vibrato settling"]
    details = frontier["lyric-cluster-001"]["details"]
    assert details[0].startswith("top notes sit ") and details[0].endswith("a stretch, not a leap")
    assert "one nudge from the settled 5.5 Hz zone" in details[1]
    assert set(visible_texts(window, "ruleChip")) == {"range stretch", "vibrato settling"}
    assert details[1] in visible_texts(window, "frontierDetail")
    assert not one(window, "frontierEmpty").isVisible()

    shown: list[str] = []
    monkeypatch.setattr(window.model.corpus, "show_query", shown.append)
    named(window, "dueSong")[0].clicked.emit()
    assert window.model.screen == "corpus" and shown == [practice.dueSongs[0]["clusterId"]]
    window.model.navigate("practice")
    named(window, "frontierSong")[1].clicked.emit()
    assert window.model.screen == "corpus" and shown[-1] == practice.frontier[1]["clusterId"]
    practice.openCluster("")  # nothing to open
    assert len(shown) == 2


@pytest.mark.checklist("P-02", "P-03")
def test_practice_empty_due_and_frontier(qtbot: Any, clusterless_rig: AppRig) -> None:
    with open_window(clusterless_rig) as window:
        practice = show_practice(qtbot, window)
        assert practice.dueSongs == [] and practice.frontier == []
        empty = one(window, "dueEmpty")
        assert empty.isVisible() and empty.property("text") == (
            "Nothing due — every recurring song has a take inside the 90-day window. "
            "Go sing something new."
        )
        assert one(window, "frontierEmpty").isVisible()
        assert practice.scopeOptions == [{"value": "", "label": "Whole corpus"}]
    assert due_empty_text({}) == due_empty_text({"params": {"dueAfterDays": 90}})
    assert "inside the 30-day window" in due_empty_text({"params": {"dueAfterDays": 30}})


# ---- P-04 ---------------------------------------------------------------------------------


@pytest.mark.checklist("P-04")
def test_practice_goals_list(qtbot: Any, rig: AppRig, monkeypatch: Any) -> None:
    write_goals(
        rig,
        [
            goal("achieved", "2018-01-01T00:00:00.000Z", clusterId="lyric-cluster-001"),
            goal("missed", "2018-01-02T00:00:00.000Z", target=50),
            goal("active", "2099-01-01T00:00:00.000Z", target=5.5, takesRequired=10),
            goal(
                "abandoned",
                "2018-01-03T00:00:00.000Z",
                status="abandoned",
                abandonedAt="2026-10-01T12:00:00.000Z",
            ),
        ],
    )
    with open_window(rig) as window:
        practice = show_practice(qtbot, window)
        rows = {row["goalId"]: row for row in practice.goals}
        assert [row["goalId"] for row in practice.goals] == [
            "active",
            "abandoned",
            "missed",
            "achieved",
        ]  # newest first
        assert rows["achieved"]["status"] == "achieved" and rows["achieved"]["tone"] == "ok"
        assert rows["achieved"]["scope"] == "lyric-cluster-001"
        assert rows["achieved"]["progress"] == (
            "3 of 3 takes · running median 10 vs target 9 · registered 2018-01-01"
        )
        assert (rows["missed"]["status"], rows["missed"]["tone"]) == ("missed", "danger")
        assert rows["missed"]["scope"] == "whole corpus"
        assert rows["active"]["progress"] == (
            "0 of 10 takes · running median — vs target 5.5 · registered 2099-01-01"
        )
        assert (rows["active"]["tone"], rows["active"]["canAbandon"]) == ("info", True)
        assert rows["abandoned"]["progress"] == "abandoned 2026-10-01"
        assert (rows["abandoned"]["tone"], rows["abandoned"]["canAbandon"]) == ("neutral", False)
        assert not rows["achieved"]["canAbandon"]
        assert visible_texts(window, "goalTitle") == [
            "goal active",
            "goal abandoned",
            "goal missed",
            "goal achieved",
        ]
        assert visible_texts(window, "goalStatus") == ["active", "abandoned", "missed", "achieved"]
        abandon = [b for b in named(window, "abandonGoal") if b.isVisible()]
        assert len(abandon) == 1
        assert not one(window, "goalsEmpty").isVisible()

        # Goals that do not load: a notice in the card; the rest of the page stays.
        def refuse(self: ApiClient, kind: str) -> Any:
            raise ApiRefused("no goals today (forbidden)", status=403, code="forbidden")

        monkeypatch.setattr(ApiClient, "document", refuse)
        practice.forget()
        practice.refreshGoals()
        qtbot.waitUntil(lambda: practice.goalsState == "failed")
        assert practice.goalsError == "no goals today (forbidden)"


@pytest.mark.checklist("P-04")
def test_practice_goal_abandon(qtbot: Any, window: Window) -> None:
    practice = show_practice(qtbot, window)
    assert practice.goals == [] and one(window, "goalsEmpty").isVisible()
    practice.setField("metric", "cpps")
    practice.setField("target", "9")
    practice.submitGoal()
    qtbot.waitUntil(lambda: len(practice.goals) == 1)
    assert practice.goals[0]["status"] == "active"
    button = [b for b in named(window, "abandonGoal") if b.isVisible()]
    assert len(button) == 1
    button[0].clicked.emit()
    qtbot.waitUntil(lambda: practice.goals[0]["status"] == "abandoned")
    row = practice.goals[0]
    assert row["progress"].startswith("abandoned 20") and not row["canAbandon"]
    assert [b for b in named(window, "abandonGoal") if b.isVisible()] == []

    seen = toasts_of(practice)
    practice.abandonGoal("no-such-goal")
    qtbot.waitUntil(lambda: bool(seen))
    assert seen[0][1] == "danger" and seen[0][0].startswith("Goal not abandoned: ")
    assert "goal_not_found" in seen[0][0]


# ---- P-05 ---------------------------------------------------------------------------------


@pytest.mark.checklist("P-05")
def test_practice_goal_form(qtbot: Any, window: Window, monkeypatch: Any) -> None:
    practice = show_practice(qtbot, window)
    seen = toasts_of(practice)
    assert [o["label"] for o in practice.metricOptions] == [
        "Choose…",
        "vibrato rate (Hz)",
        "vibrato extent (cents)",
        "clarity — CPPS (dB)",
        "longest sustain (s)",
        "top of range — p95 (Hz)",
        "tuning error (cents)",
    ]
    assert practice.directionOptions == [
        {"value": "at_least", "label": "at least"},
        {"value": "at_most", "label": "at most"},
    ]
    assert practice.scopeOptions == [
        {"value": "", "label": "Whole corpus"},
        {"value": "lyric-cluster-001", "label": "recurring_lyric_cluster_001 (18)"},
        {"value": "lyric-cluster-002", "label": "recurring_lyric_cluster_002 (12)"},
    ]
    metric, direction, scope = (
        one(window, "goalMetric"),
        one(window, "goalDirection"),
        one(window, "goalScopeSelect"),
    )
    target, takes_field, title = (
        one(window, "goalTarget"),
        one(window, "goalTakes"),
        one(window, "goalTitleField"),
    )
    submit = one(window, "preRegister")
    assert (practice.formMetric, practice.formDirection, practice.formTakes) == (
        "",
        "at_least",
        "10",
    )
    assert metric.property("currentLabel") == "Choose…" and not submit.property("enabled")
    practice.setField("colour", "blue")  # not a field of the form
    assert practice.formTitle == "" and not practice.canSubmit

    # The form's controls write the model; Pre-register waits for a metric and a number.
    metric.valueSelected.emit("vibratoRateHz")
    assert practice.formMetric == "vibratoRateHz" and not practice.canSubmit
    type_into(target, "abc")
    assert practice.formTarget == "abc" and not practice.canSubmit
    type_into(target, "5.5")
    assert practice.canSubmit and submit.property("enabled")
    direction.valueSelected.emit("at_most")
    scope.valueSelected.emit("lyric-cluster-001")
    type_into(takes_field, "")  # Number("") || 10
    type_into(title, "  settle vibrato on Song C  ")
    assert (practice.formDirection, practice.formCluster) == ("at_most", "lyric-cluster-001")

    submit.clicked.emit()
    # The form empties at once, controls included.
    assert (practice.formMetric, practice.formTarget, practice.formTakes) == ("", "", "10")
    assert target.property("text") == "" and takes_field.property("text") == "10"
    assert title.property("text") == ""
    assert metric.property("currentLabel") == "Choose…"
    assert direction.property("value") == "at_least" and scope.property("value") == ""
    qtbot.waitUntil(lambda: len(practice.goals) == 1)
    assert seen == [(CREATED, "ok")]
    created = practice.goals[0]
    assert created["title"] == "settle vibrato on Song C"
    assert created["scope"] == "lyric-cluster-001" and created["status"] == "active"
    assert created["progress"].startswith("0 of 10 takes · running median — vs target 5.5")

    # Without a title the server names the goal; takes "0" is the SPA's default 10.
    practice.setField("metric", "cpps")
    practice.setField("target", "11")
    practice.setField("takesRequired", "0")
    practice.submitGoal()
    qtbot.waitUntil(lambda: len(practice.goals) == 2)
    assert practice.goals[0]["title"] == "clarity — CPPS (dB) ≥ 11"

    # Refusals: the form's own, and the server's.
    practice.setField("metric", "cpps")
    practice.setField("target", "9")
    practice.setField("takesRequired", "2")
    practice.submitGoal()
    assert seen[-1][1] == "danger" and seen[-1][0].startswith("Goal not created: Takes (N): ")
    practice.setField("metric", "cpps")
    practice.setField("target", "9")
    practice.setField("takesRequired", "2.5")
    practice.submitGoal()
    assert seen[-1] == ("Goal not created: Takes (N) must be a whole number", "danger")

    def refuse(self: ApiClient, request: Any) -> Any:
        raise ApiRefused("unknown_metric: no (invalid_goal)", status=400, code="invalid_goal")

    monkeypatch.setattr(ApiClient, "create_goal", refuse)
    practice.setField("metric", "cpps")
    practice.setField("target", "9")
    practice.submitGoal()
    qtbot.waitUntil(lambda: seen[-1][0] == "Goal not created: unknown_metric: no (invalid_goal)")
    assert seen[-1][1] == "danger" and len(practice.goals) == 2
    practice.submitGoal()  # the form is empty again: nothing happens
    assert len(seen) == 5


@pytest.mark.checklist("P-05")
def test_practice_form_rules() -> None:
    base = {
        "metric": "cpps",
        "direction": "at_least",
        "target": "1e1",
        "takesRequired": "12",
        "clusterId": "",
        "title": " ",
    }
    request = goal_request(base)
    assert (request.target, request.takes_required) == (10.0, 12)
    assert (request.cluster_id, request.title) == (None, None)
    with pytest.raises(ValueError, match="choose a metric"):
        goal_request({**base, "metric": ""})
    with pytest.raises(ValueError, match="choose a metric"):
        goal_request({**base, "target": "Infinity"})
    assert practice_module.target_number("") is None
    assert practice_module.target_number(" 7 ") == 7.0
    assert text(None) == "" and text(763.0) == "763" and text(4.8) == "4.8"
    from cole_voice_lab.journey.jscompat import MISSING

    assert text(MISSING) == ""
    assert practice_module.goal_rows({"goals": "not a list"}) == []
    assert practice_module.frontier_rows({"frontier": [{"flags": [{"rule": "new-rule"}]}]}) == [
        {"clusterId": "", "label": "", "rules": ["new-rule"], "details": [""]}
    ]
