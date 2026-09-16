"""Native UI checks with synthetic state: no microphone or human recording is used."""

from __future__ import annotations

import io
import os
import threading
import time
import wave
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from cole_voice_lab.capture import CaptureInterrupted, CaptureState
from cole_voice_lab.desktop_audio import PcmAudioClip
from cole_voice_lab.errors import ContractError
from cole_voice_lab.models import AudioQcMetrics, CaptureDevice
from cole_voice_lab.recording_edits import RecordingEdit
from cole_voice_lab.recording_library import (
    RecordingHistory,
    RecordingLibrary,
    RecordingRow,
    RecordingSessionInfo,
)
from cole_voice_lab.resources import load_prompt_suite

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtCore = pytest.importorskip("PySide6.QtCore")
QtGui = pytest.importorskip("PySide6.QtGui")
QtTest = pytest.importorskip("PySide6.QtTest")
desktop_module = pytest.importorskip("cole_voice_lab.desktop_window")
DesktopWindow = desktop_module.DesktopWindow

RECORDED_AT = datetime(2026, 8, 10, 17, 24, 35, tzinfo=UTC)
SYNTHETIC_PATH = Path("/synthetic/recording.wav")
SYNTHETIC_DEVICE = CaptureDevice(
    backend="coreaudio",
    device_id="synthetic-coreaudio-umik-1-uid",
    display_name="UMIK-1 synthetic input",
)
PILOT_SUITE = load_prompt_suite("pilot")
SPEECH_PROMPT = PILOT_SUITE.prompts[1]
NARRATION_PROMPT = next(
    prompt for prompt in PILOT_SUITE.prompts if prompt.prompt_id == "reference-narration-01"
)
ROOM_TONE_PROMPTS = tuple(
    prompt for prompt in PILOT_SUITE.prompts if prompt.category == "room-tone"
)
READING_PALETTE_BACKGROUNDS = ("#efefef", "#232527")
SYNTHETIC_CLIP = PcmAudioClip(
    sample_rate=48_000,
    frame_count=576_000,
    sample_width=3,
    channels=1,
    pcm=b"\x00\x10\x00\x00\xf0\xff" * 288_000,
    waveform_bins=((-1 / 2048, 1 / 2048),) * 64,
    source_sha256="a" * 64,
)


def color_contrast_ratio(foreground: object, background: object) -> float:
    def luminance(color: object) -> float:
        components = (color.redF(), color.greenF(), color.blueF())
        linear = tuple(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in components
        )
        return sum(
            weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear, strict=True)
        )

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("background", READING_PALETTE_BACKGROUNDS)
def test_delivery_instructions_remain_readable_in_light_and_dark_palettes(make_window, background):
    window = make_window()
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    palette = window.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(background))
    window.setPalette(palette)
    QtWidgets.QApplication.processEvents()

    foreground = window.style_label.palette().color(QtGui.QPalette.ColorRole.WindowText)
    assert color_contrast_ratio(foreground, QtGui.QColor(background)) >= 4.5


class SyntheticSession:
    def __init__(self, library: SyntheticLibrary, prompt_ids: Sequence[str]) -> None:
        self.library = library
        self.prompts = tuple(
            p
            for p in library.prompt_suite.prompts
            if p.prompt_id in prompt_ids or p.category == "room-tone"
        )
        self.index = 0
        self.phase = "ready"
        self.aborted = False
        self.finalized = False
        self.fail_start = False
        self.auto_stop = False
        self.start_count = 0
        self.stop_count = 0
        self.accept_count = 0
        self.reject_count = 0
        self.start_release: threading.Event | None = None
        self.stop_release: threading.Event | None = None
        self.abort_release: threading.Event | None = None
        self.fail_stop = False
        self.elapsed_seconds: float | None = library.initial_media_seconds
        self.recorded: set[int] = set()
        self.session_id = f"synthetic-session-{library.begin_count}"
        self.evidence = SimpleNamespace(
            session_id=self.session_id, selected_suite=SimpleNamespace(prompts=self.prompts)
        )

    def select_prompt(self, prompt_id: str) -> CaptureState:
        assert self.phase != "recording"
        if self.library.fail_select:
            raise ContractError("Synthetic prompt selection failed")
        self.index = next(
            i for i, prompt in enumerate(self.prompts) if prompt.prompt_id == prompt_id
        )
        self.phase = "review" if self.index in self.recorded else "ready"
        return self.current()

    def _retain_take(self) -> None:
        if self.index not in self.recorded:
            self.recorded.add(self.index)
            self.library.add_take(self.prompts[self.index].prompt_id, self.session_id)

    def current(self) -> CaptureState:
        self.library.worker_threads.append(threading.get_ident())
        if self.auto_stop:
            self.phase = "review"
            self._retain_take()
        prompt = self.prompts[self.index] if self.index < len(self.prompts) else None
        metrics = (
            AudioQcMetrics(
                path="audio/test.wav",
                duration_seconds=12.0,
                peak_dbfs=-8.0,
                rms_dbfs=-24.0,
                dc_offset=0.0,
                clipped_samples=0,
                longest_zero_run_ms=0.0,
                passed=True,
            )
            if self.phase == "review"
            else None
        )
        return CaptureState(
            session_directory=Path("/synthetic/session"),
            session_id=self.session_id,
            status=self.phase,
            index=self.index if prompt else None,
            total=len(self.prompts),
            prompt=prompt,
            kind="room-tone" if prompt and prompt.category == "room-tone" else "utterance",
            relative=f"audio/{prompt.prompt_id}.wav" if prompt else None,
            metrics=metrics,
            findings=("auto-stopped",) if self.auto_stop else (),
            elapsed_seconds=self.elapsed_seconds if self.phase == "recording" else None,
            takes=(),
        )

    def start_take(self) -> CaptureState:
        self.start_count += 1
        if self.start_release is not None:
            assert self.start_release.wait(5), "synthetic microphone start was not released"
        if self.fail_start:
            raise ContractError("Synthetic microphone is unavailable")
        self.phase = "recording"
        return self.current()

    def stop_take(self) -> CaptureState:
        self.stop_count += 1
        if self.stop_release is not None:
            assert self.stop_release.wait(5), "synthetic microphone stop was not released"
        if self.fail_stop:
            raise ContractError("Synthetic microphone stop failed")
        self.phase = "review"
        self._retain_take()
        return self.current()

    def accept_take(self) -> CaptureState:
        if self.library.fail_accept:
            raise ContractError("Synthetic Keep failed")
        self.accept_count += 1
        self.index = next(
            (i for i in range(len(self.prompts)) if i not in self.recorded), len(self.prompts)
        )
        self.phase = "complete" if self.index == len(self.prompts) else "ready"
        return self.current()

    def reject_take(self, reason: str) -> CaptureState:
        assert reason
        self.reject_count += 1
        self.recorded.discard(self.index)
        self.library.reject_take(self.prompts[self.index].prompt_id, self.session_id)
        self.phase = "ready"
        return self.current()

    def finalize(self) -> None:
        self.finalized = True

    def abort(self) -> None:
        self.aborted = True
        if self.abort_release is not None:
            assert self.abort_release.wait(5), "synthetic microphone abort was not released"
        if self.phase == "recording":
            self.phase = "ready"
            raise CaptureInterrupted(Path("/synthetic/session"), Path("/synthetic/partial.wav"))

    def preview_path(self) -> Path:
        return SYNTHETIC_PATH


class SyntheticLibrary:
    ffmpeg = "synthetic-ffmpeg"

    def __init__(self) -> None:
        self.prompt_suite = PILOT_SUITE
        self.worker_threads: list[int] = []
        self.fail_rows = False
        self.previewed: list[RecordingHistory] = []
        self.session: SyntheticSession | None = None
        self.begun_ids: tuple[str, ...] = ()
        self.begun_devices: list[CaptureDevice] = []
        self.resumed = False
        self.begin_count = 0
        self.begin_release: threading.Event | None = None
        self.initial_media_seconds: float | None = 3.0
        self.edits: dict[tuple[str, str], RecordingEdit] = {}
        self.saved_trims: list[tuple[RecordingHistory, int, int]] = []
        self.removed: list[RecordingHistory] = []
        self.restored: list[RecordingHistory] = []
        self.fail_preview = False
        self.fail_remove = False
        self.fail_begin = False
        self.fail_initial_start = False
        self.fail_select = False
        self.fail_save_trim = False
        self.fail_accept = False
        self.resume_phase = "ready"
        self.resume_prompt_id: str | None = None
        self.row_values = tuple(
            RecordingRow(p, "Missing", None, 0, ()) for p in PILOT_SUITE.prompts
        )

    def rows(self) -> tuple[RecordingRow, ...]:
        self.worker_threads.append(threading.get_ident())
        if self.fail_rows:
            raise ContractError("Synthetic history digest does not match")
        return self.row_values

    def sessions(self) -> tuple[RecordingSessionInfo, ...]:
        return (RecordingSessionInfo("old-session", RECORDED_AT, 3, 1, "Recorded", True),)

    def begin_session(self, prompt_ids: Sequence[str], device: CaptureDevice) -> SyntheticSession:
        if self.begin_release is not None:
            assert self.begin_release.wait(5), "synthetic session preparation was not released"
        if self.fail_begin:
            raise ContractError("Synthetic batch creation failed")
        assert "UMIK-1" in device.display_name
        assert device.backend == "coreaudio"
        self.begun_devices.append(device)
        self.begin_count += 1
        self.begun_ids = tuple(prompt_ids)
        self.session = SyntheticSession(self, prompt_ids)
        self.session.fail_start = self.fail_initial_start
        return self.session

    def resume_session(self, session_id: str) -> SyntheticSession:
        assert session_id == "old-session"
        self.resumed = True
        self.session = SyntheticSession(self, (PILOT_SUITE.prompts[1].prompt_id,))
        if self.resume_prompt_id is not None:
            self.session.select_prompt(self.resume_prompt_id)
        self.session.phase = self.resume_phase
        if self.resume_phase == "review":
            self.session._retain_take()
        return self.session

    def preview_path(self, history: RecordingHistory) -> Path:
        return self.original_preview_path(history)

    def original_preview_path(self, history: RecordingHistory) -> Path:
        if self.fail_preview:
            raise ContractError("Synthetic source digest no longer matches")
        self.previewed.append(history)
        return SYNTHETIC_PATH

    def add_take(self, prompt_id: str, session_id: str) -> None:
        history = RecordingHistory(
            session_id,
            prompt_id,
            RECORDED_AT + timedelta(days=1),
            "take",
            "Recorded",
            f"audio/{prompt_id}.wav",
            "b" * 64,
            Path("/synthetic/session"),
            next(prompt for prompt in PILOT_SUITE.prompts if prompt.prompt_id == prompt_id),
        )
        self.row_values = tuple(
            replace(
                row,
                status="Recorded",
                latest_recorded_at=history.recorded_at,
                take_count=row.take_count + 1,
                history=(history, *row.history),
            )
            if row.prompt.prompt_id == prompt_id
            else row
            for row in self.row_values
        )

    def reject_take(self, prompt_id: str, session_id: str) -> None:
        for row in self.row_values:
            if row.prompt.prompt_id == prompt_id:
                history = next(h for h in row.history if h.session_id == session_id)
                self._set_history_status(history, "Rejected")
                return

    def edit_state(self, history: RecordingHistory) -> RecordingEdit:
        key = (history.session_id, history.relative_path)
        return self.edits.get(key) or RecordingEdit(
            subject_id="cole-self",
            session_id=history.session_id,
            prompt_id=history.prompt_id,
            source_sha256=history.sha256,
            source_frames=SYNTHETIC_CLIP.frame_count,
            start_frame=0,
            end_frame=SYNTHETIC_CLIP.frame_count,
            updated_at=RECORDED_AT,
        )

    def save_trim(self, history: RecordingHistory, start: int, end: int) -> RecordingEdit:
        if self.fail_save_trim:
            raise ContractError("Synthetic trim save failed")
        self.saved_trims.append((history, start, end))
        trimmed = start != 0 or end != SYNTHETIC_CLIP.frame_count
        edit = RecordingEdit.model_validate(
            {
                **self.edit_state(history).model_dump(),
                "start_frame": start,
                "end_frame": end,
                "derived_path": "derived/recording-edits/synthetic.wav" if trimmed else None,
                "derived_sha256": "c" * 64 if trimmed else None,
            }
        )
        self.edits[(history.session_id, history.relative_path)] = edit
        return edit

    def clear_trim(self, history: RecordingHistory) -> RecordingEdit:
        return self.save_trim(history, 0, SYNTHETIC_CLIP.frame_count)

    def _set_history_status(self, history: RecordingHistory, status: str) -> None:
        updated = []
        for row in self.row_values:
            values = tuple(
                replace(item, status=status)
                if (item.session_id, item.relative_path)
                == (history.session_id, history.relative_path)
                else item
                for item in row.history
            )
            usable = tuple(item for item in values if item.status not in {"Removed", "Rejected"})
            updated.append(
                replace(
                    row,
                    history=values,
                    status=usable[0].status if usable else "Missing",
                    latest_recorded_at=usable[0].recorded_at if usable else None,
                )
            )
        self.row_values = tuple(updated)

    def remove_take(self, history: RecordingHistory, reason: str) -> RecordingEdit:
        if self.fail_remove:
            raise ContractError("Synthetic delete failed")
        self.removed.append(history)
        edit = RecordingEdit.model_validate(
            {
                **self.edit_state(history).model_dump(),
                "removed": True,
                "removal_reason": reason,
            }
        )
        self.edits[(history.session_id, history.relative_path)] = edit
        self._set_history_status(history, "Removed")
        return edit

    def restore_take(self, history: RecordingHistory) -> RecordingEdit:
        self.restored.append(history)
        edit = RecordingEdit.model_validate(
            {
                **self.edit_state(history).model_dump(),
                "removed": False,
                "removal_reason": None,
            }
        )
        self.edits[(history.session_id, history.relative_path)] = edit
        self._set_history_status(history, "Finalized")
        return edit

    def with_history(self) -> SyntheticLibrary:
        prompt = PILOT_SUITE.prompts[1]
        recent = RecordingHistory(
            "recent-session",
            prompt.prompt_id,
            RECORDED_AT,
            "take",
            "Finalized",
            "audio/test.wav",
            "a" * 64,
            Path("/synthetic/recent"),
            prompt,
        )
        old = replace(
            recent,
            session_id="old-session",
            recorded_at=RECORDED_AT - timedelta(days=90),
            date_precision="session",
            session_directory=Path("/synthetic/old"),
        )
        self.row_values = tuple(
            RecordingRow(row.prompt, "Finalized", RECORDED_AT, 2, (recent, old))
            if row.prompt.prompt_id == prompt.prompt_id
            else row
            for row in self.row_values
        )
        return self

    def with_recorded_prompts(self, prompt_ids: Sequence[str]) -> SyntheticLibrary:
        for prompt_id in prompt_ids:
            self.add_take(prompt_id, "earlier-session")
        return self

    def with_small_catalog(self) -> SyntheticLibrary:
        prompts = (ROOM_TONE_PROMPTS[0], SPEECH_PROMPT, ROOM_TONE_PROMPTS[1])
        self.prompt_suite = PILOT_SUITE.model_copy(update={"prompts": prompts})
        self.row_values = tuple(row for row in self.row_values if row.prompt in prompts)
        return self


