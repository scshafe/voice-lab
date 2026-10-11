"""Analysis → Practice (design §6.1; checklist P): the coach and pre-registered goals.

The port of voice-journey's Practice page (``web/src/components/practice/
PracticePageComponent.jsx`` with ``CoachManager.js`` and ``GoalsManager.js``) onto
``/api/v1``: ``GET /api/v1/coach`` (due songs, the frontier; the server computes both from
the live manifests, so every visit refetches), ``GET /api/v1/goals`` (the goals with their
derived status, the metrics, the cluster options), ``POST /api/v1/goals`` and ``POST
/api/v1/goals/{id}/abandon``, each mutation followed by a refetch so the derived statuses
stay the server's. Due and frontier songs open the Corpus filtered to their song.

Numbers inside the documents print as JavaScript's ``String()`` did (``763`` rather than
``763.0``), so every line reads as the SPA's did.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from pydantic import ValidationError
from PySide6.QtCore import Signal, Slot

from cole_voice_lab.api.journey_contracts import GoalRequest, JourneyDocument
from cole_voice_lab.journey import jscompat as js

from ..viewmodel import AppContext, ViewModel, qproperty

RULE_LABELS: Final = {
    "range-stretch": "range stretch",
    "vibrato-settling": "vibrato settling",
    "sustain-pb": "sustain PB",
    "tuning-near": "nearly in tune",
    "clarity-thin": "evidence-thin",
}
#: The SPA's goal chip classes as SuiChip tones: flat (active), improving (achieved),
#: declining (missed), insufficient_data (abandoned).
GOAL_TONES: Final = {"active": "info", "achieved": "ok", "missed": "danger", "abandoned": "neutral"}
DIRECTION_OPTIONS: Final = (
    {"value": "at_least", "label": "at least"},
    {"value": "at_most", "label": "at most"},
)
EMPTY_FORM: Final = {
    "metric": "",
    "direction": "at_least",
    "target": "",
    "takesRequired": "10",
    "clusterId": "",
    "title": "",
}
CREATED: Final = "Goal pre-registered — the next qualifying takes decide it."


def text(value: Any) -> str:
    """``String(value)`` for a JSON value (``null`` and absent as ``""``)."""
    if value is None or value is js.MISSING:
        return ""
    return js.to_string(value)


def _list(document: Mapping[str, Any], key: str) -> list[Any]:
    value = document.get(key)
    return list(value) if isinstance(value, list) else []


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def due_rows(coach: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``DueCard``'s rows."""
    rows = []
    for song in _list(coach, "dueSongs"):
        song = _map(song)
        rows.append(
            {
                "clusterId": text(song.get("clusterId")),
                "label": text(song.get("label")),
                "takes": f"{text(song.get('takeCount'))} takes",
                "years": f"{text(song.get('firstYear'))}–{text(song.get('lastYear'))}",
                "lastSung": f"{text(song.get('daysSinceLast'))} d ago",
                "why": [text(reason) for reason in _list(song, "why")],
            }
        )
    return rows


def due_empty_text(coach: Mapping[str, Any]) -> str:
    days = _map(coach.get("params")).get("dueAfterDays")
    window = text(days) if days is not None else "90"
    return (
        f"Nothing due — every recurring song has a take inside the {window}-day window. "
        "Go sing something new."
    )


