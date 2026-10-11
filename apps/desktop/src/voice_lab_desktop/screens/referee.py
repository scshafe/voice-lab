"""Referee (design §6.1; checklist R): blind same-song A/B trials and the results by year.

The port of the SPA's Referee view (``RefereePageComponent.jsx``, ``RefereeManager.js``,
``BradleyTerryChart``) onto ``/api/v1``. ``GET /api/v1/referee/next`` hands the current
pair: two audio paths and a gain per side, never a year, a date or a cluster. A
``403 playback_not_approved`` means the server's operator has not recorded a playback
approval: the screen then shows the gate notice with the number of blind pairs waiting,
and the results still show. ``POST /api/v1/referee/verdicts`` stores the choice on the
server and answers with the reveal; 1.4 s later the next pair loads.
``GET /api/v1/referee/results`` is the Bradley–Terry fit by year.

Playback is loudness-matched and band-limited: each side plays with the server's gain
(``gain_db``) and a low-pass at 8 kHz (``playback.REFEREE_LOWPASS_HZ``), as the SPA's
WebAudio chain did, so the recording chain does not give a year away.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

from PySide6.QtCore import (
    Property,
    QObject,
    QTimer,
    Signal,
    Slot,
)

from cole_voice_lab.api.journey_contracts import (
    RefereeNext,
    RefereeResults,
    RefereeVerdictAnswer,
    RefereeVerdictRequest,
)

from ..api import ApiRefused
from ..formatting import text
from ..playback import REFEREE_LOWPASS_HZ
from ..tablemodel import TableRows
from ..viewmodel import AppContext, ViewModel, qproperty

#: The SPA's pause between the reveal and the next blind pair.
REVEAL_ADVANCE_MS: Final = 1400
#: The choices, in the SPA's order: (choice, label, primary).
CHOICES: Final = (
    ("a", "A sounds better", True),
    ("b", "B sounds better", True),
    ("too_close", "Too close", False),
    ("not_same_song", "Not the same song", False),
    ("skip", "Skip", False),
)
CHOICE_KEYS: Final = tuple(choice for choice, _label, _primary in CHOICES)
#: The counts table's columns (SuiTable).
YEAR_COLUMNS: Final = (
    {"key": "year", "title": "Year", "width": 80},
    {"key": "strength", "title": "Strength (log2)", "width": 130, "kind": "number"},
    {"key": "wins", "title": "Wins", "width": 80, "kind": "number"},
    {"key": "losses", "title": "Losses", "width": 80, "kind": "number"},
    {"key": "ties", "title": "Ties", "width": 80, "kind": "number"},
    {"key": "trials", "title": "Trials", "width": 80, "kind": "number"},
)
PLAYER_KEYS: Final = ("referee:a", "referee:b")


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def gate_text(pool_size: int) -> str:
    """The SPA's gate notice, naming the server's playback approval instead of a relaunch."""
    return (
        "The referee needs release-gated playback. The server plays blind trials only with "
        f"its recorded playback approval (serve --playback-approval); {pool_size:,} blind "
        "pairs are waiting. Existing results still show below."
    )


def progress_text(judged: int, pool_size: int) -> str:
    return f"{judged} of {pool_size} pairs judged."


def reveal_text(reveal: Mapping[str, Any]) -> str:
    """``A was <date> · B was <date> · <cluster>``: what the blind trial hid."""
    a = text(reveal.get("a_captured_at"))[:10]
    b = text(reveal.get("b_captured_at"))[:10]
    return f"A was {a} · B was {b} · {text(reveal.get('cluster_id'))}"


def audio_error_text(message: str) -> str:
    """One toast per pair when a track fails (the SPA named macOS Full Disk Access)."""
    return (
        f"Track failed to load ({message}). The usual cause is the server's read access to "
        "the corpus (the API's read-only mount of the phone drop); fix it, then reload."
    )


def year_rows(years: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The counts table: one row a judged year."""
    return [
        {
            "_key": text(row.get("year")),
            "year": text(row.get("year")),
            "strength": text(row.get("strengthLog2")),
            "wins": text(row.get("wins")),
            "losses": text(row.get("losses")),
            "ties": text(row.get("ties")),
            "trials": text(row.get("trials")),
        }
        for row in years
    ]


