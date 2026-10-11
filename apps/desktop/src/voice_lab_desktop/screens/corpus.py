"""Corpus (design §6.1; checklist C): the recordings table, row detail, playback, verdicts.

voice-journey's Corpus view (``CorpusPageComponent`` and its parts; ``RowsManager``,
``DetailManager``, ``PlaybackManager``, ``SummaryManager``) over ``/api/v1``:

- ``GET /api/v1/summary``: the filter options and the server's gates (playback,
  transcript text);
- ``GET /api/v1/recordings``: one page of ``fields=list`` rows at a time (100, the SPA's
  page), filtered and sorted by the server; the next page loads at the end of the table;
- ``GET /api/v1/recordings/{id}``: the full row, for the detail sheet;
- ``GET /api/v1/recordings/{id}/transcript``: the local transcript text, only when asked
  for, and dropped when the sheet closes or another row opens;
- ``POST /api/v1/review/verdicts``: a spot-check verdict, which updates the table's row,
  the open detail and the totals.

Playback is the app's one player (``player``): a row's Play and the detail's player both
play the take as ``corpus:<id>``, so starting another take stops the one playing.
"""

from __future__ import annotations

from typing import Any, Final, cast

from PySide6.QtCore import (
    Property,
    QObject,
    QTimer,
    Signal,
    Slot,
)

from cole_voice_lab.api.journey_contracts import (
    JourneyRecording,
    JourneyRecordings,
    JourneySummary,
    JourneyTranscript,
    ReviewVerdictAnswer,
    ReviewVerdictName,
    ReviewVerdictRequest,
)

from ..formatting import (
    bucket_tone,
    fmt_bytes,
    fmt_captured,
    fmt_confidence,
    fmt_seconds,
    labelize,
    text,
)
from ..tablemodel import TableRows
from ..viewmodel import AppContext, ViewModel, qproperty

Json = dict[str, Any]

PAGE_LIMIT: Final = 100
SEARCH_DELAY_MS: Final = 250
FILTER_KEYS: Final = (
    "q",
    "bucket",
    "year",
    "reviewQueue",
    "spotCheckStatus",
    "transcriptStatus",
    "lyricMatchStatus",
)
VERDICT_CHOICES: Final = (
    "clean_singing",
    "noise_contaminated_singing",
    "music_contaminated_singing",
    "non_singing",
    "still_uncertain",
)
#: The select filters, in the SPA's order: (key, label, summary options key or None).
SELECT_FILTERS: Final = (
    ("bucket", "Bucket", "buckets"),
    ("year", "Year", "years"),
    ("reviewQueue", "Queue", None),
    ("spotCheckStatus", "Spot-check", "spot_check_statuses"),
    ("transcriptStatus", "Transcript", "transcript_statuses"),
    ("lyricMatchStatus", "Lyric mark", "lyric_match_statuses"),
)
PLAY_TOOLTIP: Final = "Stream this take (per-click, release-gated)"
NO_PLAYBACK: Final = "Playback requires the release-gated launch"
#: The table's columns (the SuiTable contract): the SPA's, in its order.
COLUMNS: Final = (
    {"key": "queue", "title": "Queue", "width": 64, "align": "right", "sortable": True,
     "sortKey": "reviewQueueOrder", "kind": "number"},
    {"key": "filename", "title": "Filename", "width": 240, "sortable": True, "kind": "link"},
    {"key": "captured", "title": "Captured", "width": 150, "sortable": True,
     "sortKey": "capturedAt", "kind": "number"},
    {"key": "year", "title": "Year", "width": 64, "sortable": True},
    {"key": "duration", "title": "Duration", "width": 84, "align": "right", "sortable": True,
     "sortKey": "durationSeconds", "kind": "number"},
    {"key": "bucket", "title": "Bucket", "width": 220, "sortable": True, "kind": "chip"},
    {"key": "confidence", "title": "Conf.", "width": 66, "align": "right", "sortable": True,
     "kind": "number"},
    {"key": "spotCheck", "title": "Spot-check", "width": 170, "sortable": True,
     "sortKey": "spotCheckStatus", "kind": "stack"},
    {"key": "transcript", "title": "Transcript", "width": 130, "sortable": True,
     "sortKey": "transcriptStatus", "kind": "stack"},
    {"key": "cluster", "title": "Lyric cluster", "width": 220, "kind": "link"},
    {"key": "actions", "title": "Listen / review", "width": 190, "kind": "actions"},
)  # fmt: skip


