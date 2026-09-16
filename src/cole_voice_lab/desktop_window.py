"""Native recording library and capture window; all asset operations run off the GUI thread."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QObject,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QKeyEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .capture import (
    CaptureInterrupted,
    CaptureState,
)
from .capture_native import list_coreaudio_input_devices
from .desktop_audio import NativeAudioPlayer, PcmAudioClip, load_pcm_audio
from .desktop_waveform import WaveformView
from .errors import ContractError
from .models import CaptureDevice
from .recording_edits import RecordingEdit
from .recording_library import (
    DesktopRecordingSession,
    RecordingHistory,
    RecordingLibrary,
    RecordingRow,
    RecordingSessionInfo,
)


@dataclass(frozen=True)
class _LibrarySnapshot:
    rows: tuple[RecordingRow, ...]
    sessions: tuple[RecordingSessionInfo, ...]


@dataclass(frozen=True)
class _EditorAudio:
    clip: PcmAudioClip
    history: RecordingHistory
    edit: RecordingEdit
    path: Path


class _SessionTransitionError(RuntimeError):
    """The previous session was saved, but preparing its replacement failed."""


class _LibraryWorker(QObject):
    """Serialize filesystem, validation and capture work away from Qt's event loop."""

    succeeded = Signal(object)
    failed = Signal(object)

    @Slot(object)
    def execute(self, operation: Callable[[], object]) -> None:
        try:
            result = operation()
        except Exception as error:
            # This is the GUI boundary: exceptions remain visible and stop polling.
            self.failed.emit(error)
        else:
            self.succeeded.emit(result)


class _SortItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left < right
        return self.text().casefold() < other.text().casefold()


class _PromptContent(QStackedWidget):
    """Let scrollable scripts shrink before squeezing the non-scrollable editor.

    Qt promotes a nested card's preferred height-for-width to its minimum during
    layout. The stock text editor's tall preferred size otherwise overlaps the
    waveform/footer on laptops even though the script can scroll at a smaller size.
    """

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width(), self.minimumSizeHint().height())


def _date_text(value: datetime | None) -> str:
    return "Never" if value is None else value.astimezone().strftime("%b %d, %Y  %H:%M:%S %Z")


def _age_text(value: datetime | None) -> str:
    if value is None:
        return "—"
    days = max(0, (datetime.now(UTC) - value).days)
    return "Today" if days == 0 else f"{days} day{'s' if days != 1 else ''}"


