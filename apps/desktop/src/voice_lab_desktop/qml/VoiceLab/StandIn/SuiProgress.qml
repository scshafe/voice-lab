// STAND-IN, marked for removal: scshafe-qt's determinate-progress gap (design §6.4).
// The API is the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md
// ("SuiProgress"); when scshafe-qt releases it, delete this file and its qmldir line.
//
// SuiProgress: a determinate progress bar with an optional label, detail and cancel.
// - `value` in [0, 1] (clamped); `indeterminate` shows activity without a value;
// - `label` (left, strong) and `detail` (right, muted; e.g. "12.4 of 48 MB");
// - `cancelable` shows a Cancel button; `cancelRequested()` fires when it is used.
// Accessible as a progress bar with the value as a percentage.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui

ColumnLayout {
    id: root

    property real value: 0
    property bool indeterminate: false
    property string label: ""
    property string detail: ""
    property bool cancelable: false
    property string tone: "info"
    readonly property real clamped: Math.max(0, Math.min(1, value))

    signal cancelRequested()

    spacing: SuiTheme.spaceXs
    Accessible.role: Accessible.ProgressBar
    Accessible.name: label
    Accessible.description: indeterminate ? "working" : Math.round(clamped * 100) + "%"

    RowLayout {
        Layout.fillWidth: true
        visible: root.label !== "" || root.detail !== "" || root.cancelable
        spacing: SuiTheme.spaceSm
        Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            text: root.label
            color: SuiTheme.text
            font.pixelSize: SuiTheme.fontSizeSm
            font.weight: SuiTheme.fontWeightStrong
            elide: Text.ElideRight
            Accessible.ignored: true
        }
        Text {
            textFormat: Text.PlainText
            text: root.detail
            color: SuiTheme.muted
            font.pixelSize: SuiTheme.fontSizeSm
            font.features: ({ "tnum": 1 })
            Accessible.ignored: true
        }
        SuiButton {
            objectName: "cancelButton"
            visible: root.cancelable
            text: "Cancel"
            variant: "ghost"
            onClicked: root.cancelRequested()
        }
    }
    Rectangle {
        id: track
        Layout.fillWidth: true
        implicitHeight: 6
        radius: 3
        color: SuiTheme.line
        clip: true
        Rectangle {
            objectName: "bar"
            height: parent.height
            radius: 3
            color: SuiTheme.toneBase(root.tone)
            width: root.indeterminate ? parent.width * 0.3 : parent.width * root.clamped
            x: root.indeterminate ? sweep.x : 0
            Behavior on width { enabled: !root.indeterminate; NumberAnimation { duration: SuiTheme.duration } }
        }
        Item {
            id: sweep
            visible: false
            SequentialAnimation on x {
                running: root.indeterminate && SuiTheme.duration > 0 && root.visible
                loops: Animation.Infinite
                NumberAnimation { from: -track.width * 0.3; to: track.width; duration: 1200 }
            }
        }
    }
}