class SyntheticPlayer:
    """Inert native-clock adapter: no frameworks, files, processes, or audio devices."""

    def __init__(self) -> None:
        self.loaded: list[bytes] = []
        self.started: list[float] = []
        self.seeks: list[float] = []
        self.pause_count = self.stop_count = self.close_count = 0
        self.position = 0.0
        self.playing = False
        self.fail_on: str | None = None

    def _check(self, action: str) -> None:
        if self.fail_on == action:
            raise ContractError(f"Synthetic audio {action} failed")

    def load(self, payload: bytes) -> None:
        self._check("load")
        self.loaded.append(payload)
        self.position = 0.0

    @property
    def duration_seconds(self) -> float:
        with wave.open(io.BytesIO(self.loaded[-1]), "rb") as reader:
            return reader.getnframes() / reader.getframerate()

    @property
    def position_seconds(self) -> float:
        self._check("clock")
        return self.position

    @property
    def is_playing(self) -> bool:
        self._check("state")
        return self.playing

    def play(self) -> None:
        self._check("play")
        self.started.append(self.position)
        self.playing = True

    def pause(self) -> None:
        self._check("pause")
        self.pause_count += 1
        self.playing = False

    def stop(self) -> None:
        self._check("stop")
        self.stop_count += 1
        self.playing = False
        self.position = 0.0

    def seek(self, seconds: float) -> None:
        self._check("seek")
        self.seeks.append(seconds)
        self.position = seconds

    def close(self) -> None:
        self._check("close")
        self.close_count += 1
        self.playing = False


@pytest.fixture(scope="module")
def qt_app() -> Iterator[object]:
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield application


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    assert predicate(), "native UI did not complete its asynchronous operation"


def settle(window: object) -> None:
    """Drain deferred waveform loads as well as the initial serialized operation."""
    for _ in range(3):
        QtWidgets.QApplication.processEvents()
        wait_until(lambda: not window._busy)


@pytest.fixture
def make_window(qt_app: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., object]]:
    del qt_app
    monkeypatch.setattr(
        desktop_module,
        "load_pcm_audio",
        lambda path, *, expected_sha256, bin_count=2048: replace(
            SYNTHETIC_CLIP, source_sha256=expected_sha256
        ),
    )
    windows: list[object] = []

    def create(
        library: SyntheticLibrary | None = None,
        *,
        devices: bool = True,
        device_loader: Callable[[], tuple[CaptureDevice, ...]] | None = None,
        use_default_device_loader: bool = False,
        requested_device: str | None = None,
        playback: Callable[[Path], None] | None = None,
    ) -> object:
        library = library or SyntheticLibrary()
        window = DesktopWindow(
            cast(RecordingLibrary, library),
            device_loader=None
            if use_default_device_loader
            else device_loader or (lambda: (SYNTHETIC_DEVICE,) if devices else ()),
            requested_device=requested_device,
            playback=playback,
            player_factory=SyntheticPlayer,
        )
        windows.append(window)
        window.show()
        wait_until(
            lambda: (
                not window._initial_scan
                and not window._busy
                and (window.device_combo.count() > 0 or window._failed)
            )
        )
        settle(window)
        return window

    yield create
    for window in windows:
        wait_until(lambda window=window: not window._busy)
        window._player.fail_on = None
        window._trim_dirty = False
        if window._session is not None:
            window._session.phase = "ready"
            window._state = window._session.current()
        window.close()
        wait_until(lambda window=window: not window._thread.isRunning())


def select_prompt(window: object, prompt_id: str) -> None:
    for index in range(window.recordings_table.rowCount()):
        item = window.recordings_table.item(index, 0)
        if item.data(QtCore.Qt.ItemDataRole.UserRole + 1) == prompt_id:
            window.recordings_table.selectRow(index)
            settle(window)
            return
    raise AssertionError(f"prompt absent from table: {prompt_id}")


def click_and_wait(button: object, window: object) -> None:
    assert button.isEnabled()
    button.click()
    settle(window)


def key_and_wait(window: object, key: object, *, target: object | None = None) -> None:
    if target is None:
        target = window.prompt_text if window.prompt_text.isVisible() else window.room_tone_panel
    window.activateWindow()
    target.setFocus()
    QtWidgets.QApplication.processEvents()
    QtTest.QTest.keyClick(target, key)
    settle(window)


def show_review(window: object) -> None:
    click_and_wait(window.record_selected_button, window)
    click_and_wait(window.start_button, window)
    click_and_wait(window.stop_button, window)


def test_full_catalog_browses_without_a_microphone(make_window: Callable[..., object]) -> None:
    window = make_window(devices=False)
    assert window.recordings_table.rowCount() == 36
    assert window.category_filter.count() == 9
    assert "0 of 36 prompts recorded" in window.summary_label.text()
    assert not window.record_selected_button.isEnabled()
    assert "without a microphone" in window.message_label.text()


