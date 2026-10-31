"""The menu bar (D-058; the recording UX review's item 4): every recording command in a native
menu, with its key, disabled (never hidden) when it cannot run.

On macOS a parentless ``QMenuBar`` is the system's menu bar for the app: VoiceOver reads it,
Full Keyboard Access drives it, and System Settings can remap its keys (App Shortcuts). Its
menus follow the HIG's order: File, View, Recording, Window and Help (Qt makes the app menu,
with Quit, which ``QuitGuard`` holds). Elsewhere it is not shown; the studio's buttons and
keys are the same.

The keys stay the studio's own ``Shortcut``s (StudioScreen.qml), which know when to keep
quiet: while typing, with a menu or dialog open, on another screen, on a held key's repeats.
A menu item shows its key in ``Qt.WidgetShortcut`` context, so Qt's shortcut map never fires
it: a key is never taken twice. (A QAction's shortcut cannot fire over a QML window anyway:
Qt's widget context matcher wants a widget window active.) On macOS, AppKit offers a ⌘ key
to the window's views and then to the menu; whichever takes it consumes the key event, so the
command runs once.

An item is enabled when its command can run and the studio takes commands
(``StudioModel.commandsEnabled``: the screen shown, no menu or dialog of its own open; its QML
says so), so the menu never acts behind a dialog or on another screen. The same table
(``shortcut_rows``) fills the shortcut overlay (⌘/).

Nothing here closes over the menu bar itself (the GC hang, PHASE-4-LOG.md): its commands read
the models, and the models reach it only through Qt connections to its declared slot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtGui import QAction, QKeySequence, QWindow
from PySide6.QtWidgets import QMenu, QMenuBar

if TYPE_CHECKING:
    from .app import AppModel
    from .screens.studio import StudioModel

#: The keys, as Qt names them (``Ctrl`` is ⌘ on macOS).
RECORD_KEY: Final = "R"
PLAY_KEY: Final = "Space"
SAVE_NEXT_KEY: Final = "Return"
PREVIOUS_PROMPT_KEY: Final = "Ctrl+Up"
NEXT_PROMPT_KEY: Final = "Ctrl+Down"
CONTINUE_MISSING_KEY: Final = "Ctrl+J"
NEXT_FLAGGED_KEY: Final = "Ctrl+Shift+J"
SHORTCUTS_KEY: Final = "Ctrl+/"
BIGGER_KEY: Final = "Ctrl+="
SMALLER_KEY: Final = "Ctrl+-"
STANDARD_SIZE_KEY: Final = "Ctrl+0"
MINIMIZE_KEY: Final = "Ctrl+M"


def key_text(key: QKeySequence | QKeySequence.StandardKey | str) -> str:
    """A key as this platform writes it (⌘S on macOS, Ctrl+S elsewhere)."""
    sequence = key if isinstance(key, QKeySequence) else QKeySequence(key)
    return sequence.toString(QKeySequence.SequenceFormat.NativeText)


def key_hints() -> dict[str, str]:
    """The keys the studio's buttons show, as this platform writes them."""
    return {
        "record": key_text(RECORD_KEY),
        "play": key_text(PLAY_KEY),
        "save": key_text(QKeySequence.StandardKey.Save),
        "saveNext": key_text(SAVE_NEXT_KEY),
        "continueMissing": key_text(CONTINUE_MISSING_KEY),
    }


@dataclass(frozen=True)
class Command:
    """One menu item: its ``name`` (its action's ``menu-<name>``), its title (read again
    whenever the studio changes), what it runs, when it can, and the key it shows."""

    name: str
    title: Callable[[], str]
    run: Callable[[], Any]
    can: Callable[[], bool]
    key: QKeySequence | None = None


def _fixed(text: str) -> Callable[[], str]:
    return lambda: text