class DesktopWindow(QMainWindow):
    """@canonical native recording UI, backed by the shared capture engine and library.

    Accepts a private RecordingLibrary and optional device discovery/playback adapters.
    No microphone is opened until the operator explicitly starts a queued take.
    Backend errors are displayed in the window without replacing evidence or retrying.
    """

    operation_requested = Signal(object)

    def __init__(
        self,
        library: RecordingLibrary,
        *,
        requested_device: str | None = None,
        device_loader: Callable[[], tuple[CaptureDevice, ...]] | None = None,
        playback: Callable[[Path], None] | None = None,
        player_factory: Callable[[], NativeAudioPlayer] = NativeAudioPlayer,
    ) -> None:
        super().__init__()
        self.library = library
        self.requested_device = requested_device
        self._device_loader = device_loader or list_coreaudio_input_devices
        self._playback = playback
        self._rows: tuple[RecordingRow, ...] = ()
        self._session: DesktopRecordingSession | None = None
        self._state: CaptureState | None = None
        self._pending_save_state: CaptureState | None = None
        self._busy = False
        self._failed = False
        self._audio_fault = False
        self._callback: Callable[[object], None] | None = None
        self._close_after_end = False
        self._initial_scan = True
        self._player = player_factory()
        self._audio: _EditorAudio | None = None
        self._editor_key: tuple[str, str, str, str] | None = None
        self._player_loaded = False
        self._playing = False
        self._paused = False
        self._trim_dirty = False
        self._refreshing = False
        self._focus_current_take = False
        self._position_frame = 0
        self._take_positions: dict[tuple[str, str, str], int] = {}
        self._capture_transition: Literal["starting", "stopping"] | None = None
        self._captured_seconds = 0.0
        self._displayed_history: tuple[RecordingHistory, ...] | None = None
        self._inspected_prompt_id: str | None = None
        self._idle_status = "Select prompts in the library to prepare a recording batch."
        interface_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        interface_font.setPointSize(13)
        self.setFont(interface_font)
        self.setWindowTitle("Cole Voice Lab — Recording Library")
        # Leave room for the menu bar, window frame, and Dock on laptop displays.
        self.setMinimumSize(1060, 700)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        self.resize(
            max(1060, min(1440, available.width() - 48)) if available else 1440,
            max(700, min(980, available.height() - 48)) if available else 980,
        )
        self._build_ui()
        self._thread = QThread(self)
        self._worker = _LibraryWorker()
        self._worker.moveToThread(self._thread)
        self.operation_requested.connect(self._worker.execute)
        self._worker.succeeded.connect(self._succeeded)
        self._worker.failed.connect(self._operation_failed)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self._poll)
        self._transport_timer = QTimer(self)
        self._transport_timer.setInterval(50)
        self._transport_timer.timeout.connect(self._render_transport_state)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self._apply_palette()
        self._update_controls()
        QTimer.singleShot(0, self.refresh_library)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(12)
        self.library_pane = QWidget()
        self.library_pane.setMinimumWidth(370)
        library_layout = QVBoxLayout(self.library_pane)
        library_layout.setContentsMargins(0, 0, 0, 0)
        library_layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("Recording library")
        title_font = QFont(self.font())
        title_font.setPointSize(20)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        heading.addWidget(title, 1)
        self.refresh_button = QPushButton("Refresh")
        heading.addWidget(self.refresh_button)
        library_layout.addLayout(heading)
        self.summary_label = QLabel("Loading the full prompt catalog…")
        self.summary_label.setWordWrap(True)
        library_layout.addWidget(self.summary_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        library_layout.addWidget(self.progress_bar)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search prompt text, style, or ID")
        self.search_edit.setClearButtonEnabled(True)
        library_layout.addWidget(self.search_edit)
        filters = QHBoxLayout()
        self.category_filter = QComboBox()
        self.category_filter.addItem("All categories", "")
        self.category_filter.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.category_filter.setMinimumContentsLength(10)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All statuses", "Missing", "Recorded", "Needs review"])
        self.clear_filters_button = QPushButton("Clear")
        filters.addWidget(self.category_filter, 1)
        filters.addWidget(self.status_filter, 1)
        filters.addWidget(self.clear_filters_button)
        library_layout.addLayout(filters)
        library_options = QHBoxLayout()
        self.show_history_columns = QCheckBox("Dates, age & takes")
        self.show_history_columns.toggled.connect(self._toggle_history_columns)
        library_options.addWidget(self.show_history_columns)
        library_options.addStretch()
        self.coverage_button = QPushButton("Coverage")
        self.coverage_button.setCheckable(True)
        library_options.addWidget(self.coverage_button)
        library_layout.addLayout(library_options)
        self.coverage_label = QLabel()
        self.coverage_label.setWordWrap(True)
        self.coverage_label.hide()
        self.coverage_button.toggled.connect(self.coverage_label.setVisible)
        library_layout.addWidget(self.coverage_label)
        self.search_edit.textChanged.connect(self._filter_rows)
        self.category_filter.currentIndexChanged.connect(self._filter_rows)
        self.status_filter.currentIndexChanged.connect(self._filter_rows)
        self.clear_filters_button.clicked.connect(self._clear_filters)
        self.refresh_button.clicked.connect(self.refresh_library)
        self.recordings_table = QTableWidget(0, 7)
        self.recordings_table.setHorizontalHeaderLabels(
            ["#", "Category", "Recording task", "Status", "Latest recording", "Age", "Takes"]
        )
        self._configure_table(self.recordings_table)
        self.recordings_table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self.recordings_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.recordings_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        for column, width in ((0, 35), (1, 115), (3, 102), (4, 230), (5, 85), (6, 60)):
            self.recordings_table.setColumnWidth(column, width)
        header = self.recordings_table.horizontalHeader()
        header.moveSection(header.visualIndex(3), 0)
        header.moveSection(header.visualIndex(1), 1)
        header.moveSection(header.visualIndex(2), 2)
        self.recordings_table.setColumnHidden(0, True)
        self._toggle_history_columns(False)
        self.recordings_table.itemSelectionChanged.connect(self._library_selection_changed)
        library_layout.addWidget(self.recordings_table, 1)
        device_bar = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setToolTip("Microphone — capture starts only when you press Record")
        self.device_combo.setMinimumContentsLength(12)
        self.device_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.device_combo.currentIndexChanged.connect(self._update_controls)
        device_bar.addWidget(self.device_combo, 1)
        self.scan_button = QPushButton("Re-scan")
        self.scan_button.clicked.connect(self.scan_devices)
        device_bar.addWidget(self.scan_button)
        library_layout.addLayout(device_bar)
        batch_bar = QHBoxLayout()
        self.record_selected_button = QPushButton("Record selected")
        self.record_selected_button.clicked.connect(self._record_selected)
        self.record_missing_button = QPushButton("Record missing")
        self.record_missing_button.clicked.connect(self._record_missing)
        batch_bar.addWidget(self.record_selected_button)
        batch_bar.addWidget(self.record_missing_button)
        library_layout.addLayout(batch_bar)
        resume_bar = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.setMinimumContentsLength(12)
        self.session_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        resume_bar.addWidget(self.session_combo, 1)
        self.resume_button = QPushButton("Resume")
        self.resume_button.clicked.connect(self._resume_session)
        resume_bar.addWidget(self.resume_button)
        library_layout.addLayout(resume_bar)
        self.main_splitter.addWidget(self.library_pane)

        self.inspector_pane = QWidget()
        self.inspector_pane.setMinimumWidth(540)
        details = QVBoxLayout(self.inspector_pane)
        self.inspector_layout = details
        details.setContentsMargins(4, 0, 0, 0)
        details.setSpacing(6)
        self.prompt_card = QFrame()
        self.prompt_card.setObjectName("promptCard")
        self.prompt_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        prompt_layout = QVBoxLayout(self.prompt_card)
        prompt_layout.setContentsMargins(12, 6, 12, 6)
        prompt_layout.setSpacing(4)
        inspector_heading = QHBoxLayout()
        self.inspector_title = QLabel("Recording task")
        self.inspector_title.setFont(title_font)
        inspector_heading.addWidget(self.inspector_title, 1)
        self.state_label = QLabel("Ready")
        state_font = QFont(self.font())
        state_font.setWeight(QFont.Weight.DemiBold)
        self.state_label.setFont(state_font)
        inspector_heading.addWidget(self.state_label)
        self.elapsed_label = QLabel("00:00")
        timer_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        timer_font.setPointSize(13)
        self.elapsed_label.setFont(timer_font)
        inspector_heading.addWidget(self.elapsed_label)
        prompt_layout.addLayout(inspector_heading)
        self.prompt_title = QLabel("Select a prompt")
        self.prompt_title.setWordWrap(True)
        self.prompt_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        prompt_layout.addWidget(self.prompt_title)
        self.capture_status = QLabel(self._idle_status)
        self.capture_status.setWordWrap(True)
        library_layout.addWidget(self.capture_status)
        self.prompt_text = QPlainTextEdit()
        self.prompt_text.setReadOnly(True)
        self.prompt_text.setAccessibleName("Spoken script — read aloud")
        script_font = QFont(self.font())
        script_font.setPointSize(24)
        self.prompt_text.setFont(script_font)
        self.prompt_text.document().setDocumentMargin(16)
        self.prompt_text.setPlaceholderText("Select a prompt to see the full script.")
        self.prompt_text.setMinimumHeight(96)
        self.prompt_text.setFrameShape(QFrame.Shape.NoFrame)
        self.prompt_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.capture_prompt = self.prompt_text
        self.prompt_content = _PromptContent()
        self.prompt_content.addWidget(self.prompt_text)
        self.room_tone_panel = QScrollArea()
        self.room_tone_panel.setWidgetResizable(True)
        self.room_tone_panel.setFrameShape(QFrame.Shape.NoFrame)
        self.room_tone_panel.setMinimumHeight(96)
        self.room_tone_panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.room_tone_panel.setAccessibleName("Silent room tone instructions — do not speak")
        self.room_tone_body = QFrame()
        self.room_tone_body.setObjectName("roomToneInstructions")
        room_layout = QVBoxLayout(self.room_tone_body)
        room_layout.setContentsMargins(16, 16, 16, 16)
        room_layout.setSpacing(8)
        room_heading = QHBoxLayout()
        self.room_tone_icon = QLabel()
        self.room_tone_icon.setFixedSize(32, 32)
        room_heading.addWidget(self.room_tone_icon)
        self.room_tone_heading = QLabel("Do not speak")
        silence_font = QFont(script_font)
        silence_font.setWeight(QFont.Weight.DemiBold)
        self.room_tone_heading.setFont(silence_font)
        self.room_tone_heading.setWordWrap(True)
        room_heading.addWidget(self.room_tone_heading, 1)
        room_layout.addLayout(room_heading)
        self.room_tone_duration = QLabel()
        self.room_tone_duration.setWordWrap(True)
        self.room_tone_duration.setFont(state_font)
        room_layout.addWidget(self.room_tone_duration)
        self.room_tone_instructions = QLabel()
        self.room_tone_instructions.setFont(self.font())
        self.room_tone_instructions.setTextFormat(Qt.TextFormat.PlainText)
        self.room_tone_instructions.setWordWrap(True)
        self.room_tone_instructions.setAccessibleName("Instructions only — not a spoken script")
        room_layout.addWidget(self.room_tone_instructions)
        self.room_tone_help = QLabel(
            "Keep your normal recording position and avoid touching the microphone or desk.\n\n"
            "This captures background noise for quality checks. "
            "Nothing on this card should be read aloud."
        )
        self.room_tone_help.setWordWrap(True)
        self.room_tone_help.setFont(self.font())
        room_layout.addWidget(self.room_tone_help)
        room_layout.addStretch(1)
        self.room_tone_panel.setWidget(self.room_tone_body)
        self.prompt_content.addWidget(self.room_tone_panel)
        prompt_layout.addWidget(self.prompt_content, 1)
        self.style_label = QLabel()
        self.style_label.setWordWrap(True)
        self.style_label.setTextFormat(Qt.TextFormat.PlainText)
        self.style_label.setAccessibleName("Recording guidance")
        instruction_font = QFont(self.font())
        instruction_font.setItalic(True)
        self.style_label.setFont(instruction_font)
        self.style_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.capture_instruction = self.style_label
        prompt_layout.addWidget(self.style_label)
        details.addWidget(self.prompt_card, 3)

        self.editor_card = QFrame()
        self.editor_card.setObjectName("editorCard")
        editor = QVBoxLayout(self.editor_card)
        editor.setContentsMargins(12, 6, 12, 6)
        editor.setSpacing(4)
        editor_heading = QHBoxLayout()
        self.history_caption = QPushButton("Takes")
        self.history_caption.setCheckable(True)
        self.history_caption.setChecked(True)
        self.history_caption.setFlat(True)
        self.history_caption.setStyleSheet("border: none; padding: 0;")
        self.history_caption.toggled.connect(self._toggle_history)
        self.history_caption.setFont(state_font)
        self.history_caption.setToolTip("Show or hide the dated list of takes for this prompt")
        editor_heading.addWidget(self.history_caption)
        self.editor_caption = QLabel("Original waveform")
        self.editor_caption.setWordWrap(True)
        editor_heading.addWidget(self.editor_caption, 1)
        self.fit_waveform = QCheckBox("Fit height")
        self.fit_waveform.setAccessibleName("Fit waveform height — display only")
        self.fit_waveform.setToolTip(
            "Enlarge quiet waveforms visually. Does not change audio level, playback, or files."
        )
        editor_heading.addWidget(self.fit_waveform)
        editor.addLayout(editor_heading)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(
            ["Take · recorded (local time)", "Date precision", "Status", "Session"]
        )
        self._configure_table(self.history_table)
        self.history_table.sortItems(0, Qt.SortOrder.DescendingOrder)
        self.history_table.setFixedHeight(66)
        self.history_table.horizontalHeader().hide()
        self.history_table.setColumnHidden(1, True)
        self.history_table.setColumnHidden(3, True)
        self.history_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.history_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.history_table.setAccessibleName("Takes for this prompt — newest first")
        self.history_table.itemSelectionChanged.connect(self._history_selection_changed)
        editor.addWidget(self.history_table)
        self.waveform = WaveformView()
        self.waveform.setMinimumHeight(116)
        self.waveform.setMaximumHeight(220)
        self.waveform.seek_requested.connect(self._seek_frame)
        self.waveform.trim_changed.connect(self._trim_changed)
        self.waveform.play_requested.connect(self._play_from_frame)
        self.fit_waveform.toggled.connect(self.waveform.set_fit_amplitude)
        self.fit_waveform.setChecked(True)
        editor.addWidget(self.waveform, 1)
        self.timeline_panel = QWidget()
        timeline = QHBoxLayout(self.timeline_panel)
        timeline.setContentsMargins(0, 0, 0, 0)
        self.playback_slider = QSlider(Qt.Orientation.Horizontal)
        self.playback_slider.setAccessibleName("Playback position")
        self.playback_slider.valueChanged.connect(self._seek_frame)
        self.playback_time = QLabel("0:00.00 / 0:00.00")
        timeline.addWidget(self.playback_slider, 1)
        timeline.addWidget(self.playback_time)
        editor.addWidget(self.timeline_panel)

        self.trim_panel = QWidget()
        trim = QHBoxLayout(self.trim_panel)
        trim.setContentsMargins(0, 0, 0, 0)
        self.trim_start = QDoubleSpinBox()
        self.trim_end = QDoubleSpinBox()
        for control, label in ((self.trim_start, "Trim start"), (self.trim_end, "Trim end")):
            control.setDecimals(3)
            control.setSuffix(" s")
            control.setSingleStep(0.01)
            control.setAccessibleName(label)
            control.valueChanged.connect(self._trim_fields_changed)
        self.save_trim_button = QPushButton("Save trim")
        self.reset_trim_button = QPushButton("Reset")
        self.save_trim_button.clicked.connect(self._save_trim)
        self.reset_trim_button.clicked.connect(self._reset_trim)
        trim.addWidget(QLabel("In"))
        trim.addWidget(self.trim_start)
        trim.addStretch(1)
        trim.addWidget(self.save_trim_button)
        trim.addWidget(self.reset_trim_button)
        trim.addStretch(1)
        trim.addWidget(QLabel("Out"))
        trim.addWidget(self.trim_end)
        editor.addWidget(self.trim_panel)

        self.transport_panel = QWidget()
        transport = QHBoxLayout(self.transport_panel)
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(8)
        self.record_panel = QWidget()
        record_playback = QHBoxLayout(self.record_panel)
        record_playback.setContentsMargins(0, 0, 0, 0)
        self.start_button = QPushButton("●  Record    R")
        self.stop_button = QPushButton("■  Stop recording    R")
        self.play_button = QPushButton()
        self.stop_playback_button = QPushButton()
        self.rewind_button = QPushButton()
        self.rewind_button.clicked.connect(lambda: self._seek_frame(self.waveform.start_frame))
        self.skip_back_button = QPushButton()
        self.skip_forward_button = QPushButton()
        self.skip_back_button.clicked.connect(lambda: self._skip_playback(-1))
        self.skip_forward_button.clicked.connect(lambda: self._skip_playback(1))
        self.retry_button = QPushButton("Retake")
        self.keep_button = QPushButton("Save & next    ↵")
        self.finalize_button = QPushButton("Finalize session")
        self.end_button = QPushButton("End session")
        self.start_button.setToolTip("R starts a take; the microphone never starts automatically")
        self.stop_button.setToolTip("R stops the current recording")
        self.play_button.setToolTip("Space plays the current review or selected historical take")
        self.stop_playback_button.setToolTip("Stop playback and return to the trim start")
        self.keep_button.setToolTip(
            "Save the current take and trim, then prepare the next unrecorded prompt"
        )
        self.start_button.clicked.connect(self._start_take)
        self.stop_button.clicked.connect(self._stop_take)
        self.play_button.clicked.connect(self._toggle_playback)
        self.stop_playback_button.clicked.connect(self._stop_playback)
        self.retry_button.clicked.connect(self._retry_take)
        self.keep_button.clicked.connect(self._keep_take)
        self.finalize_button.clicked.connect(self._finalize_session)
        self.end_button.clicked.connect(self._end_session)
        for button in (self.start_button, self.stop_button):
            button.setMinimumHeight(44)
            record_playback.addWidget(button, 1)
        editor.addWidget(self.record_panel)
        transport.addStretch(1)
        for button in (
            self.rewind_button,
            self.skip_back_button,
            self.play_button,
            self.skip_forward_button,
            self.stop_playback_button,
        ):
            button.setMinimumHeight(44)
            transport.addWidget(button)
        transport.addStretch(1)
        editor.addWidget(self.transport_panel)
        self.qc_label = QLabel()
        self.qc_label.setWordWrap(True)
        self.qc_label.hide()
        editor.addWidget(self.qc_label)
        self.review_hint = QLabel()
        self.review_hint.setWordWrap(True)
        self.review_hint.hide()
        editor.addWidget(self.review_hint)
        details.addWidget(self.editor_card, 2)
        self.action_bar = QFrame()
        self.action_bar.setObjectName("workflowActions")
        decisions = QHBoxLayout(self.action_bar)
        decisions.setContentsMargins(0, 0, 0, 0)
        decisions.setSpacing(8)
        self.retake_history_button = QPushButton("Retake")
        self.retake_history_button.clicked.connect(self._retake_history)
        self.delete_history_button = QPushButton("Delete")
        self.delete_history_button.clicked.connect(self._delete_history)
        self.restore_history_button = QPushButton("Restore")
        self.restore_history_button.clicked.connect(self._restore_history)
        for button in (
            self.end_button,
            self.delete_history_button,
            self.restore_history_button,
            self.retry_button,
            self.retake_history_button,
        ):
            button.setMinimumHeight(38)
            decisions.addWidget(button)
        decisions.addStretch(1)
        self.keep_button.setMinimumHeight(44)
        self.finalize_button.setMinimumHeight(44)
        decisions.addWidget(self.keep_button)
        decisions.addWidget(self.finalize_button)
        details.addWidget(self.action_bar)
        self.main_splitter.addWidget(self.inspector_pane)
        self.main_splitter.setSizes([500, 780])
        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 3)
        layout.addWidget(self.main_splitter, 1)
        self.message_label = QLabel()
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.message_label)
        self._apply_control_icons()

    def _toggle_history_columns(self, visible: bool) -> None:
        for column in (4, 5, 6):
            self.recordings_table.setColumnHidden(column, not visible)

    def _apply_control_icons(self) -> None:
        for button, icon, description in (
            (self.refresh_button, QStyle.StandardPixmap.SP_BrowserReload, "Refresh recordings"),
            (self.scan_button, QStyle.StandardPixmap.SP_BrowserReload, "Refresh microphones"),
            (self.play_button, QStyle.StandardPixmap.SP_MediaPlay, "Play or pause · Space"),
            (self.stop_playback_button, QStyle.StandardPixmap.SP_MediaStop, "Stop playback"),
            (self.rewind_button, QStyle.StandardPixmap.SP_MediaSkipBackward, "Back to trim start"),
            (self.skip_back_button, QStyle.StandardPixmap.SP_MediaSeekBackward, "Back 1 second"),
            (
                self.skip_forward_button,
                QStyle.StandardPixmap.SP_MediaSeekForward,
                "Forward 1 second",
            ),
            (self.retry_button, QStyle.StandardPixmap.SP_BrowserReload, "Retry current take"),
            (
                self.retake_history_button,
                QStyle.StandardPixmap.SP_BrowserReload,
                "Record a new take",
            ),
            (
                self.delete_history_button,
                QStyle.StandardPixmap.SP_TrashIcon,
                "Delete selected take",
            ),
            (
                self.restore_history_button,
                QStyle.StandardPixmap.SP_ArrowBack,
                "Restore deleted take",
            ),
            (
                self.save_trim_button,
                QStyle.StandardPixmap.SP_DialogApplyButton,
                "Save reversible trim; original audio stays unchanged",
            ),
            (
                self.reset_trim_button,
                QStyle.StandardPixmap.SP_ArrowBack,
                "Restore full original audio",
            ),
            (
                self.keep_button,
                QStyle.StandardPixmap.SP_DialogApplyButton,
                "Save current take and trim, then next unrecorded prompt · Enter",
            ),
        ):
            button.setIcon(
                self._native_icon(icon, primary=button in (self.keep_button, self.save_trim_button))
            )
            button.setIconSize(QSize(20, 20))
            button.setAccessibleName(description)
            button.setToolTip(description)
        for button in (
            self.play_button,
            self.stop_playback_button,
            self.rewind_button,
            self.skip_back_button,
            self.skip_forward_button,
        ):
            button.setFixedWidth(48)
            button.setIconSize(QSize(24, 24))
        self.play_button.setFixedWidth(64)
        self.play_button.setIconSize(QSize(28, 28))
        self.refresh_button.setText("")
        self.scan_button.setText("")
        self._toggle_history(self.history_caption.isChecked())

    def _native_icon(self, icon: QStyle.StandardPixmap, *, primary: bool = False) -> QIcon:
        """Tint native silhouettes for the window palette, including dark-mode transport."""
        pixmap = self.style().standardIcon(icon).pixmap(QSize(20, 20))
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(
            pixmap.rect(),
            QColor("white") if primary else self.palette().color(QPalette.ColorRole.WindowText),
        )
        painter.end()
        return QIcon(pixmap)

    def _status_icon(self, status: str) -> QIcon:
        good = status in {"Finalized", "Accepted raw", "Recorded"}
        removed = status in {"Removed", "Rejected"}
        color = QColor(
            "#269d62"
            if good
            else "#bf4858"
            if removed
            else "#b17b12"
            if status == "Needs review"
            else "#7d858e"
        )
        pixels = QPixmap(20, 20)
        pixels.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixels)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(color, 2.4))
        if good:
            painter.drawLine(3, 10, 8, 15)
            painter.drawLine(8, 15, 17, 4)
        elif removed:
            painter.drawLine(5, 5, 15, 15)
            painter.drawLine(5, 15, 15, 5)
        else:
            painter.drawEllipse(3, 3, 14, 14)
            if status == "Needs review":
                painter.drawLine(10, 6, 10, 11)
                painter.drawPoint(10, 14)
        painter.end()
        return QIcon(pixels)

    def _toggle_history(self, expanded: bool) -> None:
        self.history_table.setVisible(expanded and self.history_table.rowCount() > 0)
        self.history_caption.setIcon(
            self._native_icon(
                QStyle.StandardPixmap.SP_ArrowDown
                if expanded
                else QStyle.StandardPixmap.SP_ArrowRight
            )
        )

    def _selected_history(self) -> RecordingHistory | None:
        selected = self.history_table.selectionModel().selectedRows()
        item = self.history_table.item(selected[0].row(), 0) if selected else None
        return cast(RecordingHistory, item.data(Qt.ItemDataRole.UserRole + 1)) if item else None

    def _select_history_row(self, index: int) -> None:
        # Restore selection even while user input remains disabled after worker completion.
        self.history_table.selectionModel().setCurrentIndex(
            self.history_table.model().index(index, 0),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )

    def _history_selection_changed(self) -> None:
        if self._refreshing:
            return
        if not self._busy and not self._is_playback_active():
            QTimer.singleShot(0, self._ensure_editor)
        self._update_controls()

    def _apply_palette(self) -> None:
        palette = self.palette()
        dark = palette.color(QPalette.ColorRole.Window).lightness() < 128
        crimson = "#d63b50" if dark else "#b51f38"
        blue = "#286cc8" if dark else "#185db3"
        disabled_background = palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button
        ).name()
        disabled_foreground = palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
        ).name()
        for button, color in (
            (self.start_button, crimson),
            (self.stop_button, crimson),
            (self.keep_button, blue),
            (self.finalize_button, blue),
            (self.save_trim_button, blue),
        ):
            button.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: white; border: none; "
                "border-radius: 6px; padding: 7px 12px; font-weight: 600; }"
                "QPushButton:pressed { padding-top: 8px; padding-bottom: 6px; }"
                f"QPushButton:disabled {{ background-color: {disabled_background}; "
                f"color: {disabled_foreground}; }}"
            )
        self.stop_button.setStyleSheet(
            self.stop_button.styleSheet()
            + 'QPushButton[capture_active="true"]:disabled '
            + f"{{ background-color: {crimson}; color: white; }}"
        )
        secondary_palette = QPalette(palette)
        # Instructions are reading content, not placeholders or disabled controls.
        secondary_palette.setColor(
            QPalette.ColorRole.WindowText, QColor("#b4b8be" if dark else "#606468")
        )
        self.style_label.setPalette(secondary_palette)
        self.capture_status.setPalette(secondary_palette)
        background = palette.color(QPalette.ColorRole.Base).name()
        border = palette.color(QPalette.ColorRole.Mid).name()
        for card in (self.prompt_card, self.editor_card):
            card.setStyleSheet(
                f"QFrame#{card.objectName()} {{ background-color: {background}; "
                f"border: 1px solid {border}; border-radius: 8px; }}"
            )
        # Styled-card repolishing can reset child palettes after an appearance change.
        self.style_label.setStyleSheet(f"color: {'#b4b8be' if dark else '#606468'};")
        accent = "#80baf0" if dark else "#286597"
        self.room_tone_body.setStyleSheet(
            f"QFrame#roomToneInstructions {{ background-color: {background}; "
            f"border: 2px solid {accent}; border-radius: 8px; }}"
        )
        # A styled parent does not reliably propagate later native palette changes.
        for label in (
            self.room_tone_heading,
            self.room_tone_duration,
            self.room_tone_instructions,
        ):
            label.setPalette(palette)
        self.room_tone_help.setPalette(secondary_palette)
        self.room_tone_icon.setPixmap(
            self._native_icon(QStyle.StandardPixmap.SP_MediaVolumeMuted).pixmap(QSize(32, 32))
        )
        self._apply_control_icons()
        if hasattr(self, "_transport_timer"):
            self._render_transport_state()

    def changeEvent(self, event: QEvent) -> None:
        """Keep transport accents legible when the operating system changes appearance."""
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        } and hasattr(self, "start_button"):
            self._apply_palette()

    @staticmethod
    def _configure_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setSortingEnabled(True)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(32)

    def _submit(
        self, label: str, operation: Callable[[], object], callback: Callable[[object], None]
    ) -> None:
        if self._busy or self._audio_fault and not self._close_after_end:
            return
        self._busy = True
        self._failed = False
        self._callback = callback
        self._poll_timer.stop()
        self.message_label.setStyleSheet("")
        self.message_label.setText(label)
        self._update_controls()
        self.operation_requested.emit(operation)

    @Slot(object)
    def _succeeded(self, result: object) -> None:
        self._busy = False
        callback, self._callback = self._callback, None
        self.message_label.clear()
        if callback is not None:
            callback(result)
        self._update_controls()
        QTimer.singleShot(0, self._ensure_editor)

    @Slot(object)
    def _operation_failed(self, error: Exception) -> None:
        self._busy = False
        self._failed = True
        self._capture_transition = None
        self._callback = None
        pending_save_state, self._pending_save_state = self._pending_save_state, None
        if pending_save_state is not None:
            self._clear_editor()
        if isinstance(error, _SessionTransitionError):
            self._session = None
            self._state = None
            self._selection_changed()
        elif pending_save_state is not None:
            self._show_state(pending_save_state)
        self._poll_timer.stop()
        self.message_label.setStyleSheet("color: #c34739;")
        self.message_label.setText(f"Could not complete the operation: {error}")
        self._update_controls()

    def refresh_library(self) -> None:
        """Refresh durable recording evidence on the serialized worker, preserving selection."""
        if self._state is not None and self._state.status == "recording":
            return
        self._submit("Reading recording history…", self._snapshot, self._show_snapshot)

    def _snapshot(self) -> _LibrarySnapshot:
        return _LibrarySnapshot(self.library.rows(), self.library.sessions())

    def _show_snapshot(self, result: object) -> None:
        self._refreshing = True
        snapshot = cast(_LibrarySnapshot, result)
        selected = self._selected_prompt_ids()
        self._rows = snapshot.rows
        self.recordings_table.clearSelection()
        self.recordings_table.setSortingEnabled(False)
        self.recordings_table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            silent = row.prompt.category == "room-tone"
            task_text = (
                f"Silent room tone · {row.prompt.prompt_id} · "
                f"{row.prompt.expected_seconds:g} s — do not speak"
                if silent
                else row.prompt.require_text()
            )
            values = (
                str(index + 1),
                row.prompt.category.replace("-", " ").title(),
                task_text,
                row.status,
                _date_text(row.latest_recorded_at),
                _age_text(row.latest_recorded_at),
                str(row.take_count),
            )
            for column, value in enumerate(values):
                item = _SortItem(value)
                item.setData(Qt.ItemDataRole.UserRole + 1, row.prompt.prompt_id)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, index)
                elif column == 6:
                    item.setData(Qt.ItemDataRole.UserRole, row.take_count)
                elif column in {4, 5}:
                    timestamp = row.latest_recorded_at.timestamp() if row.latest_recorded_at else -1
                    item.setData(Qt.ItemDataRole.UserRole, timestamp if column == 4 else -timestamp)
                item.setToolTip(value)
                if column == 2 and silent:
                    item.setIcon(self._native_icon(QStyle.StandardPixmap.SP_MediaVolumeMuted))
                    item.setToolTip(f"{task_text}\nInstructions only: {row.prompt.text}")
                if column == 3:
                    item.setIcon(self._status_icon(row.status))
                self.recordings_table.setItem(index, column, item)
        self.recordings_table.setSortingEnabled(True)
        current_category = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All categories", "")
        for category in sorted({row.prompt.category for row in self._rows}):
            self.category_filter.addItem(category.replace("-", " ").title(), category)
        self.category_filter.setCurrentIndex(
            max(0, self.category_filter.findData(current_category))
        )
        self.category_filter.blockSignals(False)
        self.session_combo.clear()
        for session in snapshot.sessions:
            if session.resumable:
                started = (
                    _date_text(session.session_started_at)
                    if session.session_started_at
                    else "Date unavailable"
                )
                self.session_combo.addItem(
                    f"{session.session_id} · {started} · "
                    f"{session.recorded_count}/{session.prompt_count} taken",
                    session.session_id,
                )
        if not self.session_combo.count():
            self.session_combo.addItem("No unfinished sessions", None)
        for index in range(self.recordings_table.rowCount()):
            selected_item = self.recordings_table.item(index, 0)
            if (
                selected_item is not None
                and selected_item.data(Qt.ItemDataRole.UserRole + 1) in selected
            ):
                self.recordings_table.selectionModel().select(
                    self.recordings_table.model().index(index, 0),
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
        if not selected and self._rows:
            first_item = self.recordings_table.item(0, 0)
            if first_item is not None:
                self._pin_prompt_row(
                    str(first_item.data(Qt.ItemDataRole.UserRole + 1)), scroll=True
                )
        categories = sorted({row.prompt.category for row in self._rows})
        completed = {
            category: sum(
                r.status != "Missing" for r in self._rows if r.prompt.category == category
            )
            for category in categories
        }
        self.coverage_label.setText(
            "  ·  ".join(
                f"{category.replace('-', ' ').title()} "
                f"{completed[category]}/"
                f"{sum(row.prompt.category == category for row in self._rows)}"
                for category in categories
            )
        )
        recorded = sum(row.status != "Missing" for row in self._rows)
        self.progress_bar.setRange(0, max(1, len(self._rows)))
        self.progress_bar.setValue(recorded)
        self._filter_rows()
        self._selection_changed()
        self._focus_current_take = False
        self._refreshing = False
        if self._initial_scan:
            self._initial_scan = False
            QTimer.singleShot(0, self.scan_devices)
        QTimer.singleShot(0, self._ensure_editor)

    def _selected_prompt_ids(self) -> tuple[str, ...]:
        return tuple(
            str(item.data(Qt.ItemDataRole.UserRole + 1))
            for index in self.recordings_table.selectionModel().selectedRows()
            if not self.recordings_table.isRowHidden(index.row())
            if (item := self.recordings_table.item(index.row(), 0)) is not None
        )

    def _selected_row(self) -> RecordingRow | None:
        if self._session is not None:
            prompt = self._state.prompt if self._state is not None else None
            return next((row for row in self._rows if prompt and row.prompt == prompt), None)
        return self._highlighted_row()

    def _highlighted_row(self) -> RecordingRow | None:
        ids = self._selected_prompt_ids()
        item = self.recordings_table.item(self.recordings_table.currentRow(), 0)
        current = str(item.data(Qt.ItemDataRole.UserRole + 1)) if item is not None else None
        target = current if current in ids else ids[0] if ids else None
        return next((row for row in self._rows if row.prompt.prompt_id == target), None)

    def _library_selection_changed(self) -> None:
        if self._refreshing:
            return
        row = self._highlighted_row()
        if row is None:
            if self._audio is not None and not self._discard_unsaved_trim():
                self._pin_prompt_row(self._audio.history.prompt_id, scroll=False)
                return
            self._selection_changed()
            return
        if (
            self._busy
            or self._audio_fault
            or (self._state is not None and self._state.status == "recording")
        ):
            if self._state is not None and self._state.prompt is not None:
                self._pin_prompt_row(self._state.prompt.prompt_id, scroll=False)
            return
        if self._audio is not None and self._audio.history.prompt_id != row.prompt.prompt_id:
            if not self._discard_unsaved_trim():
                self._pin_prompt_row(self._audio.history.prompt_id, scroll=False)
                return
        if self._is_playback_active():
            self._clear_editor()
            if self._audio_fault:
                return
        session = self._session
        if session is not None and (
            self._state is None
            or self._state.prompt is None
            or self._state.prompt.prompt_id != row.prompt.prompt_id
        ):
            if not self._discard_unsaved_trim():
                if self._state is not None and self._state.prompt is not None:
                    self._pin_prompt_row(self._state.prompt.prompt_id, scroll=False)
                return
            prompt_id = row.prompt.prompt_id
            device = self.device_combo.currentData()

            def select() -> tuple[DesktopRecordingSession, CaptureState]:
                return self._select_session_prompt(session, (prompt_id,), device, prompt_id)

            self._clear_editor()
            self._submit("Selecting the highlighted prompt…", select, self._session_opened)
        else:
            self._selection_changed()
            QTimer.singleShot(0, self._ensure_editor)

    def _filter_rows(self) -> None:
        query = self.search_edit.text().strip().casefold()
        category = self.category_filter.currentData()
        status = self.status_filter.currentText()
        if self._session is not None:
            query, category, status = "", "", "All statuses"
        rows = {row.prompt.prompt_id: row for row in self._rows}
        visible = 0
        for index in range(self.recordings_table.rowCount()):
            item = self.recordings_table.item(index, 0)
            if item is None:
                continue
            row = rows[str(item.data(Qt.ItemDataRole.UserRole + 1))]
            search_text = f"{row.prompt.text} {row.prompt.style} {row.prompt.prompt_id}".casefold()
            matches_status = (
                status == "All statuses"
                or row.status == status
                or status == "Recorded"
                and row.status in {"Finalized", "Accepted raw"}
            )
            show = (
                (not query or query in search_text)
                and (not category or row.prompt.category == category)
                and matches_status
            )
            self.recordings_table.setRowHidden(index, not show)
            visible += show
        recorded = sum(row.status != "Missing" for row in self._rows)
        self.summary_label.setText(
            f"{recorded} of {len(self._rows)} prompts recorded · "
            f"{len(self._rows) - recorded} still missing · {visible} shown"
        )
        self._selection_changed()

    def _clear_filters(self) -> None:
        self.search_edit.clear()
        self.category_filter.setCurrentIndex(0)
        self.status_filter.setCurrentIndex(0)

    def _selection_changed(self) -> None:
        row = self._selected_row()
        if self._session is not None:
            prompt = self._state.prompt if self._state is not None else None
        else:
            prompt = row.prompt if row is not None else None
        prompt_id = prompt.prompt_id if prompt is not None else None
        changed_prompt = prompt_id != self._inspected_prompt_id
        silent = prompt is not None and prompt.category == "room-tone"
        self.prompt_text.setAccessibleName(
            "Spoken script — read aloud" if prompt is not None and not silent else "Recording task"
        )
        self.prompt_content.setCurrentWidget(self.room_tone_panel if silent else self.prompt_text)
        self.style_label.setVisible(not silent)
        spoken = prompt is not None and not silent
        self.style_label.setAccessibleName(
            "Delivery guidance for the script" if spoken else "Recording guidance"
        )
        delivery_help = (
            "How to perform the script above. This guidance is not part of the spoken script. "
            "The length is approximate; finish the full text at a natural pace."
            if spoken
            else ""
        )
        self.style_label.setToolTip(delivery_help)
        self.style_label.setAccessibleDescription(delivery_help)
        if prompt is not None:
            self.prompt_title.setText(
                f"{prompt.category.replace('-', ' ').title()} · {prompt.prompt_id}"
            )
            if silent:
                self.inspector_title.setText("Silent room tone")
                self._set_script("")
                self.room_tone_duration.setText(
                    f"Record about {prompt.expected_seconds:g} seconds of silence."
                )
                self.room_tone_instructions.setText(
                    f"Instructions only:\n{prompt.text}\n{prompt.style}"
                )
                self.room_tone_instructions.setAccessibleDescription(
                    self.room_tone_instructions.text()
                )
                self.style_label.clear()
                if changed_prompt:
                    self.room_tone_panel.verticalScrollBar().setValue(0)
            else:
                self.inspector_title.setText("Read aloud")
                self._set_script(prompt.require_text())
                self.style_label.setText(
                    f"Delivery guidance: {prompt.style}  "
                    f"Approx. length: {prompt.expected_seconds:g} seconds (not a limit)."
                )
            if self._session is not None:
                self._pin_prompt_row(prompt.prompt_id, scroll=changed_prompt)
        elif self._session is not None:
            self.inspector_title.setText("Recording task")
            self.prompt_title.setText("Batch complete")
            self._set_script("All queued takes are kept. Finalize this session when you are ready.")
            self.style_label.setText("Earlier recordings remain in your library.")
        else:
            self.inspector_title.setText("Recording task")
            self.prompt_title.setText("Select a prompt")
            self._set_script("")
            self.style_label.clear()
        self._inspected_prompt_id = prompt_id
        if self._session is None:
            self.capture_status.setText(self._idle_status)
        history_values = row.history if row is not None else ()
        if changed_prompt or self._displayed_history != history_values:
            selected_history = None
            selected_rows = self.history_table.selectionModel().selectedRows()
            if selected_rows and not changed_prompt:
                selected_item = self.history_table.item(selected_rows[0].row(), 0)
                if selected_item is not None:
                    selected_history = selected_item.data(Qt.ItemDataRole.UserRole + 1)
            self.history_table.setSortingEnabled(False)
            self.history_table.clearSelection()
            self.history_table.setRowCount(len(history_values))
            chronological = sorted(
                history_values,
                key=lambda take: (take.recorded_at, take.session_id, take.relative_path),
            )
            for index, history in enumerate(history_values):
                take_number = chronological.index(history) + 1
                current = " · Current" if self._is_current_take(history) else ""
                values = (
                    f"Take {take_number}{current} · {_date_text(history.recorded_at)}",
                    "Take time" if history.date_precision == "take" else "Session start only",
                    history.status,
                    history.session_id,
                )
                for column, value in enumerate(values):
                    item = _SortItem(value)
                    item.setData(Qt.ItemDataRole.UserRole + 1, history)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, history.recorded_at.timestamp())
                    precision = (
                        "Exact take time"
                        if history.date_precision == "take"
                        else "Session start only"
                    )
                    item.setToolTip(
                        f"{value}\n{history.status} · {precision}\n{history.session_id}"
                    )
                    if column == 2:
                        item.setIcon(self._status_icon(history.status))
                    self.history_table.setItem(index, column, item)
            self.history_table.setSortingEnabled(True)
            if history_values:
                self._select_history_row(0)
                for index in range(self.history_table.rowCount()):
                    history_item = self.history_table.item(index, 0)
                    if (
                        history_item is not None
                        and history_item.data(Qt.ItemDataRole.UserRole + 1) == selected_history
                    ):
                        self._select_history_row(index)
            self._displayed_history = history_values
        if self._focus_current_take and self._state is not None:
            for index in range(self.history_table.rowCount()):
                review_item = self.history_table.item(index, 0)
                if review_item is not None:
                    candidate = cast(
                        RecordingHistory, review_item.data(Qt.ItemDataRole.UserRole + 1)
                    )
                    if self._is_current_take(candidate):
                        self._select_history_row(index)
                        break
        self.history_caption.setText(f"Takes · {len(history_values)}")
        self.history_table.setVisible(bool(history_values) and self.history_caption.isChecked())
        self._update_controls()

    def _set_script(self, text: str) -> None:
        if self.prompt_text.toPlainText() != text:
            self.prompt_text.setPlainText(text)

    def _pin_prompt_row(self, prompt_id: str, *, scroll: bool) -> None:
        table = self.recordings_table
        for index in range(table.rowCount()):
            item = table.item(index, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole + 1) == prompt_id:
                table.setRowHidden(index, False)
                table.blockSignals(True)
                table.selectionModel().select(
                    table.model().index(index, 0),
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
                table.blockSignals(False)
                if scroll:
                    visible_item = table.item(index, 2)
                    if visible_item is not None:
                        table.scrollToItem(visible_item, QAbstractItemView.ScrollHint.EnsureVisible)
                return

    def scan_devices(self) -> None:
        """Discover available UMIK-1 inputs without opening a capture stream."""
        self._submit("Looking for your UMIK-1…", self._device_loader, self._show_devices)

    def _show_devices(self, result: object) -> None:
        self.device_combo.clear()
        for device in cast(tuple[CaptureDevice, ...], result):
            if "umik-1" in device.display_name.casefold():
                self.device_combo.addItem(device.display_name, device)
                self.device_combo.setItemData(
                    self.device_combo.count() - 1,
                    f"{device.backend} · {device.device_id}",
                    Qt.ItemDataRole.ToolTipRole,
                )
                if self.requested_device in {device.device_id, device.display_name}:
                    self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
        if not self.device_combo.count():
            self.device_combo.addItem("No UMIK-1 found — connect it and re-scan", None)
            self.message_label.setText(
                "You can browse your recording library without a microphone."
            )

    def _update_controls(self) -> None:
        idle = not self._busy and not self._audio_fault
        phase = self._state.status if self._state is not None else None
        active = self._session is not None
        recording = phase == "recording"
        # Status polls temporarily lock the serialized worker, but capture is
        # still active. Only a real shutdown should mute Stop's recording color.
        capture_active = recording and self._capture_transition != "stopping"
        if self.stop_button.property("capture_active") != capture_active:
            self.stop_button.setProperty("capture_active", capture_active)
            self.stop_button.style().unpolish(self.stop_button)
            self.stop_button.style().polish(self.stop_button)
        row = self._selected_row()
        prompt = self._state.prompt if active and self._state is not None else None
        if not active and row is not None:
            prompt = row.prompt
        silent = prompt is not None and prompt.category == "room-tone"
        self.start_button.setText("●  Record silence    R" if silent else "●  Record    R")
        self.start_button.setAccessibleName(
            "Record silent room tone" if silent else "Record speech"
        )
        self.start_button.setToolTip(
            "R starts recording. Stay silent; stop manually after the requested duration."
            if silent
            else "R starts a take; read only the spoken script aloud"
        )
        self.stop_button.setText(
            "■  Stop silent recording    R" if silent else "■  Stop recording    R"
        )
        playing = self._is_playback_active()
        browse = idle and not active and not playing
        selectable = idle and not recording and not self._playing
        self.recordings_table.setEnabled(selectable)
        self.recordings_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
            if selectable
            else QAbstractItemView.SelectionMode.NoSelection
        )
        for control in (
            self.search_edit,
            self.category_filter,
            self.status_filter,
            self.clear_filters_button,
        ):
            control.setEnabled(browse)
        self.refresh_button.setEnabled(idle and not recording)
        self.scan_button.setEnabled(browse)
        self.device_combo.setEnabled(browse)
        available = browse and isinstance(self.device_combo.currentData(), CaptureDevice)
        self.record_selected_button.setEnabled(available and bool(self._selected_prompt_ids()))
        self.record_missing_button.setEnabled(
            available and any(row.status == "Missing" for row in self._rows)
        )
        self.session_combo.setEnabled(browse)
        self.resume_button.setEnabled(browse and self.session_combo.currentData() is not None)
        has_history = bool(self.history_table.selectionModel().selectedRows())
        usable_audio = bool(
            self._audio is not None
            and not self._audio.edit.removed
            and self._selected_history() == self._audio.history
        )
        reviewed_current = self._reviewing_current_take()
        reviewing = phase == "review" or not active and has_history
        self.start_button.setEnabled(
            idle
            and not playing
            and not reviewing
            and (
                phase == "ready" or not active and available and self._highlighted_row() is not None
            )
        )
        self.stop_button.setEnabled(idle and recording)
        self.play_button.setEnabled(idle and not recording and usable_audio)
        self.play_button.setIcon(
            self._native_icon(
                QStyle.StandardPixmap.SP_MediaPause
                if self._playing
                else QStyle.StandardPixmap.SP_MediaPlay
            )
        )
        self.play_button.setToolTip("Pause · Space" if self._playing else "Play selection · Space")
        self.stop_playback_button.setEnabled(playing)
        self.rewind_button.setEnabled(idle and not recording and usable_audio)
        self.skip_back_button.setEnabled(idle and not recording and usable_audio)
        self.skip_forward_button.setEnabled(idle and not recording and usable_audio)
        self.retry_button.setEnabled(idle and reviewed_current and not playing)
        self.keep_button.setEnabled(idle and reviewed_current and not playing)
        self.finalize_button.setEnabled(idle and phase == "complete" and not playing)
        self.end_button.setEnabled(idle and active and not playing)
        self.start_button.setVisible(not recording and not reviewing and phase != "complete")
        self.stop_button.setVisible(recording)
        self.record_panel.setVisible(recording or not reviewing and phase != "complete")
        self.transport_panel.setVisible(usable_audio and not recording)
        self.waveform.setVisible(has_history and not recording)
        self.playback_slider.setVisible(has_history and not recording)
        self.playback_time.setVisible(has_history and not recording)
        self.trim_panel.setVisible(has_history and not recording)
        self.timeline_panel.setVisible(has_history and not recording)
        self.fit_waveform.setVisible(has_history and not recording)
        self.history_caption.setVisible(has_history)
        self.keep_button.setVisible(phase == "review")
        self.finalize_button.setVisible(phase == "complete")
        self.retry_button.setVisible(phase == "review" and reviewed_current)
        self.end_button.setVisible(active)
        self.qc_label.setVisible(
            bool(self.qc_label.text())
            and (reviewed_current or self._state is not None and self._state.metrics is None)
        )
        historical_review = phase == "review" and not reviewed_current
        self.review_hint.setText(
            "Earlier take selected. Select the Current take above to save & next."
            if historical_review and self._audio is not None
            else "Loading the current take for review…"
            if historical_review
            else ""
        )
        self.review_hint.setVisible(bool(self.review_hint.text()))
        history = self._selected_history()
        removed = history is not None and history.status == "Removed"
        self.delete_history_button.setEnabled(
            idle and not recording and not playing and has_history
        )
        self.delete_history_button.setVisible(has_history and not removed and not recording)
        self.restore_history_button.setVisible(removed and not recording)
        self.restore_history_button.setEnabled(idle and not recording and not playing)
        self.retake_history_button.setEnabled(
            idle
            and not recording
            and not playing
            and has_history
            and isinstance(self.device_combo.currentData(), CaptureDevice)
        )
        self.retake_history_button.setVisible(
            has_history and not recording and not reviewed_current and phase != "ready"
        )
        self.fit_waveform.setEnabled(has_history and not recording)
        if recording:
            self.editor_caption.setText("Recording — editing is available after Stop")
        elif self._audio is None:
            self.editor_caption.setText("Loading take…" if has_history else "Ready for a new take")
        editable = idle and not recording and not self._playing and usable_audio
        self.waveform.setEnabled(idle and not recording and usable_audio)
        self.waveform.editable = editable
        self.playback_slider.setEnabled(idle and not recording and usable_audio)
        self.trim_start.setEnabled(editable)
        self.trim_end.setEnabled(editable)
        self.save_trim_button.setEnabled(editable and self._trim_dirty)
        self.reset_trim_button.setEnabled(
            editable and (self._trim_dirty or self._audio is not None and self._audio.edit.trimmed)
        )
        self.history_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
            if idle and not recording and not playing
            else QAbstractItemView.SelectionMode.NoSelection
        )
        if hasattr(self, "_poll_timer"):
            if idle and recording and not self._failed:
                self._poll_timer.start()
            else:
                self._poll_timer.stop()
        if hasattr(self, "_transport_timer"):
            self._render_transport_state()

        # QSplitter can allocate below descendants' minimum-size hints. Reserve the
        # editor's true height so its waveform and controls never overlap on laptops.
        editor_layout = self.editor_card.layout()
        if editor_layout is not None:
            required_height = max(
                self.editor_card.minimumSizeHint().height(),
                editor_layout.minimumHeightForWidth(self.editor_card.width()) + 2,
            )
            self.editor_card.setMinimumHeight(required_height)
            self.editor_card.setMaximumHeight(
                required_height + self.waveform.maximumHeight() - self.waveform.minimumHeight()
                if has_history and not recording
                else required_height
            )
        self.inspector_layout.setStretchFactor(self.prompt_card, 1 if reviewing else 3)
        self.inspector_layout.setStretchFactor(self.editor_card, 2 if reviewing else 0)

    def _is_playback_active(self) -> bool:
        return self._playing or self._paused

    def _reviewing_current_take(self) -> bool:
        """Approval/retry must target the loaded and selected pending take, never unseen audio."""
        return bool(
            self._state is not None
            and self._state.status == "review"
            and self._audio is not None
            and not self._audio.edit.removed
            and self._selected_history() == self._audio.history
            and self._is_current_take(self._audio.history)
        )

    def _render_transport_state(self) -> None:
        phase = self._state.status if self._state is not None else None
        seconds = 0.0
        if self._audio_fault:
            status = "Audio error · reopen app"
        elif self._is_playback_active():
            status = "Paused" if self._paused else "Playing"
            try:
                seconds = self._player.position_seconds
                if self._audio is not None:
                    self._position_frame = min(
                        self.waveform.end_frame,
                        self.waveform.start_frame + round(seconds * self._audio.clip.sample_rate),
                    )
                    self._show_playhead()
                if self._playing and not self._player.is_playing:
                    self._playing = self._paused = False
                    self.message_label.setText("Playback stopped.")
                    self._update_controls()
                    return
            except Exception as error:
                self._audio_failed(error)
                return
        elif self._failed:
            status = "Needs attention"
            if phase == "recording":
                seconds = self._captured_seconds
        elif self._capture_transition == "starting":
            status = "Starting microphone — wait"
        elif self._capture_transition == "stopping":
            status = "Stopping — checking audio"
            seconds = self._captured_seconds
        elif phase == "recording":
            prompt = self._state.prompt if self._state is not None else None
            seconds = self._captured_seconds
            if seconds <= 0.0:
                status = "Starting microphone — wait"
            else:
                status = (
                    "Recording silence"
                    if prompt is not None and prompt.category == "room-tone"
                    else "Recording"
                )
        elif self._audio is not None and (
            self._session is None or phase == "review" and not self._reviewing_current_take()
        ):
            status = "Reviewing take"
            seconds = self._audio.clip.duration_seconds
        elif phase == "review":
            status = "Review"
            if self._state is not None and self._state.metrics is not None:
                seconds = self._state.metrics.duration_seconds
        elif phase == "complete":
            status = "Ready to finalize"
        else:
            status = "Ready"
        self.state_label.setText(status)
        minutes, remainder = divmod(int(seconds), 60)
        self.elapsed_label.setText(f"{minutes:02d}:{remainder:02d}")
        media_duration = phase in {"recording", "review"} or self._capture_transition is not None
        self.elapsed_label.setAccessibleName(
            "Playback position"
            if self._is_playback_active()
            else "Captured audio duration"
            if media_duration
            else "Elapsed time"
        )
        self.elapsed_label.setToolTip(
            "Native audio playback position."
            if self._is_playback_active()
            else "Confirmed audio duration; microphone startup and file checks are excluded."
            if media_duration
            else "Elapsed time for the current audio task."
        )
        foreground = self.palette().color(QPalette.ColorRole.WindowText).name()
        if status in {"Recording", "Recording silence"}:
            foreground = (
                "#ed6578"
                if self.palette().color(QPalette.ColorRole.Window).lightness() < 128
                else "#b51f38"
            )
        elif status == "Playing":
            foreground = (
                "#78c994"
                if self.palette().color(QPalette.ColorRole.Window).lightness() < 128
                else "#206b43"
            )
        self.state_label.setStyleSheet(f"color: {foreground};")
        # Recording time changes only when confirmed media arrives from the backend.
        # A wall clock would count device startup, stalled capture, and stop/QC work.
        if self._playing:
            if not self._transport_timer.isActive():
                self._transport_timer.start()
        else:
            self._transport_timer.stop()

    def _record_selected(self) -> None:
        self._begin_session(self._selected_prompt_ids())

    def _record_missing(self) -> None:
        self._begin_session(
            tuple(row.prompt.prompt_id for row in self._rows if row.status == "Missing")
        )

    def _begin_session(self, prompt_ids: tuple[str, ...], *, start: bool = False) -> None:
        device = self.device_combo.currentData()
        if (
            self._busy
            or self._is_playback_active()
            or not prompt_ids
            or not isinstance(device, CaptureDevice)
            or self._session is not None
        ):
            return
        if not self._discard_unsaved_trim():
            return
        self._clear_editor()
        if start:
            self._capture_transition = "starting"
            self._captured_seconds = 0.0
        highlighted = self._highlighted_row()
        prompt_id = (
            highlighted.prompt.prompt_id
            if highlighted is not None and highlighted.prompt.prompt_id in prompt_ids
            else prompt_ids[0]
        )

        def begin() -> tuple[DesktopRecordingSession, CaptureState]:
            return self._open_selected_session(prompt_ids, device, prompt_id)

        def opened(result: object) -> None:
            self._session_opened(result)
            if start:
                # Publish the resumable session before any microphone operation can fail.
                self._start_take()

        self._submit("Preparing your recording queue…", begin, opened)

    def _open_selected_session(
        self, prompt_ids: tuple[str, ...], device: CaptureDevice, prompt_id: str
    ) -> tuple[DesktopRecordingSession, CaptureState]:
        session = self.library.begin_session(prompt_ids, device)
        try:
            return session, session.select_prompt(prompt_id)
        except Exception:
            session.abort()
            raise

    def _select_session_prompt(
        self,
        session: DesktopRecordingSession,
        prompt_ids: tuple[str, ...],
        device: object,
        prompt_id: str,
    ) -> tuple[DesktopRecordingSession, CaptureState]:
        # canonical: desktop prompt selection preserves batches before switching sessions.
        if prompt_id in {p.prompt_id for p in session.evidence.selected_suite.prompts}:
            return session, session.select_prompt(prompt_id)
        if not isinstance(device, CaptureDevice):
            raise ValueError("Select a microphone before preparing this prompt")
        session.abort()
        try:
            return self._open_selected_session(prompt_ids, device, prompt_id)
        except Exception as error:
            raise _SessionTransitionError(
                f"Previous batch saved for Resume; new prompt failed: {error}"
            ) from error

    def _resume_session(self) -> None:
        session_id = self.session_combo.currentData()
        if (
            self._busy
            or self._is_playback_active()
            or not isinstance(session_id, str)
            or self._session is not None
        ):
            return
        self._stop_playback()

        def resume() -> tuple[DesktopRecordingSession, CaptureState]:
            session = self.library.resume_session(session_id)
            return session, session.current()

        self._submit("Opening the unfinished session…", resume, self._session_opened)

    def _session_opened(self, result: object) -> None:
        if self._pending_save_state is not None:
            self._pending_save_state = None
            self._clear_editor()
        self._session, state = cast(tuple[DesktopRecordingSession, CaptureState], result)
        self._focus_current_take = state.status == "review"
        self._state = state
        self._clear_filters()
        if state.prompt is not None:
            self._pin_prompt_row(state.prompt.prompt_id, scroll=True)
        self._show_state(state)
        if state.status == "review":
            self.refresh_library()

    def _show_state(self, result: object) -> None:
        state = cast(CaptureState, result)
        if state.status == "review" and (self._state is None or self._state.status != "review"):
            self._focus_current_take = True
        self._state = state
        position = (
            f"{state.index + 1} of {state.total}"
            if state.index is not None
            else f"{state.total} takes"
        )
        target = state.prompt.prompt_id if state.prompt else "batch complete"
        self.capture_status.setText(f"Recording target: {target} · {position}")
        self.capture_status.setToolTip(state.session_id)
        self._captured_seconds = max(0.0, state.elapsed_seconds or 0.0)
        if state.metrics is None:
            self.qc_label.setText(" · ".join(state.findings))
        else:
            metrics = state.metrics
            findings = ", ".join(state.findings) or "No automatic issues flagged"
            self.qc_label.setText(
                f"Original-audio {'QC passed' if metrics.passed else 'QC needs review'} · "
                f"{metrics.duration_seconds:.1f}s · Peak {metrics.peak_dbfs:.1f} dBFS · "
                f"{metrics.clipped_samples} clipped samples · {findings}"
            )
        self.qc_label.setVisible(bool(self.qc_label.text()))
        self._selection_changed()

    def _capture_action(
        self,
        label: str,
        operation: Callable[[], CaptureState],
        *,
        refresh: bool = True,
        transition: Literal["starting", "stopping"] | None = None,
    ) -> None:
        def finished(result: object) -> None:
            self._capture_transition = None
            self._show_state(result)
            if refresh:
                self.refresh_library()

        self._capture_transition = transition
        if transition == "starting":
            self._captured_seconds = 0.0
        self._submit(label, operation, finished)

    def _start_take(self) -> None:
        if not self.start_button.isEnabled():
            return
        if self._session is None:
            row = self._highlighted_row()
            if row is not None:
                self._begin_session((row.prompt.prompt_id,), start=True)
            return
        if self._session is not None and self.start_button.isEnabled():
            self._clear_editor()
            self._capture_action(
                "Starting the microphone…",
                self._session.start_take,
                refresh=False,
                transition="starting",
            )

    def _stop_take(self) -> None:
        if self._session is not None and self.stop_button.isEnabled():
            self._capture_action(
                "Stopping and checking the take…", self._session.stop_take, transition="stopping"
            )

    def _retry_take(self) -> None:
        session = self._session
        if session is not None and self.retry_button.isEnabled() and self._reviewing_current_take():
            if not self._discard_unsaved_trim():
                return
            self._clear_editor()
            self._capture_action(
                "Saving this attempt and preparing a retry…",
                lambda: session.reject_take("Operator requested a retry in desktop"),
            )

    def _keep_take(self) -> None:
        if self._state is None or self._state.status != "review":
            return
        if not self._reviewing_current_take():
            self.message_label.setText(
                "Select the Current take before saving & advancing. "
                "Save or reset a historical take's trim separately."
            )
            return
        if self._session is not None and self.keep_button.isEnabled():
            session = self._session
            prompt = self._state.prompt
            assert prompt is not None
            audio = self._audio
            dirty = self._trim_dirty
            start, end = self.waveform.start_frame, self.waveform.end_frame

            def keep() -> CaptureState:
                if dirty and audio is not None:
                    self.library.save_trim(audio.history, start, end)
                return session.accept_take()

            def kept(result: object) -> None:
                self._trim_dirty = False
                # The engine's next batch prompt is provisional. Keep the saved
                # prompt visible until library navigation resolves the actual target.
                self._pending_save_state = cast(CaptureState, result)
                self._submit(
                    "Finding the next unrecorded prompt…",
                    self._snapshot,
                    lambda snapshot: self._advance_after_keep(snapshot, prompt.prompt_id),
                )

            self._submit("Keeping the take and its trim…", keep, kept)

    def _advance_after_keep(self, result: object, saved_prompt_id: str) -> None:
        # canonical: Save & next follows fresh library evidence, independent of table sorting.
        snapshot = cast(_LibrarySnapshot, result)
        self._show_snapshot(snapshot)
        session = self._session
        if session is None:
            return
        rows = snapshot.rows
        saved_index = next(
            index for index, row in enumerate(rows) if row.prompt.prompt_id == saved_prompt_id
        )
        missing = tuple(
            row.prompt.prompt_id
            for row in (*rows[saved_index + 1 :], *rows[: saved_index + 1])
            if row.status == "Missing"
        )
        if not missing:
            self._clear_editor()
            if (
                self._pending_save_state is not None
                and self._pending_save_state.status == "complete"
            ):
                self._show_state(self._pending_save_state)
                self._pending_save_state = None
                self.message_label.setText(
                    "All prompts have recordings. This batch can be finalized."
                )
            else:
                self._end_session(all_recorded=True)
            return
        device = self.device_combo.currentData()
        self._submit(
            "Preparing the next unrecorded prompt…",
            lambda: self._select_session_prompt(session, missing, device, missing[0]),
            self._session_opened,
        )

    def _is_current_take(self, history: RecordingHistory) -> bool:
        state = self._state
        return bool(
            state is not None
            and state.prompt is not None
            and history.session_id == state.session_id
            and history.prompt_id == state.prompt.prompt_id
            and history.relative_path == state.relative
            and history.status not in {"Rejected", "Removed"}
        )

    def _poll(self) -> None:
        if self._session is not None and not self._busy:

            def polled(result: object) -> None:
                self._show_state(result)
                if self._state is not None and self._state.status != "recording":
                    self.refresh_library()

            self._submit("Recording…", self._session.current, polled)

    def _clear_editor(self) -> None:
        if self._audio is not None:
            history = self._audio.history
            self._take_positions[(history.session_id, history.relative_path, history.sha256)] = (
                self._position_frame
            )
        if not self._stop_playback():
            return
        self._audio = None
        self._editor_key = None
        self._player_loaded = False
        self._trim_dirty = False
        self.waveform.set_clip(None)
        self.playback_slider.setRange(0, 0)
        self.playback_time.setText("0:00.00 / 0:00.00")
        self.editor_caption.setText("Ready for a new take")

    def _discard_unsaved_trim(self) -> bool:
        if not self._trim_dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unapplied trim",
            "Discard this unapplied trim? The original audio is unchanged.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Discard:
            return False
        self._trim_dirty = False
        return True

    def _ensure_editor(self) -> None:
        if self._busy or self._failed or self._refreshing or self._is_playback_active():
            return
        if self._state is not None and self._state.status == "recording":
            return
        history = self._selected_history()
        if history is None:
            if self._audio is not None:
                self._clear_editor()
                self._update_controls()
            return
        key = (history.session_id, history.relative_path, history.sha256, history.status)
        if key == self._editor_key:
            return
        if not self._discard_unsaved_trim():
            if self._audio is not None:
                self.history_table.blockSignals(True)
                for index in range(self.history_table.rowCount()):
                    item = self.history_table.item(index, 0)
                    if item is not None and (
                        item.data(Qt.ItemDataRole.UserRole + 1) == self._audio.history
                    ):
                        self._select_history_row(index)
                self.history_table.blockSignals(False)
            return
        self._clear_editor()
        self._editor_key = key

        def load() -> _EditorAudio:
            path = self.library.original_preview_path(history)
            edit = self.library.edit_state(history)
            clip = load_pcm_audio(path, expected_sha256=history.sha256)
            if clip.frame_count != edit.source_frames or clip.sample_rate != edit.sample_rate:
                raise ContractError("Waveform and edit evidence disagree on source frames")
            return _EditorAudio(clip, history, edit, path)

        self._submit("Loading the original waveform…", load, self._editor_loaded)

    def _editor_loaded(self, result: object) -> None:
        audio = cast(_EditorAudio, result)
        self._audio = audio
        self.waveform.set_clip(audio.clip, audio.edit.start_frame, audio.edit.end_frame)
        remembered = self._take_positions.get(
            (audio.history.session_id, audio.history.relative_path, audio.history.sha256),
            audio.edit.start_frame,
        )
        self._position_frame = max(audio.edit.start_frame, min(remembered, audio.edit.end_frame))
        self.playback_slider.setRange(0, audio.clip.frame_count)
        for field, frame in (
            (self.trim_start, audio.edit.start_frame),
            (self.trim_end, audio.edit.end_frame),
        ):
            field.blockSignals(True)
            field.setRange(0, audio.clip.duration_seconds)
            field.setValue(frame / audio.clip.sample_rate)
            field.blockSignals(False)
        suffix = (
            " · deleted" if audio.edit.removed else " · trimmed copy" if audio.edit.trimmed else ""
        )
        self.editor_caption.setText(f"Original waveform{suffix}")
        self.editor_caption.setToolTip(f"Recorded {_date_text(audio.history.recorded_at)}")
        self._show_playhead()

    @staticmethod
    def _audio_time(seconds: float) -> str:
        return f"{int(seconds // 60)}:{seconds % 60:05.2f}"

    def _show_playhead(self) -> None:
        if self._audio is None:
            return
        self.waveform.set_position(self._position_frame)
        self.playback_slider.blockSignals(True)
        self.playback_slider.setValue(self._position_frame)
        self.playback_slider.blockSignals(False)
        self.playback_time.setText(
            f"{self._audio_time(self._position_frame / self._audio.clip.sample_rate)} / "
            f"{self._audio_time(self._audio.clip.duration_seconds)}"
        )

    def _seek_frame(self, frame: int) -> None:
        if (
            self._audio is None
            or self._busy
            or self._state is not None
            and self._state.status == "recording"
        ):
            return
        self._position_frame = max(self.waveform.start_frame, min(frame, self.waveform.end_frame))
        if self._player_loaded:
            try:
                self._player.seek(
                    (self._position_frame - self.waveform.start_frame)
                    / self._audio.clip.sample_rate
                )
            except Exception as error:
                self._audio_failed(error)
                return
        self._show_playhead()

        if self._paused:
            self._render_transport_state()

    def _skip_playback(self, seconds: int) -> None:
        """Seek relative to the displayed source clock, clamped by the existing trim bounds."""
        if self._audio is not None and self.skip_back_button.isEnabled():
            self._seek_frame(self._position_frame + seconds * self._audio.clip.sample_rate)

    def _play_from_frame(self, frame: int) -> None:
        """Double-click seeks and starts/resumes preview; an already-playing clip keeps playing."""
        if not self.play_button.isEnabled():
            return
        self._seek_frame(frame)
        if not self._playing:
            self._toggle_playback()

    def _trim_changed(self, start: int, end: int) -> None:
        if self._audio is None or self._busy or self._playing:
            return
        if not self._stop_playback():
            return
        self._player_loaded = False
        self.waveform.set_range(start, end)
        self._trim_dirty = (start, end) != (
            self._audio.edit.start_frame,
            self._audio.edit.end_frame,
        )
        for field, frame in ((self.trim_start, start), (self.trim_end, end)):
            field.blockSignals(True)
            field.setValue(frame / self._audio.clip.sample_rate)
            field.blockSignals(False)
        self._seek_frame(max(start, min(self._position_frame, end)))
        self._update_controls()

    def _trim_fields_changed(self) -> None:
        if self._audio is None:
            return
        start = round(self.trim_start.value() * self._audio.clip.sample_rate)
        end = round(self.trim_end.value() * self._audio.clip.sample_rate)
        if start >= end:
            self.message_label.setText("Trim end must be after trim start.")
            for field, frame in (
                (self.trim_start, self.waveform.start_frame),
                (self.trim_end, self.waveform.end_frame),
            ):
                field.blockSignals(True)
                field.setValue(frame / self._audio.clip.sample_rate)
                field.blockSignals(False)
            return
        self._trim_changed(start, min(end, self._audio.clip.frame_count))

    def _edit_finished(self, _result: object) -> None:
        self._trim_dirty = False
        self._editor_key = None
        self.refresh_library()

    def _save_trim(self) -> None:
        if self._audio is not None and self.save_trim_button.isEnabled():
            if not self._stop_playback(rewind=False):
                return
            history = self._audio.history
            start, end = self.waveform.start_frame, self.waveform.end_frame
            self._submit(
                "Saving a frame-exact trimmed copy; preserving the original…",
                lambda: self.library.save_trim(history, start, end),
                self._edit_finished,
            )

    def _reset_trim(self) -> None:
        if self._audio is not None and self.reset_trim_button.isEnabled():
            if not self._stop_playback(rewind=False):
                return
            history = self._audio.history
            self._submit(
                "Restoring the full original selection…",
                lambda: self.library.clear_trim(history),
                self._edit_finished,
            )

    def _play_current(self) -> None:
        self._toggle_playback()

    def _audio_failed(self, error: Exception) -> None:
        # An unobservable player must never permit simultaneous microphone capture.
        self._audio_fault = True
        self._playing = self._paused = False
        try:
            self._player.close()
        except Exception as close_error:
            error = RuntimeError(f"{error}; audio cleanup also failed: {close_error}")
        self._operation_failed(error)

    def _stop_playback(self, *, rewind: bool = True) -> bool:
        if self._audio_fault:
            return False
        if self._player_loaded:
            try:
                self._player.stop()
            except Exception as error:
                self._audio_failed(error)
                return False
        self._playing = self._paused = False
        if self._audio is not None and rewind:
            self._position_frame = self.waveform.start_frame
            self._show_playhead()
        if hasattr(self, "_transport_timer"):
            self._update_controls()
        return True

    def _toggle_playback(self) -> None:
        if self._busy or self._audio is None or not self.play_button.isEnabled():
            return
        audio = self._audio
        if self._playing:
            try:
                self._player.pause()
                self._playing, self._paused = False, True
                self._update_controls()
            except Exception as error:
                self._audio_failed(error)
            return

        start, end = self.waveform.start_frame, self.waveform.end_frame

        def validated() -> bytes:
            # Revalidate live evidence before every start/resume, not just waveform loading.
            path = self.library.original_preview_path(audio.history)
            load_pcm_audio(path, expected_sha256=audio.clip.source_sha256, bin_count=1)
            return audio.clip.wav_bytes(start, end)

        def play(result: object) -> None:
            try:
                if self._playback is not None:
                    self._playback(audio.path)
                    return
                if not self._player_loaded:
                    self._player.load(cast(bytes, result))
                    self._player_loaded = True
                if self._position_frame >= self.waveform.end_frame:
                    self._position_frame = self.waveform.start_frame
                self._player.seek(
                    (self._position_frame - self.waveform.start_frame) / audio.clip.sample_rate
                )
                self._player.play()
                self._playing, self._paused = True, False
                self._update_controls()
            except Exception as error:
                self._audio_failed(error)

        self._submit("Validating the selected playback frames…", validated, play)

    def _delete_history(self) -> None:
        history = self._selected_history()
        if history is None or not self.delete_history_button.isEnabled():
            return
        answer = QMessageBox.question(
            self,
            "Delete this take?",
            f"Remove this take from usable recordings for {history.prompt_id}?\n"
            "The original remains recoverable. Use Restore to undo this action.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes or not self._discard_unsaved_trim():
            return
        session = self._session

        def remove() -> RecordingEdit:
            ending = session is not None and session.evidence.session_id == history.session_id
            if ending and session is not None:
                session.abort()
            try:
                return self.library.remove_take(
                    history, "Operator deleted an incorrect take in desktop"
                )
            except Exception as error:
                if ending:
                    raise _SessionTransitionError(
                        f"Batch saved for Resume; delete failed: {error}"
                    ) from error
                raise

        def removed(result: object) -> None:
            if session is not None and session.evidence.session_id == history.session_id:
                self._session = None
                self._state = None
            self._edit_finished(result)

        self._submit(
            "Removing take eligibility; retaining the original for Restore…", remove, removed
        )

    def _restore_history(self) -> None:
        history = self._selected_history()
        if history is not None and self.restore_history_button.isEnabled():
            self._submit(
                "Restoring the selected take…",
                lambda: self.library.restore_take(history),
                self._edit_finished,
            )

    def _retake_history(self) -> None:
        history = self._selected_history()
        device = self.device_combo.currentData()
        if (
            history is None
            or not isinstance(device, CaptureDevice)
            or not self.retake_history_button.isEnabled()
        ):
            return
        if not self._discard_unsaved_trim():
            return
        session = self._session
        self._clear_editor()

        def retake() -> tuple[DesktopRecordingSession, CaptureState]:
            if session is not None:
                session.abort()
            try:
                return self._open_selected_session((history.prompt_id,), device, history.prompt_id)
            except Exception as error:
                if session is not None:
                    raise _SessionTransitionError(
                        f"Previous batch saved for Resume; retake failed: {error}"
                    ) from error
                raise

        self._submit(
            "Preparing a new take for this exact prompt; retaining its history…",
            retake,
            self._session_opened,
        )

    def _finalize_session(self) -> None:
        if self._session is not None and self.finalize_button.isEnabled():

            def finalized(_result: object) -> None:
                self._session = None
                self._state = None
                self._idle_status = "Session finalized and saved in your recording library."
                self.capture_status.setText(self._idle_status)
                self.qc_label.clear()
                self.qc_label.hide()
                self.refresh_library()

            self._submit(
                "Finalizing and verifying the complete session…", self._session.finalize, finalized
            )

    def _end_session(self, *, all_recorded: bool = False) -> None:
        session = self._session
        if session is None or self._busy or self._is_playback_active():
            return
        if self._state is not None and self._state.status == "recording":
            answer = QMessageBox.question(
                self,
                "Stop recording?",
                "Stop this recording and keep the recoverable partial? "
                "You can resume the unfinished session from the library.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._close_after_end = False
                return
        self._stop_playback()
        if self._state is not None and self._state.status == "recording":
            self._capture_transition = "stopping"

        def end() -> None:
            try:
                session.abort()
            except CaptureInterrupted:
                # Aborting deliberately preserves a recoverable partial recording.
                pass

        def ended(_result: object) -> None:
            self._capture_transition = None
            self._pending_save_state = None
            self._session = None
            self._state = None
            self._idle_status = (
                "All prompts have recordings. Session saved for later review and finalization."
                if all_recorded
                else "Session saved for later. Choose Resume session to continue."
            )
            self.capture_status.setText(self._idle_status)
            self.qc_label.clear()
            self.qc_label.hide()
            if self._close_after_end:
                self.close()
            else:
                self.refresh_library()

        self._submit("Ending the session and preserving recordings…", end, ended)

    def _keyboard_action(self, action: Callable[[], None]) -> None:
        if self._shortcut_context_allowed() and not self._busy:
            action()

    def _shortcut_context_allowed(self) -> bool:
        if (
            QApplication.activeWindow() is not self
            or QApplication.activeModalWidget() is not None
            or QApplication.activePopupWidget() is not None
        ):
            return False
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QComboBox, QAbstractSpinBox)):
            return False
        if isinstance(focused, (QPlainTextEdit, QTextEdit)) and not focused.isReadOnly():
            return False
        return focused is None or focused.window() is self

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Handle transport keys only within this window, leaving ordinary editing untouched."""
        if (
            not isinstance(watched, QWidget)
            or watched.window() is not self
            or not isinstance(event, QKeyEvent)
        ):
            return super().eventFilter(watched, event)
        actions = {
            Qt.Key.Key_R: self._toggle_recording,
            Qt.Key.Key_Space: self._toggle_playback,
            Qt.Key.Key_Return: self._keep_take,
            Qt.Key.Key_Enter: self._keep_take,
        }
        action = actions.get(Qt.Key(event.key()))
        if (
            action is None
            or event.modifiers()
            not in {Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier}
            or not self._shortcut_context_allowed()
        ):
            return super().eventFilter(watched, event)
        if event.type() in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyRelease}:
            event.accept()
            return True
        if event.type() == QEvent.Type.KeyPress:
            event.accept()
            if not event.isAutoRepeat() and not self._busy:
                action()
            return True
        return super().eventFilter(watched, event)

    def _toggle_recording(self) -> None:
        if self.stop_button.isEnabled():
            self._stop_take()
        elif self.start_button.isEnabled():
            self._start_take()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Leave captures resumable and shut down the worker only after active work finishes."""
        if self._busy:
            self.message_label.setText(
                "Finishing the current operation. Close again when it completes."
            )
            event.ignore()
            return
        if not self._discard_unsaved_trim():
            event.ignore()
            return
        if self._is_playback_active() and not self._stop_playback():
            event.ignore()
            return
        if self._session is not None:
            self._close_after_end = True
            event.ignore()
            self._end_session()
            return
        try:
            self._player.close()
        except Exception as error:
            self._audio_failed(error)
            event.ignore()
            return
        self._poll_timer.stop()
        self._transport_timer.stop()
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)
        self._thread.quit()
        self._thread.wait()
        event.accept()
