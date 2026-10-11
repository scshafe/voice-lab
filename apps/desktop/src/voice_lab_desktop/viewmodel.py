"""What every screen's model shares: the app context and the property plumbing.

A screen's model is a ``QObject`` whose properties QML binds to. Values live in one dict
(``_v``) and change through :meth:`ViewModel._set`, which emits the given notify signal
only when something changed (voice-journey-capture's controller pattern). Network work goes
through the context's :class:`~voice_lab_desktop.tasks.Tasks`, so properties only change on
the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from PySide6.QtCore import Property, QObject, Signal, SignalInstance

from .api import ApiClient, ApiError
from .auth import AuthError, Session, SignInRequired
from .config import Config, ConfigError
from .playback import Player
from .tasks import Tasks


def qproperty(kind: Any, name: str, notify: Signal) -> Any:
    """A read-only Qt property backed by ``self._v[name]``."""

    def getter(self: ViewModel) -> Any:
        return self._v[name]

    return Property(kind, getter, notify=notify)


class AppContext(QObject):
    """The app's shared services. ``signInRequired`` fires when a request found the
    sign-in gone (the app model then shows the person signed out)."""

    signInRequired = Signal(str)
    configChanged = Signal()

    def __init__(
        self,
        config: Config,
        session: Session,
        http: httpx.Client,
        tasks: Tasks,
        *,
        open_url: Callable[[str], object],
        player: Player | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.session = session
        self.http = http
        self.tasks = tasks
        self.open_url = open_url
        self.player = player if player is not None else Player(tasks, self.fetch_audio)

    def api(self) -> ApiClient:
        """The API client for the configured server; ``ConfigError`` when none is set."""
        return ApiClient(self.config.require_server(), self.session, self.http)

    def fetch_audio(self, source: str) -> bytes:
        """A recording's bytes: ``source`` is a recording id or a server-named audio path."""
        api = self.api()
        return api.audio_url(source) if source.startswith("/") else api.audio(source)

    def set_config(self, config: Config) -> None:
        self.config = config
        self.session.config = config
        self.configChanged.emit()


class ViewModel(QObject):
    """A screen's model: properties in ``_v``, a toast signal, the shared context."""

    toast = Signal(str, str)  # text, tone ("info" | "ok" | "warn" | "danger")

    def __init__(self, context: AppContext, values: dict[str, Any]) -> None:
        super().__init__()
        self.context = context
        self._v: dict[str, Any] = dict(values)

    def _set(self, signal: SignalInstance, **values: Any) -> None:
        changed = False
        for key, value in values.items():
            if self._v.get(key) != value:
                self._v[key] = value
                changed = True
        if changed:
            signal.emit()

    def failure(self, error: BaseException) -> str:
        """The message to show for a failed request; re-raises what is a bug.

        A lost sign-in is also announced to the app model (``signInRequired``)."""
        if isinstance(error, SignInRequired):
            self.context.signInRequired.emit(str(error))
            return f"Sign in on the Status screen ({error})."
        if isinstance(error, (ApiError, AuthError, ConfigError)):
            return str(error)
        raise error