def _present(value: Any) -> bool:
    return value is not None and value != "" and value is not False


def play_label(state: str) -> str:
    """The row's Play button for the player's state of that take."""
    return {"loading": "Loading…", "playing": "Pause", "paused": "Resume"}.get(state, "Play")


def table_row(row: Json, *, playback: bool, play_state: str = "idle") -> Json:
    """A ``fields=list`` row as the table shows it (the SPA's column renderers)."""
    order = row.get("reviewQueueOrder")
    words = row.get("transcriptWordCount")
    status = row.get("spotCheckStatus")
    verdict = row.get("humanVerdict")
    return {
        "_key": text(row.get("recordingId")),
        "_highlight": bool(row.get("reviewQueueSelected")),
        "queue": f"#{text(order)}" if _present(order) else "",
        "filename": text(row.get("filename")),
        "captured": fmt_captured(row.get("capturedAt")),
        "year": text(row.get("year")),
        "duration": fmt_seconds(row.get("durationSeconds")),
        "bucket": {"text": labelize(row.get("bucket")), "tone": bucket_tone(row.get("bucket"))},
        "confidence": fmt_confidence(row.get("confidence")),
        "spotCheck": {
            "primary": labelize(status),
            "secondary": labelize(verdict) if _present(verdict) else "",
            "muted": status == "not_reviewed",
        },
        "transcript": {
            "primary": labelize(row.get("transcriptStatus")),
            "secondary": f"{text(words)} words" if words is not None else "",
            "muted": False,
        },
        "cluster": text(row.get("lyricMatchClusterLabel") or row.get("lyricMatchCluster") or ""),
        "actions": [
            {
                "id": "play",
                "text": play_label(play_state),
                "enabled": playback,
                "tooltip": PLAY_TOOLTIP if playback else NO_PLAYBACK,
            },
            {"id": "review", "text": "Review", "enabled": True, "tooltip": "Open row detail"},
        ],
    }


def detail_entries(row: Json) -> list[Json]:
    """The detail sheet's list (``MetaList``): label and value, empty values left out."""
    order = row.get("reviewQueueOrder")
    words = row.get("transcriptWordCount")
    language = row.get("transcriptLanguage")
    verdict = row.get("humanVerdict")
    reviewed_at = row.get("reviewedAt")
    transcript = labelize(row.get("transcriptStatus"))
    if words is not None:
        transcript += f" · {text(words)} words"
    if _present(language):
        transcript += f" · {text(language)}"
    entries = [
        ("Captured", fmt_captured(row.get("capturedAt"))),
        ("Duration", fmt_seconds(row.get("durationSeconds"))),
        ("Size", fmt_bytes(row.get("sizeBytes"))),
        ("Bucket", labelize(row.get("bucket"))),
        ("Confidence", fmt_confidence(row.get("confidence"))),
        ("Spot-check", labelize(row.get("spotCheckStatus"))),
        ("Human verdict", labelize(verdict) if _present(verdict) else None),
        ("Reviewed by", text(row.get("reviewedBy"))),
        ("Reviewed at", fmt_captured(reviewed_at) if _present(reviewed_at) else None),
        (
            "Queue",
            f"#{text(order)} — {text(row.get('reviewQueueReason'))}" if _present(order) else None,
        ),
        ("Transcript", transcript),
        (
            "Lyric cluster",
            text(row.get("lyricMatchClusterLabel") or row.get("lyricMatchCluster") or ""),
        ),
        ("Lyric confidence", text(row.get("lyricMatchConfidence"))),
        ("Wordless vocalise", "marked" if row.get("wordlessVocaliseMarked") else None),
    ]
    return [{"label": label, "value": value} for label, value in entries if _present(value)]


