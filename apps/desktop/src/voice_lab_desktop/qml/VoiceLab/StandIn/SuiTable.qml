// STAND-IN, marked for removal: scshafe-qt's table gap (design §6.4: sortable,
// virtualised). The API is the one asked of scshafe-qt in
// docs/design/PHASE-4-LOG.md ("SuiTable"); when scshafe-qt releases it, delete
// this file and its qmldir line.
//
// SuiTable: a sortable, virtualised table over a list model (thousands of rows;
// only the visible rows have delegates).
// - `model`: a list model with one role, `row`: a map from column key to the
//   cell's value, plus `_key` (a stable row id) and `_highlight` (bool, tints
//   the row). Cells are values already formatted for display.
// - `columns`: [{ key, title, width, align ("left" | "right"), sortable,
//   sortKey (default key), kind }]. `kind` says how a cell's value draws:
//   "text" (default) a string; "number" a string, right-aligned, tabular;
//   "chip" { text, tone } (SuiChip tones); "stack" { primary, secondary,
//   muted }: two lines; "link" a string that activates (cellActivated);
//   "actions" [{ id, text, enabled, tooltip }]: buttons (actionTriggered).
// - `pinnedColumns` (default 1): the first columns stay in view while the rest
//   scroll sideways when the columns are wider than the table.
// - `sortKey`, `sortDescending`: the sort shown (▲ / ▼); a header click on a
//   sortable column emits `sortRequested(sortKey)`; the owner sorts.
// - Selection: `currentIndex`; Up/Down/Home/End/PageUp/PageDown move it, Enter
//   emits `activated(index)`, as does a double click.
// - `endReached()`: the view came within `endThreshold` rows of the end (for
//   paging); emitted once per row count.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui

