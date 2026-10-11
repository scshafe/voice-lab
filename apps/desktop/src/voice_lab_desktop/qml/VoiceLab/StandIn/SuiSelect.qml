// STAND-IN, marked for removal: scshafe-qt's form-controls gap (design §6.4). The
// API is the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md ("SuiSelect");
// when scshafe-qt releases it, delete this file and its qmldir line.
//
// SuiSelect: a single choice from a short list (a filter, a form field).
// - `options`: [{ value: string, label: string }]; `value`: the chosen value
//   (one of the options' values; "" is a normal value, e.g. "All");
// - `label`: the visible caption above the control (also its accessible name);
// - `valueSelected(value)` fires on a user choice (mouse, Enter, Space, arrows);
//   `value` follows it. The popup lists the labels; Escape closes it.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui

ColumnLayout {
    id: root

    property var options: []
    property string value: ""
    property string label: ""
    readonly property int currentIndex: _indexOf(value)
    readonly property string currentLabel: currentIndex >= 0 ? options[currentIndex].label : ""
    readonly property bool popupOpen: combo.popup.visible

    signal valueSelected(string value)

    function _indexOf(v) {
        for (let i = 0; i < options.length; i++)
            if (options[i].value === v) return i
        return -1
    }
    function select(v) {
        if (_indexOf(v) < 0 || v === value) return
        value = v
        valueSelected(v)
    }

    spacing: 2

    Text {
        textFormat: Text.PlainText
        visible: root.label !== ""
        text: root.label
        color: SuiTheme.muted
        font.pixelSize: SuiTheme.fontSizeXs
        font.weight: SuiTheme.fontWeightStrong
        Accessible.ignored: true
    }

    T.ComboBox {
        id: combo
        objectName: "combo"
        Layout.fillWidth: true
        implicitWidth: Math.max(140, implicitContentWidth + leftPadding + rightPadding)
        implicitHeight: SuiTheme.controlHeight
        leftPadding: SuiTheme.spaceSm
        rightPadding: SuiTheme.spaceLg + SuiTheme.spaceXs
        focusPolicy: Qt.StrongFocus
        hoverEnabled: true
        font.pixelSize: SuiTheme.fontSizeMd
        model: root.options
        textRole: "label"
        valueRole: "value"
        currentIndex: root.currentIndex
        opacity: enabled ? 1 : SuiTheme.disabledOpacity

        Accessible.role: Accessible.ComboBox
        Accessible.name: root.label
        Accessible.description: root.currentLabel

        onActivated: (index) => root.select(root.options[index].value)

        contentItem: Text {
            textFormat: Text.PlainText
            text: root.currentLabel
            font: combo.font
            color: SuiTheme.text
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        indicator: Text {
            textFormat: Text.PlainText
            x: combo.width - width - SuiTheme.spaceSm
            y: (combo.height - height) / 2
            text: "▾"
            color: SuiTheme.muted
            font.pixelSize: SuiTheme.fontSizeSm
            Accessible.ignored: true
        }
        background: Rectangle {
            radius: SuiTheme.radiusSm
            color: SuiTheme.field
            border.width: 1
            border.color: combo.activeFocus ? SuiTheme.accent
                          : combo.hovered ? SuiTheme.borderHover : SuiTheme.fieldBorder
            Rectangle {
                visible: combo.visualFocus
                anchors.fill: parent
                anchors.margins: -(SuiTheme.focusRingOffset + SuiTheme.focusRingWidth)
                radius: parent.radius + SuiTheme.focusRingOffset + SuiTheme.focusRingWidth
                color: "transparent"
                border.width: SuiTheme.focusRingWidth
                border.color: SuiTheme.focusRing
            }
        }
        delegate: T.ItemDelegate {
            id: option
            required property int index
            required property var modelData
            width: ListView.view ? ListView.view.width : implicitWidth
            implicitHeight: SuiTheme.rowHeight
            leftPadding: SuiTheme.spaceSm
            highlighted: combo.highlightedIndex === index
            contentItem: Text {
                textFormat: Text.PlainText
                text: option.modelData.label
                color: option.index === root.currentIndex ? SuiTheme.textStrong : SuiTheme.text
                font.pixelSize: SuiTheme.fontSizeMd
                font.weight: option.index === root.currentIndex ? SuiTheme.fontWeightStrong
                                                                : SuiTheme.fontWeightNormal
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            background: Rectangle {
                color: option.highlighted ? SuiTheme.bgHover : "transparent"
            }
        }
        popup: T.Popup {
            y: combo.height + 2
            width: combo.width
            implicitHeight: Math.min(contentItem.implicitHeight + 2 * padding, 320)
            padding: 2
            contentItem: ListView {
                clip: true
                implicitHeight: contentHeight
                model: combo.popup.visible ? combo.delegateModel : null
                currentIndex: combo.highlightedIndex
                boundsBehavior: Flickable.StopAtBounds
            }
            background: Rectangle {
                radius: SuiTheme.radiusSm
                color: SuiTheme.popover
                border.width: 1
                border.color: SuiTheme.borderStrong
            }
        }
    }
}