def choices_text(by_choice: Mapping[str, Any]) -> str:
    def n(key: str) -> str:
        return text(by_choice.get(key, 0))

    return (
        f"choices: A {n('a')} · B {n('b')} · too close {n('too_close')} · "
        f"not same song {n('not_same_song')} · skip {n('skip')}"
    )


def flags_text(flags: Sequence[Mapping[str, Any]]) -> str:
    """The chained-cluster flags ("" when there are none)."""
    if not flags:
        return ""
    listed = ", ".join(f"{text(f.get('clusterId'))} ×{text(f.get('count'))}" for f in flags)
    return f"Chained-cluster flags: {listed} — these feed the cluster repair pass."


def bradley_terry_spec(years: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The SPA's ``BradleyTerryChart`` as a SuiChart spec: the zero line, the strengths per
    year as a line with a dot sized by trials, the signed strength over each dot and the
    year under it. Fewer than two years: no chart (``{}``)."""
    if len(years) < 2:
        return {}
    values = [float(row.get("strengthLog2") or 0) for row in years]
    lo, hi = min(*values, 0.0), max(*values, 0.0)
    pad = max((hi - lo) * 0.15, 0.2)
    lo, hi = lo - pad, hi + pad
    plot_height = 220 - 14 - 34  # the SPA's height less its top and bottom padding
    above = (hi - lo) * 12 / plot_height  # the SPA's label sits 12 px over its dot
    points = []
    labels = []
    for index, row in enumerate(years):
        value = values[index]
        trials = _count(row.get("trials"))
        strength = text(row.get("strengthLog2"))
        signed = ("+" if value >= 0 else "") + strength
        points.append(
            {
                "x": index,
                "y": value,
                "r": max(3.0, min(7.0, 2 + math.sqrt(trials))),
                "tooltip": f"{text(row.get('year'))} · {signed} · {trials} trials",
            }
        )
        labels.append({"x": index, "y": value + above, "text": signed, "align": "center"})
    return {
        "label": "Perceived quality by year, Bradley-Terry log2 strengths",
        "xMin": 0,
        "xMax": len(years) - 1,
        "xTicks": [{"value": i, "label": text(row.get("year"))} for i, row in enumerate(years)],
        "padLeft": 56,
        "padRight": 20,
        "panels": [
            {
                "height": plot_height,
                "title": "",
                "yMin": lo,
                "yMax": hi,
                "yScale": "linear",
                "yTicks": [],
                "layers": [
                    {"kind": "rule", "y": 0, "tone": "line", "label": "0", "labelSide": "left"},
                    {
                        "kind": "line",
                        "tone": "ok",
                        "width": 2,
                        "points": [[i, v] for i, v in enumerate(values)],
                    },
                    {"kind": "points", "tone": "ok", "radius": 3, "points": points},
                    {"kind": "text", "items": labels},
                ],
            }
        ],
    }


class RefereeModel(ViewModel):
    changed = Signal()
    trialChanged = Signal()
    resultsChanged = Signal()

    state = qproperty(str, "state", changed)  # idle | loading | loaded | failed
    error = qproperty(str, "error", changed)
    enabled = qproperty(bool, "enabled", changed)
    gated = qproperty(bool, "gated", changed)
    gateText = qproperty(str, "gateText", changed)
    hasPair = qproperty(bool, "hasPair", trialChanged)
    pairId = qproperty(str, "pairId", trialChanged)
    aSource = qproperty(str, "aSource", trialChanged)
    bSource = qproperty(str, "bSource", trialChanged)
    aGainDb = qproperty(float, "aGainDb", trialChanged)
    bGainDb = qproperty(float, "bGainDb", trialChanged)
    lowpassHz = qproperty(float, "lowpassHz", trialChanged)
    submitting = qproperty(bool, "submitting", trialChanged)
    revealed = qproperty(bool, "revealed", trialChanged)
    revealText = qproperty(str, "revealText", trialChanged)
    lastChoice = qproperty(str, "lastChoice", trialChanged)
    judged = qproperty(int, "judged", trialChanged)
    poolSize = qproperty(int, "poolSize", trialChanged)
    progressText = qproperty(str, "progressText", trialChanged)
    poolCompleteText = qproperty(str, "poolCompleteText", trialChanged)
    choices = qproperty("QVariantList", "choices", trialChanged)
    hasResults = qproperty(bool, "hasResults", resultsChanged)
    resultsPoolSize = qproperty(int, "resultsPoolSize", resultsChanged)
    chartSpec = qproperty("QVariantMap", "chartSpec", resultsChanged)
    hasChart = qproperty(bool, "hasChart", resultsChanged)
    yearColumns = qproperty("QVariantList", "yearColumns", resultsChanged)
    yearCount = qproperty(int, "yearCount", resultsChanged)
    choicesText = qproperty(str, "choicesText", resultsChanged)
    flagsText = qproperty(str, "flagsText", resultsChanged)

    def __init__(self, context: AppContext) -> None:
        super().__init__(context, {})
        self._v.update(self._initial())
        self._years = TableRows(parent=self)
        self._error_pair = ""
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(REVEAL_ADVANCE_MS)
        self.timer.timeout.connect(self.advance)
        context.player.failed.connect(self._player_failed)

    @staticmethod
    def _initial() -> dict[str, Any]:
        return {
            "state": "idle",
            "error": "",
            "enabled": False,
            "gated": False,
            "gateText": gate_text(0),
            "hasPair": False,
            "pairId": "",
            "aSource": "",
            "bSource": "",
            "aGainDb": 0.0,
            "bGainDb": 0.0,
            "lowpassHz": REFEREE_LOWPASS_HZ,
            "submitting": False,
            "revealed": False,
            "revealText": "",
            "lastChoice": "",
            "judged": 0,
            "poolSize": 0,
            "progressText": "",
            "poolCompleteText": progress_text(0, 0),
            "choices": [
                {"choice": choice, "label": label, "primary": primary}
                for choice, label, primary in CHOICES
            ],
            "hasResults": False,
            "resultsPoolSize": 0,
            "chartSpec": {},
            "hasChart": False,
            "yearColumns": [dict(column) for column in YEAR_COLUMNS],
            "yearCount": 0,
            "choicesText": "",
            "flagsText": "",
        }

    def _get_years(self) -> TableRows:
        return self._years

    #: The counts table's rows (a list model for SuiTable).
    yearRows = Property(QObject, _get_years, constant=True)

    # -- lifecycle --------------------------------------------------------------------------

    def activate(self) -> None:
        if self._v["state"] in ("idle", "failed"):
            self.refresh()

    def forget(self) -> None:
        self.timer.stop()
        self._error_pair = ""
        initial = self._initial()
        trial = {k: initial[k] for k in initial if k in _TRIAL_KEYS}
        results = {k: initial[k] for k in initial if k in _RESULTS_KEYS}
        self._set(self.changed, **{k: initial[k] for k in _STATE_KEYS})
        self._set(self.trialChanged, **trial)
        self._set(self.resultsChanged, **results)
        self._years.replace([])

    def shutdown(self) -> None:
        self.timer.stop()

    # -- loading ----------------------------------------------------------------------------

    @Slot()
    def refresh(self) -> None:
        context = self.context
        if not context.config.configured or not context.session.signed_in:
            self.forget()
            return
        self._set(self.changed, state="loading", error="")
        self._fetch_next()
        self.fetch_results()

    def _fetch_next(self) -> None:
        context = self.context

        def work() -> RefereeNext | None:
            try:
                return context.api().referee_next()
            except ApiRefused as exc:
                if exc.code == "playback_not_approved":
                    return None  # the gate: no approval recorded on the server
                raise

        def done(answer: RefereeNext | None, error: BaseException | None) -> None:
            if error is not None:
                self._set(self.changed, state="failed", error=self.failure(error))
                return
            if answer is None:
                self._set(self.changed, state="loaded", enabled=False, gated=True)
                self._show_pair(None, judged=0, pool_size=self._v["resultsPoolSize"])
                return
            self._set(self.changed, state="loaded", enabled=True, gated=False)
            self._show_pair(answer, judged=answer.judged, pool_size=answer.pool_size)

        context.tasks.submit(work, done)

    def _show_pair(self, answer: RefereeNext | None, *, judged: int, pool_size: int) -> None:
        pair = answer.pair if answer is not None else None
        self._error_pair = ""
        self._set(
            self.trialChanged,
            hasPair=pair is not None,
            pairId=pair.pair_id if pair else "",
            aSource=pair.a.audio_url if pair else "",
            bSource=pair.b.audio_url if pair else "",
            aGainDb=pair.a.gain_db if pair else 0.0,
            bGainDb=pair.b.gain_db if pair else 0.0,
            submitting=False,
            revealed=False,
            revealText="",
            lastChoice="",
            judged=judged,
            poolSize=pool_size,
            progressText=progress_text(judged, pool_size),
            poolCompleteText=f"Pool complete: {progress_text(judged, pool_size)}",
        )

    @Slot()
    def fetch_results(self) -> None:
        context = self.context

        def done(results: RefereeResults | None, error: BaseException | None) -> None:
            if error is not None or results is None:
                self._set(self.changed, error=self.failure(error or ValueError("no results")))
                return
            self._show_results(results)

        context.tasks.submit(lambda: context.api().referee_results(), done)

    def _show_results(self, results: RefereeResults) -> None:
        fit: Mapping[str, Any] = results.fit
        raw_years = fit.get("years")
        years: list[Mapping[str, Any]] = (
            [row for row in raw_years if isinstance(row, Mapping)]
            if isinstance(raw_years, list)
            else []
        )
        by_choice = fit.get("byChoice")
        raw_flags = fit.get("notSameSongFlags")
        flags = (
            [f for f in raw_flags if isinstance(f, Mapping)] if isinstance(raw_flags, list) else []
        )
        spec = bradley_terry_spec(years)
        self._years.replace(year_rows(years))
        self._set(
            self.resultsChanged,
            hasResults=bool(fit.get("judgedTotal")),
            resultsPoolSize=results.pool_size,
            chartSpec=spec,
            hasChart=bool(spec),
            yearCount=len(years),
            choicesText=choices_text(by_choice if isinstance(by_choice, Mapping) else {}),
            flagsText=flags_text(flags),
        )
        self._set(self.changed, gateText=gate_text(results.pool_size))

    # -- judging ----------------------------------------------------------------------------

    @Slot(str)
    def judge(self, choice: str) -> None:
        """Send the choice for the current pair; the reveal follows, then the next pair."""
        v = self._v
        if choice not in CHOICE_KEYS or not v["hasPair"] or v["submitting"] or v["revealed"]:
            return
        self.context.player.stop()
        pair_id = v["pairId"]
        self._set(self.trialChanged, submitting=True, lastChoice=choice)
        context = self.context
        request = RefereeVerdictRequest.model_validate({"pair_id": pair_id, "choice": choice})

        def done(answer: RefereeVerdictAnswer | None, error: BaseException | None) -> None:
            if error is not None or answer is None:
                message = self.failure(error or ValueError("no answer"))
                self._set(self.trialChanged, submitting=False, lastChoice="")
                self.toast.emit(f"Judgment failed: {message}", "danger")
                return
            judged, pool_size = answer.judged, answer.pool_size
            self._set(
                self.trialChanged,
                submitting=False,
                revealed=True,
                revealText=reveal_text(answer.reveal.model_dump()),
                judged=judged,
                poolSize=pool_size,
                progressText=progress_text(judged, pool_size),
            )
            self.fetch_results()  # stale after a new judgment
            self.timer.start()

        context.tasks.submit(lambda: context.api().referee_verdict(request), done)

    @Slot()
    def advance(self) -> None:
        """After the reveal: the next blind pair."""
        self.timer.stop()
        self._set(self.trialChanged, hasPair=False, revealed=False, revealText="", lastChoice="")
        self._fetch_next()

    # -- playback ---------------------------------------------------------------------------

    @Slot(str, str)
    def _player_failed(self, key: str, message: str) -> None:
        pair = self._v["pairId"]
        if key not in PLAYER_KEYS or not pair or self._error_pair == pair:
            return
        self._error_pair = pair
        self.toast.emit(audio_error_text(message), "danger")


_STATE_KEYS: Final = ("state", "error", "enabled", "gated", "gateText")
_TRIAL_KEYS: Final = frozenset(
    {
        "hasPair",
        "pairId",
        "aSource",
        "bSource",
        "aGainDb",
        "bGainDb",
        "lowpassHz",
        "submitting",
        "revealed",
        "revealText",
        "lastChoice",
        "judged",
        "poolSize",
        "progressText",
        "poolCompleteText",
        "choices",
    }
)
_RESULTS_KEYS: Final = frozenset(
    {
        "hasResults",
        "resultsPoolSize",
        "chartSpec",
        "hasChart",
        "yearColumns",
        "yearCount",
        "choicesText",
        "flagsText",
    }
)
