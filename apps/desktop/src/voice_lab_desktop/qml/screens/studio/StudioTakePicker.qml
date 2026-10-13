// The take picker (NATIVE_RECORDING_UX.md §4: "a compact take-picker row above the waveform;
// a popover ... holds extended history"): the prompt's takes, newest first, from this
// machine and the PC; the selected take's actions (Remove, Reject with a reason, Restore);
// and History, every take with its date, length, status and where it lives. The model is
// the studio's `takes` (screens/studio_takes.py).
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui
import VoiceLab.StandIn

Item {
    id: picker
    objectName: "studioTakePickerRow"

    required property var takes
    property bool busy: false
    readonly property bool menuOpen: combo.popup.visible || historyPopup.visible || rejectDialog.visible

    function indexOf(key) {
        const options = takes.takeOptions
        for (let i = 0; i < options.length; ++i)
            if (options[i].value === key) return i
        return -1
    }

    component RowButton: SuiButton {
        Layout.preferredHeight: 24
        topPadding: 0
        bottomPadding: 0
        variant: "ghost"
    }

    RowLayout {
        anchors.fill: parent
        spacing: SuiTheme.spaceXs

        T.ComboBox {
            id: combo
            objectName: "studioTakeSelect"
            Layout.fillWidth: true
            Layout.preferredHeight: 24
            leftPadding: SuiTheme.spaceSm
            rightPadding: SuiTheme.spaceLg + SuiTheme.spaceXs
            focusPolicy: Qt.StrongFocus
            font.pixelSize: SuiTheme.fontSizeSm
            model: picker.takes.takeOptions
            textRole: "label"
            valueRole: "value"
            currentIndex: picker.indexOf(picker.takes.selectedKey)
            enabled: !picker.busy && count > 0
            opacity: enabled ? 1 : SuiTheme.disabledOpacity
            displayText: count === 0 ? "No takes of this prompt yet"
                : currentIndex < 0 ? count + (count === 1 ? " take" : " takes") + " · none selected"
                : currentText
            Accessible.role: Accessible.ComboBox
            Accessible.name: "Take"
            Accessible.description: displayText

            onActivated: (index) => {
                currentIndex = Qt.binding(() => picker.indexOf(picker.takes.selectedKey))
                picker.takes.selectTake(picker.takes.takeOptions[index].value)
            }

            contentItem: Text {
                textFormat: Text.PlainText
                text: combo.displayText
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
                border.color: combo.activeFocus ? SuiTheme.accent : SuiTheme.fieldBorder
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
                    color: SuiTheme.text
                    font.pixelSize: SuiTheme.fontSizeSm
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                background: Rectangle { color: option.highlighted ? SuiTheme.bgHover : "transparent" }
            }
            popup: T.Popup {
                y: combo.height + 2
                width: Math.max(combo.width, 420)
                implicitHeight: Math.min(contentItem.implicitHeight + 2 * padding, 300)
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
        RowButton {
            objectName: "studioRemoveTake"
            text: "Remove"
            enabled: picker.takes.canRemove
            Accessible.description: picker.takes.actionNote
            onClicked: picker.takes.remove()
        }
        RowButton {
            objectName: "studioRejectTake"
            text: "Reject…"
            enabled: picker.takes.canReject
            Accessible.description: picker.takes.actionNote
            onClicked: {
                reason.text = ""
                rejectDialog.open()
            }
        }
        RowButton {
            objectName: "studioRestoreTake"
            text: "Restore"
            enabled: picker.takes.canRestore
            Accessible.description: picker.takes.actionNote
            onClicked: picker.takes.restore()
        }
        RowButton {
            id: historyButton
            objectName: "studioHistory"
            text: "History"
            enabled: picker.takes.takeCount > 0 || picker.takes.historyNote !== ""
            onClicked: historyPopup.visible ? historyPopup.close() : historyPopup.open()
        }
    }

    // Every take of the prompt: when, how long, status (changed elsewhere included), where.
    T.Popup {
        id: historyPopup
        objectName: "studioHistoryPopup"
        y: picker.height + 2
        x: 0
        width: picker.width
        padding: SuiTheme.spaceSm
        implicitHeight: Math.min(historyColumn.implicitHeight + 2 * padding, 360)
        closePolicy: T.Popup.CloseOnEscape | T.Popup.CloseOnPressOutside
        background: Rectangle {
            radius: SuiTheme.radiusMd
            color: SuiTheme.popover
            border.width: 1
            border.color: SuiTheme.borderStrong
        }
        contentItem: Flickable {
            clip: true
            contentHeight: historyColumn.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            ColumnLayout {
                id: historyColumn
                width: parent.width
                spacing: 2
                SuiText {
                    objectName: "studioHistoryNote"
                    Layout.fillWidth: true
                    visible: text !== ""
                    text: picker.takes.historyNote
                    variant: "caption"
                    tone: "warn"
                    wrapMode: Text.Wrap
                }
                Repeater {
                    model: picker.takes.history
                    delegate: T.ItemDelegate {
                        id: entry
                        required property var modelData
                        objectName: "studioHistoryRow-" + modelData.key
                        Layout.fillWidth: true
                        implicitHeight: entryColumn.implicitHeight + 2 * SuiTheme.spaceXs
                        padding: SuiTheme.spaceXs
                        enabled: !picker.busy
                        Accessible.role: Accessible.ListItem
                        Accessible.name: modelData.when + ", " + modelData.status + ", " + modelData.where
                        onClicked: {
                            // Closed first: selecting rebuilds this list, this row included.
                            const key = modelData.key
                            historyPopup.close()
                            picker.takes.selectTake(key)
                        }
                        background: Rectangle {
                            radius: SuiTheme.radiusSm
                            color: entry.modelData.selected ? SuiTheme.accentSubtle
                                   : entry.hovered ? SuiTheme.bgHover : "transparent"
                        }
                        contentItem: ColumnLayout {
                            id: entryColumn
                            spacing: 0
                            SuiText {
                                Layout.fillWidth: true
                                text: entry.modelData.when + " · " + entry.modelData.length
                                wrapMode: Text.Wrap
                            }
                            SuiText {
                                objectName: "studioHistoryStatus"
                                Layout.fillWidth: true
                                text: entry.modelData.status + " · " + entry.modelData.where
                                variant: "caption"
                                tone: entry.modelData.changedElsewhere ? "warn" : "muted"
                                wrapMode: Text.Wrap
                            }
                        }
                    }
                }
            }
        }
    }

    // Reject needs its reason: the button stays off until one is written.
    SuiDialog {
        id: rejectDialog
        objectName: "studioRejectDialog"
        title: "Reject this take?"
        description: "Say why, in a few words (a cough, a stumble, the wrong word). The take stays, rejected; Restore brings it back."
        initialFocusItem: reason
        ColumnLayout {
            width: rejectDialog.availableWidth
            spacing: SuiTheme.spaceSm
            SuiTextField {
                id: reason
                objectName: "studioRejectReason"
                Layout.fillWidth: true
                label: "Why this take is rejected"
                placeholderText: "Why"
                maximumLength: 500
                onAccepted: if (confirmReject.enabled) confirmReject.clicked()
            }
        }
        actions: [
            SuiButton { text: "Cancel"; onClicked: rejectDialog.reject() },
            SuiButton {
                id: confirmReject
                objectName: "studioConfirmReject"
                text: "Reject"
                variant: "primary"
                enabled: reason.text.trim() !== ""
                onClicked: {
                    if (picker.takes.reject(reason.text))
                        rejectDialog.accept()
                }
            }
        ]
    }
}