def frontier_rows(coach: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``FrontierCard``'s rows: rule chips, then one detail line per flag."""
    rows = []
    for entry in _list(coach, "frontier"):
        entry = _map(entry)
        flags = [_map(flag) for flag in _list(entry, "flags")]
        rows.append(
            {
                "clusterId": text(entry.get("clusterId")),
                "label": text(entry.get("label")),
                "rules": [RULE_LABELS.get(text(f.get("rule")), text(f.get("rule"))) for f in flags],
                "details": [text(f.get("detail")) for f in flags],
            }
        )
    return rows


def goal_rows(goals: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``GoalsCard``'s list."""
    rows = []
    for goal in _list(goals, "goals"):
        goal = _map(goal)
        status = text(goal.get("derivedStatus"))
        progress = goal.get("progress")
        if isinstance(progress, Mapping):
            median = progress.get("runningMedian")
            line = (
                f"{text(progress.get('takesCounted'))} of {text(progress.get('takesRequired'))} "
                f"takes · running median {text(median) if median is not None else '—'} "
                f"vs target {text(goal.get('target'))} · registered "
                f"{text(goal.get('createdAt'))[:10]}"
            )
        else:
            line = f"abandoned {text(goal.get('abandonedAt'))[:10]}"
        cluster = text(goal.get("clusterId"))
        rows.append(
            {
                "goalId": text(goal.get("goalId")),
                "title": text(goal.get("title")),
                "status": status,
                "tone": GOAL_TONES.get(status, "info"),
                "scope": cluster or "whole corpus",
                "canAbandon": status == "active",
                "progress": line,
            }
        )
    return rows


def metric_options(goals: Mapping[str, Any]) -> list[dict[str, str]]:
    options = [{"value": "", "label": "Choose…"}]
    for metric in _list(goals, "metrics"):
        metric = _map(metric)
        options.append({"value": text(metric.get("key")), "label": text(metric.get("label"))})
    return options


def scope_options(goals: Mapping[str, Any]) -> list[dict[str, str]]:
    options = [{"value": "", "label": "Whole corpus"}]
    for cluster in _list(goals, "clusterOptions"):
        cluster = _map(cluster)
        label = f"{text(cluster.get('label'))} ({text(cluster.get('takeCount'))})"
        options.append({"value": text(cluster.get("clusterId")), "label": label})
    return options


def target_number(value: str) -> float | None:
    """``Number(form.target)`` when the SPA's form would accept it (finite, not "")."""
    if value == "":
        return None
    number = js.to_number(value)
    return float(number) if js.is_finite(number) else None


def goal_request(form: Mapping[str, str]) -> GoalRequest:
    """The form as the SPA sent it (``Number(takes) || 10``, empty scope and title as
    null). Raises ``ValueError`` (pydantic's) for what ``/api/v1`` would refuse."""
    target = target_number(form["target"])
    if not form["metric"] or target is None:
        raise ValueError("choose a metric and a numeric target")
    takes = js.to_number(form["takesRequired"])
    takes_required = takes if js.truthy(takes) else 10
    if not float(takes_required).is_integer():
        raise ValueError("Takes (N) must be a whole number")
    title = form["title"].strip()
    return GoalRequest.model_validate(
        {
            "metric": form["metric"],
            "direction": form["direction"],
            "target": target,
            "takes_required": int(takes_required),
            "cluster_id": form["clusterId"] or None,
            "title": title or None,
        }
    )


def refusal_text(error: ValidationError) -> str:
    """A pydantic refusal of the form, in a few words."""
    first = error.errors()[0]
    names = {"takes_required": "Takes (N)", "target": "Target", "metric": "Metric"}
    field = names.get(str(first["loc"][0]) if first["loc"] else "", "the goal")
    return f"{field}: {first['msg']}"


class PracticeModel(ViewModel):
    changed = Signal()
    goalsChanged = Signal()
    formChanged = Signal()

    # the coach (P-01 to P-03)
    state = qproperty(str, "state", changed)  # idle | loading | loaded | failed
    error = qproperty(str, "error", changed)
    dueSongs = qproperty("QVariantList", "dueSongs", changed)
    dueEmptyText = qproperty(str, "dueEmptyText", changed)
    frontier = qproperty("QVariantList", "frontier", changed)
    # goals (P-04)
    goalsState = qproperty(str, "goalsState", goalsChanged)
    goalsError = qproperty(str, "goalsError", goalsChanged)
    goals = qproperty("QVariantList", "goals", goalsChanged)
    metricOptions = qproperty("QVariantList", "metricOptions", goalsChanged)
    scopeOptions = qproperty("QVariantList", "scopeOptions", goalsChanged)
    # the form (P-05)
    directionOptions = qproperty("QVariantList", "directionOptions", formChanged)
    formMetric = qproperty(str, "formMetric", formChanged)
    formDirection = qproperty(str, "formDirection", formChanged)
    formTarget = qproperty(str, "formTarget", formChanged)
    formTakes = qproperty(str, "formTakes", formChanged)
    formCluster = qproperty(str, "formCluster", formChanged)
    formTitle = qproperty(str, "formTitle", formChanged)
    canSubmit = qproperty(bool, "canSubmit", formChanged)

    _FIELDS: Final = {
        "metric": "formMetric",
        "direction": "formDirection",
        "target": "formTarget",
        "takesRequired": "formTakes",
        "clusterId": "formCluster",
        "title": "formTitle",
    }

    def __init__(self, context: AppContext, open_corpus: Callable[[str], None]) -> None:
        super().__init__(
            context,
            {
                "state": "idle",
                "error": "",
                "dueSongs": [],
                "dueEmptyText": due_empty_text({}),
                "frontier": [],
                "goalsState": "idle",
                "goalsError": "",
                "goals": [],
                "metricOptions": metric_options({}),
                "scopeOptions": scope_options({}),
                "directionOptions": [dict(option) for option in DIRECTION_OPTIONS],
                "canSubmit": False,
            },
        )
        self.open_corpus = open_corpus
        self._form: dict[str, str] = dict(EMPTY_FORM)
        self._publish_form()

    # -- loading ----------------------------------------------------------------------------

    def activate(self) -> None:
        """Every visit asks the coach again (the server recomputes from live manifests)."""
        self.refresh()

    def forget(self) -> None:
        self._set(
            self.changed,
            state="idle",
            error="",
            dueSongs=[],
            dueEmptyText=due_empty_text({}),
            frontier=[],
        )
        self._set(
            self.goalsChanged,
            goalsState="idle",
            goalsError="",
            goals=[],
            metricOptions=metric_options({}),
            scopeOptions=scope_options({}),
        )
        self.resetForm()

    def _ready(self) -> bool:
        context = self.context
        return context.config.configured and context.session.signed_in

    @Slot()
    def refresh(self) -> None:
        if not self._ready():
            self.forget()
            return
        if self._v["state"] != "loaded":
            self._set(self.changed, state="loading", error="")
        api = self.context.api

        def done(document: JourneyDocument | None, error: BaseException | None) -> None:
            if error is not None or document is None:
                self._set(self.changed, state="failed", error=self.failure(error or ValueError()))
                return
            coach = document.document
            self._set(
                self.changed,
                state="loaded",
                error="",
                dueSongs=due_rows(coach),
                dueEmptyText=due_empty_text(coach),
                frontier=frontier_rows(coach),
            )

        self.context.tasks.submit(lambda: api().document("coach"), done)
        self.refreshGoals()

    @Slot()
    def refreshGoals(self) -> None:  # noqa: N802 (QML name)
        if not self._ready():
            return
        if self._v["goalsState"] != "loaded":
            self._set(self.goalsChanged, goalsState="loading", goalsError="")
        api = self.context.api

        def done(document: JourneyDocument | None, error: BaseException | None) -> None:
            if error is not None or document is None:
                message = self.failure(error or ValueError())
                self._set(self.goalsChanged, goalsState="failed", goalsError=message)
                return
            goals = document.document
            self._set(
                self.goalsChanged,
                goalsState="loaded",
                goalsError="",
                goals=goal_rows(goals),
                metricOptions=metric_options(goals),
                scopeOptions=scope_options(goals),
            )

        self.context.tasks.submit(lambda: api().document("goals"), done)

    # -- the coach's click-throughs (P-02, P-03) ----------------------------------------------

    @Slot(str)
    def openCluster(self, cluster_id: str) -> None:  # noqa: N802
        """The Corpus, filtered to the song (the SPA's ``openCorpusWithQueryThunk``)."""
        if cluster_id:
            self.open_corpus(cluster_id)

    # -- goals (P-04, P-05) -------------------------------------------------------------------

    def _publish_form(self) -> None:
        form = self._form
        can_submit = bool(form["metric"]) and target_number(form["target"]) is not None
        self._set(
            self.formChanged,
            formMetric=form["metric"],
            formDirection=form["direction"],
            formTarget=form["target"],
            formTakes=form["takesRequired"],
            formCluster=form["clusterId"],
            formTitle=form["title"],
            canSubmit=can_submit,
        )

    @Slot(str, str)
    def setField(self, name: str, value: str) -> None:  # noqa: N802
        if name in self._FIELDS:
            self._form[name] = value
            self._publish_form()

    @Slot()
    def resetForm(self) -> None:  # noqa: N802
        self._form = dict(EMPTY_FORM)
        self._publish_form()

    @Slot()
    def submitGoal(self) -> None:  # noqa: N802
        """Pre-register the form's goal; the form empties at once, as the SPA's did."""
        if not self._v["canSubmit"]:
            return
        form = dict(self._form)
        self.resetForm()
        try:
            request = goal_request(form)
        except ValidationError as exc:
            self.toast.emit(f"Goal not created: {refusal_text(exc)}", "danger")
            return
        except ValueError as exc:
            self.toast.emit(f"Goal not created: {exc}", "danger")
            return
        api = self.context.api

        def done(_created: Any, error: BaseException | None) -> None:
            if error is not None:
                self.toast.emit(f"Goal not created: {self.failure(error)}", "danger")
                return
            self.toast.emit(CREATED, "ok")
            self.refreshGoals()

        self.context.tasks.submit(lambda: api().create_goal(request), done)

    @Slot(str)
    def abandonGoal(self, goal_id: str) -> None:  # noqa: N802
        api = self.context.api

        def done(_abandoned: Any, error: BaseException | None) -> None:
            if error is not None:
                self.toast.emit(f"Goal not abandoned: {self.failure(error)}", "danger")
            self.refreshGoals()

        self.context.tasks.submit(lambda: api().abandon_goal(goal_id), done)
