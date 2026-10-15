"""Offscreen waveform interaction/render tests using synthetic in-memory PCM only."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cole_voice_lab.desktop_audio import PcmAudioClip
from cole_voice_lab.desktop_waveform import WaveformView

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WAVEFORM_RATE = 48_000
WAVEFORM_FRAMES = 96_000
WAVEFORM_WIDTH = 504
WAVEFORM_HEIGHT = 160
WAVEFORM_AMPLITUDES = (16_384, 32_767, 8_192, 0)
QUIET_WAVEFORM_AMPLITUDE = 1_024


def synthetic_clip(frame_count: int = WAVEFORM_FRAMES, bins: int = 4) -> PcmAudioClip:
    """Construct a consistent symmetric envelope without opening files or audio devices."""
    amplitudes = WAVEFORM_AMPLITUDES[:bins]
    pcm = bytearray()
    envelope: list[tuple[float, float]] = []
    for index, amplitude in enumerate(amplitudes):
        count = (index + 1) * frame_count // bins - index * frame_count // bins
        samples = tuple(amplitude if i % 2 else -amplitude for i in range(count))
        pcm.extend(b"".join(value.to_bytes(2, "little", signed=True) for value in samples))
        envelope.append((min(samples) / 32768, max(samples) / 32768))
    return PcmAudioClip(WAVEFORM_RATE, frame_count, 2, 1, bytes(pcm), tuple(envelope), "1" * 64)


def symmetric_clip(amplitude: int) -> PcmAudioClip:
    """Produce a quiet or silent clip with real PCM matching its single extrema bin."""
    pair = (-amplitude).to_bytes(2, "little", signed=True) + amplitude.to_bytes(
        2, "little", signed=True
    )
    return PcmAudioClip(
        WAVEFORM_RATE,
        WAVEFORM_FRAMES,
        2,
        1,
        pair * (WAVEFORM_FRAMES // 2),
        ((-amplitude / 32768, amplitude / 32768),),
        "2" * 64,
    )


@pytest.fixture(scope="module")
def qt_app() -> Iterator[QApplication]:
    existing = QApplication.instance()
    application = existing if isinstance(existing, QApplication) else QApplication([])
    yield application


@pytest.fixture
def waveform(qt_app: QApplication) -> Iterator[WaveformView]:
    del qt_app
    widget = WaveformView()
    widget.resize(WAVEFORM_WIDTH, WAVEFORM_HEIGHT)
    widget.show()
    widget.activateWindow()
    # Every test's window opens in the same place, and the pointer stays where the last test
    # left it: Qt sends no move event for a move to where it already is. Park it in the
    # corner, where no test moves first, so each test's first move is seen.
    QTest.mouseMove(widget, QPoint(0, 0))
    QApplication.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


def point(widget: WaveformView, frame: int) -> QPoint:
    return QPoint(round(widget._x(frame)), round(widget._plot().center().y()))


def test_no_clip_is_safe_to_paint_click_seek_and_reset(waveform: WaveformView) -> None:
    seeks: list[int] = []
    trims: list[tuple[int, int]] = []
    waveform.seek_requested.connect(seeks.append)
    waveform.trim_changed.connect(lambda start, end: trims.append((start, end)))
    waveform.set_range(1, 10)
    waveform.set_position(20)
    QTest.mouseClick(waveform, Qt.MouseButton.LeftButton, pos=QPoint(100, 50))
    QTest.mouseMove(waveform, QPoint(120, 50))
    QTest.keyClick(waveform, Qt.Key.Key_Left)
    assert waveform.grab().size() == waveform.size()
    assert not seeks and not trims
    assert (waveform.start_frame, waveform.end_frame, waveform.position_frame) == (0, 0, 0)
    waveform.set_clip(synthetic_clip())
    waveform.set_clip(None)
    assert waveform.clip is None and waveform.end_frame == 0


@pytest.mark.parametrize(
    "bounds,expected",
    [
        ((-100, 200_000), (0, WAVEFORM_FRAMES)),
        ((100_000, 200_000), (WAVEFORM_FRAMES - 1, WAVEFORM_FRAMES)),
        ((20, 10), (20, 21)),
    ],
)
def test_clip_and_range_clamp_to_nonempty_original_frame_bounds(
    waveform: WaveformView,
    bounds: tuple[int, int],
    expected: tuple[int, int],
) -> None:
    clip = synthetic_clip()
    waveform.set_clip(clip, *bounds)
    assert (waveform.start_frame, waveform.end_frame) == expected
    assert waveform.position_frame == expected[0]
    waveform.set_range(*bounds)
    assert (waveform.start_frame, waveform.end_frame) == expected
    waveform.set_position(-1)
    assert waveform.position_frame == 0
    waveform.set_position(200_000)
    assert waveform.position_frame == clip.frame_count
    assert waveform._frame(-1000) == 0 and waveform._frame(10000) == clip.frame_count


def test_click_and_scrub_emit_original_frames_within_trim_bounds(waveform: WaveformView) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    seeks: list[int] = []
    waveform.seek_requested.connect(seeks.append)
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_000))
    QTest.mouseMove(waveform, point(waveform, 60_000))
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 60_000))
    assert seeks[0] == 48_000 and seeks[-1] == 60_000
    QTest.mouseClick(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 0))
    assert seeks[-1] == 24_000
    QTest.mouseClick(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, WAVEFORM_FRAMES))
    assert seeks[-1] == 72_000
    assert waveform.position_frame == 24_000


@pytest.mark.parametrize(
    "edge,target,expected",
    [
        ("start", 48_000, (48_000, 72_000)),
        ("start", 96_000, (71_999, 72_000)),
        ("end", 48_000, (24_000, 48_000)),
        ("end", 0, (24_000, 24_001)),
    ],
)
def test_trim_handles_drag_without_crossing_or_mutating_clip(
    waveform: WaveformView,
    edge: str,
    target: int,
    expected: tuple[int, int],
) -> None:
    clip = synthetic_clip()
    original = clip.pcm
    waveform.set_clip(clip, 24_000, 72_000)
    trims: list[tuple[int, int]] = []
    waveform.trim_changed.connect(lambda start, end: trims.append((start, end)))
    initial = waveform.start_frame if edge == "start" else waveform.end_frame
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, initial))
    QTest.mouseMove(waveform, point(waveform, target))
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, target))
    assert trims[-1] == expected
    assert (waveform.start_frame, waveform.end_frame) == expected and waveform._drag is None
    assert clip.pcm == original


def test_narrow_range_end_handle_remains_draggable(waveform: WaveformView) -> None:
    waveform.set_clip(synthetic_clip(), 48_000, 49_000)
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 49_000))
    assert waveform._drag == "end"
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 60_000))
    assert (waveform.start_frame, waveform.end_frame) == (48_000, 60_000)


def test_one_frame_range_can_expand_at_coincident_handles(waveform: WaveformView) -> None:
    waveform.set_clip(synthetic_clip(), 48_000, 48_001)
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_001))
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 60_000))
    assert waveform.end_frame == 60_000
    assert waveform.start_frame >= 0


@pytest.mark.parametrize(
    "key,expected",
    [
        (Qt.Key.Key_Left, 43_200),
        (Qt.Key.Key_Right, 52_800),
        (Qt.Key.Key_Home, 24_000),
        (Qt.Key.Key_End, 72_000),
    ],
)
def test_keyboard_seeks_by_tenth_second_or_trim_bound(
    waveform: WaveformView,
    key: Qt.Key,
    expected: int,
) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    waveform.set_position(48_000)
    seeks: list[int] = []
    waveform.seek_requested.connect(seeks.append)
    QTest.keyClick(waveform, key)
    assert seeks == [expected]
    waveform.set_position(24_000)
    QTest.keyClick(waveform, Qt.Key.Key_Left)
    assert seeks[-1] == 24_000
    before = list(seeks)
    QTest.keyClick(waveform, Qt.Key.Key_A)
    assert seeks == before


def test_readonly_waveform_allows_seek_but_not_trim(waveform: WaveformView) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    waveform.editable = False
    seeks: list[int] = []
    trims: list[tuple[int, int]] = []
    waveform.seek_requested.connect(seeks.append)
    waveform.trim_changed.connect(lambda start, end: trims.append((start, end)))
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 24_000))
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_000))
    assert seeks[-1] == 48_000 and not trims
    assert (waveform.start_frame, waveform.end_frame) == (24_000, 72_000)


@pytest.mark.parametrize("disable_kind", ["widget", "editing"])
def test_disabling_during_handle_drag_cancels_it_before_reenable(
    waveform: WaveformView,
    disable_kind: str,
) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 24_000))
    if disable_kind == "widget":
        waveform.setEnabled(False)
    else:
        waveform.editable = False
    assert waveform._drag is None
    waveform.setEnabled(True)
    waveform.editable = True
    QTest.mouseMove(waveform, point(waveform, 48_000))
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_000))
    assert waveform.start_frame == 24_000


def test_disabled_and_secondary_mouse_buttons_emit_no_actions(waveform: WaveformView) -> None:
    waveform.set_clip(synthetic_clip())
    seeks: list[int] = []
    waveform.seek_requested.connect(seeks.append)
    waveform.setEnabled(False)
    QTest.mouseClick(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_000))
    QTest.keyClick(waveform, Qt.Key.Key_End)
    waveform.setEnabled(True)
    QTest.mouseClick(waveform, Qt.MouseButton.RightButton, pos=point(waveform, 48_000))
    assert not seeks and waveform._drag is None


def test_hover_cursors_identify_editable_edges_and_seek_area(waveform: WaveformView) -> None:
    waveform.set_clip(synthetic_clip())
    QTest.mouseMove(waveform, point(waveform, 0))
    assert waveform.cursor().shape() == Qt.CursorShape.SizeHorCursor
    QTest.mouseMove(waveform, point(waveform, 48_000))
    assert waveform.cursor().shape() == Qt.CursorShape.CrossCursor
    waveform.editable = False
    QTest.mouseMove(waveform, point(waveform, WAVEFORM_FRAMES))
    assert waveform.cursor().shape() == Qt.CursorShape.CrossCursor


@pytest.mark.parametrize("background,accent", [("#ffffff", "#236db3"), ("#232527", "#7bb4ff")])
@pytest.mark.parametrize("bin_count", [1, 2, 4])
def test_waveform_extrema_render_within_plot_in_both_palettes(
    waveform: WaveformView,
    background: str,
    accent: str,
    bin_count: int,
) -> None:
    palette = waveform.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(background))
    palette.setColor(QPalette.ColorRole.Window, QColor(background))
    waveform.setPalette(palette)
    waveform.set_clip(synthetic_clip(bins=bin_count))
    rendered = waveform.grab().toImage()
    assert rendered.pixelColor(waveform.width() // 2, 2) == QColor(background)
    plot = waveform._plot()
    first_x = round(plot.left() + plot.width() / (bin_count * 2))
    assert rendered.pixelColor(first_x, round(plot.center().y())) == QColor(accent)
    above_first_peak = round(plot.center().y() - plot.height() * 0.46 * 0.5 - 5)
    assert rendered.pixelColor(first_x, above_first_peak) == QColor(background)


def test_tiny_clip_and_widget_geometry_stay_bounded(waveform: WaveformView) -> None:
    waveform.resize(20, 96)
    waveform.set_clip(synthetic_clip(frame_count=1, bins=1))
    assert waveform._frame(-100) == 0 and waveform._frame(100) == 1
    assert waveform._plot().width() >= 1
    assert not waveform.grab().isNull()


def test_single_sample_level_is_visible_without_exaggerating_amplitude(
    waveform: WaveformView,
) -> None:
    palette = waveform.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    waveform.setPalette(palette)
    waveform.set_clip(synthetic_clip(frame_count=1, bins=1))
    image = waveform.grab().toImage()
    plot = waveform._plot()
    x = round(plot.center().x())
    sample_y = round(plot.center().y() + plot.height() * 0.46 * 0.5)
    assert image.pixelColor(x, sample_y) != QColor("#ffffff")
    assert image.pixelColor(x, sample_y + 4) == QColor("#ffffff")


@pytest.mark.parametrize("editable", [True, False])
def test_double_click_retained_waveform_requests_seek_then_play_without_trimming(
    waveform: WaveformView, editable: bool
) -> None:
    clip = synthetic_clip()
    waveform.set_clip(clip, 24_000, 72_000)
    waveform.editable = editable
    actions: list[tuple[str, int]] = []
    trims: list[tuple[int, int]] = []
    waveform.seek_requested.connect(lambda frame: actions.append(("seek", frame)))
    waveform.play_requested.connect(lambda frame: actions.append(("play", frame)))
    waveform.trim_changed.connect(lambda start, end: trims.append((start, end)))
    QTest.mouseDClick(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_000))
    assert actions == [("seek", 48_000), ("play", 48_000)]
    assert not trims and waveform._drag is None
    assert waveform.clip is clip and waveform.position_frame == 24_000


@pytest.mark.parametrize("target", [0, 24_000, 72_000, WAVEFORM_FRAMES])
@pytest.mark.parametrize("editable", [True, False])
def test_double_click_excluded_audio_or_grips_never_requests_playback(
    waveform: WaveformView, target: int, editable: bool
) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    waveform.editable = editable
    plays: list[int] = []
    waveform.play_requested.connect(plays.append)
    QTest.mouseDClick(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, target))
    assert not plays
    assert (waveform.start_frame, waveform.end_frame) == (24_000, 72_000)


@pytest.mark.parametrize("state", ["empty", "disabled", "secondary", "outside_plot"])
def test_double_click_without_available_primary_waveform_never_requests_playback(
    waveform: WaveformView, state: str
) -> None:
    plays: list[int] = []
    waveform.play_requested.connect(plays.append)
    if state != "empty":
        waveform.set_clip(synthetic_clip())
    if state == "disabled":
        waveform.setEnabled(False)
    target = point(waveform, 48_000) if state != "outside_plot" else QPoint(250, 2)
    button = Qt.MouseButton.RightButton if state == "secondary" else Qt.MouseButton.LeftButton
    QTest.mouseDClick(waveform, button, pos=target)
    assert not plays


def test_hover_timestamp_is_precise_and_does_not_move_playhead_or_request_seek(
    waveform: WaveformView,
) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    seeks: list[int] = []
    waveform.seek_requested.connect(seeks.append)
    QTest.mouseMove(waveform, point(waveform, 60_000))
    assert waveform.hover_frame == 60_000
    assert waveform.hover_timestamp_text == "0:01.250"
    assert waveform.position_frame == 24_000 and not seeks
    assert not waveform.grab().isNull()


@pytest.mark.parametrize("transition", ["leave", "disable", "readonly", "replace", "clear"])
def test_hover_cursor_and_timestamp_clear_with_invalidated_interaction_context(
    waveform: WaveformView, transition: str
) -> None:
    waveform.set_clip(synthetic_clip())
    QTest.mouseMove(waveform, point(waveform, 48_000))
    assert waveform.hover_frame is not None
    if transition == "leave":
        QApplication.sendEvent(waveform, QEvent(QEvent.Type.Leave))
    elif transition == "disable":
        waveform.setEnabled(False)
    elif transition == "readonly":
        waveform.editable = False
    elif transition == "replace":
        waveform.set_clip(symmetric_clip(QUIET_WAVEFORM_AMPLITUDE))
    else:
        waveform.set_clip(None)
    assert waveform.hover_frame is None and waveform.hover_timestamp_text == ""
    assert waveform.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_hover_leaves_plot_without_stale_feedback_and_scrub_can_continue_outside(
    waveform: WaveformView,
) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    seeks: list[int] = []
    waveform.seek_requested.connect(seeks.append)
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_000))
    QApplication.sendEvent(waveform, QEvent(QEvent.Type.Leave))
    assert waveform._drag == "seek"
    QTest.mouseMove(waveform, QPoint(waveform.width() + 20, 50))
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=QPoint(waveform.width() + 20, 50))
    assert seeks[-1] == 72_000
    assert waveform.hover_frame is None and waveform._drag is None


@pytest.mark.parametrize(
    "edge,offset,target,expected",
    [
        ("start", -14, 36_000, (36_000, 72_000)),
        ("end", 14, 60_000, (24_000, 60_000)),
    ],
)
def test_generous_grip_hit_target_does_not_jump_before_drag(
    waveform: WaveformView, edge: str, offset: int, target: int, expected: tuple[int, int]
) -> None:
    waveform.set_clip(synthetic_clip(), 24_000, 72_000)
    anchor = waveform.start_frame if edge == "start" else waveform.end_frame
    press = point(waveform, anchor) + QPoint(offset, 0)
    QTest.mousePress(waveform, Qt.MouseButton.LeftButton, pos=press)
    assert waveform._drag == edge
    assert (waveform.start_frame, waveform.end_frame) == (24_000, 72_000)
    release = point(waveform, target) + QPoint(offset, 0)
    QTest.mouseMove(waveform, release)
    QTest.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=release)
    assert (waveform.start_frame, waveform.end_frame) == expected


def test_coincident_grips_can_expand_start_using_left_hit_area(waveform: WaveformView) -> None:
    waveform.set_clip(synthetic_clip(), 48_000, 48_001)
    QTest.mousePress(
        waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 48_000) + QPoint(-10, 0)
    )
    assert waveform._drag == "start"
    QTest.mouseRelease(
        waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 36_000) + QPoint(-10, 0)
    )
    assert (waveform.start_frame, waveform.end_frame) == (36_000, 48_001)


@pytest.mark.parametrize("background", ["#ffffff", "#232527"])
def test_excluded_region_has_stronger_shading_than_retained_audio(
    waveform: WaveformView, background: str
) -> None:
    palette = waveform.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(background))
    waveform.setPalette(palette)
    waveform.set_clip(symmetric_clip(0), 24_000, 72_000)
    rendered = waveform.grab().toImage()
    y = round(waveform._plot().top() + 12)
    retained = rendered.pixelColor(round(waveform._x(48_000)), y)
    excluded = rendered.pixelColor(round(waveform._x(12_000)), y)
    assert retained == QColor(background)
    assert excluded.lightness() < retained.lightness() - 15


def test_quiet_amplitude_fit_is_visible_and_display_only(waveform: WaveformView) -> None:
    clip = symmetric_clip(QUIET_WAVEFORM_AMPLITUDE)
    waveform.set_clip(clip, 24_000, 72_000)
    waveform.set_position(48_000)
    before_pcm, before_bins, before_digest = clip.pcm, clip.waveform_bins, clip.source_sha256
    before_bounds = waveform.start_frame, waveform.end_frame, waveform.position_frame
    actions: list[int] = []
    waveform.seek_requested.connect(actions.append)
    waveform.play_requested.connect(actions.append)
    waveform.trim_changed.connect(lambda start, end: actions.extend((start, end)))
    assert not waveform.fit_amplitude and waveform.display_gain == 1
    assert waveform.amplitude_scale_text == ""
    original_render = waveform.grab().toImage()
    waveform.set_fit_amplitude(True)
    assert waveform.display_gain == 32
    assert waveform.amplitude_scale_text == "Display only · ×32.0"
    assert "display only" in waveform.accessibleDescription()
    fitted_render = waveform.grab().toImage()
    plot = waveform._plot()
    sample = QPoint(round(waveform._x(60_000)), round(plot.center().y() - plot.height() / 4))
    assert fitted_render.pixelColor(sample) != original_render.pixelColor(sample)
    assert waveform.clip is clip and (clip.pcm, clip.waveform_bins, clip.source_sha256) == (
        before_pcm,
        before_bins,
        before_digest,
    )
    assert (waveform.start_frame, waveform.end_frame, waveform.position_frame) == before_bounds
    assert not actions
    waveform.set_fit_amplitude(False)
    assert waveform.display_gain == 1 and waveform.grab().toImage() == original_render


@pytest.mark.parametrize(
    "amplitude,gain,label",
    [
        (0, 1.0, "Display only · silence"),
        (1, 64.0, "Display only · ×64.0"),
        (QUIET_WAVEFORM_AMPLITUDE, 32.0, "Display only · ×32.0"),
    ],
)
def test_fitted_silence_and_tiny_extrema_remain_finite_and_bounded(
    waveform: WaveformView, amplitude: int, gain: float, label: str
) -> None:
    waveform.set_fit_amplitude(True)
    waveform.set_clip(symmetric_clip(amplitude))
    assert waveform.display_gain == gain and waveform.amplitude_scale_text == label
    assert not waveform.grab().isNull()
    waveform.set_clip(None)
    assert waveform.display_gain == 1 and waveform.fit_amplitude
    assert not waveform.grab().isNull()


def test_fit_uses_original_clip_extrema_not_only_retained_range(waveform: WaveformView) -> None:
    clip = synthetic_clip()
    waveform.set_fit_amplitude(True)
    waveform.set_clip(clip, 72_000, 96_000)
    original_gain = waveform.display_gain
    waveform.set_range(0, 24_000)
    assert waveform.display_gain == original_gain == pytest.approx(32768 / 32767)
    waveform.set_clip(symmetric_clip(QUIET_WAVEFORM_AMPLITUDE))
    assert waveform.display_gain == 32


def test_fitted_silence_stays_flat_and_visible_scale_label_is_displayed(
    waveform: WaveformView,
) -> None:
    waveform.set_clip(symmetric_clip(0))
    original = waveform.grab().toImage()
    waveform.set_fit_amplitude(True)
    fitted = waveform.grab().toImage()
    plot = waveform._plot()
    interior = plot.adjusted(25, 0, -25, 0).toRect()
    assert original.copy(interior) == fitted.copy(interior)
    footer = QPoint(round(plot.center().x()), round(plot.bottom() + 3))
    label_region = original.rect().adjusted(footer.x() - 95, footer.y(), -footer.x() + 95, 0)
    assert original.copy(label_region) != fitted.copy(label_region)


@pytest.mark.parametrize("fitted", [False, True])
def test_display_fit_does_not_change_seek_or_double_click_frame_coordinates(
    waveform: WaveformView, fitted: bool
) -> None:
    waveform.set_clip(symmetric_clip(QUIET_WAVEFORM_AMPLITUDE), 24_000, 72_000)
    waveform.set_fit_amplitude(fitted)
    requests: list[int] = []
    waveform.play_requested.connect(requests.append)
    QTest.mouseDClick(waveform, Qt.MouseButton.LeftButton, pos=point(waveform, 60_000))
    assert requests == [60_000]
