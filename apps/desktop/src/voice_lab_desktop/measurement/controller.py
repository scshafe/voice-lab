"""The window's state and actions, for QML.

Slow work (device scans, the pre-session probe, sign-in, uploads) runs on a
small thread pool; results come back to the GUI thread through a queued
signal, so properties only change on the GUI thread. The ritual itself
(segment start/stop, the level polls while recording, the meters) runs on the
GUI thread on timers; the audio is written on the recorder's own thread.
"""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from PySide6.QtCore import Property, QObject, Qt, QTimer, Signal, SignalInstance, Slot

from ..auth import AuthError, Session, SignInRequired, sign_in
from ..config import Config, ConfigError, save_settings
from ..tokens import NoKeychainError, Tokens, TokenStore, jwt_claims
from . import chain
from .devices import AudioBackend, DeviceError, InputDevice, choose_umik
from .levels import InputLevel, LevelReader, LevelUnreadable
from .package import PackageError
from .pin import CalFileError, GuardError, LevelMoved, LevelMuted, NotPinned, StateStore, guard
from .ritual import Ritual, RitualError
from .store import SessionStore
from .upload import IntakeClient, UploadSummary, new_http, upload_pending
from .wav import meter_fraction

METER_MS = 50
LEVEL_POLL_MS = 1000
SETUP_POLL_MS = 2000


