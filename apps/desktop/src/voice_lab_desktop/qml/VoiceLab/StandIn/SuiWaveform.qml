// STAND-IN, marked for removal: scshafe-qt's waveform gap (design §6.4). The API is
// the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md ("SuiWaveform"); the
// reference is voice-lab's desktop_waveform.py (WaveformView). When scshafe-qt
// releases it, delete this file and its qmldir line.
//
// SuiWaveform: a recording's waveform with trim handles and a playhead, in frames of
// the original (never resampled, never edited here).
// - `bins`: [min0, max0, min1, max1, ...] in [-1, 1] (about 2,048 pairs for any length;
//   the owner computes them); `frameCount`: the original's frames; `sampleRate`
//   (48000) for the keyboard's step.
// - `trimStart`, `trimEnd`: the kept frames [trimStart, trimEnd). A controlled
//   component: it never assigns them; dragging a handle shows the new edge and, on
//   release, emits `trimChanged(start, end)`; the owner's binding carries it back.
// - `playhead`: the frame playing (-1: none).
// - `seekRequested(frame)`: a click in the waveform, or Left/Right (0.1 s), Home/End
//   (the trim's edges) with the keyboard focus on it. `playRequested(frame)`: a
//   double-click.
// - `editable` (true): the handles can be dragged; `label`: the accessible name.
// Implicit size 600 × 120. Accessible as a slider whose value is the playhead.
import QtQuick
import Scshafe.Ui

Item {
    id: wave

    property var bins: []
    property int frameCount: 0
    property int sampleRate: 48000
    property int trimStart: 0
    property int trimEnd: 0
    property int playhead: -1
    property bool editable: true
    property string label: "Waveform"

    signal seekRequested(int frame)
    signal trimChanged(int start, int end)
    signal playRequested(int frame)

    // While a handle is dragged: the edge shown (-1: none).
    property int _dragStart: -1
    property int _dragEnd: -1
    readonly property int shownStart: _dragStart >= 0 ? _dragStart : trimStart
    readonly property int shownEnd: _dragEnd >= 0 ? _dragEnd : (trimEnd > 0 ? trimEnd : frameCount)
    readonly property real _framesPerPixel: width > 0 && frameCount > 0 ? frameCount / width : 1

    implicitWidth: 600
    implicitHeight: 120
    activeFocusOnTab: true
    Accessible.role: Accessible.Slider
    Accessible.name: label
    Accessible.description: frameCount > 0
        ? "kept " + (shownStart / sampleRate).toFixed(2) + " to " + (shownEnd / sampleRate).toFixed(2)
          + " of " + (frameCount / sampleRate).toFixed(2) + " seconds"
        : "no audio"

    function frameAt(x) {
        if (frameCount <= 0 || width <= 0)
            return 0
        return Math.max(0, Math.min(frameCount, Math.round(x / width * frameCount)))
    }
    function xAt(frame) {
        return frameCount > 0 ? frame / frameCount * width : 0
    }

    onBinsChanged: canvas.requestPaint()
    onWidthChanged: canvas.requestPaint()
    onHeightChanged: canvas.requestPaint()

    Keys.onPressed: (event) => {
        if (frameCount <= 0 || event.modifiers !== Qt.NoModifier)
            return
        const step = Math.round(sampleRate / 10)
        const from = playhead >= 0 ? playhead : shownStart
        if (event.key === Qt.Key_Left) {
            wave.seekRequested(Math.max(0, from - step)); event.accepted = true
        } else if (event.key === Qt.Key_Right) {
            wave.seekRequested(Math.min(frameCount, from + step)); event.accepted = true
        } else if (event.key === Qt.Key_Home) {
            wave.seekRequested(shownStart); event.accepted = true
        } else if (event.key === Qt.Key_End) {
            wave.seekRequested(shownEnd); event.accepted = true
        }
    }

    Rectangle {
        anchors.fill: parent
        radius: SuiTheme.radiusSm
        color: SuiTheme.tint
        border.color: wave.activeFocus ? SuiTheme.focusRing : SuiTheme.line
        border.width: wave.activeFocus ? SuiTheme.focusRingWidth : 1
    }

    Canvas {
        id: canvas
        objectName: "waveformCanvas"
        anchors.fill: parent
        anchors.margins: 1
        renderStrategy: Canvas.Immediate
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const pairs = Math.floor((wave.bins ? wave.bins.length : 0) / 2)
            if (pairs <= 0)
                return
            const mid = height / 2
            ctx.strokeStyle = SuiTheme.accent
            ctx.lineWidth = Math.max(1, width / pairs)
            ctx.beginPath()
            for (let i = 0; i < pairs; ++i) {
                const x = (i + 0.5) / pairs * width
                const low = Math.max(-1, Math.min(1, wave.bins[2 * i]))
                const high = Math.max(-1, Math.min(1, wave.bins[2 * i + 1]))
                ctx.moveTo(x, mid - high * mid)
                ctx.lineTo(x, Math.max(mid - low * mid, mid - high * mid + 1))
            }
            ctx.stroke()
        }
    }

    // Outside the trim: shaded.
    Rectangle {
        objectName: "trimShadeStart"
        x: 0; y: 0
        width: wave.xAt(wave.shownStart)
        height: wave.height
        color: SuiTheme.bg
        opacity: 0.6
    }
    Rectangle {
        objectName: "trimShadeEnd"
        x: wave.xAt(wave.shownEnd)
        y: 0
        width: Math.max(0, wave.width - x)
        height: wave.height
        color: SuiTheme.bg
        opacity: 0.6
    }

    Rectangle {
        objectName: "playheadLine"
        visible: wave.playhead >= 0 && wave.frameCount > 0
        x: wave.xAt(Math.max(0, wave.playhead)) - 1
        width: 2
        height: wave.height
        color: SuiTheme.textStrong
    }

    MouseArea {
        objectName: "waveformArea"
        anchors.fill: parent
        enabled: wave.frameCount > 0
        onClicked: (mouse) => { wave.forceActiveFocus(); wave.seekRequested(wave.frameAt(mouse.x)) }
        onDoubleClicked: (mouse) => wave.playRequested(wave.frameAt(mouse.x))
    }

    component Handle: Rectangle {
        id: handle
        property bool atStart: true
        width: 10
        height: wave.height
        x: wave.xAt(atStart ? wave.shownStart : wave.shownEnd) - width / 2
        radius: 2
        color: area.pressed || area.containsMouse ? SuiTheme.accentHover : SuiTheme.accent
        opacity: wave.editable ? 0.9 : 0
        visible: wave.frameCount > 0
        MouseArea {
            id: area
            anchors.fill: parent
            anchors.margins: -4
            enabled: wave.editable
            hoverEnabled: true
            cursorShape: Qt.SizeHorCursor
            preventStealing: true
            onPositionChanged: (mouse) => {
                if (!pressed)
                    return
                const frame = wave.frameAt(handle.mapToItem(wave, mouse.x, 0).x)
                if (handle.atStart)
                    wave._dragStart = Math.min(frame, wave.shownEnd - 1)
                else
                    wave._dragEnd = Math.max(frame, wave.shownStart + 1)
            }
            onReleased: {
                const start = wave.shownStart
                const end = wave.shownEnd
                wave._dragStart = -1
                wave._dragEnd = -1
                if (start !== wave.trimStart || end !== wave.trimEnd)
                    wave.trimChanged(start, end)
            }
        }
    }

    Handle { objectName: "trimStartHandle"; atStart: true }
    Handle { objectName: "trimEndHandle"; atStart: false }
}
