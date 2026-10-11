"""The measurement screen's model: voice-journey-capture's UMIK-1 ritual, for QML.

It is the model of the main window's Record → Measurement screen (design §6.1, phase 4b),
over the app's shared context (``AppContext``: settings, sign-in, worker pool): the ritual's
code paths are voice-journey-capture's, unchanged. The capture client's own sign-in,
settings and uploads are the app's (Status and Uploads); when a session is saved the model
calls ``on_saved``, which hands it to Uploads.

Slow work (device scans, the pre-session probe) runs on the app's pool; results come back to
the GUI thread, so properties only change there. The ritual itself (segment start/stop, the
level polls while recording, the meters) runs on the GUI thread on timers; the audio is
written on the recorder's own thread.

The device scan and the level polls before a session run only while the screen is shown
(``activate``/``deactivate``). A session in progress keeps running when another screen is
shown: its guards (the level polls that abort on a moved level, the timed segments' stops)
live here, not in the screen, and the keyboard only acts on the measurement screen. PortAudio
is loaded the first time the screen is shown; when it cannot be, the screen says why.

When the screen is shown the model also reads the server's chain registry
(``GET /api/v1/chains``) and compares it with the chain fixed in ``chain.py``: a registry that
describes the chain differently blocks a new session; a registry that cannot be read (signed
out, offline, a server without the route) leaves the fixed chain in use and says so.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Signal, Slot

from ..api import ApiError, ApiRefused
from ..auth import AuthError, SignInRequired
from ..config import ConfigError
from ..viewmodel import AppContext, ViewModel, qproperty
from . import chain
from .devices import AudioBackend, DeviceError, InputDevice, choose_umik
from .levels import InputLevel, LevelReader, LevelUnreadable
from .package import PackageError
from .pin import CalFileError, GuardError, LevelMoved, LevelMuted, NotPinned, StateStore, guard
from .ritual import Ritual, RitualError
from .store import SessionStore
from .wav import meter_fraction

METER_MS = 50
LEVEL_POLL_MS = 1000
SETUP_POLL_MS = 2000
#: The phases in which no stream is open and a new session may start.
IDLE_PHASES = ("setup", "done", "aborted")
#: The phases of a session in progress.
SESSION_PHASES = ("segment", "recording", "review")


class Controller(ViewModel):
    setupChanged = Signal()
    sessionChanged = Signal()
    meterChanged = Signal()
    settingsChanged = Signal()

    distanceCm = qproperty(int, "distanceCm", settingsChanged)
    # setup: device, level, cal file
    deviceName = qproperty(str, "deviceName", setupChanged)
    deviceOk = qproperty(bool, "deviceOk", setupChanged)
    deviceMessage = qproperty(str, "deviceMessage", setupChanged)
    # ok | unpinned | moved | muted | unreadable | unknown
    levelState = qproperty(str, "levelState", setupChanged)
    levelText = qproperty(str, "levelText", setupChanged)
    levelMessage = qproperty(str, "levelMessage", setupChanged)
    pinnedText = qproperty(str, "pinnedText", setupChanged)
    calOk = qproperty(bool, "calOk", setupChanged)
    calText = qproperty(str, "calText", setupChanged)
    chainState = qproperty(str, "chainState", setupChanged)
    chainText = qproperty(str, "chainText", setupChanged)
    canStart = qproperty(bool, "canStart", setupChanged)
    startBlocker = qproperty(str, "startBlocker", setupChanged)
    busy = qproperty(bool, "busy", setupChanged)
    # the session: setup | starting | segment | recording | review | done | aborted
    phase = qproperty(str, "phase", sessionChanged)
    inSession = qproperty(bool, "inSession", sessionChanged)
    sessionId = qproperty(str, "sessionId", sessionChanged)
    segments = qproperty("QVariantList", "segments", sessionChanged)
    currentIndex = qproperty(int, "currentIndex", sessionChanged)
    currentLabel = qproperty(str, "currentLabel", sessionChanged)
    currentMode = qproperty(str, "currentMode", sessionChanged)
    canRedo = qproperty(bool, "canRedo", sessionChanged)
    abortReason = qproperty(str, "abortReason", sessionChanged)
    calTone = qproperty(bool, "calTone", sessionChanged)
    skipFree = qproperty(bool, "skipFree", sessionChanged)
    # meters
    meterPeak = qproperty(float, "meterPeak", meterChanged)
    meterRms = qproperty(float, "meterRms", meterChanged)
    meterText = qproperty(str, "meterText", meterChanged)
    clipping = qproperty(bool, "clipping", meterChanged)
    elapsedText = qproperty(str, "elapsedText", meterChanged)

    def __init__(
        self,
        context: AppContext,
        store: SessionStore,
        backend: Callable[[], AudioBackend],
        levels: Callable[[], LevelReader],
        *,
        on_saved: Callable[[], None] = lambda: None,
    ) -> None:
        """``backend`` and ``levels`` make the audio backend and the level reader the first
        time the screen is shown (PortAudio is a system library on Linux)."""
        super().__init__(
            context,
            {
                "distanceCm": context.config.distance_cm,
                "deviceName": "",
                "deviceOk": False,
                "deviceMessage": "Looking for the UMIK-1…",
                "levelState": "unknown",
                "levelText": "",
                "levelMessage": "",
                "pinnedText": "",
                "calOk": False,
                "calText": "",
                "chainState": "unchecked",
                "chainText": "",
                "canStart": False,
                "startBlocker": "",
                "busy": False,
                "phase": "setup",
                "inSession": False,
                "sessionId": "",
                "segments": [],
                "currentIndex": -1,
                "currentLabel": "",
                "currentMode": "",
                "canRedo": False,
                "abortReason": "",
                "calTone": False,
                "skipFree": False,
                "meterPeak": 0.0,
                "meterRms": 0.0,
                "meterText": "",
                "clipping": False,
                "elapsedText": "",
            },
        )
        self.state = StateStore(context.config.measurement_dir)
        self.store = store
        self.on_saved = on_saved
        self._make_backend = backend
        self._make_levels = levels
        self.backend: AudioBackend | None = None
        self._levels: LevelReader | None = None
        self.device: InputDevice | None = None
        self.ritual: Ritual | None = None
        self.shown = False
        self._closed = False
        self._meter = QTimer(self)
        self._meter.setInterval(METER_MS)
        self._meter.timeout.connect(self._on_meter)
        self._level_poll = QTimer(self)
        self._level_poll.setInterval(LEVEL_POLL_MS)
        self._level_poll.timeout.connect(self._on_level_poll)
        self._setup_poll = QTimer(self)
        self._setup_poll.setInterval(SETUP_POLL_MS)
        self._setup_poll.timeout.connect(self._on_setup_poll)
        context.configChanged.connect(self._config_changed)

    # --- plumbing ------------------------------------------------------------------------
    @property
    def levels(self) -> LevelReader:
        if self._levels is None:
            self._levels = self._make_levels()
        return self._levels

    def _submit(
        self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        if self._closed:
            return

        def guarded(result: Any, error: BaseException | None) -> None:
            if not self._closed:
                done(result, error)

        self.context.tasks.submit(work, guarded)

    def _set_phase(self, phase: str, **values: Any) -> None:
        self._set(self.sessionChanged, phase=phase, inSession=phase in SESSION_PHASES, **values)

    @Slot()
    def _config_changed(self) -> None:
        self._set(self.settingsChanged, distanceCm=self.context.config.distance_cm)

    # --- lifecycle -----------------------------------------------------------------------
    def activate(self) -> None:
        """The screen is shown: find the UMIK-1 and watch its level (never during a take)."""
        self.shown = True
        if self._v["chainState"] in ("unchecked", "unavailable"):
            self.checkChainRegistry()
        if self.backend is None and not self._load_backend():
            return
        self._setup_poll.start()
        self.refreshDevices()

    def deactivate(self) -> None:
        """Another screen is shown: stop scanning and polling before a session. A session in
        progress keeps running (its own timers guard it)."""
        self.shown = False
        self._setup_poll.stop()

    def _load_backend(self) -> bool:
        try:
            self.backend = self._make_backend()
        except DeviceError as exc:
            self._set(self.setupChanged, deviceName="", deviceOk=False, deviceMessage=str(exc))
            self._update_can_start()
            return False
        return True

    def shutdown(self) -> None:
        if self._closed:
            return
        if self.ritual is not None and self.ritual.status == "open":
            self.ritual.abort("the app was closed during the session")
        self._closed = True
        for timer in (self._meter, self._level_poll, self._setup_poll):
            timer.stop()

    # --- setup: the chain registry, device, level, cal file ---------------------------------
    @Slot()
    def checkChainRegistry(self) -> None:  # noqa: N802 (QML name)
        """Compare the server's chain registry with this app's chain (off the GUI thread)."""
        fixed = f"this app's chain ({chain.LABEL}) is used; the intake still checks each session"
        if not self.context.session.signed_in or not self.context.config.server_url:
            self._chain("unavailable", f"Not checked against the server (signed out): {fixed}.")
            return
        context = self.context

        def done(chains: Any, error: BaseException | None) -> None:
            if error is not None:
                if isinstance(error, ApiRefused) and error.status == 404:
                    reason = "the server does not serve the chain registry yet"
                elif isinstance(error, SignInRequired):
                    reason = "sign in again on the Status screen"
                elif isinstance(error, (ApiError, AuthError, ConfigError)):
                    reason = str(error)
                else:
                    raise error
                self._chain(
                    "unavailable", f"The chain registry could not be read ({reason}): {fixed}."
                )
                return
            disagreement = chain.registry_disagreement(chains.chains)
            if disagreement is None:
                self._chain(
                    "agrees", f"Chain {chain.CHAIN_ID} matches the server's chain registry."
                )
            else:
                self._chain("differs", disagreement[0].upper() + disagreement[1:])

        self._submit(lambda: context.api().chains(), done)

    def _chain(self, state: str, text: str) -> None:
        self._set(self.setupChanged, chainState=state, chainText=text)
        self._update_can_start()

    @Slot()
    def refreshDevices(self) -> None:  # noqa: N802 (QML name)
        if self._v["phase"] not in IDLE_PHASES or self._v["busy"]:
            return  # never re-scan PortAudio while a stream may be open
        backend = self.backend
        if backend is None:
            return
        self._set(self.setupChanged, busy=True)

        def work() -> tuple[InputDevice, tuple[InputLevel | None, str | None]]:
            devices = backend.list_inputs()
            device = choose_umik(devices)
            return device, self._read_level(device)

        def done(
            result: tuple[InputDevice, tuple[InputLevel | None, str | None]] | None,
            error: BaseException | None,
        ) -> None:
            self._set(self.setupChanged, busy=False)
            if error is not None:
                self.device = None
                if not isinstance(error, DeviceError):
                    error = DeviceError(f"listing input devices failed ({type(error).__name__})")
                self._set(
                    self.setupChanged, deviceName="", deviceOk=False, deviceMessage=str(error)
                )
                self._apply_level(None, None)
                return
            assert result is not None
            device, (level, level_error) = result
            self.device = device
            self._set(
                self.setupChanged,
                deviceName=device.name,
                deviceOk=True,
                deviceMessage=f"{chain.LABEL} via {device.host_api}",
            )
            self._apply_level(level, level_error)

        self._submit(work, done)

    def _read_level(self, device: InputDevice) -> tuple[InputLevel | None, str | None]:
        try:
            return self.levels.read(device), None
        except LevelUnreadable as exc:
            return None, str(exc)

    def _apply_level(self, level: InputLevel | None, error: str | None) -> None:
        pin = None
        pin_error = None
        try:
            pin = self.state.pin()
        except GuardError as exc:
            pin_error = str(exc)
        pinned = (
            f"pinned at {pin.level.volume}% ({pin.level.source}) since {pin.pinned_at[:10]}"
            if pin
            else "not pinned"
        )
        if self.device is None:
            state, text, message = "unknown", "", ""
        elif level is None:
            state, text, message = (
                "unreadable",
                "unreadable",
                f"The input level cannot be read, so recording is refused: {error}",
            )
        else:
            text = f"{level.volume}%" + (" (muted)" if level.muted else "")
            try:
                if pin_error:
                    raise GuardError(pin_error)
                guard(pin, level)
                state, message = "ok", f"Matches the pin ({level.source})."
            except LevelMuted as exc:
                state, message = "muted", str(exc)
            except NotPinned as exc:
                state, message = "unpinned", str(exc)
            except LevelMoved as exc:
                state, message = "moved", str(exc)
            except GuardError as exc:
                state, message = "unreadable", str(exc)
        cal_text, cal_ok = self._cal_status()
        self._set(
            self.setupChanged,
            levelState=state,
            levelText=text,
            levelMessage=message,
            pinnedText=pinned,
            calText=cal_text,
            calOk=cal_ok,
        )
        self._update_can_start()

    def _cal_status(self) -> tuple[str, bool]:
        try:
            cal, _digest = self.state.current_cal_sha256()
        except (CalFileError, GuardError) as exc:
            return str(exc), False
        if cal is None:
            return (
                "No cal file imported: sessions are recorded uncalibrated (calFileSha256 null).",
                False,
            )
        serial = f", serial {cal.serial}" if cal.serial else ""
        return f"{cal.name}{serial} (sha256 {cal.sha256[:12]}…)", True

    def _update_can_start(self) -> None:
        blocker = ""
        if self._v["chainState"] == "differs":
            blocker = self._v["chainText"]
        elif not self._v["deviceOk"]:
            blocker = "Plug in the UMIK-1 (see Microphone)."
        elif self._v["levelState"] != "ok":
            blocker = {
                "unpinned": "Pin the input level first.",
                "moved": "The input level moved: restore it, or re-pin deliberately.",
                "muted": "Unmute the UMIK-1's input.",
                "unreadable": "The input level cannot be read, so recording is refused.",
            }.get(self._v["levelState"], "Waiting for the input level.")
        self._set(
            self.setupChanged,
            canStart=blocker == "" and self._v["phase"] in IDLE_PHASES,
            startBlocker=blocker,
        )

    @Slot()
    def _on_setup_poll(self) -> None:
        if (
            not self.shown
            or self._v["phase"] not in IDLE_PHASES
            or self.device is None
            or self._v["busy"]
        ):
            return
        device = self.device
        self._submit(
            lambda: self._read_level(device),
            lambda result, error: self._apply_level(*result) if error is None else None,
        )

    @Slot()
    def pinLevel(self) -> None:  # noqa: N802
        if self.device is None or self._v["phase"] not in IDLE_PHASES:
            return
        level, error = self._read_level(self.device)
        if level is None:
            self.toast.emit(f"Not pinned: the level cannot be read ({error}).", "danger")
            self._apply_level(None, error)
            return
        try:
            previous, new = self.state.set_pin(level, self.device.name)
        except GuardError as exc:
            self.toast.emit(f"Not pinned: {exc}", "danger")
            return
        if previous and not previous.level.same_setting(level):
            self.toast.emit(
                f"Re-pinned at {new.level.volume}% (was {previous.level.volume}%): "
                "logged as a chain era event.",
                "warn",
            )
        else:
            self.toast.emit(f"Input level pinned at {new.level.volume}%.", "ok")
        self._apply_level(level, None)

    @Slot(str)
    def importCalFile(self, path: str) -> None:  # noqa: N802
        text = path.strip()
        if text.startswith("file://"):
            from urllib.parse import unquote, urlsplit

            text = unquote(urlsplit(text).path)
        if not text:
            return
        try:
            cal = self.state.import_cal_file(Path(text).expanduser())
        except CalFileError as exc:
            self.toast.emit(f"Cal file not imported: {exc}", "danger")
            return
        self.toast.emit(f"Cal file {cal.name} imported.", "ok")
        cal_text, cal_ok = self._cal_status()
        self._set(self.setupChanged, calText=cal_text, calOk=cal_ok)

    @Slot(bool)
    def setCalTone(self, on: bool) -> None:  # noqa: N802
        if self._v["phase"] in IDLE_PHASES:
            self._set(self.sessionChanged, calTone=bool(on))

    @Slot(bool)
    def setSkipFree(self, on: bool) -> None:  # noqa: N802
        if self._v["phase"] in IDLE_PHASES:
            self._set(self.sessionChanged, skipFree=bool(on))

    # --- the ritual ------------------------------------------------------------------------
    @Slot()
    def startSession(self) -> None:  # noqa: N802
        if not self._v["canStart"] or self.device is None or self.backend is None:
            return
        ritual = Ritual(
            self.backend,
            self.device,
            self.levels,
            self.state,
            self.store,
            self.context.config.distance_cm,
            cal_tone=self._v["calTone"],
            skip_free=self._v["skipFree"],
        )
        self._set_phase("starting", abortReason="")
        self._update_can_start()

        def done(_result: None, error: BaseException | None) -> None:
            if error is not None:
                self._set_phase("setup")
                self._update_can_start()
                if isinstance(error, (RitualError, GuardError, CalFileError)):
                    self.toast.emit(f"Session not started: {error}", "danger")
                    return
                raise error
            self.ritual = ritual
            self._set_phase("segment", sessionId=ritual.session_id)
            self._sync_segments()

        self._submit(ritual.begin, done)

    def _sync_segments(self) -> None:
        r = self.ritual
        if r is None:
            return
        rows = []
        for i, seg in enumerate(r.plan):
            if i < len(r.results):
                state, detail = "done", f"{r.results[i]['durationSeconds']:.1f} s"
            elif i == r.index and r.recording is not None:
                state, detail = "recording", ""
            elif i == r.index and r.status == "open":
                state, detail = (
                    "current",
                    f"{seg.seconds} s, stops by itself" if seg.mode == "timed" else "you stop it",
                )
            else:
                state, detail = "pending", f"{seg.seconds} s" if seg.mode == "timed" else ""
            rows.append(
                {
                    "key": seg.key,
                    "label": seg.label,
                    "mode": seg.mode,
                    "state": state,
                    "detail": detail,
                }
            )
        current = r.current
        self._set(
            self.sessionChanged,
            segments=rows,
            currentIndex=r.index if current else -1,
            currentLabel=current.label if current else "",
            currentMode=current.mode if current else "",
            canRedo=bool(r.results) and r.recording is None and r.status == "open",
        )

    @Slot()
    def toggleRecording(self) -> None:  # noqa: N802
        """Space: start the current segment, or stop a manual one."""
        if self._v["phase"] == "segment":
            self.startSegment()
        elif self._v["phase"] == "recording" and self._v["currentMode"] == "manual":
            self.stopSegment()

    @Slot()
    def startSegment(self) -> None:  # noqa: N802
        r = self.ritual
        if r is None or self._v["phase"] != "segment":
            return
        try:
            r.start_segment()
        except RitualError as exc:
            self._aborted(str(exc))
            return
        self._set_phase("recording")
        self._sync_segments()
        self._meter.start()
        self._level_poll.start()

    @Slot()
    def stopSegment(self) -> None:  # noqa: N802
        r = self.ritual
        if r is None or self._v["phase"] != "recording":
            return
        self._meter.stop()
        self._level_poll.stop()
        try:
            r.finish_segment()
        except RitualError as exc:
            if r.status == "aborted":
                self._aborted(str(exc))
                return
            self.toast.emit(str(exc), "warn")  # too short: record it again
        self._set(self.meterChanged, meterPeak=0.0, meterRms=0.0, clipping=False)
        self._set_phase("segment" if r.current else "review")
        self._sync_segments()

    @Slot()
    def redoSegment(self) -> None:  # noqa: N802
        r = self.ritual
        if r is None or self._v["phase"] not in ("segment", "review"):
            return
        try:
            r.redo_last()
        except RitualError as exc:
            self.toast.emit(str(exc), "warn")
            return
        self._set_phase("segment")
        self._sync_segments()

    @Slot()
    def finishSession(self) -> None:  # noqa: N802
        r = self.ritual
        if r is None or self._v["phase"] != "review":
            return
        try:
            manifest = r.complete()
        except (RitualError, PackageError) as exc:
            self._aborted(str(exc))
            return
        self._set_phase("done")
        self._sync_segments()
        self._update_can_start()
        self.toast.emit(
            f"Session {manifest['sessionId']} saved: {len(manifest['files'])} files. "
            "It uploads when signed in (see Uploads).",
            "ok",
        )
        self.on_saved()

    @Slot()
    def abortSession(self) -> None:  # noqa: N802
        r = self.ritual
        if r is None or r.status != "open":
            return
        r.abort("stopped by you")
        self._aborted("stopped by you")

    @Slot()
    def newSession(self) -> None:  # noqa: N802
        if self._v["phase"] not in ("done", "aborted"):
            return
        self.ritual = None
        self._set_phase(
            "setup",
            sessionId="",
            segments=[],
            currentIndex=-1,
            currentLabel="",
            currentMode="",
            canRedo=False,
            abortReason="",
        )
        self._update_can_start()
        self.refreshDevices()

    def _aborted(self, reason: str) -> None:
        self._meter.stop()
        self._level_poll.stop()
        self._set(self.meterChanged, meterPeak=0.0, meterRms=0.0, clipping=False, elapsedText="")
        self._set_phase("aborted", abortReason=reason)
        self._sync_segments()
        self._update_can_start()
        self.toast.emit(f"Session aborted: {reason}", "danger")
        self.refreshDevices()

    @Slot()
    def _on_meter(self) -> None:
        r = self.ritual
        if r is None or r.recording is None:
            return
        recording = r.recording
        block = recording.last_block
        seconds = recording.seconds
        segment = r.current
        if segment and segment.seconds:
            elapsed = f"{max(0.0, segment.seconds - seconds):.1f} s left"
        else:
            elapsed = f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
        peak = block.peak_dbfs
        self._set(
            self.meterChanged,
            meterPeak=meter_fraction(peak),
            meterRms=meter_fraction(block.rms_dbfs),
            meterText="—" if peak is None else f"{peak:.1f} dBFS peak",
            clipping=recording.stats.clipped > 0,
            elapsedText=elapsed,
        )
        if recording.finished.is_set():  # a timed segment reached its length (or the writer failed)
            self.stopSegment()

    @Slot()
    def _on_level_poll(self) -> None:
        r = self.ritual
        if r is None or r.recording is None:
            return
        try:
            r.poll_level()
        except RitualError as exc:
            self._aborted(str(exc))