def recording_commands(studio: StudioModel) -> list[Command | None]:
    """The Recording menu, in order (``None``: a separator)."""
    takes = studio.take_review

    def on(can: Callable[[], bool]) -> Callable[[], bool]:
        return lambda: bool(studio.commandsEnabled) and can()

    def record_title() -> str:
        phase = studio.phase
        if phase in ("starting", "recording"):
            return "Stop"
        return "Retake" if phase in ("review", "failed") else "Record"

    return [
        Command(
            "record",
            record_title,
            studio.toggleRecord,
            on(lambda: bool(studio.canRecord or studio.canStop)),
            QKeySequence(RECORD_KEY),
        ),
        Command(
            "play",
            lambda: "Pause" if takes.playing else "Play",
            takes.togglePlay,
            on(lambda: bool(takes.canPlay)),
            QKeySequence(PLAY_KEY),
        ),
        None,
        Command(
            "save",
            _fixed("Save"),
            studio.save,
            on(lambda: bool(studio.canSave)),
            QKeySequence(QKeySequence.StandardKey.Save),
        ),
        Command(
            "save-next",
            _fixed("Save & Next"),
            studio.saveNext,
            on(lambda: bool(studio.canSaveNext)),
            QKeySequence(SAVE_NEXT_KEY),
        ),
        Command(
            "discard", _fixed("Discard Take"), studio.discard, on(lambda: bool(studio.canDiscard))
        ),
        None,
        Command("remove", _fixed("Remove Take"), takes.remove, on(lambda: bool(takes.canRemove))),
        Command(
            "reject", _fixed("Reject Take…"), takes.askReject, on(lambda: bool(takes.canReject))
        ),
        Command(
            "restore", _fixed("Restore Take"), takes.restore, on(lambda: bool(takes.canRestore))
        ),
        None,
        Command(
            "check", _fixed("Check Input…"), studio.openCheck, on(lambda: bool(studio.canCheck))
        ),
        None,
        Command(
            "continue-missing",
            _fixed("Continue Missing"),
            studio.continueMissing,
            on(lambda: bool(studio.canContinueMissing)),
            QKeySequence(CONTINUE_MISSING_KEY),
        ),
        Command(
            "next-flagged",
            _fixed("Next Flagged"),
            studio.nextFlagged,
            on(lambda: bool(studio.canNextFlagged)),
            QKeySequence(NEXT_FLAGGED_KEY),
        ),
        Command(
            "previous-prompt",
            _fixed("Previous Prompt"),
            studio.previousPrompt,
            on(lambda: studio.neighbour(-1) is not None),
            QKeySequence(PREVIOUS_PROMPT_KEY),
        ),
        Command(
            "next-prompt",
            _fixed("Next Prompt"),
            studio.nextPrompt,
            on(lambda: studio.neighbour(1) is not None),
            QKeySequence(NEXT_PROMPT_KEY),
        ),
    ]


def view_commands(studio: StudioModel) -> list[Command | None]:
    """The View menu: the script's reading size."""

    def on(can: Callable[[], bool]) -> Callable[[], bool]:
        return lambda: bool(studio.commandsEnabled) and can()

    return [
        Command(
            "bigger",
            _fixed("Bigger Text"),
            lambda: studio.stepReadingSize(1),
            on(lambda: studio.readingSize < max(studio.readingSizes)),
            QKeySequence(BIGGER_KEY),
        ),
        Command(
            "smaller",
            _fixed("Smaller Text"),
            lambda: studio.stepReadingSize(-1),
            on(lambda: studio.readingSize > min(studio.readingSizes)),
            QKeySequence(SMALLER_KEY),
        ),
        Command(
            "standard-size",
            _fixed("Standard Text Size"),
            lambda: studio.setReadingSize(min(studio.readingSizes)),
            on(lambda: studio.readingSize != min(studio.readingSizes)),
            QKeySequence(STANDARD_SIZE_KEY),
        ),
    ]


