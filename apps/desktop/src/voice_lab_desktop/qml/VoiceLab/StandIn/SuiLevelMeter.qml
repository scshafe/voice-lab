// STAND-IN, marked for removal: scshafe-qt's level-meter gap (design §6.4). The API
// is the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md ("SuiLevelMeter");
// when scshafe-qt releases it, delete this file and its qmldir line.
//
// SuiLevelMeter: a live input level (a recording's meter).
// - `rms` in [0, 1]: the bar (the signal's body); `peak` in [0, 1]: a thin marker
//   (the block's highest sample). Both are fractions of the meter's scale, which the
//   owner maps from dBFS (the measurement capture maps -60…0 dBFS); clamped.
// - `clipping`: the signal clipped (the bar and the marker turn danger); `warnAt`
//   (0.92): a peak above it turns the bar warn.
// - `label`: the accessible name; `text` (optional): a short reading for assistive
//   technology ("-12.3 dBFS peak"); `orientation`: Qt.Horizontal (default) or
//   Qt.Vertical.
// - Updated about 20 times a second (the capture's 50 ms meter timer); no animation,
//   so it is never late. Implicit size 200 × 14 (horizontal), 14 × 120 (vertical).
// Accessible as a progress bar whose description is the peak as a percentage.
import QtQuick
import Scshafe.Ui

Item {
    id: meter

    property real peak: 0
    property real rms: 0
    property bool clipping: false
    property real warnAt: 0.92
    property string label: "Input level"
    property string text: ""
    property int orientation: Qt.Horizontal
    readonly property bool horizontal: orientation !== Qt.Vertical
    readonly property real peakClamped: Math.max(0, Math.min(1, peak))
    readonly property real rmsClamped: Math.max(0, Math.min(1, rms))
    readonly property color barColor: clipping ? SuiTheme.toneRed
                                    : peakClamped > warnAt ? SuiTheme.toneYellow : SuiTheme.ok

    implicitWidth: horizontal ? 200 : 14
    implicitHeight: horizontal ? 14 : 120
    Accessible.role: Accessible.ProgressBar
    Accessible.name: label
    Accessible.description: (clipping ? "clipped, " : "") + Math.round(peakClamped * 100) + "%"
        + (text !== "" ? ", " + text : "")

    Rectangle {
        id: track
        objectName: "track"
        anchors.fill: parent
        radius: SuiTheme.radiusSm
        color: SuiTheme.tint
        border.color: SuiTheme.line
        clip: true
        Rectangle {
            objectName: "bar"
            x: 0
            y: meter.horizontal ? 0 : parent.height * (1 - meter.rmsClamped)
            width: meter.horizontal ? parent.width * meter.rmsClamped : parent.width
            height: meter.horizontal ? parent.height : parent.height * meter.rmsClamped
            radius: SuiTheme.radiusSm
            color: meter.barColor
        }
        Rectangle {
            objectName: "peakMarker"
            x: meter.horizontal ? Math.max(0, parent.width * meter.peakClamped - width) : 0
            y: meter.horizontal ? 0
               : Math.min(parent.height - height, parent.height * (1 - meter.peakClamped))
            width: meter.horizontal ? 2 : parent.width
            height: meter.horizontal ? parent.height : 2
            color: meter.clipping ? SuiTheme.toneRed : SuiTheme.textStrong
            visible: meter.peakClamped > 0
        }
    }
}
