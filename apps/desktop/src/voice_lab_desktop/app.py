"""The app model: the account, the settings, the sidebar and the screens' models.

This is the app's composition root. It builds one model per screen over a shared
:class:`~voice_lab_desktop.viewmodel.AppContext` and hands them to QML as context
properties (``app``, ``status``, ``uploads``, ``measurement``, ``corpus``, ``journey``,
``practice``, ``referee``, ``player``; Record: prompts' model is ``app.promptsModel``). It
owns the sign-in (Pocket ID, PKCE, a loopback
redirect; ``auth.py``) and the settings (``config.py``), and it navigates: a screen's model is
activated when its screen is shown, which loads its data the first time, and deactivated when
another is (the measurement screen stops scanning the audio devices).
"""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from typing import Any, Final

import httpx
from PySide6.QtCore import Property, QObject, Signal, Slot

from . import __version__
from .auth import AuthError, Session, sign_in
from .capture.types import TakeRecorder
from .config import Config, ConfigError, save_settings
from .measurement.controller import Controller
from .measurement.devices import AudioBackend
from .measurement.levels import LevelReader
from .measurement.store import SessionStore
from .playback import AudioOutput, Player
from .prompted.store import PromptedStore
from .screens.corpus import CorpusModel
from .screens.journey import JourneyModel
from .screens.practice import PracticeModel
from .screens.prompts import PromptsModel, default_recorder
from .screens.referee import RefereeModel
from .screens.status import StatusModel
from .screens.uploads import UploadsModel
from .tablemodel import RoleRows
from .tasks import Tasks
from .tokens import KeyringStore, NoKeychainError, TokenStore, jwt_claims
from .viewmodel import AppContext, ViewModel, qproperty

#: The sidebar, in order: (key, section, label, icon).
SCREENS: Final = (
    ("status", "Lab", "Status", "user"),
    ("uploads", "Lab", "Uploads", "inbox"),
    ("prompts", "Record", "Prompts", "dot"),
    ("measurement", "Record", "Measurement", "dot"),
    ("corpus", "Corpus", "Corpus", "archive"),
    ("journey", "Analysis", "Journey", "clock"),
    ("practice", "Analysis", "Practice", "tag"),
    ("referee", "Referee", "Referee", "check"),
)
SCREEN_KEYS: Final = tuple(key for key, *_ in SCREENS)
SIDEBAR_ROLES: Final = ("key", "section", "text", "iconName", "count", "status")


