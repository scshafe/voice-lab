// A chart's "Data table" (the SPA's <details>): collapsed until asked for.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ColumnLayout {
    id: root
    property alias model: table.model
    property alias columns: table.columns
    property bool open: false
    Layout.fillWidth: true
    spacing: SuiTheme.spaceXs

    SuiButton {
        objectName: "dataTableToggle"
        text: (root.open ? "▾" : "▸") + " Data table"
        variant: "ghost"
        onClicked: root.open = !root.open
    }
    SuiTable {
        id: table
        objectName: "dataTable"
        Layout.fillWidth: true
        Layout.preferredHeight: headerHeight + Math.max(1, count) * rowHeight + 10
        visible: root.open
        rowHeight: 28
        pinnedColumns: 1
        label: "Data table"
    }
}