FocusScope {
    id: table

    property alias model: rows.model
    property var columns: []
    property int pinnedColumns: 1
    property string sortKey: ""
    property bool sortDescending: false
    property int rowHeight: 40
    property int headerHeight: 30
    property int endThreshold: 10
    property string label: "Table"
    property alias currentIndex: rows.currentIndex
    readonly property int count: rows.count
    property real hOffset: 0
    readonly property alias view: rows

    signal sortRequested(string key)
    signal activated(int index)
    signal cellActivated(int index, string key)
    signal actionTriggered(int index, string actionId)
    signal endReached()

    readonly property real _pinnedWidth: _sum(0, pinnedColumns)
    readonly property real _scrolledWidth: _sum(pinnedColumns, columns.length)
    readonly property real _viewport: Math.max(0, width - _pinnedWidth)
    readonly property real _maxOffset: Math.max(0, _scrolledWidth - _viewport)
    property int _endEmittedAt: -1

    function _sum(from, to) {
        let total = 0
        for (let i = from; i < Math.min(to, columns.length); i++)
            total += columns[i].width || 120
        return total
    }
    function positionAtRow(index) { rows.positionViewAtIndex(index, ListView.Contain) }
    function _checkEnd() {
        if (rows.count === 0 || rows.count === _endEmittedAt) return
        const last = rows.indexAt(0, rows.contentY + rows.height - 1)
        const end = last < 0 ? (rows.contentHeight <= rows.height ? rows.count - 1 : -1) : last
        if (end >= 0 && end >= rows.count - 1 - endThreshold) {
            _endEmittedAt = rows.count
            endReached()
        }
    }

    onHOffsetChanged: if (hOffset > _maxOffset) hOffset = _maxOffset
    on_MaxOffsetChanged: if (hOffset > _maxOffset) hOffset = _maxOffset
    Accessible.role: Accessible.Table
    Accessible.name: label

    component Cell: Item {
        id: cell
        required property var column
        property var value
        property int rowIndex: -1
        property bool header: false
        width: column.width || 120
        height: parent ? parent.height : 0
        clip: true

        readonly property string cellKind: column.kind || "text"
        readonly property bool alignEnd: column.align === "right" || cellKind === "number"

        // header
        T.AbstractButton {
            id: headerButton
            visible: cell.header
            anchors.fill: parent
            enabled: !!cell.column.sortable
            focusPolicy: Qt.NoFocus
            hoverEnabled: true
            Accessible.role: Accessible.ColumnHeader
            Accessible.name: cell.column.title
            onClicked: table.sortRequested(cell.column.sortKey || cell.column.key)
            contentItem: Text {
                textFormat: Text.PlainText
                leftPadding: SuiTheme.spaceSm
                rightPadding: SuiTheme.spaceSm
                horizontalAlignment: cell.alignEnd ? Text.AlignRight : Text.AlignLeft
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                font.pixelSize: SuiTheme.fontSizeSm
                font.weight: SuiTheme.fontWeightStrong
                color: headerButton.hovered && headerButton.enabled ? SuiTheme.textStrong : SuiTheme.muted
                readonly property bool active: table.sortKey !== ""
                    && table.sortKey === (cell.column.sortKey || cell.column.key)
                text: cell.column.title + (active ? (table.sortDescending ? " ▼" : " ▲") : "")
            }
        }

        // body
        Loader {
            anchors.fill: parent
            active: !cell.header
            sourceComponent: {
                switch (cell.cellKind) {
                case "chip": return chipCell
                case "stack": return stackCell
                case "link": return linkCell
                case "actions": return actionsCell
                default: return textCell
                }
            }
        }
        Component {
            id: textCell
            Text {
                textFormat: Text.PlainText
                leftPadding: SuiTheme.spaceSm
                rightPadding: SuiTheme.spaceSm
                text: cell.value === undefined || cell.value === null ? "" : String(cell.value)
                horizontalAlignment: cell.alignEnd ? Text.AlignRight : Text.AlignLeft
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                color: SuiTheme.text
                font.pixelSize: SuiTheme.fontSizeMd
                font.features: cell.cellKind === "number" ? ({ "tnum": 1 }) : ({})
            }
        }
        Component {
            id: chipCell
            Item {
                SuiChip {
                    visible: !!cell.value && cell.value.text !== ""
                    anchors.verticalCenter: parent.verticalCenter
                    x: SuiTheme.spaceSm
                    width: Math.min(implicitWidth, parent.width - 2 * SuiTheme.spaceSm)
                    text: cell.value ? cell.value.text : ""
                    tone: cell.value ? cell.value.tone : "neutral"
                }
            }
        }
        Component {
            id: stackCell
            Item {
            Column {
                anchors.verticalCenter: parent.verticalCenter
                leftPadding: SuiTheme.spaceSm
                rightPadding: SuiTheme.spaceSm
                Text {
                    textFormat: Text.PlainText
                    width: cell.width - 2 * SuiTheme.spaceSm
                    text: cell.value ? cell.value.primary : ""
                    color: cell.value && cell.value.muted ? SuiTheme.muted : SuiTheme.text
                    font.pixelSize: SuiTheme.fontSizeMd
                    elide: Text.ElideRight
                }
                Text {
                    textFormat: Text.PlainText
                    width: cell.width - 2 * SuiTheme.spaceSm
                    visible: !!cell.value && !!cell.value.secondary
                    text: cell.value ? (cell.value.secondary || "") : ""
                    color: SuiTheme.muted
                    font.pixelSize: SuiTheme.fontSizeXs
                    elide: Text.ElideRight
                }
            }
            }
        }
        Component {
            id: linkCell
            T.AbstractButton {
                id: link
                visible: !!cell.value
                hoverEnabled: true
                focusPolicy: Qt.NoFocus
                Accessible.role: Accessible.Link
                Accessible.name: String(cell.value || "")
                onClicked: table.cellActivated(cell.rowIndex, cell.column.key)
                contentItem: Text {
                    textFormat: Text.PlainText
                    leftPadding: SuiTheme.spaceSm
                    rightPadding: SuiTheme.spaceSm
                    text: String(cell.value || "")
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                    color: SuiTheme.accent
                    font.pixelSize: SuiTheme.fontSizeMd
                    font.underline: link.hovered
                }
            }
        }
        Component {
            id: actionsCell
            Row {
                leftPadding: SuiTheme.spaceSm
                spacing: SuiTheme.spaceXs
                height: cell.height
                Repeater {
                    model: cell.value || []
                    delegate: SuiButton {
                        required property var modelData
                        y: ((parent ? parent.height : height) - height) / 2
                        objectName: "action-" + modelData.id
                        text: modelData.text
                        enabled: modelData.enabled !== false
                        variant: "secondary"
                        Accessible.description: modelData.tooltip || ""
                        onClicked: table.actionTriggered(cell.rowIndex, modelData.id)
                        HoverHandler { id: hover }
                        Rectangle {
                            visible: hover.hovered && !!modelData.tooltip
                            z: 10
                            y: -height - 4
                            width: tip.implicitWidth + 2 * SuiTheme.spaceSm
                            height: tip.implicitHeight + SuiTheme.spaceXs
                            radius: SuiTheme.radiusSm
                            color: SuiTheme.popover
                            border.color: SuiTheme.borderStrong
                            Text {
                                id: tip
                                objectName: "tooltip"
                                textFormat: Text.PlainText
                                anchors.centerIn: parent
                                text: modelData.tooltip || ""
                                color: SuiTheme.text
                                font.pixelSize: SuiTheme.fontSizeSm
                            }
                        }
                    }
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // header
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: table.headerHeight
            color: SuiTheme.panel
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: SuiTheme.line }
            Row {
                id: pinnedHeader
                height: parent.height
                Repeater {
                    model: table.columns.slice(0, table.pinnedColumns)
                    delegate: Cell { required property var modelData; column: modelData; header: true }
                }
            }
            Item {
                x: table._pinnedWidth
                width: table._viewport
                height: parent.height
                clip: true
                Row {
                    x: -table.hOffset
                    height: parent.height
                    Repeater {
                        model: table.columns.slice(table.pinnedColumns)
                        delegate: Cell { required property var modelData; column: modelData; header: true }
                    }
                }
            }
        }

        ListView {
            id: rows
            objectName: "rows"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            focus: true
            activeFocusOnTab: true
            boundsBehavior: Flickable.StopAtBounds
            reuseItems: true
            cacheBuffer: table.rowHeight * 10
            keyNavigationEnabled: true
            highlightMoveDuration: 0
            currentIndex: -1
            onContentYChanged: table._checkEnd()
            onCountChanged: Qt.callLater(table._checkEnd)
            onHeightChanged: Qt.callLater(table._checkEnd)
            Keys.onReturnPressed: if (currentIndex >= 0) table.activated(currentIndex)
            Keys.onEnterPressed: if (currentIndex >= 0) table.activated(currentIndex)
            Keys.onLeftPressed: table.hOffset = Math.max(0, table.hOffset - 48)
            Keys.onRightPressed: table.hOffset = Math.min(table._maxOffset, table.hOffset + 48)
            T.ScrollBar.vertical: T.ScrollBar {
                id: vbar
                width: 8
                policy: T.ScrollBar.AsNeeded
                contentItem: Rectangle { radius: 3; color: SuiTheme.borderStrong; opacity: vbar.active ? 1 : 0.5 }
            }

            delegate: Rectangle {
                id: rowItem
                required property int index
                required property var row
                width: rows.width
                height: table.rowHeight
                color: ListView.isCurrentItem ? SuiTheme.accentSubtle
                     : rowItem.row && rowItem.row._highlight ? SuiTheme.alpha(SuiTheme.toneYellow, 0.08)
                     : hoverRow.hovered ? SuiTheme.bgHover : "transparent"
                Accessible.role: Accessible.Row
                Accessible.name: rowItem.row ? String(rowItem.row[table.columns.length ? table.columns[0].key : ""] || "") : ""
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: SuiTheme.line }
                HoverHandler { id: hoverRow }
                TapHandler {
                    onTapped: { rows.currentIndex = rowItem.index; rows.forceActiveFocus() }
                    onDoubleTapped: table.activated(rowItem.index)
                }
                Row {
                    height: parent.height
                    Repeater {
                        model: table.columns.slice(0, table.pinnedColumns)
                        delegate: Cell {
                            required property var modelData
                            column: modelData
                            rowIndex: rowItem.index
                            value: rowItem.row ? rowItem.row[modelData.key] : undefined
                        }
                    }
                }
                Item {
                    x: table._pinnedWidth
                    width: table._viewport
                    height: parent.height
                    clip: true
                    Row {
                        x: -table.hOffset
                        height: parent.height
                        Repeater {
                            model: table.columns.slice(table.pinnedColumns)
                            delegate: Cell {
                                required property var modelData
                                column: modelData
                                rowIndex: rowItem.index
                                value: rowItem.row ? rowItem.row[modelData.key] : undefined
                            }
                        }
                    }
                }
            }
        }

        T.ScrollBar {
            id: hbar
            objectName: "horizontalScrollBar"
            Layout.fillWidth: true
            Layout.leftMargin: table._pinnedWidth
            visible: table._maxOffset > 0
            orientation: Qt.Horizontal
            implicitHeight: 8
            size: table._scrolledWidth > 0 ? Math.min(1, table._viewport / table._scrolledWidth) : 1
            position: table._scrolledWidth > 0 ? table.hOffset / table._scrolledWidth : 0
            onPositionChanged: if (pressed) table.hOffset = position * table._scrolledWidth
            contentItem: Rectangle { radius: 3; color: SuiTheme.borderStrong }
        }
    }
}
