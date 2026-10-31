// The trim row: In and Out from the playhead, Save trim, Reset. The take under review keeps its
// edit until Save; a take recorded elsewhere shows its trim. One line in the centre; in the
// inspector (D-058's layout) `compact`: In, Out, then Save trim and Reset, a line each. The
// model is the studio's `takes` (screens/studio_takes.py).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui

Item {
    id: trimRow

    required property var takes
    property bool compact: false
    // The In and Out captions only where a line has room for them (they depend on the
    // window's size alone): on the smallest window, Set In and Set Out name the times beside
    // them.
    readonly property bool captions: compact || width >= 520

    implicitHeight: compact ? grid.implicitHeight : 24

    component CompactButton: SuiButton {
        Layout.preferredHeight: 24
        topPadding: 0
        bottomPadding: 0
        variant: "ghost"
    }

    GridLayout {
        id: grid
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: trimRow.compact ? undefined : parent.verticalCenter
        columnSpacing: SuiTheme.spaceXs
        rowSpacing: SuiTheme.spaceXs

        SuiText {
            Layout.row: 0
            Layout.column: 0
            text: "In"
            variant: "caption"
            visible: trimRow.captions
        }
        SuiText {
            objectName: "studioTrimIn"
            Layout.row: 0
            Layout.column: 1
            text: trimRow.takes.inText
            variant: "code"
            tone: trimRow.takes.trimDirty ? "warn" : ""
            Accessible.name: "In " + text
        }
        CompactButton {
            objectName: "studioSetIn"
            Layout.row: 0
            Layout.column: 2
            text: "Set In"
            enabled: trimRow.takes.canEditTrim
            onClicked: trimRow.takes.setIn()
        }
        SuiText {
            Layout.row: trimRow.compact ? 1 : 0
            Layout.column: trimRow.compact ? 0 : 3
            Layout.leftMargin: trimRow.compact ? 0 : SuiTheme.spaceSm
            text: "Out"
            variant: "caption"
            visible: trimRow.captions
        }
        SuiText {
            objectName: "studioTrimOut"
            Layout.row: trimRow.compact ? 1 : 0
            Layout.column: trimRow.compact ? 1 : 4
            Layout.leftMargin: trimRow.captions || trimRow.compact ? 0 : SuiTheme.spaceSm
            text: trimRow.takes.outText
            variant: "code"
            tone: trimRow.takes.trimDirty ? "warn" : ""
            Accessible.name: "Out " + text
        }
        CompactButton {
            objectName: "studioSetOut"
            Layout.row: trimRow.compact ? 1 : 0
            Layout.column: trimRow.compact ? 2 : 5
            text: "Set Out"
            enabled: trimRow.takes.canEditTrim
            onClicked: trimRow.takes.setOut()
        }
        Item {
            id: trimGap
            Layout.row: 0
            Layout.column: trimRow.compact ? 3 : 6
            Layout.fillWidth: true
        }
        CompactButton {
            objectName: "studioSaveTrim"
            Layout.row: trimRow.compact ? 2 : 0
            Layout.column: trimRow.compact ? 0 : 7
            Layout.columnSpan: trimRow.compact ? 2 : 1
            text: "Save trim"
            variant: "secondary"
            enabled: trimRow.takes.canSaveTrim
            onClicked: trimRow.takes.saveTrim()
        }
        CompactButton {
            objectName: "studioResetTrim"
            Layout.row: trimRow.compact ? 2 : 0
            Layout.column: trimRow.compact ? 2 : 8
            text: "Reset"
            enabled: trimRow.takes.canResetTrim
            onClicked: trimRow.takes.resetTrim()
        }
    }
    // "edited" sits in the gap and never takes room from the row: shown when it fits there
    // (the times turn amber either way).
    SuiText {
        objectName: "studioTrimEdited"
        x: trimGap.x + Math.round((trimGap.width - width) / 2)
        y: grid.y + trimGap.y + Math.round((trimGap.height - height) / 2)
        text: "edited"
        variant: "caption"
        tone: "warn"
        visible: trimRow.takes.trimDirty && trimGap.width >= width + 2 * SuiTheme.spaceXs
    }
}