def shortcut_rows() -> list[dict[str, Any]]:
    """The shortcut overlay's entries (⌘/): the studio's keys, by group."""
    return [
        {"group": "Recording", "keys": [key_text(RECORD_KEY)], "description": "Record; Stop"},
        {
            "group": "Recording",
            "keys": [key_text(PLAY_KEY)],
            "description": "Stop a recording; otherwise play or pause the take",
        },
        {
            "group": "Recording",
            "keys": [key_text(SAVE_NEXT_KEY)],
            "description": "Save & Next (pressed while the take is checked, done when ready)",
        },
        {
            "group": "Recording",
            "keys": [key_text(QKeySequence.StandardKey.Save)],
            "description": "Save",
        },
        {
            "group": "Prompts",
            "keys": [key_text(CONTINUE_MISSING_KEY)],
            "description": "Continue Missing",
        },
        {
            "group": "Prompts",
            "keys": [key_text(NEXT_FLAGGED_KEY)],
            "description": "Next Flagged: flagged here, or failed the PC's QC",
        },
        {
            "group": "Prompts",
            "keys": [key_text(PREVIOUS_PROMPT_KEY), key_text(NEXT_PROMPT_KEY)],
            "separator": " / ",
            "description": "Previous or next prompt in the list, without saving",
        },
        {
            "group": "Waveform (when it has the focus)",
            "keys": ["←", "→"],
            "separator": " / ",
            "description": "The playhead a tenth of a second back or on",
        },
        {
            "group": "Waveform (when it has the focus)",
            "keys": ["Home", "End"],
            "separator": " / ",
            "description": "The playhead to the kept frames' start or end",
        },
        {
            "group": "View",
            "keys": [key_text(BIGGER_KEY), key_text(SMALLER_KEY), key_text(STANDARD_SIZE_KEY)],
            "separator": " ",
            "description": "Bigger, smaller or standard reading size",
        },
        {"group": "View", "keys": [key_text(SHORTCUTS_KEY)], "description": "These shortcuts"},
    ]


class MenuBar(QObject):
    """The app's menu bar over its model; ``window`` is the main window (File's Close Window,
    Window's Minimize and Zoom). ``close`` takes it down with the window."""

    def __init__(self, model: AppModel, window: QWindow | None) -> None:
        super().__init__()
        self.bar = QMenuBar()
        self._items: list[tuple[QAction, Command]] = []
        studio = model.studio
        file_menu = self.bar.addMenu("File")
        if window is not None:
            self._add(
                file_menu,
                Command(
                    "close-window",
                    _fixed("Close Window"),
                    window.close,
                    lambda: True,
                    QKeySequence(QKeySequence.StandardKey.Close),
                ),
            )
        view_menu = self.bar.addMenu("View")
        if studio is not None:
            self._fill(view_menu, view_commands(studio))
            recording = self.bar.addMenu("Recording")
            self._fill(recording, recording_commands(studio))
        window_menu = self.bar.addMenu("Window")
        if window is not None:
            self._fill(
                window_menu,
                [
                    Command(
                        "minimize",
                        _fixed("Minimize"),
                        window.showMinimized,
                        lambda: True,
                        QKeySequence(MINIMIZE_KEY),
                    ),
                    Command("zoom", _fixed("Zoom"), lambda: _zoom(window), lambda: True),
                ],
            )
        help_menu = self.bar.addMenu("Help")
        if studio is not None:
            self._add(
                help_menu,
                Command(
                    "shortcuts",
                    _fixed("Keyboard Shortcuts"),
                    studio.showShortcuts,
                    lambda: bool(studio.commandsEnabled),
                    QKeySequence(SHORTCUTS_KEY),
                ),
            )
            self._signals = [
                studio.transportChanged,
                studio.promptChanged,
                studio.libraryChanged,
                studio.rowsChanged,
                studio.viewChanged,
                studio.commandsChanged,
                studio.take_review.takesChanged,
                studio.take_review.playChanged,
                studio.input_check.checkChanged,
            ]
        else:
            self._signals = []
        for signal in self._signals:
            signal.connect(self._update)
        self._update()

    def _fill(self, menu: QMenu, commands: list[Command | None]) -> None:
        for command in commands:
            if command is None:
                menu.addSeparator()
            else:
                self._add(menu, command)

    def _add(self, menu: QMenu, command: Command) -> None:
        action = QAction(command.title(), menu)
        action.setObjectName(f"menu-{command.name}")
        # A key is shown, never fired by Qt: it is the studio's Shortcut's (see above).
        action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        if command.key is not None:
            action.setShortcut(command.key)
            action.setAutoRepeat(False)
        action.triggered.connect(command.run)
        menu.addAction(action)
        self._items.append((action, command))

    @property
    def actions(self) -> dict[str, QAction]:
        """Every item's action, by its command's name."""
        return {command.name: action for action, command in self._items}

    @Slot()
    def _update(self) -> None:
        for action, command in self._items:
            action.setText(command.title())
            action.setEnabled(command.can())

    def close(self) -> None:
        for signal in self._signals:
            signal.disconnect(self._update)
        self._signals = []
        self._items = []
        self.bar.deleteLater()


def _zoom(window: QWindow) -> None:
    if window.visibility() == QWindow.Visibility.Maximized:
        window.showNormal()
    else:
        window.showMaximized()