def test_master_detail_has_one_large_script_and_optional_sortable_history_columns(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    assert window.main_splitter.orientation() == QtCore.Qt.Orientation.Horizontal
    assert window.main_splitter.count() == 2
    assert window.main_splitter.widget(0) is window.library_pane
    assert window.main_splitter.widget(1) is window.inspector_pane
    assert window.prompt_text is window.capture_prompt
    assert window.findChildren(QtWidgets.QPlainTextEdit) == [window.prompt_text]
    assert window.prompt_text.font().pointSizeF() >= 24
    assert window.font().pointSizeF() == 13
    assert window.style_label.font().italic()
    assert all(window.recordings_table.isColumnHidden(column) for column in (0, 4, 5, 6))
    window.show_history_columns.setChecked(True)
    assert all(not window.recordings_table.isColumnHidden(column) for column in (4, 5, 6))
    window.recordings_table.sortItems(6, QtCore.Qt.SortOrder.DescendingOrder)
    assert window.recordings_table.item(0, 6).text() == "2"
    assert "2026" in window.recordings_table.item(0, 4).text()
    assert "day" in window.recordings_table.item(0, 5).text()
    window.show_history_columns.setChecked(False)
    assert all(window.recordings_table.isColumnHidden(column) for column in (4, 5, 6))


def test_category_search_status_and_clear_preserve_full_catalog(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    window.status_filter.setCurrentText("Recorded")
    assert "1 shown" in window.summary_label.text()
    window.search_edit.setText("nonexistent prompt")
    assert "0 shown" in window.summary_label.text()
    window.clear_filters_button.click()
    window.category_filter.setCurrentIndex(window.category_filter.findData("technical"))
    expected = sum(p.category == "technical" for p in PILOT_SUITE.prompts)
    assert f"{expected} shown" in window.summary_label.text()
    assert window.recordings_table.rowCount() == 36
    window.clear_filters_button.click()
    assert "36 shown" in window.summary_label.text()


def test_history_dates_precision_and_playback_are_visible(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    played: list[Path] = []
    window = make_window(library, playback=played.append)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    assert window.prompt_text.toPlainText() == PILOT_SUITE.prompts[1].text
    assert window.history_table.rowCount() == 2
    precision = {window.history_table.item(index, 1).text() for index in range(2)}
    assert precision == {"Take time", "Session start only"}
    assert any("2026" in window.history_table.item(index, 0).text() for index in range(2))
    click_and_wait(window.play_button, window)
    assert played == [SYNTHETIC_PATH]
    assert len(library.previewed) == 2  # Original waveform load, then fresh playback validation.


def test_refresh_preserves_selected_prompt_after_sort(make_window: Callable[..., object]) -> None:
    window = make_window()
    table = window.recordings_table
    assert table.selectionMode() == QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
    select_prompt(window, PILOT_SUITE.prompts[3].prompt_id)
    selected = set(window._selected_prompt_ids())
    table.sortItems(2, QtCore.Qt.SortOrder.AscendingOrder)
    click_and_wait(window.refresh_button, window)
    assert set(window._selected_prompt_ids()) == selected


def test_record_selected_queues_roomtone_and_supports_retry_keep_finalize(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_small_catalog()
    played: list[Path] = []
    window = make_window(library, playback=played.append)
    prompt = PILOT_SUITE.prompts[1]
    select_prompt(window, prompt.prompt_id)
    click_and_wait(window.record_selected_button, window)
    assert library.begun_ids == (prompt.prompt_id,)
    assert window._state.status == "ready"
    assert window._state.prompt.prompt_id == prompt.prompt_id
    assert window.capture_prompt.toPlainText() == prompt.text
    assert not window.record_missing_button.isEnabled()
    click_and_wait(window.start_button, window)
    assert window.stop_button.isEnabled()
    assert not window.play_button.isEnabled()
    click_and_wait(window.stop_button, window)
    assert "QC passed" in window.qc_label.text()
    click_and_wait(window.play_button, window)
    assert played == [SYNTHETIC_PATH]
    click_and_wait(window.retry_button, window)
    assert window._state.status == "ready"
    while window._state.status != "complete":
        click_and_wait(window.start_button, window)
        click_and_wait(window.stop_button, window)
        click_and_wait(window.keep_button, window)
    click_and_wait(window.finalize_button, window)
    assert library.session.finalized
    assert window._session is None
    assert "finalized" in window.capture_status.text()


def test_record_missing_skips_historical_takes_and_runs_off_gui_thread(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    click_and_wait(window.record_missing_button, window)
    assert PILOT_SUITE.prompts[1].prompt_id not in library.begun_ids
    assert len(library.begun_ids) == 35
    assert set(library.worker_threads) != {threading.get_ident()}
    assert len(set(library.worker_threads)) == 1


def test_keep_advances_in_catalog_order_and_wraps_despite_previous_filters(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history().with_small_catalog()
    window = make_window(library)
    prompt = PILOT_SUITE.prompts[1]
    window.status_filter.setCurrentText("Recorded")
    window.search_edit.setText(prompt.prompt_id)
    select_prompt(window, prompt.prompt_id)
    click_and_wait(window.record_selected_button, window)
    expected = (prompt, ROOM_TONE_PROMPTS[1], ROOM_TONE_PROMPTS[0])
    assert len(expected) == 3
    assert not window.search_edit.isEnabled()
    assert not window.category_filter.isEnabled()
    assert not window.status_filter.isEnabled()
    assert library.session.start_count == 0
    for index, active_prompt in enumerate(expected):
        assert window._state.prompt.prompt_id == active_prompt.prompt_id
        assert window._selected_prompt_ids() == (active_prompt.prompt_id,)
        if active_prompt.category == "room-tone":
            assert window.prompt_text.toPlainText() == ""
            assert active_prompt.text in window.room_tone_instructions.text()
        else:
            assert window.prompt_text.toPlainText() == active_prompt.text
        assert window.history_table.rowCount() == (2 if index == 0 else 0)
        assert library.session.start_count == index
        assert window.start_button.isEnabled()
        click_and_wait(window.start_button, window)
        click_and_wait(window.stop_button, window)
        click_and_wait(window.keep_button, window)
    assert window._state.status == "complete"
    assert library.session.start_count == 3
    assert library.session.accept_count == 3
    assert window.finalize_button.isEnabled()
    assert not window.start_button.isEnabled()


@pytest.mark.parametrize("whole_catalog", [False, True], ids=["new-batch", "same-batch"])
@pytest.mark.parametrize("recorded_status", ["Recorded", "Finalized", "Accepted raw"])
def test_save_next_skips_two_roomtone_takes_and_recorded_speech(
    make_window: Callable[..., object], whole_catalog: bool, recorded_status: str
) -> None:
    recorded_ids = tuple(prompt.prompt_id for prompt in ROOM_TONE_PROMPTS)
    library = SyntheticLibrary().with_recorded_prompts(recorded_ids)
    for prompt_id in recorded_ids:
        library.add_take(prompt_id, "second-earlier-session")
    library.add_take(PILOT_SUITE.prompts[2].prompt_id, "earlier-session")
    library.row_values = tuple(
        replace(row, status=recorded_status) if row.status != "Missing" else row
        for row in library.row_values
    )
    history_before = {row.prompt.prompt_id: row.history for row in library.rows() if row.history}
    window = make_window(library)
    window.recordings_table.sortItems(2, QtCore.Qt.SortOrder.DescendingOrder)
    window.search_edit.setText(SPEECH_PROMPT.prompt_id)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(
        window.record_missing_button if whole_catalog else window.record_selected_button, window
    )
    saved_session = library.session
    click_and_wait(window.start_button, window)
    click_and_wait(window.stop_button, window)
    history = window._audio.history
    window._trim_changed(123, 456_789)
    click_and_wait(window.keep_button, window)

    assert window._state.prompt == PILOT_SUITE.prompts[3]
    assert window._selected_prompt_ids() == (PILOT_SUITE.prompts[3].prompt_id,)
    assert window.prompt_text.toPlainText() == PILOT_SUITE.prompts[3].text
    assert window._state.status == "ready"
    assert saved_session.accept_count == 1
    assert saved_session.start_count == 1
    assert saved_session.aborted is (not whole_catalog)
    assert library.session.start_count == (1 if whole_catalog else 0)
    assert library.saved_trims == [(history, 123, 456_789)]
    assert not window._trim_dirty
    for row in library.rows():
        if row.prompt.prompt_id in history_before:
            assert row.history == history_before[row.prompt.prompt_id]


@pytest.mark.parametrize("status", ["Rejected", "Removed"])
def test_save_next_records_prompt_with_only_unusable_history(
    make_window: Callable[..., object], status: str
) -> None:
    next_prompt = PILOT_SUITE.prompts[2]
    library = SyntheticLibrary().with_recorded_prompts((next_prompt.prompt_id,))
    history = library.row_values[2].history[0]
    library._set_history_status(history, status)
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)
    click_and_wait(window.keep_button, window)

    assert window._state.prompt == next_prompt
    assert window._state.status == "ready"
    assert library.row_values[2].take_count == 1
    assert library.row_values[2].history[0].status == status


@pytest.mark.parametrize("whole_catalog", [False, True], ids=["new-batch", "same-batch"])
@pytest.mark.parametrize("delayed_stage", ["snapshot", "selection"])
def test_save_next_keeps_saved_prompt_visible_until_final_target_is_ready(
    make_window: Callable[..., object],
    monkeypatch: pytest.MonkeyPatch,
    whole_catalog: bool,
    delayed_stage: str,
) -> None:
    library = SyntheticLibrary().with_recorded_prompts(
        tuple(prompt.prompt_id for prompt in ROOM_TONE_PROMPTS)
    )
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(
        window.record_missing_button if whole_catalog else window.record_selected_button, window
    )
    click_and_wait(window.start_button, window)
    click_and_wait(window.stop_button, window)
    saved_session = library.session
    saved_audio = window._audio
    saved_status = window.capture_status.text()
    window._trim_changed(123, 456_789)
    script_changes: list[str] = []
    window.prompt_text.textChanged.connect(
        lambda: script_changes.append(window.prompt_text.toPlainText())
    )
    entered, release = threading.Event(), threading.Event()
    target = library if delayed_stage == "snapshot" else window
    method = "rows" if delayed_stage == "snapshot" else "_select_session_prompt"
    original = getattr(target, method)

    def delayed(*args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(5), "synthetic navigation delay was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, delayed)
    window.keep_button.click()
    try:
        wait_until(entered.is_set)
        assert saved_session.accept_count == 1
        assert window._busy
        assert window._state.prompt == SPEECH_PROMPT
        assert window._selected_prompt_ids() == (SPEECH_PROMPT.prompt_id,)
        assert window.capture_status.text() == saved_status
        assert window.prompt_text.toPlainText() == SPEECH_PROMPT.text
        assert window.inspector_title.text() == "Read aloud"
        assert not window.room_tone_panel.isVisible()
        assert window._audio is saved_audio
        assert (window.waveform.start_frame, window.waveform.end_frame) == (123, 456_789)
        assert script_changes == []
        assert not window.start_button.isEnabled()
        assert not window.keep_button.isEnabled()
        assert not window.recordings_table.isEnabled()
    finally:
        release.set()
        settle(window)

    assert script_changes == [PILOT_SUITE.prompts[2].text]
    assert window._state.prompt == PILOT_SUITE.prompts[2]
    assert window._state.status == "ready"
    assert window._pending_save_state is None
    assert saved_session.start_count == 1
    assert library.session.start_count == (1 if whole_catalog else 0)


@pytest.mark.parametrize("failure", ["snapshot", "selection"])
def test_save_next_restores_backend_state_when_navigation_fails_after_saving(
    make_window: Callable[..., object], failure: str
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(window.record_missing_button, window)
    click_and_wait(window.start_button, window)
    click_and_wait(window.stop_button, window)
    saved_history = window._audio.history
    window._trim_changed(123, 456_789)
    if failure == "snapshot":
        library.fail_rows = True
    else:
        library.fail_select = True
    click_and_wait(window.keep_button, window)

    assert library.session.accept_count == 1
    assert window._state == library.session.current()
    assert window._pending_save_state is None
    assert window._audio is None
    assert not window._trim_dirty
    assert library.saved_trims == [(saved_history, 123, 456_789)]
    assert "Could not complete the operation" in window.message_label.text()
    assert window.end_button.isEnabled()
    assert not window.keep_button.isEnabled()


def test_save_next_wraps_to_earlier_unrecorded_prompt(
    make_window: Callable[..., object],
) -> None:
    missing = (SPEECH_PROMPT, ROOM_TONE_PROMPTS[1])
    library = SyntheticLibrary().with_recorded_prompts(
        tuple(prompt.prompt_id for prompt in PILOT_SUITE.prompts if prompt not in missing)
    )
    window = make_window(library)
    select_prompt(window, ROOM_TONE_PROMPTS[1].prompt_id)
    show_review(window)
    saved_session = library.session
    click_and_wait(window.keep_button, window)

    assert window._state.prompt == SPEECH_PROMPT
    assert window._state.status == "ready"
    assert saved_session.aborted
    assert library.session.start_count == 0


def test_save_next_stops_when_library_is_recorded_and_preserves_unfinished_batch(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_recorded_prompts(
        tuple(p.prompt_id for p in PILOT_SUITE.prompts if p != SPEECH_PROMPT)
    )
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)
    saved_session = library.session
    click_and_wait(window.keep_button, window)

    assert saved_session.accept_count == 1
    assert saved_session.aborted
    assert not saved_session.finalized
    assert library.begin_count == 1
    assert window._session is None
    assert window._state is None
    assert "All prompts have recordings" in window.capture_status.text()
    assert not window.keep_button.isEnabled()
    assert not window.finalize_button.isEnabled()


@pytest.mark.parametrize("failure", ["begin", "select"])
def test_save_next_preserves_saved_take_when_next_batch_fails(
    make_window: Callable[..., object], failure: str
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)
    saved_session = library.session
    history = window._audio.history
    window._trim_changed(123, 456_789)
    setattr(library, f"fail_{failure}", True)
    click_and_wait(window.keep_button, window)

    assert saved_session.accept_count == 1
    assert saved_session.aborted
    assert library.saved_trims == [(history, 123, 456_789)]
    assert not window._trim_dirty
    assert window._session is None
    assert "Previous batch saved for Resume" in window.message_label.text()
    assert library.session.start_count == (1 if failure == "begin" else 0)


def test_recording_inspector_cannot_be_retargeted_by_library_selection(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, PILOT_SUITE.prompts[5].prompt_id)
    click_and_wait(window.record_selected_button, window)
    click_and_wait(window.start_button, window)
    active_prompt = window._state.prompt
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    assert window._selected_row().prompt.prompt_id == active_prompt.prompt_id
    assert window.prompt_text.toPlainText() == active_prompt.text
    assert window.history_table.rowCount() == 0
    assert not window.play_button.isEnabled()


def test_resume_and_end_leave_session_recoverable(make_window: Callable[..., object]) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.resume_button, window)
    assert library.resumed
    click_and_wait(window.end_button, window)
    assert library.session.aborted
    assert "Resume session" in window.capture_status.text()
    assert window._session is None


def test_resume_preserves_unaccepted_review_without_reopening_the_microphone(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    library.resume_phase = "review"
    library.add_take(PILOT_SUITE.prompts[0].prompt_id, "synthetic-session-0")
    window = make_window(library)
    click_and_wait(window.resume_button, window)
    assert window._state.status == "review"
    assert library.session.start_count == 0
    assert window.keep_button.isEnabled()
    assert window.retry_button.isEnabled()
    assert window.play_button.isEnabled()
    assert not window.start_button.isEnabled()
    assert "Review" in window.state_label.text()


def test_backend_error_is_visible_and_does_not_start_polling(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_selected_button, window)
    library.session.fail_start = True
    click_and_wait(window.start_button, window)
    assert "Synthetic microphone is unavailable" in window.message_label.text()
    assert not window._poll_timer.isActive()
    assert window.end_button.isEnabled()
    assert window._state.status == "ready"
    assert "Recording" not in window.state_label.text()


def test_bad_history_error_preserves_existing_catalog(make_window: Callable[..., object]) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    library.fail_rows = True
    click_and_wait(window.refresh_button, window)
    assert "digest does not match" in window.message_label.text()
    assert window.recordings_table.rowCount() == 36


def test_rejected_attempts_do_not_count_as_completed_prompts(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    library.row_values = tuple(
        replace(row, status="Missing", latest_recorded_at=None) if row.take_count else row
        for row in library.row_values
    )
    window = make_window(library)
    assert "0 of 36 prompts recorded" in window.summary_label.text()
    assert window.progress_bar.value() == 0
    click_and_wait(window.record_missing_button, window)
    assert len(library.begun_ids) == 36


def test_slow_worker_keeps_gui_responsive_and_blocks_conflicting_actions(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    release = threading.Event()
    completed: list[object] = []
    window._submit(
        "Checking a synthetic slow operation…", lambda: release.wait(2), completed.append
    )
    assert not window.record_missing_button.isEnabled()
    window.search_edit.setText("technical")
    QtWidgets.QApplication.processEvents()
    assert window.search_edit.text() == "technical"
    window.close()
    assert window.isVisible()
    assert "Finishing the current operation" in window.message_label.text()
    release.set()
    wait_until(lambda: not window._busy)
    assert completed == [True]


@pytest.mark.parametrize("dimensions", [(1320, 860), (1060, 760), (1060, 700)])
def test_laptop_layout_keeps_details_and_long_capture_text_within_the_window(
    make_window: Callable[..., object],
    dimensions: tuple[int, int],
) -> None:
    window = make_window()
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    window.resize(*dimensions)
    assert (window.width(), window.height()) == dimensions
    click_and_wait(window.record_selected_button, window)
    prompt = window._state.prompt.model_copy(update={"text": "A long readable transcript. " * 74})
    window._show_state(replace(window._state, prompt=prompt))
    QtTest.QTest.qWait(30)
    assert window.capture_prompt.toPlainText() == prompt.text
    assert window.capture_prompt.height() > 90
    assert window.capture_prompt.verticalScrollBar().maximum() > 0
    assert window.prompt_text.mapTo(window, window.prompt_text.rect().bottomLeft()).y() < (
        window.style_label.mapTo(window, window.style_label.rect().topLeft()).y()
    )
    for phase in ("ready", "recording", "review"):
        window._show_state(replace(window._state, status=phase))
        QtTest.QTest.qWait(30)
        for button in (
            window.start_button,
            window.stop_button,
            window.play_button,
            window.stop_playback_button,
            window.retry_button,
            window.keep_button,
            window.finalize_button,
            window.end_button,
        ):
            if button.isVisible():
                bottom = button.mapTo(window, button.rect().bottomRight())
                assert bottom.x() < window.width()
                assert bottom.y() < window.height()


def test_recording_timer_updates_elapsed_without_resetting_script_scroll(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(window.record_selected_button, window)
    click_and_wait(window.start_button, window)
    prompt = window._state.prompt.model_copy(update={"text": "A long readable transcript. " * 74})
    state = replace(window._state, prompt=prompt)
    window._show_state(state)
    QtTest.QTest.qWait(30)
    scrollbar = window.prompt_text.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    position = scrollbar.value()
    assert position > 0
    window._show_state(replace(state, elapsed_seconds=65.0))
    assert "01:05" in window.elapsed_label.text()
    assert "Recording" in window.state_label.text()
    assert scrollbar.value() == position
    click_and_wait(window.stop_button, window)
    assert "Review" in window.state_label.text()
    assert not window._poll_timer.isActive()


def test_recording_poll_preserves_historical_selection_and_scroll(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    prompt = PILOT_SUITE.prompts[1]
    row = next(row for row in library.row_values if row.prompt.prompt_id == prompt.prompt_id)
    history = tuple(
        replace(
            row.history[0],
            session_id=f"synthetic-history-{index}",
            recorded_at=RECORDED_AT - timedelta(days=index),
        )
        for index in range(100)
    )
    library.row_values = tuple(
        replace(item, history=history, take_count=len(history)) if item is row else item
        for item in library.row_values
    )
    window = make_window(library)
    select_prompt(window, prompt.prompt_id)
    click_and_wait(window.record_selected_button, window)
    assert window._state.prompt.prompt_id == prompt.prompt_id
    window.history_caption.setChecked(True)
    window.history_table.selectRow(30)
    settle(window)
    click_and_wait(window.start_button, window)
    scrollbar = window.history_table.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    position = scrollbar.value()
    assert position > 0
    library.session.elapsed_seconds = 42.0
    window._poll()
    wait_until(lambda: not window._busy)
    assert scrollbar.value() == position
    assert window.history_table.selectionModel().selectedRows()[0].row() == 30


def test_automatic_stop_updates_review_and_stops_polling(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_selected_button, window)
    click_and_wait(window.start_button, window)
    library.session.auto_stop = True
    window._poll()
    wait_until(lambda: not window._busy)
    assert window._state.status == "review"
    assert "auto-stopped" in window.qc_label.text()
    assert not window._poll_timer.isActive()


def button_background_color(button: object) -> object:
    """Sample the rendered fill away from text and rounded corners at native scale."""
    rendered = button.grab().toImage()
    return rendered.pixelColor(round(4 * rendered.devicePixelRatio()), rendered.height() // 2)


@pytest.mark.parametrize("background", READING_PALETTE_BACKGROUNDS)
@pytest.mark.parametrize("prompt", [SPEECH_PROMPT, ROOM_TONE_PROMPTS[0]], ids=["speech", "silence"])
def test_stop_button_stays_red_during_recording_status_polls(
    make_window: Callable[..., object],
    monkeypatch: pytest.MonkeyPatch,
    background: str,
    prompt: object,
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    palette = window.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(background))
    window.setPalette(palette)
    select_prompt(window, prompt.prompt_id)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    session = library.session
    original_current = session.current
    entered, release = threading.Event(), threading.Event()

    def delayed_current() -> CaptureState:
        entered.set()
        assert release.wait(5), "synthetic recording poll was not released"
        return original_current()

    monkeypatch.setattr(session, "current", delayed_current)
    expected = QtGui.QColor("#d63b50" if background == "#232527" else "#b51f38")
    assert button_background_color(window.stop_button) == expected
    label = window.stop_button.text()
    for _ in range(3):
        entered.clear()
        release.clear()
        window._poll()
        try:
            wait_until(entered.is_set)
            assert window._busy
            assert window._state.status == "recording"
            assert window.stop_button.isVisible()
            assert window.stop_button.text() == label
            assert button_background_color(window.stop_button) == expected
            assert not window.stop_button.isEnabled()
            assert not window.recordings_table.isEnabled()
            assert session.stop_count == 0
        finally:
            release.set()
            settle(window)
        assert window.stop_button.isEnabled()
        assert button_background_color(window.stop_button) == expected


@pytest.mark.parametrize("background", READING_PALETTE_BACKGROUNDS)
def test_stop_button_uses_disabled_appearance_during_actual_capture_shutdown(
    make_window: Callable[..., object], background: str
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    palette = window.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(background))
    window.setPalette(palette)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    active_color = button_background_color(window.stop_button)
    release = threading.Event()
    library.session.stop_release = release
    window.stop_button.click()
    try:
        wait_until(lambda: library.session.stop_count == 1)
        assert window._capture_transition == "stopping"
        assert not window.stop_button.isEnabled()
        assert button_background_color(window.stop_button) != active_color
        assert window.state_label.text() == "Stopping — checking audio"
    finally:
        release.set()
        settle(window)
    assert window._state.status == "review"
    assert not window.stop_button.isVisible()


def test_keyboard_shortcuts_do_not_trigger_while_editing_search(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    window.activateWindow()
    window.search_edit.setFocus()
    called: list[bool] = []
    window._keyboard_action(lambda: called.append(True))
    assert called == []
    window.recordings_table.setFocus()
    window._keyboard_action(lambda: called.append(True))
    assert called == [True]


def test_keyboard_record_stop_keep_and_retry_never_automatically_start_next_take(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_missing_button, window)
    key_and_wait(window, QtCore.Qt.Key.Key_Space)
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert library.session.start_count == 0
    assert library.session.accept_count == 0
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._state.status == "recording"
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    key_and_wait(window, QtCore.Qt.Key.Key_Space)
    assert library.session.stop_count == 0
    assert library.session.accept_count == 0
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._state.status == "review"
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert window._state.status == "ready"
    assert library.session.start_count == 1
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    click_and_wait(window.retry_button, window)
    assert window._state.status == "ready"
    assert library.session.start_count == 2
    assert library.session.reject_count == 1


@pytest.mark.parametrize(
    "widget_class",
    [
        QtWidgets.QLineEdit,
        QtWidgets.QPlainTextEdit,
        QtWidgets.QTextEdit,
        QtWidgets.QSpinBox,
        QtWidgets.QComboBox,
    ],
    ids=["line-edit", "plain-text", "rich-text", "spin-box", "combo-box"],
)
def test_transport_keys_ignore_editable_controls_and_combos(
    make_window: Callable[..., object], widget_class: Callable[..., object]
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_selected_button, window)
    control = widget_class(window)
    control.show()
    try:
        key_and_wait(window, QtCore.Qt.Key.Key_R, target=control)
        assert library.session.start_count == 0
        click_and_wait(window.start_button, window)
        click_and_wait(window.stop_button, window)
        key_and_wait(window, QtCore.Qt.Key.Key_Space, target=control)
        key_and_wait(window, QtCore.Qt.Key.Key_Return, target=control)
        assert window._player.started == []
        assert library.session.accept_count == 0
    finally:
        control.close()
        control.deleteLater()


@pytest.mark.parametrize(
    "event_type", [QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride]
)
def test_transport_keys_ignore_autorepeat_and_modified_keys(
    make_window: Callable[..., object], event_type: object
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(window.record_selected_button, window)
    window.prompt_text.setFocus()
    for modifiers, repeat in (
        (QtCore.Qt.KeyboardModifier.NoModifier, True),
        (QtCore.Qt.KeyboardModifier.ControlModifier, False),
        (QtCore.Qt.KeyboardModifier.MetaModifier, False),
        (QtCore.Qt.KeyboardModifier.AltModifier, False),
        (QtCore.Qt.KeyboardModifier.ShiftModifier, False),
    ):
        event = QtGui.QKeyEvent(event_type, QtCore.Qt.Key.Key_R, modifiers, "r", repeat)
        QtWidgets.QApplication.sendEvent(window.prompt_text, event)
    QtWidgets.QApplication.processEvents()
    assert library.session.start_count == 0


def test_transport_keys_ignore_dialogs_and_popups(make_window: Callable[..., object]) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_selected_button, window)
    dialog = QtWidgets.QDialog(window)
    dialog.setModal(True)
    dialog.show()
    try:
        QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_R)
        assert library.session.start_count == 0
    finally:
        dialog.close()
    menu = QtWidgets.QMenu(window)
    menu.addAction("Synthetic menu item")
    menu.popup(window.mapToGlobal(QtCore.QPoint(30, 30)))
    try:
        QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_R)
        assert library.session.start_count == 0
    finally:
        menu.close()
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert library.session.start_count == 1


def test_pending_microphone_start_blocks_keyboard_and_conflicting_actions(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(window.record_selected_button, window)
    release = threading.Event()
    library.session.start_release = release
    window.prompt_text.setFocus()
    QtTest.QTest.keyClick(window.prompt_text, QtCore.Qt.Key.Key_R)
    try:
        wait_until(lambda: library.session.start_count == 1)
        assert window._busy
        assert window.state_label.text() == "Starting microphone — wait"
        assert window.elapsed_label.text() == "00:00"
        assert not window.start_button.isEnabled()
        assert not window.stop_button.isEnabled()
        QtTest.QTest.keyClick(window.prompt_text, QtCore.Qt.Key.Key_R)
        QtTest.QTest.keyClick(window.prompt_text, QtCore.Qt.Key.Key_Return)
        assert library.session.start_count == 1
        assert library.session.accept_count == 0
        assert not window._poll_timer.isActive()
    finally:
        release.set()
        wait_until(lambda: not window._busy)
    assert window._state.status == "recording"
    assert window.stop_button.isEnabled()


def test_space_pauses_native_playback_and_stop_releases_recording_interlocks(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    show_review(window)
    saved_session = library.session
    key_and_wait(window, QtCore.Qt.Key.Key_Space)
    player = window._player
    assert player.started == [0.0]
    assert len(player.loaded) == 1
    assert not window.keep_button.isEnabled()
    assert not window.retry_button.isEnabled()
    assert "Playing" in window.state_label.text()
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert library.session.accept_count == 0
    key_and_wait(window, QtCore.Qt.Key.Key_Space)
    assert player.pause_count == 1
    assert "Paused" in window.state_label.text()
    assert not window.keep_button.isEnabled()
    assert not window.retry_button.isEnabled()
    assert not window.end_button.isEnabled()
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert library.session.accept_count == 0
    click_and_wait(window.stop_playback_button, window)
    assert player.stop_count >= 1
    assert window.keep_button.isEnabled()
    assert "Review" in window.state_label.text()
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert saved_session.accept_count == 1
    assert library.session.start_count == 0


@pytest.mark.parametrize("stopped_position", [0.0, 3.5, 12.0])
def test_playback_uses_native_position_and_reports_honest_stopped_state(
    make_window: Callable[..., object], stopped_position: float
) -> None:
    window = make_window()
    show_review(window)
    click_and_wait(window.play_button, window)
    assert "Playing" in window.state_label.text()
    assert window.elapsed_label.text() == "00:00"
    window._player.position = 5.25
    window._render_transport_state()
    assert window.elapsed_label.text() == "00:05"
    assert window.playback_slider.value() == 252_000
    assert window.waveform.position_frame == 252_000
    assert window._transport_timer.isActive()
    window._player.position = stopped_position
    window._player.playing = False
    window._render_transport_state()
    assert window.message_label.text() == "Playback stopped."
    assert window.playback_slider.value() == round(stopped_position * 48_000)
    assert window.elapsed_label.text() == "00:12"
    assert not window._transport_timer.isActive()


def test_historical_playback_blocks_capture_and_pause_preserves_position(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    key_and_wait(window, QtCore.Qt.Key.Key_Space)
    assert len(library.previewed) == 2
    assert not window.record_selected_button.isEnabled()
    assert not window.resume_button.isEnabled()
    window._begin_session((PILOT_SUITE.prompts[1].prompt_id,))
    window._resume_session()
    assert library.session is None
    window._player.position = 4.0
    window._render_transport_state()
    key_and_wait(window, QtCore.Qt.Key.Key_Space)
    assert window._player.pause_count == 1
    assert not window.record_selected_button.isEnabled()
    key_and_wait(window, QtCore.Qt.Key.Key_Space)
    assert window._player.started == [0.0, 4.0]
    assert len(window._player.loaded) == 1
    assert len(library.previewed) == 3  # Resume revalidates source, too.
    click_and_wait(window.stop_playback_button, window)
    assert window.record_selected_button.isEnabled()
    assert window.playback_slider.value() == 0


@pytest.mark.parametrize("failure", ["load", "play", "seek", "pause", "clock", "state", "stop"])
def test_native_audio_failures_are_visible_and_latch_capture_closed(
    make_window: Callable[..., object], failure: str
) -> None:
    window = make_window()
    show_review(window)
    if failure not in {"load", "play", "seek"}:
        click_and_wait(window.play_button, window)
    window._player.fail_on = failure
    if failure in {"load", "play", "seek"}:
        click_and_wait(window.play_button, window)
    elif failure == "pause":
        click_and_wait(window.play_button, window)
    elif failure == "stop":
        window._stop_playback()
    else:
        window._render_transport_state()
    assert f"Synthetic audio {failure} failed" in window.message_label.text()
    assert window._audio_fault
    assert "Audio error" in window.state_label.text()
    assert not window.keep_button.isEnabled()
    assert not window.start_button.isEnabled()
    assert not window.play_button.isEnabled()
    assert not window.end_button.isEnabled()
    assert window._player.close_count == 1
    assert not window._poll_timer.isActive()


@pytest.mark.parametrize("active_session", [False, True], ids=["library", "active-session"])
def test_close_during_native_playback_stops_player_and_preserves_active_session(
    make_window: Callable[..., object], active_session: bool
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    if active_session:
        show_review(window)
        click_and_wait(window.play_button, window)
    else:
        click_and_wait(window.play_button, window)
    window.close()
    wait_until(lambda: not window._thread.isRunning())
    assert window._player.stop_count >= 1
    assert window._player.close_count >= 1
    assert not window.isVisible()
    if active_session:
        assert library.session.aborted


def test_close_during_capture_confirms_and_preserves_partial(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_selected_button, window)
    click_and_wait(window.start_button, window)
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.No
    )
    window.close()
    assert window.isVisible()
    assert not library.session.aborted
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.Yes
    )
    window.close()
    wait_until(lambda: not window._thread.isRunning())
    assert library.session.aborted
    assert not window.isVisible()


def test_record_key_targets_highlighted_prompt_and_stop_automatically_loads_original_waveform(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    prompt = PILOT_SUITE.prompts[5]
    select_prompt(window, prompt.prompt_id)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._state.status == "recording"
    assert window._state.prompt == prompt
    assert library.session.start_count == 1
    assert window._audio is None
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._state.status == "review"
    assert window._audio.history.prompt_id == prompt.prompt_id
    assert window._audio.history.session_id == library.session.session_id
    assert window._audio.clip.frame_count == SYNTHETIC_CLIP.frame_count
    assert window.waveform.end_frame == SYNTHETIC_CLIP.frame_count
    assert window.playback_slider.maximum() == SYNTHETIC_CLIP.frame_count
    assert window.play_button.isEnabled()


def test_trim_preview_crops_exact_source_frames_and_maps_seek_to_native_clock(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    window.waveform.trim_changed.emit(48_001, 240_003)
    assert window._trim_dirty
    assert window.save_trim_button.isEnabled()
    window.waveform.seek_requested.emit(96_007)
    assert window._position_frame == 96_007
    click_and_wait(window.play_button, window)
    player = window._player
    assert player.seeks[-1] == pytest.approx((96_007 - 48_001) / 48_000)
    with wave.open(io.BytesIO(player.loaded[-1]), "rb") as reader:
        assert reader.getnframes() == 240_003 - 48_001
        assert (
            reader.readframes(reader.getnframes()) == SYNTHETIC_CLIP.pcm[48_001 * 3 : 240_003 * 3]
        )
    assert not window.save_trim_button.isEnabled()
    assert not window.trim_start.isEnabled()
    player.position = 1.25
    window._render_transport_state()
    assert window.playback_slider.value() == 48_001 + 60_000
    window.playback_slider.setValue(144_001)
    assert player.seeks[-1] == 2.0
    window._seek_frame(0)
    assert player.seeks[-1] == 0.0
    window._seek_frame(SYNTHETIC_CLIP.frame_count)
    assert window._position_frame == 240_003
    click_and_wait(window.stop_playback_button, window)
    assert window.playback_slider.value() == 48_001


def test_apply_and_reset_trim_preserve_original_and_reopen_persisted_bounds(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    prompt = PILOT_SUITE.prompts[1]
    select_prompt(window, prompt.prompt_id)
    original_pcm = window._audio.clip.pcm
    window._trim_changed(48_000, 480_000)
    click_and_wait(window.save_trim_button, window)
    assert library.saved_trims[-1][1:] == (48_000, 480_000)
    assert not window._trim_dirty
    assert window._audio.edit.trimmed
    assert window._audio.clip.pcm == original_pcm
    select_prompt(window, PILOT_SUITE.prompts[2].prompt_id)
    assert window._audio is None
    select_prompt(window, prompt.prompt_id)
    assert (window.waveform.start_frame, window.waveform.end_frame) == (48_000, 480_000)
    click_and_wait(window.reset_trim_button, window)
    assert not window._audio.edit.trimmed
    assert (window.waveform.start_frame, window.waveform.end_frame) == (0, 576_000)
    assert window._audio.clip.pcm == original_pcm


def test_trim_fields_reject_empty_range_and_restore_visible_valid_values(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    window.trim_start.setValue(2.0)
    assert window.waveform.start_frame == 96_000
    window.trim_end.setValue(1.0)
    assert "end must be after" in window.message_label.text()
    assert window.trim_end.value() == 12.0
    assert window.waveform.end_frame == 576_000


def test_keep_saves_current_take_trim_before_advancing_without_recording(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    show_review(window)
    saved_session = library.session
    current_history = window._audio.history
    window._trim_changed(123, 456_789)
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert library.saved_trims == [(current_history, 123, 456_789)]
    assert saved_session.accept_count == 1
    assert saved_session.start_count == 1
    assert library.session.start_count == 0
    assert window._state.status == "ready"
    assert window._state.prompt == PILOT_SUITE.prompts[2]


def test_keep_does_not_silently_apply_a_historical_take_trim_to_current_take(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    show_review(window)
    window.history_caption.setChecked(True)
    window.history_table.selectRow(1)
    settle(window)
    assert window._audio.history.session_id == "recent-session"
    window._trim_changed(48_000, 480_000)
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert library.session.accept_count == 0
    assert library.saved_trims == []
    assert not window.keep_button.isEnabled()
    assert not window.retry_button.isEnabled()
    assert window.save_trim_button.isEnabled()


@pytest.mark.parametrize("discard", [False, True])
def test_switching_prompt_with_unsaved_trim_requires_explicit_discard(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch, discard: bool
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    prompt = PILOT_SUITE.prompts[1]
    other = PILOT_SUITE.prompts[2]
    select_prompt(window, prompt.prompt_id)
    click_and_wait(window.record_selected_button, window)
    original_session = library.session
    window._trim_changed(48_000, 480_000)
    answer = (
        QtWidgets.QMessageBox.StandardButton.Discard
        if discard
        else QtWidgets.QMessageBox.StandardButton.Cancel
    )
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args: answer)
    select_prompt(window, other.prompt_id)
    if discard:
        assert original_session.aborted
        assert window._state.prompt == other
        assert library.session.start_count == 0
        assert window._audio is None
    else:
        assert not original_session.aborted
        assert window._state.prompt == prompt
        assert window._selected_prompt_ids() == (prompt.prompt_id,)
        assert window._trim_dirty


def test_review_prompt_switch_preserves_draft_and_returns_to_its_waveform(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_missing_button, window)
    click_and_wait(window.start_button, window)
    click_and_wait(window.stop_button, window)
    recorded_prompt = window._state.prompt
    original_session = library.session
    select_prompt(window, PILOT_SUITE.prompts[5].prompt_id)
    assert library.session is original_session
    assert not original_session.aborted
    assert window._state.status == "ready"
    assert window._audio is None
    assert library.session.start_count == 1
    select_prompt(window, recorded_prompt.prompt_id)
    assert window._state.status == "review"
    assert window._audio.history.prompt_id == recorded_prompt.prompt_id
    assert library.session.start_count == 1


@pytest.mark.parametrize("confirm", [False, True])
def test_delete_is_confirmed_recoverable_and_restore_keeps_original_and_trim(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch, confirm: bool
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    window._trim_changed(48_000, 480_000)
    click_and_wait(window.save_trim_button, window)
    original = window._audio.clip.pcm
    answer = (
        QtWidgets.QMessageBox.StandardButton.Yes
        if confirm
        else QtWidgets.QMessageBox.StandardButton.No
    )
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args: answer)
    click_and_wait(window.delete_history_button, window)
    if not confirm:
        assert library.removed == []
        assert not window._audio.edit.removed
        return
    assert window._audio.edit.removed
    assert window._audio.clip.pcm == original
    assert not window.play_button.isEnabled()
    assert window.restore_history_button.isEnabled()
    assert window.history_table.rowCount() == 2
    click_and_wait(window.restore_history_button, window)
    assert len(library.restored) == 1
    assert not window._audio.edit.removed
    assert window._audio.edit.trimmed
    assert window._audio.clip.pcm == original
    assert window.play_button.isEnabled()


def test_retake_prepares_exact_historical_prompt_in_new_session_without_capture(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    prompt = PILOT_SUITE.prompts[1]
    select_prompt(window, prompt.prompt_id)
    click_and_wait(window.record_selected_button, window)
    previous = library.session
    click_and_wait(window.retake_history_button, window)
    assert previous.aborted
    assert library.session is not previous
    assert window._state.prompt == prompt
    assert library.session.start_count == 0
    assert window.history_table.rowCount() == 2


def test_deleting_current_take_ends_batch_and_preserves_original_for_restore(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    show_review(window)
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.Yes
    )
    click_and_wait(window.delete_history_button, window)
    assert library.session.aborted
    assert window._session is None
    assert window._audio.edit.removed
    assert "0 of 36 prompts recorded" in window.summary_label.text()
    click_and_wait(window.restore_history_button, window)
    assert "1 of 36 prompts recorded" in window.summary_label.text()


@pytest.mark.parametrize("operation", ["delete", "retake", "switch"])
def test_failed_session_transition_clears_closed_session_and_preserves_error(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    show_review(window)
    previous = library.session
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.Yes
    )
    if operation == "delete":
        library.fail_remove = True
        click_and_wait(window.delete_history_button, window)
    elif operation == "retake":
        library.fail_begin = True
        click_and_wait(window.retake_history_button, window)
    else:
        library.fail_begin = True
        select_prompt(window, PILOT_SUITE.prompts[5].prompt_id)
    assert previous.aborted
    assert window._session is None
    assert window._state is None
    assert "failed" in window.message_label.text()
    assert "Resume" in window.message_label.text()


def test_playback_revalidates_source_before_resume_and_refuses_changed_bytes(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    click_and_wait(window.play_button, window)
    click_and_wait(window.play_button, window)
    library.fail_preview = True
    click_and_wait(window.play_button, window)
    assert "source digest no longer matches" in window.message_label.text()
    assert len(window._player.started) == 1
    assert window._paused
    assert not window.start_button.isEnabled()
    click_and_wait(window.stop_playback_button, window)
    assert not window._is_playback_active()


def test_paused_playback_is_stopped_before_switching_inspector_context(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    click_and_wait(window.play_button, window)
    click_and_wait(window.play_button, window)
    select_prompt(window, PILOT_SUITE.prompts[5].prompt_id)
    assert not window._is_playback_active()
    assert window._player.stop_count >= 1
    assert window._audio is None
    assert window.prompt_text.toPlainText() == PILOT_SUITE.prompts[5].text


def test_initial_history_waveform_does_not_swallow_scheduled_microphone_discovery(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    library.add_take(PILOT_SUITE.prompts[0].prompt_id, "synthetic-prior-session")
    window = make_window(library)
    assert window.device_combo.currentData().display_name == SYNTHETIC_DEVICE.display_name
    assert window._audio is not None
    assert not window.start_button.isEnabled()
    assert window.retake_history_button.isVisible() and window.retake_history_button.isEnabled()
    click_and_wait(window.retake_history_button, window)
    assert window._state.status == "ready"
    assert window.start_button.isVisible() and window.start_button.isEnabled()


def test_record_from_idle_preserves_created_session_when_microphone_start_fails(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    library.fail_initial_start = True
    window = make_window(library)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._session is library.session
    assert window._state.status == "ready"
    assert "microphone is unavailable" in window.message_label.text()
    assert not window._poll_timer.isActive()
    assert window.end_button.isEnabled()
    click_and_wait(window.end_button, window)
    assert library.session.aborted


def test_new_session_is_aborted_when_initial_prompt_selection_fails(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    library.fail_select = True
    window = make_window(library)
    click_and_wait(window.record_selected_button, window)
    assert library.session.aborted
    assert window._session is None
    assert "prompt selection failed" in window.message_label.text()


def test_stop_focuses_new_current_take_instead_of_previous_historical_selection(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    window.history_caption.setChecked(True)
    window.history_table.selectRow(1)
    settle(window)
    assert window._audio.history.session_id == "old-session"
    show_review(window)
    assert window._audio.history.session_id == library.session.session_id
    assert window._audio.history.relative_path == window._state.relative
    assert window.history_table.selectionModel().selectedRows()[0].row() == 0


@pytest.mark.parametrize("failure", ["save_trim", "accept"])
def test_failed_keep_preserves_review_and_unsaved_trim_for_retry(
    make_window: Callable[..., object], failure: str
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    show_review(window)
    saved_session = library.session
    window._trim_changed(123, 456_789)
    setattr(library, f"fail_{failure}", True)
    click_and_wait(window.keep_button, window)
    assert "failed" in window.message_label.text()
    assert window._state.status == "review"
    assert window._trim_dirty
    assert (window.waveform.start_frame, window.waveform.end_frame) == (123, 456_789)
    assert window.keep_button.isEnabled()
    setattr(library, f"fail_{failure}", False)
    click_and_wait(window.keep_button, window)
    assert window._state.status == "ready"
    assert saved_session.start_count == 1
    assert library.session.start_count == 0


def test_close_with_unsaved_trim_can_be_cancelled_then_discarded(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, PILOT_SUITE.prompts[1].prompt_id)
    window._trim_changed(48_000, 480_000)
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.Cancel
    )
    window.close()
    assert window.isVisible()
    assert window._thread.isRunning()
    assert window._trim_dirty
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args: QtWidgets.QMessageBox.StandardButton.Discard,
    )
    window.close()
    wait_until(lambda: not window._thread.isRunning())
    assert not window.isVisible()


@pytest.mark.parametrize("prompt", ROOM_TONE_PROMPTS, ids=lambda prompt: prompt.prompt_id)
def test_room_tone_is_a_silent_instruction_card_not_a_spoken_script(
    make_window: Callable[..., object], prompt: object
) -> None:
    window = make_window()
    select_prompt(window, prompt.prompt_id)
    assert window.inspector_title.text() == "Silent room tone"
    assert window.prompt_content.currentWidget() is window.room_tone_panel
    assert window.room_tone_panel.isVisible()
    assert window.room_tone_heading.text() == "Do not speak"
    assert (
        window.room_tone_duration.text()
        == f"Record about {prompt.expected_seconds:g} seconds of silence."
    )
    assert prompt.text in window.room_tone_instructions.text()
    assert prompt.style in window.room_tone_instructions.text()
    assert "instructions" in window.room_tone_instructions.text().casefold()
    assert window.prompt_text.toPlainText() == ""
    assert not window.prompt_text.isVisible()
    assert not window.style_label.isVisible()
    assert "Record silence" in window.start_button.text()
    assert "Stop silent recording" in window.stop_button.text()
    assert window._session is None
    assert window.state_label.text() == "Ready"
    assert window._player.started == []


def test_library_labels_silent_tasks_without_modifying_spoken_text_or_corpus_identity(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    original_catalog = library.prompt_suite.model_dump_json()
    window = make_window(library)
    table = window.recordings_table
    assert table.horizontalHeaderItem(2).text() == "Recording task"
    for prompt in (*ROOM_TONE_PROMPTS, SPEECH_PROMPT):
        select_prompt(window, prompt.prompt_id)
        cell = table.item(table.selectionModel().selectedRows()[0].row(), 2)
        if prompt.category == "room-tone":
            assert cell.text() == (
                f"Silent room tone · {prompt.prompt_id} · "
                f"{prompt.expected_seconds:g} s — do not speak"
            )
            assert not cell.icon().isNull()
        else:
            assert cell.text() == prompt.text
    assert library.prompt_suite.model_dump_json() == original_catalog
    assert load_prompt_suite("pilot").model_dump_json() == original_catalog
    assert tuple(row.prompt for row in library.row_values) == PILOT_SUITE.prompts


@pytest.mark.parametrize("prompt", ROOM_TONE_PROMPTS, ids=lambda prompt: prompt.prompt_id)
def test_room_tone_shortcuts_record_review_and_keep_without_starting_the_next_take(
    make_window: Callable[..., object], prompt: object
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    select_prompt(window, prompt.prompt_id)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._state.prompt == prompt
    assert window._state.status == "recording"
    assert window.state_label.text() == "Recording silence"
    assert window.room_tone_panel.isVisible()
    assert not window.prompt_text.isVisible()
    assert library.session.start_count == 1
    window._show_state(replace(window._state, elapsed_seconds=65.0))
    assert window.elapsed_label.text() == "01:05"
    assert window.state_label.text() == "Recording silence"
    assert window.stop_button.isEnabled()
    assert library.session.stop_count == 0  # Expected duration is guidance, not a new auto-stop.
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._state.status == "review"
    assert window.state_label.text() == "Review"
    assert window.room_tone_panel.isVisible()
    assert window._audio.history.prompt_id == prompt.prompt_id
    saved_session = library.session
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert saved_session.accept_count == 1
    assert saved_session.start_count == 1
    assert window._state.status == "ready"
    if prompt == ROOM_TONE_PROMPTS[0]:
        assert window._state.prompt == SPEECH_PROMPT
        assert library.session.start_count == 0
        assert window.prompt_text.isVisible()
        assert not window.room_tone_panel.isVisible()
    else:
        assert window._state.prompt == ROOM_TONE_PROMPTS[0]
        assert window.room_tone_panel.isVisible()
        assert not window.prompt_text.isVisible()
    assert window.start_button.isEnabled()


@pytest.mark.parametrize("prompt", ROOM_TONE_PROMPTS, ids=lambda prompt: prompt.prompt_id)
@pytest.mark.parametrize("phase", ["ready", "review"])
def test_resumed_room_tone_restores_silent_instructions_without_capture(
    make_window: Callable[..., object], prompt: object, phase: str
) -> None:
    library = SyntheticLibrary()
    library.resume_prompt_id = prompt.prompt_id
    library.resume_phase = phase
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(window.resume_button, window)
    assert window._state.prompt == prompt
    assert window._state.status == phase
    assert window.room_tone_panel.isVisible()
    assert window.prompt_text.toPlainText() == ""
    assert window.inspector_title.text() == "Silent room tone"
    assert library.session.start_count == 0
    assert window.start_button.isEnabled() == (phase == "ready")
    assert window.keep_button.isEnabled() == (phase == "review")
    click_and_wait(window.end_button, window)
    assert library.session.aborted
    assert window._session is None
    assert window.room_tone_panel.isVisible()


def test_switching_between_silence_and_speech_restores_only_exact_read_aloud_text(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    for prompt in (SPEECH_PROMPT, *ROOM_TONE_PROMPTS, SPEECH_PROMPT):
        select_prompt(window, prompt.prompt_id)
        if prompt.category == "room-tone":
            assert window.prompt_text.toPlainText() == ""
            assert window.prompt_text.accessibleName() == "Recording task"
            assert window.room_tone_panel.isVisible()
        else:
            assert window.inspector_title.text() == "Read aloud"
            assert window.prompt_content.currentWidget() is window.prompt_text
            assert window.prompt_text.isVisible()
            assert window.prompt_text.toPlainText() == prompt.text
            assert window.prompt_text.accessibleName() == "Spoken script — read aloud"
            assert not window.room_tone_panel.isVisible()
            assert window.style_label.isVisible()
            assert window.style_label.text().startswith("Delivery guidance:")
            assert prompt.style in window.style_label.text()
            assert "silence" not in window.start_button.text().casefold()
            assert "silent" not in window.stop_button.text().casefold()


@pytest.mark.parametrize("phase", ["browse", "ready", "recording", "review", "paused"])
def test_narration_guidance_describes_delivery_without_contradicting_read_aloud(
    make_window: Callable[..., object], phase: str
) -> None:
    window = make_window()
    select_prompt(window, NARRATION_PROMPT.prompt_id)
    if phase != "browse":
        click_and_wait(window.record_selected_button, window)
    if phase in {"recording", "review", "paused"}:
        click_and_wait(window.start_button, window)
    if phase in {"review", "paused"}:
        click_and_wait(window.stop_button, window)
    if phase == "paused":
        click_and_wait(window.play_button, window)
        click_and_wait(window.play_button, window)

    assert window.inspector_title.text() == "Read aloud"
    assert window.prompt_text.toPlainText() == NARRATION_PROMPT.text
    assert window.style_label.text() == (
        "Delivery guidance: Calm audiobook narration.  Approx. length: 13 seconds (not a limit)."
    )
    assert "do not read aloud" not in window.style_label.text().casefold()
    assert window.style_label.accessibleName() == "Delivery guidance for the script"
    assert "not part of the spoken script" in window.style_label.accessibleDescription()
    assert "natural pace" in window.style_label.toolTip()
    assert window.style_label.accessibleDescription() == window.style_label.toolTip()


def test_delivery_accessibility_clears_when_speech_changes_to_silence_or_batch_complete(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    select_prompt(window, NARRATION_PROMPT.prompt_id)
    for prompt in (*ROOM_TONE_PROMPTS, NARRATION_PROMPT):
        select_prompt(window, prompt.prompt_id)
        if prompt.category == "room-tone":
            assert not window.style_label.isVisible()
            assert window.style_label.toolTip() == window.style_label.accessibleDescription() == ""
            assert window.room_tone_heading.text() == "Do not speak"
        else:
            assert window.style_label.isVisible()
            assert window.style_label.accessibleDescription()
    click_and_wait(window.record_selected_button, window)
    window._show_state(replace(window._state, status="complete", prompt=None))
    assert window.style_label.text() == "Earlier recordings remain in your library."
    assert window.style_label.accessibleName() == "Recording guidance"
    assert window.style_label.toolTip() == window.style_label.accessibleDescription() == ""


@pytest.mark.parametrize("background", READING_PALETTE_BACKGROUNDS)
def test_narration_delivery_guidance_fits_compact_prompt_card_in_both_palettes(
    make_window: Callable[..., object], background: str
) -> None:
    window = make_window()
    palette = window.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(background))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(background))
    window.setPalette(palette)
    window.resize(1060, 700)
    select_prompt(window, NARRATION_PROMPT.prompt_id)
    show_review(window)
    QtTest.QTest.qWait(30)
    guidance = window.style_label
    assert guidance.height() >= guidance.heightForWidth(guidance.width())
    assert guidance.mapTo(window.prompt_card, guidance.rect().bottomRight()).y() < (
        window.prompt_card.height()
    )
    assert window.prompt_text.mapTo(window, window.prompt_text.rect().bottomLeft()).y() < (
        guidance.mapTo(window, guidance.rect().topLeft()).y()
    )
    assert window.prompt_card.geometry().bottom() < window.editor_card.geometry().top()


@pytest.mark.parametrize("category", ["room-tone", "neutral"])
def test_task_presentation_uses_category_and_custom_duration_not_prompt_name(
    make_window: Callable[..., object], category: str
) -> None:
    library = SyntheticLibrary()
    custom = SPEECH_PROMPT.model_copy(
        update={
            "prompt_id": "custom-silence-check"
            if category == "room-tone"
            else "room-tone-custom-speech",
            "category": category,
            "text": "Custom words & instructions <not markup> stay exact.",
            "style": "Custom delivery instruction; preserve exactly.",
            "expected_seconds": 7.5,
        }
    )
    library.prompt_suite = PILOT_SUITE.model_copy(
        update={"prompts": (*PILOT_SUITE.prompts, custom)}
    )
    library.row_values = (*library.row_values, RecordingRow(custom, "Missing", None, 0, ()))
    window = make_window(library)
    select_prompt(window, custom.prompt_id)
    click_and_wait(window.record_selected_button, window)
    assert window._state.prompt == custom
    assert library.session.start_count == 0
    if category == "room-tone":
        assert window.room_tone_duration.text() == "Record about 7.5 seconds of silence."
        assert custom.text in window.room_tone_instructions.text()
        assert custom.style in window.room_tone_instructions.text()
        assert window.room_tone_instructions.textFormat() == QtCore.Qt.TextFormat.PlainText
        assert window.room_tone_panel.isVisible()
        assert window.prompt_text.toPlainText() == ""
    else:
        assert window.prompt_text.toPlainText() == custom.text
        assert window.prompt_text.isVisible()
        assert window.inspector_title.text() == "Read aloud"
        assert "Approx. length: 7.5 seconds (not a limit)." in window.style_label.text()


@pytest.mark.parametrize("background", READING_PALETTE_BACKGROUNDS)
@pytest.mark.parametrize("dimensions", [(1320, 860), (1060, 700)])
def test_silent_instruction_card_and_transport_remain_visible_in_compact_themed_layouts(
    make_window: Callable[..., object], background: str, dimensions: tuple[int, int]
) -> None:
    window = make_window()
    window.resize(*dimensions)
    palette = window.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(background))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(background))
    palette.setColor(
        QtGui.QPalette.ColorRole.WindowText,
        QtGui.QColor("#f5f5f5" if background == "#232527" else "#202020"),
    )
    window.setPalette(palette)
    select_prompt(window, ROOM_TONE_PROMPTS[0].prompt_id)
    click_and_wait(window.record_selected_button, window)
    assert window.room_tone_heading.font().pointSizeF() >= 24
    for phase in ("ready", "recording", "review"):
        window._show_state(replace(window._state, status=phase))
        QtWidgets.QApplication.processEvents()
        assert (window.width(), window.height()) == dimensions
        for widget in (
            window.inspector_title,
            window.room_tone_heading,
            window.room_tone_duration,
            window.room_tone_instructions,
            window.start_button,
            window.stop_button,
            window.keep_button,
            window.end_button,
        ):
            if widget.isVisible():
                top = widget.mapTo(window, widget.rect().topLeft())
                bottom = widget.mapTo(window, widget.rect().bottomRight())
                assert 0 <= top.x() <= bottom.x() < window.width()
                assert 0 <= top.y() <= bottom.y() < window.height()
        assert not window.prompt_text.isVisible()
        assert not window.style_label.isVisible()


def test_silent_card_accessibility_exposes_instructions_and_hides_read_aloud_editor(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    select_prompt(window, ROOM_TONE_PROMPTS[0].prompt_id)
    panel = QtGui.QAccessible.queryAccessibleInterface(window.room_tone_panel)
    heading = QtGui.QAccessible.queryAccessibleInterface(window.room_tone_heading)
    editor = QtGui.QAccessible.queryAccessibleInterface(window.prompt_text)
    assert "silent" in panel.text(QtGui.QAccessible.Text.Name).casefold()
    assert heading.text(QtGui.QAccessible.Text.Name) == "Do not speak"
    assert not heading.state().invisible
    assert editor.state().invisible
    for button in (window.start_button, window.stop_button):
        accessible = QtGui.QAccessible.queryAccessibleInterface(button)
        assert "silent" in accessible.text(QtGui.QAccessible.Text.Name).casefold()
    instructions = QtGui.QAccessible.queryAccessibleInterface(window.room_tone_instructions)
    exposed = " ".join(
        instructions.text(role)
        for role in (
            QtGui.QAccessible.Text.Name,
            QtGui.QAccessible.Text.Description,
            QtGui.QAccessible.Text.Value,
        )
    )
    assert ROOM_TONE_PROMPTS[0].text in exposed
    assert ROOM_TONE_PROMPTS[0].style in exposed


def test_complete_batch_and_empty_selection_do_not_claim_to_be_read_aloud_scripts(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    window.recordings_table.clearSelection()
    settle(window)
    assert window.inspector_title.text() == "Recording task"
    assert not window.room_tone_panel.isVisible()
    assert window.prompt_text.toPlainText() == ""
    assert window.prompt_text.accessibleName() == "Recording task"
    select_prompt(window, ROOM_TONE_PROMPTS[0].prompt_id)
    click_and_wait(window.record_selected_button, window)
    window._show_state(replace(window._state, prompt=None, status="complete", index=None))
    assert window.inspector_title.text() == "Recording task"
    assert window.prompt_text.accessibleName() == "Recording task"
    assert not window.room_tone_panel.isVisible()
    assert window.finalize_button.isEnabled()
    assert not window.start_button.isEnabled()


def test_keep_initial_silence_advances_to_exact_speech_without_starting_microphone(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    click_and_wait(window.record_missing_button, window)
    assert window._state.prompt == ROOM_TONE_PROMPTS[0]
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert library.session.start_count == 1
    assert window._state.status == "ready"
    assert window._state.prompt == SPEECH_PROMPT
    assert window.inspector_title.text() == "Read aloud"
    assert window.prompt_text.toPlainText() == SPEECH_PROMPT.text
    assert window.prompt_text.isVisible()
    assert not window.room_tone_panel.isVisible()
    assert "silent" not in window.stop_button.text().casefold()
    assert "silence" not in window.start_button.text().casefold()


def test_long_silence_instructions_scroll_without_hiding_transport_or_resetting_on_poll(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    original = ROOM_TONE_PROMPTS[0]
    prompt = original.model_copy(
        update={
            "text": "Stay silent and do not touch the microphone or desk. " * 35,
            "style": "Keep your usual recording position; these are instructions only.",
        }
    )
    library.prompt_suite = PILOT_SUITE.model_copy(
        update={
            "prompts": tuple(
                prompt if item.prompt_id == prompt.prompt_id else item
                for item in PILOT_SUITE.prompts
            ),
        }
    )
    library.row_values = tuple(
        replace(row, prompt=prompt) if row.prompt.prompt_id == prompt.prompt_id else row
        for row in library.row_values
    )
    window = make_window(library)
    window.resize(1060, 700)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    scrollbar = window.room_tone_panel.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum() // 2)
    position = scrollbar.value()
    assert position > 0
    window._show_state(replace(window._state, elapsed_seconds=8.0))
    QtWidgets.QApplication.processEvents()
    assert scrollbar.value() == position
    assert prompt.text in window.room_tone_instructions.text()
    assert "Record about 15 seconds" in window.room_tone_duration.text()
    assert window.state_label.text() == "Recording silence"
    assert window.stop_button.isVisible() and window.stop_button.isEnabled()
    assert window.stop_button.mapTo(window, window.stop_button.rect().bottomRight()).y() < 700
    assert window.prompt_text.toPlainText() == ""


def test_silent_transport_ignores_autorepeated_record_key_on_instruction_card(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    window.activateWindow()
    window.room_tone_panel.setFocus()
    event = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress,
        QtCore.Qt.Key.Key_R,
        QtCore.Qt.KeyboardModifier.NoModifier,
        "r",
        True,
    )
    QtWidgets.QApplication.sendEvent(window.room_tone_panel, event)
    settle(window)
    assert library.session is None
    assert window.room_tone_panel.isVisible()
    assert window.state_label.text() == "Ready"


def test_silent_card_text_retains_contrast_through_light_dark_light_palette_changes(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    select_prompt(window, ROOM_TONE_PROMPTS[0].prompt_id)
    for background, foreground in (
        ("#efefef", "#202020"),
        ("#232527", "#f5f5f5"),
        ("#efefef", "#202020"),
    ):
        palette = window.palette()
        for role in (QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Base):
            palette.setColor(role, QtGui.QColor(background))
        for role in (QtGui.QPalette.ColorRole.WindowText, QtGui.QPalette.ColorRole.Text):
            palette.setColor(role, QtGui.QColor(foreground))
        window.setPalette(palette)
        QtWidgets.QApplication.processEvents()
        card_background = window.palette().color(QtGui.QPalette.ColorRole.Base)
        for label in (
            window.room_tone_heading,
            window.room_tone_duration,
            window.room_tone_instructions,
            window.room_tone_help,
        ):
            actual = label.palette().color(QtGui.QPalette.ColorRole.WindowText)
            assert color_contrast_ratio(actual, card_background) >= 4.5, (
                label.text(),
                actual.name(),
                card_background.name(),
            )
        assert window.room_tone_instructions.font().pointSizeF() == 13.0
        assert window.room_tone_help.font().pointSizeF() == 13.0


def test_idle_record_announces_startup_before_session_preparation_finishes(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    library.initial_media_seconds = None
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    release = threading.Event()
    library.begin_release = release
    window.start_button.click()
    try:
        assert window._busy
        assert window._session is None
        assert window.state_label.text() == "Starting microphone — wait"
        assert window.elapsed_label.text() == "00:00"
        assert not window.recordings_table.isEnabled()
        assert not window._transport_timer.isActive()
    finally:
        release.set()
        settle(window)
    assert window._session is library.session
    assert library.session.start_count == 1
    assert window._state.status == "recording"
    assert window._capture_transition is None
    assert window.state_label.text() == "Starting microphone — wait"
    assert window.stop_button.isEnabled()


@pytest.mark.parametrize("prompt", [SPEECH_PROMPT, ROOM_TONE_PROMPTS[0]], ids=["speech", "silence"])
@pytest.mark.parametrize("media_seconds", [None, 0.0], ids=["unknown-media", "zero-media"])
def test_startup_waits_for_confirmed_audio_and_allows_stop_without_retargeting(
    make_window: Callable[..., object], prompt: object, media_seconds: float | None
) -> None:
    library = SyntheticLibrary()
    library.initial_media_seconds = media_seconds
    window = make_window(library)
    select_prompt(window, prompt.prompt_id)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._state.status == "recording"
    assert window.state_label.text() == "Starting microphone — wait"
    assert window.elapsed_label.text() == "00:00"
    assert window.elapsed_label.accessibleName() == "Captured audio duration"
    assert not window.recordings_table.isEnabled()
    assert not window.start_button.isEnabled()
    assert window.stop_button.isEnabled()
    select_prompt(window, PILOT_SUITE.prompts[5].prompt_id)
    assert window._state.prompt == prompt
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert library.session.stop_count == 1
    assert window._state.status == "review"
    assert window._capture_transition is None


@pytest.mark.parametrize("prompt", [SPEECH_PROMPT, ROOM_TONE_PROMPTS[0]], ids=["speech", "silence"])
def test_first_confirmed_media_changes_startup_to_recording_without_wall_clock_extrapolation(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch, prompt: object
) -> None:
    wall_clock = [100.0]
    # Keep this guard even though recording no longer imports a wall clock: a future
    # interpolation implementation must not silently reintroduce startup/QC seconds.
    monkeypatch.setattr(desktop_module, "monotonic", lambda: wall_clock[0], raising=False)
    library = SyntheticLibrary()
    library.initial_media_seconds = None
    window = make_window(library)
    select_prompt(window, prompt.prompt_id)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    wall_clock[0] += 2.0
    window._render_transport_state()
    assert window.state_label.text() == "Starting microphone — wait"
    assert window.elapsed_label.text() == "00:00"
    library.session.elapsed_seconds = 0.25
    window._poll()
    settle(window)
    assert window.state_label.text() == (
        "Recording silence" if prompt.category == "room-tone" else "Recording"
    )
    assert window.elapsed_label.text() == "00:00"
    library.session.elapsed_seconds = 9.75
    window._poll()
    settle(window)
    assert window.elapsed_label.text() == "00:09"
    for instant in (110.0, 200.0, 10_000.0):
        wall_clock[0] = instant
        window._render_transport_state()
        assert window.elapsed_label.text() == "00:09"
    assert not window._transport_timer.isActive()
    assert library.session.start_count == 1
    assert window._poll_timer.isActive()


def test_stop_freezes_confirmed_media_while_worker_checks_audio_then_shows_verified_duration(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    wall_clock = [100.0]
    monkeypatch.setattr(desktop_module, "monotonic", lambda: wall_clock[0], raising=False)
    library = SyntheticLibrary()
    library.initial_media_seconds = 11.75
    window = make_window(library)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window.elapsed_label.text() == "00:11"
    release = threading.Event()
    library.session.stop_release = release
    window.stop_button.click()
    try:
        assert window.state_label.text() == "Stopping — checking audio"
        assert window.elapsed_label.text() == "00:11"
        wait_until(lambda: library.session.stop_count == 1)
        assert window._busy
        wall_clock[0] += 3.0
        window._render_transport_state()
        assert window.elapsed_label.text() == "00:11"
        assert window.state_label.text() == "Stopping — checking audio"
        assert not window._transport_timer.isActive()
        assert not window.stop_button.isEnabled()
        assert not window.keep_button.isEnabled()
    finally:
        release.set()
        settle(window)
    assert window._capture_transition is None
    assert window.state_label.text() == "Review"
    assert window.elapsed_label.text() == "00:12"
    assert window._state.metrics.duration_seconds == 12.0
    assert "12.0s" in window.qc_label.text()
    assert window._audio.clip.duration_seconds == 12.0


@pytest.mark.parametrize("closing", [False, True], ids=["end-session", "close-window"])
def test_end_and_close_freeze_media_duration_while_preserving_capture_partial(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch, closing: bool
) -> None:
    library = SyntheticLibrary()
    library.initial_media_seconds = 9.75
    window = make_window(library)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    release = threading.Event()
    library.session.abort_release = release
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.Yes
    )
    window.close() if closing else window.end_button.click()
    try:
        wait_until(lambda: library.session.aborted)
        assert window._busy
        assert window.state_label.text() == "Stopping — checking audio"
        for _ in range(3):
            window._render_transport_state()
            assert window.elapsed_label.text() == "00:09"
        assert not window._transport_timer.isActive()
    finally:
        release.set()
        settle(window)
    assert window._capture_transition is None
    assert window._session is None
    if closing:
        wait_until(lambda: not window._thread.isRunning())
        assert not window.isVisible()
    else:
        assert "Resume session" in window.capture_status.text()


def test_capture_failures_clear_transient_status_and_keep_recoverable_controls(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    library.fail_initial_start = True
    window = make_window(library)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._capture_transition is None
    assert window.state_label.text() == "Needs attention"
    assert window.elapsed_label.text() == "00:00"
    assert window.end_button.isEnabled()
    library.session.fail_start = False
    library.session.elapsed_seconds = 8.25
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    library.session.fail_stop = True
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert window._capture_transition is None
    assert window.state_label.text() == "Needs attention"
    assert window.elapsed_label.text() == "00:08"
    assert "stop failed" in window.message_label.text()
    assert window.stop_button.isEnabled()
    assert not window._poll_timer.isActive()
    assert not window._transport_timer.isActive()
    library.session.fail_stop = False
    click_and_wait(window.stop_button, window)
    assert window.state_label.text() == "Review"


def test_progress_poll_failure_preserves_stop_and_abort_without_inventing_elapsed_time(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    library = SyntheticLibrary()
    library.initial_media_seconds = 8.25
    window = make_window(library)
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    session = library.session
    original_current = session.current

    def checked_current() -> CaptureState:
        if session.phase == "recording":
            raise ContractError("Synthetic encoded-media progress is malformed")
        return original_current()

    monkeypatch.setattr(session, "current", checked_current)
    window._poll()
    settle(window)
    assert window._session is session
    assert window._state.status == "recording"
    assert window._capture_transition is None
    assert window.state_label.text() == "Needs attention"
    assert window.elapsed_label.text() == "00:08"
    assert "progress is malformed" in window.message_label.text()
    assert window.stop_button.isEnabled()
    assert window.end_button.isEnabled()
    assert not window._poll_timer.isActive()
    assert not window._transport_timer.isActive()
    click_and_wait(window.stop_button, window)
    assert session.stop_count == 1
    assert window._state.status == "review"
    assert window.state_label.text() == "Review"
    assert window.elapsed_label.text() == "00:12"


def test_desktop_native_device_picker_preserves_requested_uid_backend_and_metadata(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    other_microphone = SYNTHETIC_DEVICE.model_copy(
        update={
            "device_id": "synthetic-built-in-uid",
            "display_name": "Built-in Microphone",
        }
    )
    selected = SYNTHETIC_DEVICE.model_copy(
        update={
            "device_id": "AppleUSBAudioEngine:miniDSP:UMIK-1:synthetic-second-uid:1,0",
            "microphone_serial": "synthetic-second-microphone",
            "calibration_sha256": "d" * 64,
        }
    )
    window = make_window(
        library,
        device_loader=lambda: (other_microphone, SYNTHETIC_DEVICE, selected),
        requested_device=selected.device_id,
    )
    assert window.device_combo.count() == 2
    assert window.device_combo.currentData() is selected
    assert window.device_combo.currentData().backend == "coreaudio"
    assert selected.device_id in window.device_combo.itemData(
        window.device_combo.currentIndex(), QtCore.Qt.ItemDataRole.ToolTipRole
    )
    click_and_wait(window.record_selected_button, window)
    assert library.begun_devices == [selected]
    assert library.begun_devices[0].microphone_serial == "synthetic-second-microphone"
    assert library.begun_devices[0].calibration_sha256 == "d" * 64
    assert library.session.start_count == 0


def test_default_native_enumeration_and_rescan_run_on_worker_without_starting_capture(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    scans: list[int] = []

    def native_devices() -> tuple[CaptureDevice, ...]:
        scans.append(threading.get_ident())
        return (SYNTHETIC_DEVICE,)

    monkeypatch.setattr(desktop_module, "list_coreaudio_input_devices", native_devices)
    library = SyntheticLibrary()
    window = make_window(library, use_default_device_loader=True)
    assert window.device_combo.currentData() is SYNTHETIC_DEVICE
    assert window.device_combo.currentData().backend == "coreaudio"
    click_and_wait(window.scan_button, window)
    assert len(scans) == 2
    assert len(set(scans)) == 1
    assert scans[0] != threading.get_ident()
    assert library.session is None
    assert library.begin_count == 0
    assert library.begun_devices == []
    assert window._player.started == []


def test_missing_native_umik_keeps_library_browsable_without_a_capture_fallback(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    built_in = SYNTHETIC_DEVICE.model_copy(
        update={
            "device_id": "synthetic-built-in-uid",
            "display_name": "Built-in Microphone",
        }
    )
    monkeypatch.setattr(desktop_module, "list_coreaudio_input_devices", lambda: (built_in,))
    library = SyntheticLibrary()
    window = make_window(library, use_default_device_loader=True)
    assert window.recordings_table.rowCount() == len(PILOT_SUITE.prompts)
    assert window.device_combo.currentData() is None
    assert "No UMIK-1 found" in window.device_combo.currentText()
    assert "without a microphone" in window.message_label.text()
    assert not window.start_button.isEnabled()
    assert not window.record_selected_button.isEnabled()
    assert not window.record_missing_button.isEnabled()
    assert window.scan_button.isEnabled()
    assert library.session is None


def test_native_device_discovery_failure_is_visible_and_can_be_retried_without_capture(
    make_window: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = [True]
    scans: list[bool] = []

    def native_devices() -> tuple[CaptureDevice, ...]:
        scans.append(failed[0])
        if failed[0]:
            raise ContractError("Synthetic Core Audio device enumeration failed")
        return (SYNTHETIC_DEVICE,)

    monkeypatch.setattr(desktop_module, "list_coreaudio_input_devices", native_devices)
    library = SyntheticLibrary()
    window = make_window(library, use_default_device_loader=True)
    assert "Core Audio device enumeration failed" in window.message_label.text()
    assert not window.start_button.isEnabled()
    assert not window.record_selected_button.isEnabled()
    assert library.session is None
    failed[0] = False
    click_and_wait(window.scan_button, window)
    assert scans == [True, False]
    assert window.device_combo.currentData() is SYNTHETIC_DEVICE
    assert window.start_button.isEnabled()
    assert library.begin_count == 0


def _select_take(window: object, session_id: str, *, settle_after: bool = True) -> None:
    """Select a synthetic take by durable identity rather than its displayed row label."""
    for index in range(window.history_table.rowCount()):
        history = window.history_table.item(index, 0).data(QtCore.Qt.ItemDataRole.UserRole + 1)
        if history.session_id == session_id:
            window.history_table.selectRow(index)
            if settle_after:
                settle(window)
            return
    raise AssertionError(f"synthetic take absent: {session_id}")


def _take_position_key(history: RecordingHistory) -> tuple[str, str, str]:
    return history.session_id, history.relative_path, history.sha256


@pytest.mark.parametrize("dimensions", [(1320, 860), (1060, 700)])
def test_editor_cards_keep_takes_and_transport_together_above_fixed_actions(
    make_window: Callable[..., object], dimensions: tuple[int, int]
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    window.resize(*dimensions)
    QtWidgets.QApplication.processEvents()

    assert window.prompt_card.isAncestorOf(window.prompt_text)
    assert window.prompt_card.isAncestorOf(window.style_label)
    for child in (window.history_table, window.waveform, window.transport_panel):
        assert window.editor_card.isAncestorOf(child)
    for child in (window.keep_button, window.finalize_button, window.end_button):
        assert window.action_bar.isAncestorOf(child)
    assert window.history_caption.isChecked()
    assert window.history_table.isVisible()
    assert window.history_table.isColumnHidden(1) and window.history_table.isColumnHidden(3)
    assert not window.history_table.isColumnHidden(0)
    assert window.history_table.item(0, 0).text().startswith("Take ")
    assert window.play_button.minimumHeight() >= 44
    assert not hasattr(window, "play_history_button")
    assert window.history_table.mapTo(window, window.history_table.rect().bottomLeft()).y() < (
        window.waveform.mapTo(window, window.waveform.rect().topLeft()).y()
    )
    for widget in (
        window.history_table,
        window.waveform,
        window.trim_start,
        window.trim_end,
        window.skip_back_button,
        window.play_button,
        window.skip_forward_button,
        window.action_bar,
    ):
        if widget.isVisible():
            top = widget.mapTo(window, widget.rect().topLeft())
            bottom = widget.mapTo(window, widget.rect().bottomRight())
            assert 0 <= top.x() <= bottom.x() < window.width()
            assert 0 <= top.y() <= bottom.y() < window.height()


@pytest.mark.parametrize("devices", [False, True])
def test_empty_prompt_emphasizes_record_without_exposing_approval(
    make_window: Callable[..., object], devices: bool
) -> None:
    window = make_window(devices=devices)
    select_prompt(window, SPEECH_PROMPT.prompt_id)

    assert window.record_panel.isVisible()
    assert window.start_button.isVisible()
    assert window.start_button.isEnabled() is devices
    assert not window.play_button.isEnabled()
    assert not window.keep_button.isEnabled()
    assert not window.retry_button.isEnabled()


def test_historical_browse_emphasizes_playback_but_prepared_retake_exposes_record(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)

    assert window._audio is not None
    assert not window.start_button.isVisible()
    assert not window.start_button.isEnabled()
    assert window.retake_history_button.isVisible()
    assert window.play_button.isEnabled()
    key_and_wait(window, QtCore.Qt.Key.Key_R)
    assert library.session is None

    click_and_wait(window.retake_history_button, window)
    assert window._state.status == "ready"
    assert window.start_button.isVisible() and window.start_button.isEnabled()
    assert library.session.start_count == 0


def test_current_review_emphasizes_playback_and_marks_its_visible_take(
    make_window: Callable[..., object],
) -> None:
    window = make_window()
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)

    assert not window.start_button.isVisible()
    assert window.play_button.isVisible() and window.play_button.isEnabled()
    assert window.retry_button.isVisible() and window.retry_button.isEnabled()
    assert window.keep_button.isEnabled()
    assert "Save & next" in window.keep_button.text()
    assert window.history_table.isVisible()
    current = window.history_table.selectionModel().selectedRows()[0].row()
    assert "Current" in window.history_table.item(current, 0).text()


@pytest.mark.parametrize("dirty", [False, True])
def test_historical_take_cannot_approve_or_retry_unseen_current_recording(
    make_window: Callable[..., object], dirty: bool
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)
    _select_take(window, "recent-session")
    if dirty:
        window._trim_changed(48_000, 480_000)

    assert not window.keep_button.isEnabled()
    assert not window.retry_button.isEnabled()
    key_and_wait(window, QtCore.Qt.Key.Key_Return)
    assert library.session.accept_count == library.session.reject_count == 0
    assert library.saved_trims == []
    assert window.save_trim_button.isEnabled() is dirty


@pytest.mark.parametrize("action", ["keep", "retry"])
def test_history_selection_race_and_stale_button_cannot_mutate_unseen_current_take(
    make_window: Callable[..., object], action: str
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)
    old_audio = window._audio
    _select_take(window, "recent-session", settle_after=False)

    assert window._audio is old_audio  # Replacement is deferred until the Qt event loop runs.
    assert window._selected_history().session_id == "recent-session"
    assert not window.keep_button.isEnabled() and not window.retry_button.isEnabled()
    assert not window.play_button.isEnabled()
    assert not window.trim_start.isEnabled() and not window.trim_end.isEnabled()
    assert not window.skip_forward_button.isEnabled()
    window.waveform.play_requested.emit(96_000)
    assert window._player.started == []
    # A queued event or programmatic stale enable cannot bypass the handler's guard.
    button = window.keep_button if action == "keep" else window.retry_button
    button.setEnabled(True)
    (window._keep_take if action == "keep" else window._retry_take)()
    settle(window)
    assert library.session.accept_count == library.session.reject_count == 0
    assert window._audio.history.session_id == "recent-session"


@pytest.mark.parametrize("action", ["keep", "retry"])
def test_review_without_loaded_audio_cannot_be_approved_by_stale_enabled_button(
    make_window: Callable[..., object], action: str
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    show_review(window)
    window._audio = None
    window._update_controls()
    assert not window.keep_button.isEnabled() and not window.retry_button.isEnabled()
    button = window.keep_button if action == "keep" else window.retry_button
    button.setEnabled(True)

    (window._keep_take if action == "keep" else window._retry_take)()
    settle(window)

    assert library.session.accept_count == library.session.reject_count == 0


def test_loaded_take_and_selected_take_must_match_digest_before_approval(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary()
    window = make_window(library)
    show_review(window)
    selected = window.history_table.selectionModel().selectedRows()[0].row()
    item = window.history_table.item(selected, 0)
    history = item.data(QtCore.Qt.ItemDataRole.UserRole + 1)
    item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, replace(history, sha256="f" * 64))
    window._update_controls()

    assert not window.keep_button.isEnabled()
    window.keep_button.setEnabled(True)
    window._keep_take()
    settle(window)
    assert library.session.accept_count == 0


def test_historical_review_does_not_display_current_take_qc_or_duration(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)
    current_session_id = window._state.session_id
    assert window.qc_label.isVisible()
    _select_take(window, "recent-session")
    metrics = window._state.metrics.model_copy(update={"duration_seconds": 9.0})
    window._show_state(replace(window._state, metrics=metrics))

    assert not window.qc_label.isVisible()
    assert window.state_label.text() == "Reviewing take"
    assert window.elapsed_label.text() == "00:12"
    _select_take(window, current_session_id)
    assert window.qc_label.isVisible()
    assert window.keep_button.isEnabled()


def test_skip_buttons_and_large_jumps_clamp_to_pending_trim_without_playing(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    window._trim_changed(48_000, 240_000)
    window._seek_frame(144_000)

    click_and_wait(window.skip_forward_button, window)
    assert window._position_frame == 192_000
    click_and_wait(window.skip_back_button, window)
    assert window._position_frame == 144_000
    window._skip_playback(99)
    assert window._position_frame == 240_000
    window._skip_playback(-99)
    assert window._position_frame == 48_000
    assert window._player.started == []


def test_waveform_play_request_starts_or_seeks_but_never_toggles_into_pause(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)

    window.waveform.play_requested.emit(96_000)
    settle(window)
    assert window._player.started == [2.0]
    window.waveform.play_requested.emit(144_000)
    settle(window)
    assert window._player.pause_count == 0
    assert window._player.position == 3.0
    assert window._playing


def test_fit_height_changes_only_waveform_display_not_pcm_trim_or_playback(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    original = window._audio.clip.pcm
    bounds = window.waveform.start_frame, window.waveform.end_frame

    assert window.fit_waveform.isChecked() and window.waveform.fit_amplitude
    window.fit_waveform.setChecked(False)
    assert not window.waveform.fit_amplitude
    window.fit_waveform.setChecked(True)
    assert window.waveform.fit_amplitude
    assert window._audio.clip.pcm is original
    assert (window.waveform.start_frame, window.waveform.end_frame) == bounds
    assert not window._trim_dirty
    assert library.saved_trims == []
    assert window._player.loaded == []


def test_take_switch_remembers_each_source_cursor_without_autoplay(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    recent = window._audio.history
    window._seek_frame(144_000)
    _select_take(window, "old-session")
    old = window._audio.history
    assert window._take_positions[_take_position_key(recent)] == 144_000
    window._seek_frame(288_000)
    _select_take(window, "recent-session")

    assert window._position_frame == 144_000
    assert window._take_positions[_take_position_key(old)] == 288_000
    _select_take(window, "old-session")
    assert window._position_frame == 288_000
    assert window._player.started == []


@pytest.mark.parametrize(("remembered", "expected"), [(-1, 48_000), (900_000, 240_000)])
def test_remembered_take_cursor_is_clamped_to_its_current_trim(
    make_window: Callable[..., object], remembered: int, expected: int
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    recent = window._audio.history
    _select_take(window, "old-session")
    library.save_trim(recent, 48_000, 240_000)
    window._take_positions[_take_position_key(recent)] = remembered

    _select_take(window, "recent-session")

    assert window._position_frame == expected
    assert window.playback_slider.value() == expected
    assert window._player.started == []


def test_take_cursor_memory_is_invalidated_when_source_digest_changes(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    window._seek_frame(144_000)
    _select_take(window, "old-session")
    library.row_values = tuple(
        replace(
            row,
            history=tuple(
                replace(history, sha256="d" * 64)
                if history.session_id == "recent-session"
                else history
                for history in row.history
            ),
        )
        for row in library.row_values
    )
    click_and_wait(window.refresh_button, window)

    _select_take(window, "recent-session")

    assert window._audio.history.sha256 == "d" * 64
    assert window._position_frame == window.waveform.start_frame == 0


def test_paused_prompt_switch_remembers_native_cursor_without_autoplay(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(window.play_button, window)
    window._player.position = 3.0
    window._render_transport_state()
    click_and_wait(window.play_button, window)
    assert window._paused

    select_prompt(window, PILOT_SUITE.prompts[5].prompt_id)
    assert not window._is_playback_active()
    select_prompt(window, SPEECH_PROMPT.prompt_id)

    assert window._position_frame == 144_000
    assert window.playback_slider.value() == 144_000
    assert window._player.started == [0.0]
    assert not window._is_playback_active()


def test_paused_seeking_updates_header_and_timeline_without_resuming_audio(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    click_and_wait(window.play_button, window)
    window._player.position = 0.5
    window._render_transport_state()
    click_and_wait(window.play_button, window)

    click_and_wait(window.skip_forward_button, window)
    assert window._player.position == 1.5
    assert window.elapsed_label.text() == "00:01"
    assert window.playback_slider.value() == 72_000
    window.playback_slider.setValue(96_000)
    assert window.elapsed_label.text() == "00:02"
    click_and_wait(window.rewind_button, window)
    assert window.elapsed_label.text() == "00:00"
    assert window.playback_slider.value() == 0
    assert window._paused and not window._playing
    assert window._player.pause_count == 1
    assert window._player.started == [0.0]


def test_saving_pending_trim_while_paused_refreshes_edit_evidence_without_autoplay(
    make_window: Callable[..., object],
) -> None:
    library = SyntheticLibrary().with_history()
    window = make_window(library)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    window._trim_changed(48_000, 240_000)
    click_and_wait(window.play_button, window)
    click_and_wait(window.play_button, window)
    assert window._paused and window._trim_dirty

    click_and_wait(window.save_trim_button, window)

    assert not window._trim_dirty
    assert window._audio.edit.trimmed
    assert (window._audio.edit.start_frame, window._audio.edit.end_frame) == (48_000, 240_000)
    assert (window.waveform.start_frame, window.waveform.end_frame) == (48_000, 240_000)
    assert window.reset_trim_button.isEnabled()
    assert not window._playing
    assert len(window._player.started) == 1


def test_resetting_saved_trim_while_paused_refreshes_waveform_and_next_preview(
    make_window: Callable[..., object],
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    window._trim_changed(48_000, 240_000)
    click_and_wait(window.save_trim_button, window)
    click_and_wait(window.play_button, window)
    click_and_wait(window.play_button, window)
    assert window._paused and window._audio.edit.trimmed

    click_and_wait(window.reset_trim_button, window)

    assert not window._audio.edit.trimmed
    assert (window.waveform.start_frame, window.waveform.end_frame) == (
        0,
        SYNTHETIC_CLIP.frame_count,
    )
    assert not window._playing
    assert len(window._player.started) == 1
    click_and_wait(window.play_button, window)
    with wave.open(io.BytesIO(window._player.loaded[-1]), "rb") as reader:
        assert reader.getnframes() == SYNTHETIC_CLIP.frame_count


@pytest.mark.parametrize("background", READING_PALETTE_BACKGROUNDS)
def test_compact_actual_review_geometry_keeps_editor_sections_separate(
    make_window: Callable[..., object], background: str
) -> None:
    window = make_window(SyntheticLibrary().with_history())
    palette = window.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(background))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(background))
    palette.setColor(
        QtGui.QPalette.ColorRole.WindowText,
        QtGui.QColor("#f5f5f5" if background == "#232527" else "#202020"),
    )
    window.setPalette(palette)
    window.resize(1060, 700)
    select_prompt(window, SPEECH_PROMPT.prompt_id)
    show_review(window)
    QtTest.QTest.qWait(30)
    assert (window.width(), window.height()) == (1060, 700)

    ordered = (
        ("takes", window.history_table),
        ("waveform", window.waveform),
        ("timeline", window.timeline_panel),
        ("trim", window.trim_panel),
        ("transport", window.transport_panel),
        ("QC", window.qc_label),
        ("footer", window.action_bar),
    )
    rectangles = []
    for name, widget in ordered:
        assert widget.isVisible(), f"{name} must remain visible during current-take review"
        rectangle = QtCore.QRect(widget.mapTo(window, widget.rect().topLeft()), widget.size())
        assert 0 <= rectangle.left() <= rectangle.right() < window.width(), name
        assert 0 <= rectangle.top() <= rectangle.bottom() < window.height(), name
        rectangles.append((name, rectangle))
    for (previous_name, previous), (name, rectangle) in zip(
        rectangles, rectangles[1:], strict=False
    ):
        assert previous.bottom() < rectangle.top(), (
            f"{previous_name} overlaps {name}: "
            + "; ".join(f"{label}={bounds.getRect()}" for label, bounds in rectangles)
        )
