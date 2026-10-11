// STAND-IN, marked for removal: scshafe-qt's form-controls gap (design §6.4). The
// API is the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md ("SuiSlider");
// when scshafe-qt releases it, delete this file and its qmldir line.
//
// SuiSlider: a horizontal value in [from, to] (a playback position, a level).
// `value`, `from`, `to`, `stepSize`, `label` (accessible name); `moved()` fires on
// a user change (drag, click, arrows, Home/End); `pressed` while dragging.
import QtQuick
import QtQuick.Templates as T
import Scshafe.Ui

T.Slider {
    id: control

    property string label: ""

    implicitWidth: 160
    implicitHeight: 20
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    opacity: enabled ? 1 : SuiTheme.disabledOpacity
    Accessible.role: Accessible.Slider
    Accessible.name: label

    background: Rectangle {
        x: control.leftPadding
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: control.availableWidth
        height: 4
        radius: 2
        color: SuiTheme.line
        Rectangle {
            width: control.visualPosition * parent.width
            height: parent.height
            radius: 2
            color: SuiTheme.accent
        }
    }
    handle: Rectangle {
        x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: 12
        height: 12
        radius: 6
        color: control.pressed ? SuiTheme.accentHover : SuiTheme.accent
        border.width: 1
        border.color: SuiTheme.panel
        Rectangle {
            visible: control.visualFocus
            anchors.fill: parent
            anchors.margins: -(SuiTheme.focusRingOffset + SuiTheme.focusRingWidth)
            radius: width / 2
            color: "transparent"
            border.width: SuiTheme.focusRingWidth
            border.color: SuiTheme.focusRing
        }
    }
}