class CorpusModel(ViewModel):
    changed = Signal()
    filtersChanged = Signal()
    pageChanged = Signal()
    detailChanged = Signal()
    transcriptChanged = Signal()

    state = qproperty(str, "state", changed)  # idle | loading | loaded | failed
    error = qproperty(str, "error", changed)
    playbackEnabled = qproperty(bool, "playbackEnabled", changed)
    transcriptTextEnabled = qproperty(bool, "transcriptTextEnabled", changed)
    filterControls = qproperty("QVariantList", "filterControls", filtersChanged)
    searchText = qproperty(str, "searchText", filtersChanged)
    anyFilter = qproperty(bool, "anyFilter", filtersChanged)
    sortKey = qproperty(str, "sortKey", filtersChanged)
    sortDescending = qproperty(bool, "sortDescending", filtersChanged)
    loading = qproperty(bool, "loading", pageChanged)
    hasMore = qproperty(bool, "hasMore", pageChanged)
    totalFiltered = qproperty(int, "totalFiltered", pageChanged)
    statusLine = qproperty(str, "statusLine", pageChanged)
    totals = qproperty("QVariantMap", "totals", pageChanged)
    detailOpen = qproperty(bool, "detailOpen", detailChanged)
    detailId = qproperty(str, "detailId", detailChanged)
    detailState = qproperty(str, "detailState", detailChanged)  # idle | loading | loaded | failed
    detailError = qproperty(str, "detailError", detailChanged)
    detailTitle = qproperty(str, "detailTitle", detailChanged)
    detailDescription = qproperty(str, "detailDescription", detailChanged)
    detailEntries = qproperty("QVariantList", "detailEntries", detailChanged)
    detailRationale = qproperty(str, "detailRationale", detailChanged)
    detailVerdict = qproperty(str, "detailVerdict", detailChanged)
    verdictBusy = qproperty(bool, "verdictBusy", detailChanged)
    transcriptAllowed = qproperty(bool, "transcriptAllowed", transcriptChanged)
    transcriptState = qproperty(str, "transcriptState", transcriptChanged)
    transcriptText = qproperty(str, "transcriptText", transcriptChanged)
    transcriptMessage = qproperty(str, "transcriptMessage", transcriptChanged)

    def __init__(self, context: AppContext) -> None:
        super().__init__(
            context,
            {
                "state": "idle",
                "error": "",
                "playbackEnabled": False,
                "transcriptTextEnabled": False,
                "filterControls": [],
                "searchText": "",
                "anyFilter": False,
                "sortKey": "capturedAt",
                "sortDescending": False,
                "loading": False,
                "hasMore": False,
                "totalFiltered": -1,
                "statusLine": "Loading rows…",
                "totals": {},
                "detailOpen": False,
                "detailId": "",
                "detailState": "idle",
                "detailError": "",
                "detailTitle": "",
                "detailDescription": "",
                "detailEntries": [],
                "detailRationale": "",
                "detailVerdict": "",
                "verdictBusy": False,
                "transcriptAllowed": False,
                "transcriptState": "idle",
                "transcriptText": "",
                "transcriptMessage": "",
            },
        )
        self._filters: dict[str, str] = {key: "" for key in FILTER_KEYS}
        self._summary: JourneySummary | None = None
        self._generation = 0  # the rows' request generation: a stale page is dropped
        self._detail_generation = 0
        self._detail_row: Json | None = None
        self._play_key = ""
        self._play_state = "idle"
        self._rows = TableRows(self._format_row, self)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._apply_search)
        self._pending_search = ""
        context.player.changed.connect(self._player_changed)
        self._refresh_controls()

    # -- properties QML reads that are not plain values -----------------------------------

    def _get_rows(self) -> QObject:
        return self._rows

    rowsModel = Property(QObject, _get_rows, constant=True)

    def _get_columns(self) -> list[Json]:
        return [dict(column) for column in COLUMNS]

    columns = Property(list, _get_columns, constant=True)

    def _get_verdicts(self) -> list[Json]:
        return [{"value": choice, "label": labelize(choice)} for choice in VERDICT_CHOICES]

    verdictChoices = Property(list, _get_verdicts, constant=True)

    @property
    def rows(self) -> TableRows:
        return self._rows

    @property
    def filters(self) -> dict[str, str]:
        return dict(self._filters)

    # -- lifecycle --------------------------------------------------------------------------

    def activate(self) -> None:
        """First show (after sign-in): the summary and the first page (A-06)."""
        if self._v["state"] in ("idle", "failed"):
            self.refresh()

    def forget(self) -> None:
        """Signed out, or the settings changed: drop everything the server said."""
        self._generation += 1
        self._detail_generation += 1
        self._summary = None
        self._detail_row = None
        self._rows.replace([])
        self._set(
            self.changed,
            state="idle",
            error="",
            playbackEnabled=False,
            transcriptTextEnabled=False,
        )
        self._set(
            self.pageChanged,
            loading=False,
            hasMore=False,
            totalFiltered=-1,
            statusLine="Loading rows…",
            totals={},
        )
        self._close_detail()
        self._refresh_controls()

    @Slot()
    def refresh(self) -> None:
        """Reload the summary and the rows from the first page."""
        context = self.context
        if not context.config.configured or not context.session.signed_in:
            return
        self._set(self.changed, state="loading", error="")

        def done(summary: JourneySummary | None, error: BaseException | None) -> None:
            if error is not None or summary is None:
                self._set(self.changed, state="failed", error=self.failure(error or ValueError()))
                return
            self._summary = summary
            self._set(
                self.changed,
                state="loaded",
                playbackEnabled=summary.playback_enabled,
                transcriptTextEnabled=summary.transcript_text_enabled,
            )
            self._refresh_controls()
            self._rows.refresh()
            self._refresh_transcript_rule()

        context.tasks.submit(lambda: context.api().summary(), done)
        self._reload()

    # -- filters and sort -------------------------------------------------------------------

    def _refresh_controls(self) -> None:
        options = self._summary.filter_options if self._summary is not None else None
        controls = []
        for key, label, source in SELECT_FILTERS:
            if source is None:
                values = [{"value": "selected", "label": "Spot-check sample"}]
            else:
                found = getattr(options, source) if options is not None else ()
                values = [{"value": text(v), "label": text(v)} for v in found]
            controls.append(
                {
                    "key": key,
                    "label": label,
                    "options": [{"value": "", "label": "All"}, *values],
                    "value": self._filters[key],
                }
            )
        self._set(
            self.filtersChanged,
            filterControls=controls,
            searchText=self._filters["q"],
            anyFilter=any(self._filters.values()),
        )

    @Slot(str, str)
    def setFilter(self, key: str, value: str) -> None:  # noqa: N802 (QML name)
        """A filter changed: refetch from the first page."""
        if key not in self._filters:
            return
        if key == "q":
            self._search_timer.stop()
        self._filters[key] = value
        self._refresh_controls()
        self._reload()

    @Slot(str)
    def setSearch(self, value: str) -> None:  # noqa: N802
        """Typing in the search field: applied once typing pauses."""
        self._pending_search = value
        self._search_timer.start()

    def _apply_search(self) -> None:
        self.setFilter("q", self._pending_search)

    @Slot()
    def clearFilters(self) -> None:  # noqa: N802
        self._search_timer.stop()
        self._filters = {key: "" for key in FILTER_KEYS}
        self._refresh_controls()
        self._reload()

    def show_query(self, query: str) -> None:
        """Open on the rows matching ``query`` (a click-through from Journey or Practice):
        every other filter cleared, from the first page."""
        self._search_timer.stop()
        self._filters = {key: "" for key in FILTER_KEYS}
        self._filters["q"] = query
        self._refresh_controls()
        if self.context.config.configured and self.context.session.signed_in:
            if self._v["state"] in ("idle", "failed"):
                self.refresh()
            else:
                self._reload()

    @Slot(str)
    def sortBy(self, field: str) -> None:  # noqa: N802
        """A header click: by that field ascending, or the other way when it is already
        the sort."""
        if field == self._v["sortKey"]:
            self._set(self.filtersChanged, sortDescending=not self._v["sortDescending"])
        else:
            self._set(self.filtersChanged, sortKey=field, sortDescending=False)
        self._reload()

    # -- pages ------------------------------------------------------------------------------

    def _params(self, offset: int) -> dict[str, str]:
        params = {key: value for key, value in self._filters.items() if value}
        params.update(
            sort=self._v["sortKey"],
            direction="desc" if self._v["sortDescending"] else "asc",
            fields="list",
            limit=str(PAGE_LIMIT),
            offset=str(offset),
        )
        return params

    def _reload(self) -> None:
        self._generation += 1
        self._rows.replace([])
        self._set(self.pageChanged, hasMore=False, totalFiltered=-1, loading=False)
        self._fetch_page()

    @Slot()
    def loadMore(self) -> None:  # noqa: N802
        """The table reached its end: the next page, when there is one."""
        if self._v["hasMore"] and not self._v["loading"]:
            self._fetch_page()

    def _fetch_page(self) -> None:
        context = self.context
        if not context.config.configured or not context.session.signed_in:
            return
        generation = self._generation
        params = self._params(len(self._rows.rows))
        self._set(self.pageChanged, loading=True)
        self._update_status()

        def done(page: JourneyRecordings | None, error: BaseException | None) -> None:
            if generation != self._generation:
                return  # the filters or the sort changed meanwhile
            if error is not None or page is None:
                message = self.failure(error or ValueError())
                self._set(self.pageChanged, loading=False, statusLine="Row fetch failed")
                self._set(self.changed, error=message)
                return
            self._rows.append([dict(row) for row in page.rows])
            total = page.page.total_filtered
            self._set(
                self.pageChanged,
                loading=False,
                totalFiltered=total,
                hasMore=len(self._rows.rows) < total,
                totals=page.totals.model_dump(),
            )
            self._update_status()

        context.tasks.submit(lambda: context.api().recordings(params), done)

    def _update_status(self) -> None:
        total = self._v["totalFiltered"]
        if total < 0:
            line = "Loading rows…"
        else:
            line = f"Showing {len(self._rows.rows):,} of {total:,} takes"
        self._set(self.pageChanged, statusLine=line)

    # -- rows -------------------------------------------------------------------------------

    def _format_row(self, row: Json) -> Json:
        key = f"corpus:{row.get('recordingId')}"
        state = self._play_state if key == self._play_key else "idle"
        return table_row(row, playback=self._v["playbackEnabled"], play_state=state)

    def _player_changed(self) -> None:
        player = self.context.player
        key, state = str(player.property("activeKey")), str(player.property("state"))
        if (key, state) == (self._play_key, self._play_state):
            return
        before = self._play_key
        self._play_key, self._play_state = key, state
        for changed in {before, key}:
            if changed.startswith("corpus:"):
                position = self._rows.index_of("recordingId", changed.removeprefix("corpus:"))
                if position >= 0:
                    self._rows.refresh(position)

    @Slot(int, str)
    def cellActivated(self, index: int, key: str) -> None:  # noqa: N802
        """A link cell: the filename opens the detail; the lyric cluster filters to it."""
        rows = self._rows.rows
        if not 0 <= index < len(rows):
            return
        row = rows[index]
        if key == "filename":
            self.openDetail(text(row.get("recordingId")))
        elif key == "cluster":
            cluster = row.get("lyricMatchCluster") or row.get("lyricMatchClusterLabel")
            if cluster:
                self.setFilter("q", text(cluster))

    @Slot(int, str)
    def actionTriggered(self, index: int, action: str) -> None:  # noqa: N802
        rows = self._rows.rows
        if not 0 <= index < len(rows):
            return
        recording = text(rows[index].get("recordingId"))
        if action == "review":
            self.openDetail(recording)
        elif action == "play":
            self.play(recording)

    @Slot(int)
    def activateRow(self, index: int) -> None:  # noqa: N802
        """Enter or a double click on a row: its detail."""
        self.cellActivated(index, "filename")

    @Slot(str)
    def play(self, recording: str) -> None:
        """A row's Play: the one player plays the take (or pauses / resumes it)."""
        if not self._v["playbackEnabled"]:
            return
        player = self.context.player
        key = f"corpus:{recording}"
        if player.property("activeKey") == key and player.property("state") in (
            "playing",
            "paused",
        ):
            player.toggle(key)
        else:
            player.play(key, recording)

    # -- the detail -------------------------------------------------------------------------

    @Slot(str)
    def openDetail(self, recording: str) -> None:  # noqa: N802
        self._detail_generation += 1
        generation = self._detail_generation
        self._detail_row = None
        self._set(
            self.detailChanged,
            detailOpen=True,
            detailId=recording,
            detailState="loading",
            detailError="",
            detailTitle=recording,
            detailDescription="",
            detailEntries=[],
            detailRationale="",
            detailVerdict="",
            verdictBusy=False,
        )
        self._reset_transcript()
        context = self.context

        def done(found: JourneyRecording | None, error: BaseException | None) -> None:
            if generation != self._detail_generation:
                return
            if error is not None or found is None:
                self._set(
                    self.detailChanged,
                    detailState="failed",
                    detailError=self.failure(error or ValueError()),
                )
                return
            self._show_detail(dict(found.row))

        context.tasks.submit(lambda: context.api().recording(recording), done)

    def _show_detail(self, row: Json) -> None:
        self._detail_row = row
        year = text(row.get("year"))
        self._set(
            self.detailChanged,
            detailState="loaded",
            detailTitle=text(row.get("filename")) or self._v["detailId"],
            detailDescription=f"{labelize(row.get('bucket'))} · {year}",
            detailEntries=detail_entries(row),
            detailRationale=text(row.get("rationale")),
            detailVerdict=text(row.get("humanVerdict")),
        )
        self._refresh_transcript_rule()

    @Slot()
    def closeDetail(self) -> None:  # noqa: N802
        self._detail_generation += 1
        self._close_detail()

    def _close_detail(self) -> None:
        self._detail_row = None
        self._set(
            self.detailChanged,
            detailOpen=False,
            detailId="",
            detailState="idle",
            detailError="",
            detailEntries=[],
            detailRationale="",
            detailVerdict="",
            verdictBusy=False,
        )
        self._reset_transcript()

    # -- the transcript, on request only --------------------------------------------------

    def _reset_transcript(self) -> None:
        self._set(self.transcriptChanged, transcriptState="idle", transcriptText="")
        self._refresh_transcript_rule()

    def _refresh_transcript_rule(self) -> None:
        row = self._detail_row
        if row is None:
            self._set(self.transcriptChanged, transcriptAllowed=False, transcriptMessage="")
            return
        completed = row.get("transcriptStatus") == "completed"
        allowed = (
            self._v["transcriptTextEnabled"]
            and completed
            and bool(row.get("transcriptTextLocalOnly"))
        )
        if allowed:
            message = ""
        elif not completed:
            message = "No completed transcript."
        else:
            message = "Local transcript text is not enabled for this launch."
        self._set(self.transcriptChanged, transcriptAllowed=allowed, transcriptMessage=message)

    @Slot()
    def loadTranscript(self) -> None:  # noqa: N802
        """ "View local text": the transcript of the open row, now and only now."""
        if not self._v["transcriptAllowed"] or self._v["transcriptState"] == "loading":
            return
        recording = self._v["detailId"]
        generation = self._detail_generation
        self._set(self.transcriptChanged, transcriptState="loading", transcriptText="")
        context = self.context

        def done(found: JourneyTranscript | None, error: BaseException | None) -> None:
            if generation != self._detail_generation:
                return  # the sheet closed, or another row opened: the text is dropped
            if error is not None or found is None:
                self._set(
                    self.transcriptChanged,
                    transcriptState="failed",
                    transcriptMessage=self.failure(error or ValueError()),
                )
                return
            self._set(self.transcriptChanged, transcriptState="loaded", transcriptText=found.text)

        context.tasks.submit(lambda: context.api().transcript(recording), done)

    # -- verdicts ---------------------------------------------------------------------------

    @Slot(str, str)
    def submitVerdict(self, verdict: str, note: str) -> None:  # noqa: N802
        """A spot-check verdict on the open row (``reviewedBy`` "operator", as the SPA)."""
        if verdict not in VERDICT_CHOICES or self._detail_row is None or self._v["verdictBusy"]:
            return
        recording = self._v["detailId"]
        request = ReviewVerdictRequest(
            recording_id=recording,
            verdict=cast(ReviewVerdictName, verdict),  # one of VERDICT_CHOICES (checked)
            note=note.strip() or None,
            reviewed_by="operator",
        )
        self._set(self.detailChanged, verdictBusy=True)
        context = self.context

        def done(answer: ReviewVerdictAnswer | None, error: BaseException | None) -> None:
            self._set(self.detailChanged, verdictBusy=False)
            if error is not None or answer is None:
                self.toast.emit(f"Verdict failed: {self.failure(error or ValueError())}", "danger")
                return
            recorded = answer.verdict
            changes = {"spotCheckStatus": "reviewed", "humanVerdict": recorded.verdict}
            position = self._rows.index_of("recordingId", recording)
            if position >= 0:
                self._rows.update(position, changes)
            if self._detail_row is not None and self._v["detailId"] == recording:
                self._show_detail(
                    {
                        **self._detail_row,
                        **changes,
                        "reviewedBy": recorded.reviewed_by,
                        "reviewedAt": recorded.reviewed_at,
                    }
                )
            self._set(self.pageChanged, totals=answer.totals.model_dump())
            self.toast.emit(f"Verdict recorded: {labelize(verdict)}", "ok")

        context.tasks.submit(lambda: context.api().review_verdict(request), done)
