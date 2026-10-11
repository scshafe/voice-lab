"""Sign-in and status (design §6.1): the server, its gates, the job runner, the spool.

The account and the settings belong to the app model (``app.py``); this model reads what
the server says about itself: ``GET /api/v1/meta`` (build, API versions served, minimum
client version), ``GET /api/v1/summary`` (the playback, transcript-text and feature-detail
gates) and ``GET /api/v1/watch`` (the job runner's watcher), and counts the local spool
(measurement sessions waiting for the server's acceptance).
"""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import Signal, Slot

from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.journey_contracts import JourneySummary, WatchStatus

from .. import __version__
from ..api import API_VERSION
from ..measurement.store import SessionStore
from ..viewmodel import AppContext, ViewModel, qproperty

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def version_tuple(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(text)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def client_problem(meta: ApiMeta, version: str = __version__) -> str:
    """Why this app cannot use the server ("" when it can)."""
    if API_VERSION not in meta.api_versions:
        served = ", ".join(meta.api_versions) or "none"
        return f"The server does not serve /api/{API_VERSION} (it serves {served}): update the app."
    mine, minimum = version_tuple(version), version_tuple(meta.minimum_client_version)
    if mine is not None and minimum is not None and mine < minimum:
        return (
            f"This app ({version}) is older than the server's minimum client version "
            f"({meta.minimum_client_version}): update it."
        )
    return ""


def watch_lines(watch: WatchStatus) -> list[str]:
    """The job runner's watcher, in a few plain lines."""
    if not watch.enabled:
        return [f"Not running ({watch.reason or 'no state yet'})."]
    state: dict[str, Any] = dict(watch.state or {})
    lines = []
    last = state.get("lastRun")
    if isinstance(last, dict):
        when = last.get("at") or last.get("finishedAt") or last.get("startedAt") or "?"
        what = last.get("status") or "?"
        extra = f", {last['newCount']} new" if isinstance(last.get("newCount"), int) else ""
        lines.append(f"Last run: {what} at {when}{extra}.")
    else:
        lines.append("No run recorded yet.")
    if state.get("nextDigestAt"):
        lines.append(f"Next digest: {state['nextDigestAt']}.")
    return lines


class StatusModel(ViewModel):
    changed = Signal()
    spoolChanged = Signal()

    state = qproperty(str, "state", changed)  # idle | loading | loaded | failed
    error = qproperty(str, "error", changed)
    serverBuild = qproperty(str, "serverBuild", changed)
    apiVersions = qproperty(str, "apiVersions", changed)
    minimumClientVersion = qproperty(str, "minimumClientVersion", changed)
    clientProblem = qproperty(str, "clientProblem", changed)
    playbackEnabled = qproperty(bool, "playbackEnabled", changed)
    transcriptTextEnabled = qproperty(bool, "transcriptTextEnabled", changed)
    featureDetailEnabled = qproperty(bool, "featureDetailEnabled", changed)
    watchLines = qproperty("QVariantList", "watchLines", changed)
    pendingCount = qproperty(int, "pendingCount", spoolChanged)
    failedCount = qproperty(int, "failedCount", spoolChanged)
    spoolText = qproperty(str, "spoolText", spoolChanged)

    def __init__(self, context: AppContext, spool: SessionStore) -> None:
        super().__init__(
            context,
            {
                "state": "idle",
                "error": "",
                "serverBuild": "",
                "apiVersions": "",
                "minimumClientVersion": "",
                "clientProblem": "",
                "playbackEnabled": False,
                "transcriptTextEnabled": False,
                "featureDetailEnabled": False,
                "watchLines": [],
                "pendingCount": 0,
                "failedCount": 0,
                "spoolText": "",
            },
        )
        self.spool = spool

    def activate(self) -> None:
        self.refresh_spool()
        if self._v["state"] in ("idle", "failed"):
            self.refresh()

    def forget(self) -> None:
        self._set(
            self.changed,
            state="idle",
            error="",
            serverBuild="",
            apiVersions="",
            minimumClientVersion="",
            clientProblem="",
            playbackEnabled=False,
            transcriptTextEnabled=False,
            featureDetailEnabled=False,
            watchLines=[],
        )

    @Slot()
    def refresh(self) -> None:
        context = self.context
        if not context.config.configured or not context.session.signed_in:
            self.forget()
            return
        self._set(self.changed, state="loading", error="")

        def work() -> tuple[ApiMeta, JourneySummary | None, WatchStatus | None, str]:
            api = context.api()
            meta = api.meta()
            try:
                summary: JourneySummary | None = api.summary()
                watch: WatchStatus | None = api.watch()
                missing = ""
            except Exception as exc:  # noqa: BLE001 - shown; meta alone still says a lot
                summary, watch, missing = None, None, str(exc)
            return meta, summary, watch, missing

        def done(
            result: tuple[ApiMeta, JourneySummary | None, WatchStatus | None, str] | None,
            error: BaseException | None,
        ) -> None:
            if error is not None or result is None:
                self._set(self.changed, state="failed", error=self.failure(error or ValueError()))
                return
            meta, summary, watch, missing = result
            self._set(
                self.changed,
                state="loaded",
                error=f"The journey routes did not answer: {missing}" if missing else "",
                serverBuild=meta.server_build,
                apiVersions=", ".join(meta.api_versions),
                minimumClientVersion=meta.minimum_client_version,
                clientProblem=client_problem(meta),
                playbackEnabled=bool(summary and summary.playback_enabled),
                transcriptTextEnabled=bool(summary and summary.transcript_text_enabled),
                featureDetailEnabled=bool(summary and summary.feature_detail_enabled),
                watchLines=watch_lines(watch) if watch is not None else [],
            )

        context.tasks.submit(work, done)

    def refresh_spool(self) -> None:
        sessions = self.spool.sessions()
        pending = sum(1 for s in sessions if s.upload_state == "pending")
        failed = sum(1 for s in sessions if s.upload_state == "failed")
        if not sessions:
            text = "No local sessions."
        elif pending or failed:
            parts = [f"{pending} waiting for a receipt"] if pending else []
            parts += [f"{failed} refused by the server"] if failed else []
            text = "Sessions: " + "; ".join(parts) + "."
        else:
            text = "Every local session has been accepted by the server."
        self._set(self.spoolChanged, pendingCount=pending, failedCount=failed, spoolText=text)
