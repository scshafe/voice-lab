// The studio's waveform (NATIVE_RECORDING_UX.md §4): the selected take's signed extrema on
// the fixed dB display scale (the owner maps them), over a view [viewStart, viewEnd) of its
// frames (Zoom and Fit), with the kept frames' edges, the trim handles and the playhead.
// While a take is recorded it draws the live bins in red, filling `slots` from the left (a
// rolling five-second viewport), without handles. Frames are the original's; nothing here
// edits audio. A click moves the playhead (and takes the keyboard focus), a double-click
// plays, dragging a handle sets the trim on release (a controlled component: the owner's
// values come back as bindings). With the focus here, ← and → step a tenth of a second and
// Home and End go to the kept frames' edges; a held key's repeats are ignored.
import QtQuick
import Scshafe.Ui

Item {
    id: wave

    property var bins: []
    property int slots: 0
    property int viewStart: 0
    property int viewEnd: 0
    property int frameCount: 0
    property int trimStart: 0
    property int trimEnd: 0
    property int playhead: -1
    property bool live: false
    property bool editable: false
    property string note: ""

    signal seekRequested(int frame)
    signal trimRequested(int start, int end)
    signal playRequested()
    signal stepRequested(int frames)
    signal edgeRequested(bool end)

    property int _dragStart: -1
    property int _dragEnd: -1
    readonly property int shownStart: _dragStart >= 0 ? _dragStart : trimStart
    readonly property int shownEnd: _dragEnd >= 0 ? _dragEnd : trimEnd
    readonly property bool hasTake: !live && frameCount > 0 && viewEnd > viewStart
    readonly property int pairs: Math.floor((bins ? bins.length : 0) / 2)

    activeFocusOnTab: hasTake
    Accessible.role: Accessible.Slider
    Accessible.name: live ? "Live waveform" : "Waveform"
    Keys.onPressed: (event) => {
        if (!wave.hasTake || event.isAutoRepeat || event.modifiers !== Qt.NoModifier)
            return
        if (event.key === Qt.Key_Left) {
            wave.stepRequested(-4800); event.accepted = true
        } else if (event.key === Qt.Key_Right) {
            wave.stepRequested(4800); event.accepted = true
        } else if (event.key === Qt.Key_Home) {
            wave.edgeRequested(false); event.accepted = true
        } else if (event.key === Qt.Key_End) {
            wave.edgeRequested(true); event.accepted = true
        }
    }
    Accessible.description: hasTake
        ? "kept " + (shownStart / 48000).toFixed(2) + " to " + (shownEnd / 48000).toFixed(2)
          + " of " + (frameCount / 48000).toFixed(2) + " seconds" : note

    function frameAt(x) {
        if (!hasTake || width <= 0)
            return 0
        const frame = viewStart + x / width * (viewEnd - viewStart)
        return Math.max(0, Math.min(frameCount, Math.round(frame)))
    }
    function xAt(frame) {
        return hasTake ? (frame - viewStart) / (viewEnd - viewStart) * width : 0
    }

    onBinsChanged: canvas.requestPaint()
    onWidthChanged: canvas.requestPaint()
    onHeightChanged: canvas.requestPaint()
    onLiveChanged: canvas.requestPaint()

    Rectangle {
        anchors.verticalCenter: parent.verticalCenter
        x: SuiTheme.spaceSm
        width: parent.width - 2 * SuiTheme.spaceSm
        height: 1
        color: wave.live ? SuiTheme.toneRed : SuiTheme.borderStrong
        opacity: 0.6
    }
    Rectangle {
        objectName: "studioWaveFocus"
        anchors.fill: parent
        visible: wave.activeFocus
        color: "transparent"
        radius: SuiTheme.radiusSm
        border.width: SuiTheme.focusRingWidth
        border.color: SuiTheme.focusRing
        z: 2
    }

    Canvas {
        id: canvas
        objectName: "studioWaveCanvas"
        anchors.fill: parent
        anchors.topMargin: 2
        anchors.bottomMargin: 2
        renderStrategy: Canvas.Immediate
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const pairs = wave.pairs
            if (pairs <= 0)
                return
            const slots = Math.max(pairs, wave.slots)
            const mid = height / 2
            ctx.strokeStyle = wave.live ? SuiTheme.toneRed : SuiTheme.accent
            ctx.lineWidth = Math.max(1, width / slots)
            ctx.beginPath()
            for (let i = 0; i < pairs; ++i) {
                const x = (i + 0.5) / slots * width
                const low = Math.max(-1, Math.min(1, wave.bins[2 * i]))
                const high = Math.max(-1, Math.min(1, wave.bins[2 * i + 1]))
                ctx.moveTo(x, mid - high * mid)
                ctx.lineTo(x, Math.max(mid - low * mid, mid - high * mid + 1))
            }
            ctx.stroke()
        }
    }

    // Outside the kept frames: shaded.
    Rectangle {
        objectName: "studioTrimShadeStart"
        visible: wave.hasTake
        x: 0
        width: Math.max(0, Math.min(wave.width, wave.xAt(wave.shownStart)))
        height: wave.height
        color: SuiTheme.bg
        opacity: 0.65
    }
    Rectangle {
        objectName: "studioTrimShadeEnd"
        visible: wave.hasTake
        x: Math.max(0, Math.min(wave.width, wave.xAt(wave.shownEnd)))
        width: Math.max(0, wave.width - x)
        height: wave.height
        color: SuiTheme.bg
        opacity: 0.65
    }
    Rectangle {
        objectName: "studioPlayhead"
        visible: wave.hasTake && wave.playhead >= wave.viewStart && wave.playhead <= wave.viewEnd
        x: wave.xAt(wave.playhead) - 1
        width: 2
        height: wave.height
        color: SuiTheme.textStrong
    }

    MouseArea {
        objectName: "studioWaveArea"
        anchors.fill: parent
        enabled: wave.hasTake
        onClicked: (mouse) => {
            wave.forceActiveFocus(Qt.MouseFocusReason)
            wave.seekRequested(wave.frameAt(mouse.x))
        }
        onDoubleClicked: wave.playRequested()
    }

    component Handle: Rectangle {
        id: handle
        property bool atStart: true
        width: 8
        height: wave.height
        x: wave.xAt(atStart ? wave.shownStart : wave.shownEnd) - width / 2
        radius: 2
        color: area.pressed || area.containsMouse ? SuiTheme.accentHover : SuiTheme.accent
        visible: wave.hasTake && wave.editable && x + width / 2 >= 0 && x + width / 2 <= wave.width
        MouseArea {
            id: area
            anchors.fill: parent
            anchors.margins: -4
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
                    wave.trimRequested(start, end)
            }
        }
    }

    Handle { objectName: "studioTrimStartHandle"; atStart: true }
    Handle { objectName: "studioTrimEndHandle"; atStart: false }

    // Why the area shows nothing (loading, no take, waiting for frames), in its middle; over
    // a waveform the owner shows the scale's name beside the timer, clear of the handles.
    Text {
        objectName: "studioWaveEmpty"
        anchors.centerIn: parent
        width: parent.width - 2 * SuiTheme.spaceLg
        visible: wave.pairs === 0 && wave.note !== ""
        horizontalAlignment: Text.AlignHCenter
        textFormat: Text.PlainText
        text: wave.note
        color: SuiTheme.muted
        font.pixelSize: SuiTheme.fontSizeSm
        wrapMode: Text.Wrap
    }
}
