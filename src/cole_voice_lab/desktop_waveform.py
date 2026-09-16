"""Native original-audio waveform with a seek cursor and frame-addressed trim handles.

@canonical desktop_waveform -- displays PCM extrema; never captures or edits audio.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .desktop_audio import PcmAudioClip

_HANDLE_HIT_RADIUS = 16.0
_MAX_DISPLAY_GAIN = 64.0


class WaveformView(QWidget):
    """Show the original clip; emit seek/trim requests in original PCM frame coordinates.

    Drag a trim handle or click/drag the waveform to seek. Left/right seek by a
    tenth of a second; Home/End move to the trim bounds. No files are mutated.
    """

    seek_requested = Signal(int)
    trim_changed = Signal(int, int)
    play_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clip: PcmAudioClip | None = None
        self.start_frame = 0
        self.end_frame = 0
        self.position_frame = 0
        self._editable = True
        self._drag: Literal["start", "end", "seek"] | None = None
        self._drag_offset = 0.0
        self._hover_frame: int | None = None
        self._fit_amplitude = False
        self._peak_amplitude = 0.0
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("Original recording waveform")
        self.setToolTip(
            "Drag the edge grips to trim. Click or drag to seek. "
            "Double-click inside the retained range to play from that point."
        )
        self.set_fit_amplitude(False)

    @property
    def fit_amplitude(self) -> bool:
        """Whether original PCM extrema are fitted vertically for display only."""
        return self._fit_amplitude

    @property
    def display_gain(self) -> float:
        """Return bounded visual gain; silence remains silence and PCM is never changed."""
        if not self._fit_amplitude or self._peak_amplitude == 0:
            return 1.0
        return max(1.0, min(_MAX_DISPLAY_GAIN, 1.0 / self._peak_amplitude))

    @property
    def hover_frame(self) -> int | None:
        """Return the pointer's original-frame position, separate from the playback clock."""
        return self._hover_frame

    @property
    def hover_timestamp_text(self) -> str:
        """Return the visible millisecond cursor timestamp, or empty when not hovering."""
        if self._hover_frame is None or self.clip is None:
            return ""
        milliseconds = round(self._hover_frame * 1000 / self.clip.sample_rate)
        minutes, remainder = divmod(milliseconds, 60_000)
        seconds, fraction = divmod(remainder, 1000)
        return f"{minutes}:{seconds:02d}.{fraction:03d}"

    @property
    def amplitude_scale_text(self) -> str:
        """Describe fitted visual scaling explicitly; never imply audio normalization."""
        if not self._fit_amplitude:
            return ""
        if self._peak_amplitude == 0:
            return "Display only · silence"
        return f"Display only · ×{self.display_gain:.1f}"

    def set_fit_amplitude(self, enabled: bool) -> None:
        """Fit original extrema vertically, capped at 64×, without changing PCM or time."""
        self._fit_amplitude = enabled
        self.setAccessibleDescription(
            "Amplitude fitted for display only; saved audio and time positions are unchanged."
            if enabled
            else "Original PCM amplitude and time positions; saved audio is unchanged."
        )
        self.update()

    @property
    def editable(self) -> bool:
        """Whether handle editing is available; seeking remains independently available."""
        return self._editable

    @editable.setter
    def editable(self, value: bool) -> None:
        self._editable = value
        if not value:
            self._drag = None
            self._clear_hover()
        self.update()

    def changeEvent(self, event: QEvent) -> None:
        """Cancel incomplete gestures when transport state disables interaction."""
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._drag = None
            self._clear_hover()
        super().changeEvent(event)

    def _clear_hover(self) -> None:
        self._hover_frame = None
        self.unsetCursor()
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        """Remove transient pointer feedback without interrupting a captured drag."""
        self._clear_hover()
        super().leaveEvent(event)

    def set_clip(
        self, clip: PcmAudioClip | None, start_frame: int = 0, end_frame: int | None = None
    ) -> None:
        """Replace the displayed original PCM clip and its selected source-frame range."""
        self.clip = clip
        self._peak_amplitude = (
            max((max(abs(low), abs(high)) for low, high in clip.waveform_bins), default=0.0)
            if clip is not None
            else 0.0
        )
        self.start_frame = 0
        self.end_frame = 0
        if clip is not None:
            self.set_range(start_frame, clip.frame_count if end_frame is None else end_frame)
        self.position_frame = self.start_frame
        self._drag = None
        self._clear_hover()
        self.update()

    def set_range(self, start_frame: int, end_frame: int) -> None:
        """Reflect already-validated frame bounds without re-emitting an edit."""
        if self.clip is None:
            return
        self.start_frame = max(0, min(start_frame, self.clip.frame_count - 1))
        self.end_frame = max(self.start_frame + 1, min(end_frame, self.clip.frame_count))
        self.update()

    def set_position(self, frame: int) -> None:
        """Reflect the native player's source-relative clock without changing trim bounds."""
        self.position_frame = max(0, min(frame, self.clip.frame_count if self.clip else 0))
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(12, 14, max(1, self.width() - 24), max(1, self.height() - 36))

    def _x(self, frame: int) -> float:
        plot = self._plot()
        return plot.left() + plot.width() * frame / max(
            1, self.clip.frame_count if self.clip else 1
        )

    def _frame(self, x: float) -> int:
        plot = self._plot()
        return round(
            max(0.0, min(1.0, (x - plot.left()) / plot.width()))
            * (self.clip.frame_count if self.clip else 0)
        )

    def _handle_at(self, x: float) -> Literal["start", "end"] | None:
        start_x, end_x = self._x(self.start_frame), self._x(self.end_frame)
        start_distance, end_distance = abs(x - start_x), abs(x - end_x)
        if min(start_distance, end_distance) > _HANDLE_HIT_RADIUS:
            return None
        if round(start_x) == round(end_x):
            # Coincident grips remain expandable in either direction. The exact
            # shared pixel selects End, preserving one-frame range precision.
            return "start" if x < start_x - 1 else "end"
        return "start" if start_distance < end_distance else "end"

    def _update_hover(self, position: QPointF) -> None:
        if self.clip is None or not self.isEnabled() or not self._plot().contains(position):
            self._clear_hover()
            return
        self._hover_frame = self._frame(position.x())
        self.setCursor(
            Qt.CursorShape.SizeHorCursor
            if self.editable and self._handle_at(position.x()) is not None
            else Qt.CursorShape.CrossCursor
        )
        self.update()

    def _paint_hover(self, painter: QPainter, plot: QRectF, palette: QPalette) -> None:
        if self._hover_frame is None or self.clip is None:
            return
        x = self._x(self._hover_frame)
        pen = QPen(palette.color(QPalette.ColorRole.Text), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        label = self.hover_timestamp_text
        metrics = painter.fontMetrics()
        width = min(float(metrics.horizontalAdvance(label) + 16), plot.width())
        box = QRectF(
            max(plot.left(), min(x + 8, plot.right() - width)),
            plot.top() + 6,
            width,
            metrics.height() + 6,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.color(QPalette.ColorRole.Text))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(palette.color(QPalette.ColorRole.Base))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Paint actual extrema, excluded regions, trim handles, and native-clock playhead."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.color(QPalette.ColorRole.Base))
        plot = self._plot()
        if self.clip is None:
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Record a take or select an earlier recording",
            )
            return
        dark = palette.color(QPalette.ColorRole.Base).lightness() < 128
        accent = QColor("#7bb4ff" if dark else "#236db3")
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
        painter.drawLine(
            QPointF(plot.left(), plot.center().y()), QPointF(plot.right(), plot.center().y())
        )
        bins = self.clip.waveform_bins
        painter.save()
        painter.setClipRect(plot)
        waveform_pen = QPen(accent, max(1.0, plot.width() / max(1, len(bins))))
        # Thick sparse bins must remain their real extrema: square end caps
        # would exaggerate amplitude and spill beyond the plot for short clips.
        waveform_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(waveform_pen)
        amplitude = plot.height() * 0.46 * self.display_gain
        for index, (minimum, maximum) in enumerate(bins):
            x = plot.left() + (index + 0.5) * plot.width() / max(1, len(bins))
            top = plot.center().y() - maximum * amplitude
            bottom = plot.center().y() - minimum * amplitude
            if minimum == maximum:
                # A constant-valued bin still represents a real sample level;
                # a zero-length FlatCap line would disappear entirely.
                painter.fillRect(
                    QRectF(x - waveform_pen.widthF() / 2, top - 0.5, waveform_pen.widthF(), 1),
                    accent,
                )
            else:
                painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        painter.restore()
        left, right = self._x(self.start_frame), self._x(self.end_frame)
        shade = QColor("#060b13" if dark else "#68717f")
        shade.setAlpha(225 if dark else 170)
        painter.fillRect(QRectF(plot.left(), plot.top(), left - plot.left(), plot.height()), shade)
        painter.fillRect(QRectF(right, plot.top(), plot.right() - right, plot.height()), shade)
        painter.setPen(QPen(accent, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(left, plot.top(), right - left, plot.height()))
        for x in (left, right):
            grip = QRectF(x - 6, plot.center().y() - 19, 12, 38)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(grip, 5, 5)
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Base), 1.5))
            for offset in (-2, 2):
                painter.drawLine(
                    QPointF(x + offset, grip.top() + 11),
                    QPointF(x + offset, grip.bottom() - 11),
                )
        cursor = QColor("#ff858b" if dark else "#ad2037")
        painter.setPen(QPen(cursor, 2))
        painter.drawLine(
            QPointF(self._x(self.position_frame), plot.top() - 3),
            QPointF(self._x(self.position_frame), plot.bottom() + 3),
        )
        self._paint_hover(painter, plot, palette)
        painter.setPen(palette.color(QPalette.ColorRole.WindowText))
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 3, plot.width(), 18),
            Qt.AlignmentFlag.AlignLeft,
            "0:00",
        )
        seconds = self.clip.duration_seconds
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 3, plot.width(), 18),
            Qt.AlignmentFlag.AlignRight,
            f"{int(seconds // 60)}:{seconds % 60:05.2f}",
        )
        if self._fit_amplitude:
            painter.drawText(
                QRectF(plot.left() + 38, plot.bottom() + 3, max(1, plot.width() - 108), 18),
                Qt.AlignmentFlag.AlignCenter,
                self.amplitude_scale_text,
            )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Begin a trim-handle drag or a playhead scrub; never begin capture."""
        if (
            self.clip is None
            or not self.isEnabled()
            or event.button() != Qt.MouseButton.LeftButton
            or not self._plot().adjusted(-16, -10, 16, 10).contains(event.position())
        ):
            return
        self.setFocus()
        x = event.position().x()
        handle = self._handle_at(x)
        self._drag_offset = 0.0
        if self.editable and handle is not None:
            self._drag = handle
            anchor = self._x(self.start_frame if handle == "start" else self.end_frame)
            if abs(x - anchor) > 1:
                self._drag_offset = x - anchor
        else:
            self._drag = "seek"
        self._update_hover(event.position())
        self._move_drag(x)
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Request seek then playback only inside retained audio, never on a trim grip."""
        if (
            self.clip is None
            or not self.isEnabled()
            or event.button() != Qt.MouseButton.LeftButton
            or not self._plot().contains(event.position())
            or self._handle_at(event.position().x()) is not None
        ):
            return
        frame = self._frame(event.position().x())
        if not self.start_frame <= frame < self.end_frame:
            return
        self._drag = None
        self.setFocus()
        self.seek_requested.emit(frame)
        self.play_requested.emit(frame)
        event.accept()

    def _move_drag(self, x: float) -> None:
        frame = self._frame(x - self._drag_offset)
        if self._drag == "start":
            self.start_frame = min(frame, self.end_frame - 1)
            self.trim_changed.emit(self.start_frame, self.end_frame)
        elif self._drag == "end":
            self.end_frame = max(frame, self.start_frame + 1)
            self.trim_changed.emit(self.start_frame, self.end_frame)
        elif self._drag == "seek":
            self.seek_requested.emit(max(self.start_frame, min(frame, self.end_frame)))
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Update the current scrub or trim drag in source-frame coordinates."""
        self._update_hover(event.position())
        if self._drag is not None:
            self._move_drag(event.position().x())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Finish a source-relative scrub/trim gesture."""
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._move_drag(event.position().x())
            self._drag = None
            event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Provide precise keyboard seeking without interfering with global transport."""
        if self.clip is None or not self.isEnabled():
            return super().keyPressEvent(event)
        step = max(1, self.clip.sample_rate // 10)
        positions = {
            Qt.Key.Key_Left: self.position_frame - step,
            Qt.Key.Key_Right: self.position_frame + step,
            Qt.Key.Key_Home: self.start_frame,
            Qt.Key.Key_End: self.end_frame,
        }
        if event.key() in positions:
            self.seek_requested.emit(
                max(self.start_frame, min(positions[Qt.Key(event.key())], self.end_frame))
            )
            event.accept()
        else:
            super().keyPressEvent(event)