class _Bridge(QObject):
    """Runs callables on the GUI thread (queued)."""

    run = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.run.connect(self._call, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _call(self, fn: Callable[[], None]) -> None:
        fn()


def _p(kind: Any, name: str, notify: Signal) -> Property:
    def getter(self: Controller) -> Any:
        return self._v[name]

    return Property(kind, getter, notify=notify)


class Controller(QObject):
    accountChanged = Signal()
    setupChanged = Signal()
    sessionChanged = Signal()
    meterChanged = Signal()
    uploadsChanged = Signal()
    toast = Signal(str, str)  # text, tone

    # account and settings
    signedIn = _p(bool, "signedIn", accountChanged)
    account = _p(str, "account", accountChanged)
    authBusy = _p(bool, "authBusy", accountChanged)
    server = _p(str, "server", accountChanged)
    serverLocked = _p(bool, "serverLocked", accountChanged)
    settingsSource = _p(str, "settingsSource", accountChanged)
    distanceCm = _p(int, "distanceCm", accountChanged)
    # setup: device, level, cal file
    deviceName = _p(str, "deviceName", setupChanged)
    deviceOk = _p(bool, "deviceOk", setupChanged)
    deviceMessage = _p(str, "deviceMessage", setupChanged)
    levelState = _p(
        str, "levelState", setupChanged
    )  # ok | unpinned | moved | muted | unreadable | unknown
    levelText = _p(str, "levelText", setupChanged)
    levelMessage = _p(str, "levelMessage", setupChanged)
    pinnedText = _p(str, "pinnedText", setupChanged)
    calOk = _p(bool, "calOk", setupChanged)
    calText = _p(str, "calText", setupChanged)
    canStart = _p(bool, "canStart", setupChanged)
    startBlocker = _p(str, "startBlocker", setupChanged)
    busy = _p(bool, "busy", setupChanged)
    # the session
    phase = _p(
        str, "phase", sessionChanged
    )  # setup | starting | segment | recording | review | done | aborted
    sessionId = _p(str, "sessionId", sessionChanged)
    segments = _p("QVariantList", "segments", sessionChanged)
    currentIndex = _p(int, "currentIndex", sessionChanged)
    currentLabel = _p(str, "currentLabel", sessionChanged)
    currentMode = _p(str, "currentMode", sessionChanged)
    canRedo = _p(bool, "canRedo", sessionChanged)
    abortReason = _p(str, "abortReason", sessionChanged)
    calTone = _p(bool, "calTone", sessionChanged)
    skipFree = _p(bool, "skipFree", sessionChanged)
    # meters
    meterPeak = _p(float, "meterPeak", meterChanged)
    meterRms = _p(float, "meterRms", meterChanged)
    meterText = _p(str, "meterText", meterChanged)
    clipping = _p(bool, "clipping", meterChanged)
    elapsedText = _p(str, "elapsedText", meterChanged)
    # uploads
    pendingCount = _p(int, "pendingCount", uploadsChanged)
    failedCount = _p(int, "failedCount", uploadsChanged)
    uploadBusy = _p(bool, "uploadBusy", uploadsChanged)
    uploadText = _p(str, "uploadText", uploadsChanged)

    def __init__(
        self,
        config: Config,
        tokens: TokenStore,
        backend: AudioBackend,
        levels: LevelReader,
        *,
        open_browser: Callable[[str], object] = webbrowser.open,
        fixed_port: bool = False,
        auto_upload: bool = True,
        http: httpx.Client | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.backend = backend
        self.levels = levels
        self.open_browser = open_browser
        self.fixed_port = fixed_port
        self.auto_upload = auto_upload
        self.http = http or new_http()
        self.session = Session(config, tokens, self.http)
        self.state = StateStore(config.measurement_dir)
        self.store = SessionStore(config.measurement_dir)
        self.device: InputDevice | None = None
        self.ritual: Ritual | None = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vjc")
        self._bridge = _Bridge()
        self._cancel_sign_in = threading.Event()
        self._closed = False
        self._v: dict[str, Any] = {
            "signedIn": self.session.signed_in,
            "account": "",
            "authBusy": False,
            "server": config.server_url,
            "serverLocked": "server_url" in config.env_keys,
            "settingsSource": config.source,
            "distanceCm": config.distance_cm,
            "deviceName": "",
            "deviceOk": False,
            "deviceMessage": "Looking for the UMIK-1…",
            "levelState": "unknown",
            "levelText": "",
            "levelMessage": "",
            "pinnedText": "",
            "calOk": False,
            "calText": "",
            "canStart": False,
            "startBlocker": "",
            "busy": False,
            "phase": "setup",
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
            "pendingCount": 0,
            "failedCount": 0,
            "uploadBusy": False,
            "uploadText": "",
        }
        self._v["account"] = self._account_label()
        self._meter = QTimer(self)
        self._meter.setInterval(METER_MS)
        self._meter.timeout.connect(self._on_meter)
        self._level_poll = QTimer(self)
        self._level_poll.setInterval(LEVEL_POLL_MS)
        self._level_poll.timeout.connect(self._on_level_poll)
        self._setup_poll = QTimer(self)
        self._setup_poll.setInterval(SETUP_POLL_MS)
        self._setup_poll.timeout.connect(self._on_setup_poll)

    # --- plumbing ------------------------------------------------------------------------
    def _set(self, signal: SignalInstance, **values: Any) -> None:
        changed = False
        for key, value in values.items():
            if self._v.get(key) != value:
                self._v[key] = value
                changed = True
        if changed:
            signal.emit()

    def _submit(
        self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        if self._closed:
            return

        def run() -> None:
            try:
                result, error = work(), None
            except BaseException as exc:  # noqa: BLE001 - handed to the GUI thread
                result, error = None, exc
            if not self._closed:
                self._bridge.run.emit(lambda: done(result, error))

        self._pool.submit(run)

    def _account_label(self) -> str:
        tokens = self.session.tokens  # display only
        if tokens is None:
            return ""
        try:
            claims = jwt_claims(tokens.id_token)
        except (ValueError, UnicodeDecodeError):
            return "signed in"
        return str(claims.get("email") or claims.get("preferred_username") or "signed in")

    # --- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        self.refreshDevices()
        self._refresh_uploads()
        self._setup_poll.start()
        if self.auto_upload and self.session.signed_in and self.store.pending():
            self.uploadPending()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._cancel_sign_in.set()
        if self.ritual is not None and self.ritual.status == "open":
            self.ritual.abort("the app was closed during the session")
        self._closed = True
        for timer in (self._meter, self._level_poll, self._setup_poll):
            timer.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- account ---------------------------------------------------------------------------
    @Slot()
    def signIn(self) -> None:  # noqa: N802 (QML name)
        if self._v["authBusy"]:
            return
        self._cancel_sign_in.clear()
        self._set(self.accountChanged, authBusy=True)

        def work() -> Tokens:
            from ..tokens import KeyringStore

            if isinstance(self.session.store, KeyringStore):
                KeyringStore.check_backend()
            return sign_in(
                self.config,
                self.http,
                self.open_browser,
                fixed_port=self.fixed_port,
                cancelled=self._cancel_sign_in,
            )

        def done(tokens: Tokens | None, error: BaseException | None) -> None:
            self._set(self.accountChanged, authBusy=False)
            if error is not None:
                failures = (AuthError, NoKeychainError, httpx.HTTPError, ConfigError)
                if isinstance(error, failures):
                    self.toast.emit(f"Sign-in failed: {error}", "danger")
                    return
                raise error
            assert tokens is not None
            try:
                self.session.adopt(tokens)
            except NoKeychainError as exc:
                self.toast.emit(str(exc), "danger")
                return
            self._set(self.accountChanged, signedIn=True, account=self._account_label())
            self.toast.emit("Signed in.", "ok")
            if self.auto_upload and self.store.pending():
                self.uploadPending()

        self._submit(work, done)

    @Slot()
    def cancelSignIn(self) -> None:  # noqa: N802
        self._cancel_sign_in.set()

    @Slot()
    def signOut(self) -> None:  # noqa: N802
        self.session.sign_out()
        self._set(self.accountChanged, signedIn=False, account="")

    @Slot(str, int, result=bool)
    def saveSettings(self, server: str, distance_cm: int) -> bool:  # noqa: N802
        changes: dict[str, Any] = {"distance_cm": int(distance_cm)}
        if "server_url" not in self.config.env_keys:
            changes["server_url"] = server.strip()
        try:
            self.config = save_settings(self.config, changes)
        except ConfigError as exc:
            self.toast.emit(f"Settings not saved: {exc}", "danger")
            return False
        self.session.config = self.config
        self._set(
            self.accountChanged,
            server=self.config.server_url,
            distanceCm=self.config.distance_cm,
            settingsSource=self.config.source,
        )
        self.toast.emit("Settings saved.", "ok")
        return True

    # --- setup: device, level, cal file ----------------------------------------------------
    @Slot()
    def refreshDevices(self) -> None:  # noqa: N802
        if self._v["phase"] not in ("setup", "done", "aborted") or self._v["busy"]:
            return  # never re-scan PortAudio while a stream may be open
        self._set(self.setupChanged, busy=True)

        def work() -> tuple[InputDevice, tuple[InputLevel | None, str | None]]:
            devices = self.backend.list_inputs()
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
        if not self._v["deviceOk"]:
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
            canStart=blocker == "" and self._v["phase"] in ("setup", "done", "aborted"),
            startBlocker=blocker,
        )

    def _on_setup_poll(self) -> None:
        if (
            self._v["phase"] not in ("setup", "done", "aborted")
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
        if self.device is None or self._v["phase"] not in ("setup", "done", "aborted"):
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
        if self._v["phase"] in ("setup", "done", "aborted"):
            self._set(self.sessionChanged, calTone=bool(on))

    @Slot(bool)
    def setSkipFree(self, on: bool) -> None:  # noqa: N802
        if self._v["phase"] in ("setup", "done", "aborted"):
            self._set(self.sessionChanged, skipFree=bool(on))

    # --- the ritual ------------------------------------------------------------------------
    @Slot()
    def startSession(self) -> None:  # noqa: N802
        if not self._v["canStart"] or self.device is None:
            return
        ritual = Ritual(
            self.backend,
            self.device,
            self.levels,
            self.state,
            self.store,
            self.config.distance_cm,
            cal_tone=self._v["calTone"],
            skip_free=self._v["skipFree"],
        )
        self._set(self.sessionChanged, phase="starting", abortReason="")
        self._update_can_start()

        def done(_result: None, error: BaseException | None) -> None:
            if error is not None:
                self._set(self.sessionChanged, phase="setup")
                self._update_can_start()
                if isinstance(error, (RitualError, GuardError, CalFileError)):
                    self.toast.emit(f"Session not started: {error}", "danger")
                    return
                raise error
            self.ritual = ritual
            self._set(self.sessionChanged, phase="segment", sessionId=ritual.session_id)
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
        self._set(self.sessionChanged, phase="recording")
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
        self._set(self.sessionChanged, phase="segment" if r.current else "review")
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
        self._set(self.sessionChanged, phase="segment")
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
        self._set(self.sessionChanged, phase="done")
        self._sync_segments()
        self._update_can_start()
        self.toast.emit(
            f"Session {manifest['sessionId']} saved: {len(manifest['files'])} files. "
            "It uploads when signed in.",
            "ok",
        )
        self._refresh_uploads()
        if self.auto_upload and self.session.signed_in:
            self.uploadPending()

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
        self._set(
            self.sessionChanged,
            phase="setup",
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
        self._set(self.sessionChanged, phase="aborted", abortReason=reason)
        self._sync_segments()
        self._update_can_start()
        self.toast.emit(f"Session aborted: {reason}", "danger")
        self.refreshDevices()

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

    def _on_level_poll(self) -> None:
        r = self.ritual
        if r is None or r.recording is None:
            return
        try:
            r.poll_level()
        except RitualError as exc:
            self._aborted(str(exc))

    # --- uploads ---------------------------------------------------------------------------
    def _refresh_uploads(self) -> None:
        sessions = self.store.sessions()
        pending = sum(1 for s in sessions if s.upload_state == "pending")
        failed = sum(1 for s in sessions if s.upload_state == "failed")
        text = (
            self._v["uploadText"]
            if self._v["uploadBusy"]
            else (
                f"{pending} session(s) waiting to upload"
                if pending
                else "Every session is uploaded"
                if sessions
                else ""
            )
        )
        if failed:
            text += f"; {failed} refused by the server (see --sessions)"
        self._set(self.uploadsChanged, pendingCount=pending, failedCount=failed, uploadText=text)

    @Slot()
    def uploadPending(self) -> None:  # noqa: N802
        if self._v["uploadBusy"]:
            return
        if not self.session.signed_in:
            self.toast.emit("Sign in to upload.", "info")
            return
        try:
            server = self.config.require_server()
        except ConfigError as exc:
            self.toast.emit(f"Upload needs the settings: {exc}", "warn")
            return
        self._set(self.uploadsChanged, uploadBusy=True, uploadText="Uploading…")
        client = IntakeClient(server, self.session, self.http)

        def progress(name: str, sent: int, size: int) -> None:
            pct = 100 * sent // max(size, 1)
            self._bridge.run.emit(
                lambda: self._set(self.uploadsChanged, uploadText=f"Uploading {name}: {pct}%")
            )

        def done(summary: UploadSummary | None, error: BaseException | None) -> None:
            self._set(self.uploadsChanged, uploadBusy=False, uploadText="")
            if error is not None:
                if isinstance(error, SignInRequired):
                    self._set(self.accountChanged, signedIn=self.session.signed_in)
                    self.toast.emit(f"Upload needs a sign-in: {error}", "warn")
                elif isinstance(error, (OSError, httpx.HTTPError)):
                    self.toast.emit(f"Upload failed: {type(error).__name__}", "danger")
                else:
                    raise error
            elif summary is not None:
                if summary.accepted:
                    self.toast.emit(f"{summary.accepted} session(s) accepted by the server.", "ok")
                if summary.failed:
                    self.toast.emit(
                        f"{summary.failed} session(s) refused: {summary.last_error}", "danger"
                    )
                if summary.pending:
                    self.toast.emit(
                        f"{summary.pending} session(s) not uploaded yet ({summary.last_error}); "
                        "retried next launch.",
                        "warn",
                    )
            self._refresh_uploads()

        self._submit(lambda: upload_pending(client, self.store, server, progress), done)
