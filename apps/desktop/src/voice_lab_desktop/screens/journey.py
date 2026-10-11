"""Analysis → Journey (design §6.1; checklist J): trends, journeys, charts, the take inspector.

The port of voice-journey's Journey page (``JourneyPageComponent.jsx``) over ``/api/v1``:
``GET trends`` and ``GET journeys`` (the manifest-derived documents), ``GET summary`` for
the playback gate, and ``GET recordings/{id}/features`` for the take inspector. What the
SPA computed in its selectors and charts is computed in ``journey_charts.py``; this model
holds the documents, the view state (the subject tab, the include-flagged toggle, the open
take) and hands QML ready-made rows and chart specs.

Click-throughs (a scorecard's song, a thread's dot) open the Corpus filtered to the song
(``open_corpus``). Era buttons play an era's sample takes one after another through the
app's one player.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import (
    Property,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Signal,
    Slot,
)

from cole_voice_lab.api.journey_contracts import JourneyDocument, JourneyFeatureDetail

from .. import journey_charts as charts
from ..api import ApiRefused
from ..education import card
from ..tablemodel import TableRows
from ..viewmodel import AppContext, ViewModel, qproperty

TRENDS_UNAVAILABLE = (
    "Voice trends need the features manifest: the PC's journey pipeline writes it (its "
    "features stage), and the server reads it on its next request."
)
FEATURE_STORE_MISSING = (
    "Per-take contours need the local feature store, which is not configured on the server "
    "(serve --feature-dir)."
)

type Json = dict[str, Any]
type Index = QModelIndex | QPersistentModelIndex


class JourneyModel(ViewModel):
    changed = Signal()
    viewChanged = Signal()
    inspectorChanged = Signal()

    state = qproperty(str, "state", changed)  # idle | loading | loaded | failed
    error = qproperty(str, "error", changed)
    available = qproperty(bool, "available", changed)
    notice = qproperty(str, "notice", changed)
    playbackEnabled = qproperty(bool, "playbackEnabled", changed)
    includeFlagged = qproperty(bool, "includeFlagged", viewChanged)
    subject = qproperty(str, "subject", viewChanged)
    tabs = qproperty("QVariantList", "tabs", viewChanged)
    statTiles = qproperty("QVariantList", "statTiles", viewChanged)
    verdictRows = qproperty("QVariantList", "verdictRows", viewChanged)
    verdictSummary = qproperty(str, "verdictSummary", viewChanged)
    rangeSpec = qproperty("QVariant", "rangeSpec", viewChanged)
    rangeHeadline = qproperty(str, "rangeHeadline", viewChanged)
    rangeLegend = qproperty("QVariantList", "rangeLegend", viewChanged)
    causeSpec = qproperty("QVariant", "causeSpec", viewChanged)
    eras = qproperty("QVariantList", "eras", viewChanged)
    vibratoSpec = qproperty("QVariant", "vibratoSpec", viewChanged)
    fingerprintSpec = qproperty("QVariant", "fingerprintSpec", viewChanged)
    yearLegend = qproperty("QVariantList", "yearLegend", viewChanged)
    trustPanels = qproperty("QVariantList", "trustPanels", viewChanged)
    tuningTile = qproperty("QVariantMap", "tuningTile", viewChanged)
    scorecards = qproperty("QVariantList", "scorecards", viewChanged)
    threadsSpec = qproperty("QVariant", "threadsSpec", viewChanged)
    education = qproperty("QVariant", "education", viewChanged)
    inspectorOpen = qproperty(bool, "inspectorOpen", inspectorChanged)
    inspectorId = qproperty(str, "inspectorId", inspectorChanged)
    inspectorState = qproperty(str, "inspectorState", inspectorChanged)
    inspectorTitle = qproperty(str, "inspectorTitle", inspectorChanged)
    inspectorTiles = qproperty("QVariantList", "inspectorTiles", inspectorChanged)
    inspectorContour = qproperty("QVariant", "inspectorContour", inspectorChanged)
    inspectorError = qproperty(str, "inspectorError", inspectorChanged)

    def __init__(self, context: AppContext, open_corpus: Callable[[str], None]) -> None:
        super().__init__(context, {})
        self.open_corpus = open_corpus
        self._range_table = TableRows(parent=self)
        self._cause_table = TableRows(parent=self)
        self.trends: Json | None = None
        self.journeys: Json | None = None
        self._generation = 0
        self._v.update(self._blank())
        self._v.update(includeFlagged=False, subject="verdict")
        self._v.update(self._view_values())
        self._v.update(self._blank_inspector())

    @staticmethod
    def _blank() -> Json:
        return {
            "state": "idle",
            "error": "",
            "available": False,
            "notice": "",
            "playbackEnabled": False,
        }

    @staticmethod
    def _blank_inspector() -> Json:
        return {
            "inspectorOpen": False,
            "inspectorId": "",
            "inspectorState": "idle",
            "inspectorTitle": "",
            "inspectorTiles": [],
            "inspectorContour": None,
            "inspectorError": "",
        }

    def _get_range_table(self) -> QObject:
        return self._range_table

    def _get_cause_table(self) -> QObject:
        return self._cause_table

    rangeTable = Property(QObject, _get_range_table, constant=True)
    causeTable = Property(QObject, _get_cause_table, constant=True)

    def _get_range_columns(self) -> list[Json]:
        return [dict(c) for c in charts.RANGE_COLUMNS]

    def _get_cause_columns(self) -> list[Json]:
        return [dict(c) for c in charts.CAUSE_COLUMNS]

    rangeColumns = Property(list, _get_range_columns, constant=True)
    causeColumns = Property(list, _get_cause_columns, constant=True)

    # -- loading ----------------------------------------------------------------------------

    def activate(self) -> None:
        """The SPA fetched summary, trends and journeys each time the page mounted."""
        self.refresh()

    def forget(self) -> None:
        self._generation += 1
        self.trends = self.journeys = None
        self._set(self.changed, **self._blank())
        self._set(self.viewChanged, **self._view_values())
        self.closeTake()

    @Slot()
    def refresh(self) -> None:
        context = self.context
        if not context.config.configured or not context.session.signed_in:
            self.forget()
            return
        if self._v["state"] != "loaded":
            self._set(self.changed, state="loading", error="")
        self._generation += 1
        generation = self._generation

        def work() -> tuple[bool, JourneyDocument, JourneyDocument | None]:
            api = context.api()
            try:
                playback = api.summary().playback_enabled
            except Exception:  # noqa: BLE001 - the gate stays closed; trends decide the page
                playback = False
            trends = api.document("trends")
            try:
                journeys: JourneyDocument | None = api.document("journeys")
            except Exception:  # noqa: BLE001 - the SPA's journeys slice failed on its own
                journeys = None
            return playback, trends, journeys

        def done(
            result: tuple[bool, JourneyDocument, JourneyDocument | None] | None,
            error: BaseException | None,
        ) -> None:
            if generation != self._generation:
                return
            if error is not None or result is None:
                self._set(self.changed, state="failed", error=self.failure(error or ValueError()))
                return
            playback, trends, journeys = result
            self.trends = dict(trends.document) if trends.available else None
            self.journeys = dict(journeys.document) if journeys and journeys.available else None
            self._set(
                self.changed,
                state="loaded",
                error="",
                available=self.trends is not None,
                notice="" if self.trends is not None else TRENDS_UNAVAILABLE,
                playbackEnabled=playback,
            )
            self._set(self.viewChanged, **self._view_values())

        context.tasks.submit(work, done)

    # -- the view ---------------------------------------------------------------------------

    def _view_values(self) -> Json:
        trends, journeys = self.trends, self.journeys
        include = bool(self._v.get("includeFlagged"))
        requested = str(self._v.get("subject") or "verdict")
        empty: Json = {
            "tabs": [],
            "statTiles": [],
            "verdictRows": [],
            "verdictSummary": "",
            "rangeSpec": None,
            "rangeHeadline": "",
            "rangeLegend": [],
            "causeSpec": None,
            "eras": [],
            "vibratoSpec": None,
            "fingerprintSpec": None,
            "yearLegend": [],
            "trustPanels": [],
            "tuningTile": {"title": "", "text": ""},
            "scorecards": [],
            "threadsSpec": None,
            "education": None,
            "subject": requested,
        }
        if trends is None:
            self._range_table.replace([])
            self._cause_table.replace([])
            return empty
        quarters = charts.active_quarters(trends, include)
        span = charts.year_span(trends)
        availability = charts.tab_availability(trends, journeys)
        subject = charts.active_subject(requested, availability)
        clusters = list(trends.get("clusters") or [])
        eras = list(trends.get("eras") or [])
        self._range_table.replace(charts.range_table(quarters))
        self._cause_table.replace(charts.cause_table(quarters))
        tuning = charts.tuning_pseudo_quarters(journeys)
        return {
            **empty,
            "subject": subject,
            "tabs": [
                {"key": k, "label": label} for k, label in charts.SUBJECT_TABS if availability[k]
            ],
            "statTiles": charts.stat_tiles(trends),
            "verdictRows": charts.verdict_rows(journeys) if journeys else [],
            "verdictSummary": charts.verdict_summary(journeys) if journeys else "",
            "rangeSpec": charts.range_river(quarters),
            "rangeHeadline": charts.range_headline(trends),
            "rangeLegend": [dict(item) for item in charts.RANGE_LEGEND],
            "causeSpec": charts.cause_effect(trends.get("monthlyCadence") or [], quarters, eras),
            "eras": charts.era_buttons(eras, bool(self._v.get("playbackEnabled"))),
            "vibratoSpec": charts.vibrato_triptych(quarters),
            "fingerprintSpec": charts.fingerprint(charts.fingerprint_takes(trends, include), span),
            "yearLegend": charts.year_legend(charts.fingerprint_years(trends), span),
            "trustPanels": charts.trust_panels(quarters, tuning, journeys),
            "tuningTile": charts.tuning_tile(journeys, charts.note_core_overall(journeys)),
            "scorecards": charts.scorecard_rows(journeys, clusters) if journeys else [],
            "threadsSpec": charts.threads(clusters),
            "education": card(subject),
        }

    @Slot(str)
    def selectSubject(self, key: str) -> None:  # noqa: N802 (QML name)
        if key not in charts.SUBJECTS:
            return
        self._v["subject"] = key
        self._set(self.viewChanged, **self._view_values())

    @Slot(bool)
    def setIncludeFlagged(self, on: bool) -> None:  # noqa: N802
        self._v["includeFlagged"] = bool(on)
        self._set(self.viewChanged, **self._view_values())

    @Slot(str)
    def openCluster(self, cluster_id: str) -> None:  # noqa: N802
        """A scorecard's song or a thread's dot: the Corpus filtered to that song."""
        if cluster_id:
            self.open_corpus(cluster_id)

    @Slot(int)
    def playEra(self, index: int) -> None:  # noqa: N802
        """An era's sample takes, one after another (the SPA's chained era playback)."""
        eras = list((self.trends or {}).get("eras") or [])
        if not self._v["playbackEnabled"] or not 0 <= index < len(eras):
            return
        samples = list(eras[index].get("sampleRecordingIds") or [])
        self.context.player.play_queue([(f"era:{index}", str(sample)) for sample in samples])

    # -- the take inspector -----------------------------------------------------------------

    @Slot(str)
    def openTake(self, recording_id: str) -> None:  # noqa: N802
        if not recording_id:
            return
        self._set(
            self.inspectorChanged,
            **{
                **self._blank_inspector(),
                "inspectorOpen": True,
                "inspectorId": recording_id,
                "inspectorState": "loading",
                "inspectorTitle": charts.inspector_title(None, recording_id),
            },
        )
        context = self.context

        def work() -> JourneyFeatureDetail:
            return context.api().features(recording_id)

        def done(detail: JourneyFeatureDetail | None, error: BaseException | None) -> None:
            if self._v["inspectorId"] != recording_id or not self._v["inspectorOpen"]:
                return  # closed, or another take opened meanwhile
            if error is not None or detail is None:
                if isinstance(error, ApiRefused) and error.code == "feature_dir_not_configured":
                    message = FEATURE_STORE_MISSING
                else:
                    message = f"Detail unavailable: {self.failure(error or ValueError())}"
                self._set(self.inspectorChanged, inspectorState="failed", inspectorError=message)
                return
            payload: Mapping[str, Any] = detail.model_dump()
            self._set(
                self.inspectorChanged,
                inspectorState="loaded",
                inspectorTitle=charts.inspector_title(payload, recording_id),
                inspectorTiles=charts.inspector_tiles(payload),
                inspectorContour=charts.f0_contour(
                    (payload.get("detail") or {}).get("f0ContourVoiced50ms")
                    if isinstance(payload.get("detail"), dict)
                    else None
                ),
            )

        context.tasks.submit(work, done)

    @Slot()
    def closeTake(self) -> None:  # noqa: N802
        key = f"inspector:{self._v.get('inspectorId', '')}"
        if self.context.player.property("activeKey") == key:
            self.context.player.stop()
        self._set(self.inspectorChanged, **self._blank_inspector())
