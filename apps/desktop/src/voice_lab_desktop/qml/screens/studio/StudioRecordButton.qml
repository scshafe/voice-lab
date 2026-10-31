// The footer's 56-point Record/Stop control (NATIVE_RECORDING_UX.md §4): one circular
// control that becomes a red Stop in the same slot. Its bounds and hit target never change.
import QtQuick
import QtQuick.Templates as T
import Scshafe.Ui

T.Button {
    id: control

    property bool stopping: false
    // The take is live: a green ring says Speak (D-058, item 3).
    property bool cue: false

    implicitWidth: 56
    implicitHeight: 56
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    opacity: enabled ? 1 : SuiTheme.disabledOpacity
    Accessible.role: Accessible.Button
    Accessible.name: control.stopping ? "Stop" : "Record"

    contentItem: Item {
        Rectangle {
            objectName: "recordGlyph"
            anchors.centerIn: parent
            width: control.stopping ? 20 : 36
            height: width
            radius: control.stopping ? 4 : width / 2
            color: SuiTheme.toneRed
        }
    }
    background: Rectangle {
        radius: width / 2
        color: control.down ? SuiTheme.alpha(SuiTheme.toneRed, 0.18)
               : control.hovered ? SuiTheme.alpha(SuiTheme.toneRed, 0.10) : SuiTheme.field
        border.width: control.cue ? 3 : 2
        border.color: control.cue ? SuiTheme.toneGreen
                      : control.stopping ? SuiTheme.toneRed : SuiTheme.borderStrong
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