class AppModel(ViewModel):
    accountChanged = Signal()
    settingsChanged = Signal()
    navigationChanged = Signal()
    # Its own signal: re-reading the sidebar on every navigation would reset the list's
    # model, and SuiSidebarList then mislabels a section header (PHASE-4-LOG.md).
    sidebarChanged = Signal()

    signedIn = qproperty(bool, "signedIn", accountChanged)
    account = qproperty(str, "account", accountChanged)
    authBusy = qproperty(bool, "authBusy", accountChanged)
    configured = qproperty(bool, "configured", settingsChanged)
    server = qproperty(str, "server", settingsChanged)
    issuer = qproperty(str, "issuer", settingsChanged)
    clientId = qproperty(str, "clientId", settingsChanged)
    distanceCm = qproperty(int, "distanceCm", settingsChanged)
    settingsSource = qproperty(str, "settingsSource", settingsChanged)
    lockedSettings = qproperty("QVariantList", "lockedSettings", settingsChanged)
    version = qproperty(str, "version", settingsChanged)
    screen = qproperty(str, "screen", navigationChanged)
    screenIndex = qproperty(int, "screenIndex", navigationChanged)
    sidebar = qproperty("QVariantList", "sidebar", sidebarChanged)

    def _get_sidebar_model(self) -> RoleRows:
        return self._sidebar_rows

    # What SuiSidebarList shows: rows changed in place, never a new model (see above).
    sidebarModel = Property(QObject, _get_sidebar_model, constant=True)

    def _get_prompts_model(self) -> PromptsModel:
        return self.prompts

    # Record: prompts' model (PromptsScreen.qml reads it through the app).
    promptsModel = Property(QObject, _get_prompts_model, constant=True)

    def __init__(
        self,
        config: Config,
        tokens: TokenStore,
        *,
        http: httpx.Client,
        tasks: Tasks | None = None,
        output: AudioOutput | None = None,
        open_browser: Callable[[str], object] = webbrowser.open,
        open_url: Callable[[str], object] | None = None,
        fixed_port: bool = False,
        measurement_backend: AudioBackend | None = None,
        level_reader: LevelReader | None = None,
        recorder_factory: Callable[[], TakeRecorder] | None = None,
        start_screen: str | None = None,
    ) -> None:
        """``measurement_backend`` and ``level_reader`` stand in for PortAudio and the
        platform's level readers (tests, ``--smoke``); by default they are made the first time
        the measurement screen is shown. ``recorder_factory`` makes Record: prompts' recorder
        (default: the capture worker). ``start_screen`` opens on that screen."""
        tasks = tasks or Tasks()
        session = Session(config, tokens, http)
        context = AppContext(config, session, http, tasks, open_url=open_url or open_browser)
        if output is not None:
            context.player = Player(tasks, context.fetch_audio, output)
        super().__init__(context, {})
        self.open_browser = open_browser
        self.fixed_port = fixed_port
        self._cancel_sign_in = threading.Event()
        self.spool = SessionStore(config.measurement_dir)
        self.prompted = PromptedStore(config.data_dir)
        self.status = StatusModel(context, self.spool)
        self.uploads = UploadsModel(context, self.spool, self.prompted)
        self.prompts = PromptsModel(
            context, recorder_factory=recorder_factory or default_recorder, store=self.prompted
        )
        self.measurement = Controller(
            context,
            self.spool,
            (lambda: measurement_backend) if measurement_backend else _portaudio_backend,
            (lambda: level_reader) if level_reader else _platform_level_reader,
            on_saved=self._measurement_saved,
        )
        self.corpus = CorpusModel(context)
        self.journey = JourneyModel(context, self.open_corpus_with_query)
        self.practice = PracticeModel(context, self.open_corpus_with_query)
        self.referee = RefereeModel(context)
        self.models: dict[str, ViewModel] = {
            "status": self.status,
            "uploads": self.uploads,
            "prompts": self.prompts,
            "measurement": self.measurement,
            "corpus": self.corpus,
            "journey": self.journey,
            "practice": self.practice,
            "referee": self.referee,
        }
        for model in self.models.values():
            model.toast.connect(self.toast)
        self.uploads.spoolChanged.connect(self.status.refresh_spool)
        self.uploads.spoolChanged.connect(self._refresh_sidebar)
        self.measurement.sessionChanged.connect(self._refresh_sidebar)
        self.prompts.transportChanged.connect(self._refresh_sidebar)
        self.prompts.packaged.connect(self._prompted_packaged)
        context.signInRequired.connect(self._signed_out_elsewhere)
        start = start_screen or ("corpus" if config.configured and session.signed_in else "status")
        if start not in SCREEN_KEYS:
            raise ValueError(f"no screen {start!r}")
        self._v.update(
            signedIn=session.signed_in,
            account=self._account_label(),
            authBusy=False,
            screen=start,
            screenIndex=SCREEN_KEYS.index(start),
            sidebar=[],
            version=__version__,
        )
        self._apply_config(config)
        self._sidebar_rows = RoleRows(SIDEBAR_ROLES, self)
        self._refresh_sidebar()

    @property
    def player(self) -> Player:
        return self.context.player

    @property
    def session(self) -> Session:
        return self.context.session

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        self.status.refresh_spool()
        self._activate(self._v["screen"])

    def shutdown(self) -> None:
        self._cancel_sign_in.set()
        self.player.shutdown()
        self.referee.shutdown()
        self.prompts.shutdown()
        self.measurement.shutdown()
        self.context.tasks.close()

    # -- navigation -------------------------------------------------------------------------

    @Slot(int)
    def navigateIndex(self, index: int) -> None:  # noqa: N802 (QML name)
        if 0 <= index < len(SCREEN_KEYS):
            self.navigate(SCREEN_KEYS[index])

    @Slot(str)
    def navigate(self, key: str) -> None:
        if key not in SCREEN_KEYS:
            return
        previous = self._v["screen"]
        self._set(self.navigationChanged, screen=key, screenIndex=SCREEN_KEYS.index(key))
        if previous != key:
            deactivate = getattr(self.models[previous], "deactivate", None)
            if deactivate is not None:
                deactivate()
        self._activate(key)

    def _activate(self, key: str) -> None:
        model = self.models[key]
        activate = getattr(model, "activate", None)
        if activate is not None:
            activate()

    def open_corpus_with_query(self, query: str) -> None:
        """Journey and Practice click-throughs: the Corpus filtered to one song."""
        self.corpus.show_query(query)
        self.navigate("corpus")

    def _measurement_saved(self) -> None:
        """A measurement session was saved: Uploads shows it, and sends it when signed in."""
        self.uploads.refresh()
        if self.session.signed_in and self.context.config.configured:
            self.uploads.uploadAll()

    def _prompted_packaged(self) -> None:
        """A prompted session became a package: Uploads shows it (and sends it when
        prompted uploads are on, D-042)."""
        self.uploads.refresh()
        config = self.context.config
        if config.prompted_uploads and self.session.signed_in and config.configured:
            self.uploads.uploadAll()

    def _refresh_sidebar(self) -> None:
        pending = self.uploads.pending_count
        failed = self.uploads.failed_count
        # A session in progress keeps running on another screen: its entry shows it.
        recording = bool(self.measurement.inSession)
        taking = self.prompts.recording
        rows = []
        for key, section, text, icon in SCREENS:
            entry: dict[str, Any] = {"key": key, "section": section, "text": text, "iconName": icon}
            entry["count"] = pending if key == "uploads" and pending else -1
            if key == "uploads" and failed:
                entry["status"] = "failing"
            elif (key == "measurement" and recording) or (key == "prompts" and taking):
                entry["status"] = "held"
            else:
                entry["status"] = "none"
            rows.append(entry)
        self._set(self.sidebarChanged, sidebar=rows)
        self._sidebar_rows.update(rows)

    # -- account ----------------------------------------------------------------------------

    def _account_label(self) -> str:
        tokens = self.session.tokens
        if tokens is None:
            return ""
        try:
            claims = jwt_claims(tokens.id_token)
        except (ValueError, UnicodeDecodeError):
            return "signed in"
        return str(claims.get("email") or claims.get("preferred_username") or "signed in")

    @Slot()
    def signIn(self) -> None:  # noqa: N802 (QML name)
        if self._v["authBusy"]:
            return
        try:
            self.context.config.require_server()
            self.context.config.require_issuer()
        except ConfigError as exc:
            self.toast.emit(f"Sign-in needs the settings: {exc}", "warn")
            return
        self._cancel_sign_in.clear()
        self._set(self.accountChanged, authBusy=True)
        config, http = self.context.config, self.context.http

        def work() -> Any:
            if isinstance(self.session.store, KeyringStore):
                KeyringStore.check_backend()
            return sign_in(
                config,
                http,
                self.open_browser,
                fixed_port=self.fixed_port,
                cancelled=self._cancel_sign_in,
            )

        def done(tokens: Any, error: BaseException | None) -> None:
            self._set(self.accountChanged, authBusy=False)
            if error is not None:
                if isinstance(error, (AuthError, NoKeychainError, httpx.HTTPError, ConfigError)):
                    self.toast.emit(f"Sign-in failed: {error}", "danger")
                    return
                raise error
            try:
                self.session.adopt(tokens)
            except NoKeychainError as exc:
                self.toast.emit(str(exc), "danger")
                return
            self._set(self.accountChanged, signedIn=True, account=self._account_label())
            self.toast.emit("Signed in.", "ok")
            self.status.refresh()
            self._activate(self._v["screen"])

        self.context.tasks.submit(work, done)

    @Slot()
    def cancelSignIn(self) -> None:  # noqa: N802
        self._cancel_sign_in.set()

    @Slot()
    def signOut(self) -> None:  # noqa: N802
        self.player.stop()
        self.session.sign_out()
        self._set(self.accountChanged, signedIn=False, account="")
        for model in self.models.values():
            forget = getattr(model, "forget", None)
            if forget is not None:
                forget()

    def _signed_out_elsewhere(self, _reason: str) -> None:
        self._set(self.accountChanged, signedIn=self.session.signed_in)
        if not self.session.signed_in:
            self._set(self.accountChanged, account="")

    # -- settings ---------------------------------------------------------------------------

    def _apply_config(self, config: Config) -> None:
        self._set(
            self.settingsChanged,
            configured=config.configured,
            server=config.server_url,
            issuer=config.issuer,
            clientId=config.client_id,
            distanceCm=config.distance_cm,
            settingsSource=config.source,
            lockedSettings=list(config.env_keys),
        )

    @Slot(str, str, str, int, result=bool)
    def saveSettings(  # noqa: N802
        self, server: str, issuer: str, client_id: str, distance_cm: int = -1
    ) -> bool:
        """The Settings dialog's Save; ``distance_cm`` -1 leaves the mouth distance as it is
        (the measurement chain's; the measurement screen shows it)."""
        config = self.context.config
        wanted: dict[str, Any] = {
            "server_url": server.strip(),
            "issuer": issuer.strip(),
            "client_id": client_id.strip(),
        }
        if distance_cm != -1:
            wanted["distance_cm"] = int(distance_cm)
        changes = {k: v for k, v in wanted.items() if k not in config.env_keys}
        try:
            saved = save_settings(config, changes)
        except ConfigError as exc:
            self.toast.emit(f"Settings not saved: {exc}", "danger")
            return False
        identity_changed = (saved.issuer, saved.client_id) != (config.issuer, config.client_id)
        if identity_changed and self.session.signed_in:
            self.signOut()
        if isinstance(self.session.store, KeyringStore):
            self.session.store = KeyringStore(saved.issuer, saved.client_id)
        self.context.set_config(saved)
        self._apply_config(saved)
        for model in self.models.values():
            forget = getattr(model, "forget", None)
            if forget is not None:
                forget()
        self.toast.emit("Settings saved.", "ok")
        self.status.refresh()
        return True

    @Slot()
    def openReport(self) -> None:  # noqa: N802
        """The evidence report, in the browser: the web view renders it (A-02)."""
        try:
            server = self.context.config.require_server()
        except ConfigError as exc:
            self.toast.emit(str(exc), "warn")
            return
        self.context.open_url(f"{server}/view/report")


def keyring_store(config: Config) -> TokenStore:
    return KeyringStore(config.issuer, config.client_id)


def _portaudio_backend() -> AudioBackend:
    from .measurement.devices import PortAudioBackend

    return PortAudioBackend()


def _platform_level_reader() -> LevelReader:
    from .measurement.levels import PlatformLevelReader

    return PlatformLevelReader()


__all__ = ["SCREENS", "AppModel", "keyring_store"]
