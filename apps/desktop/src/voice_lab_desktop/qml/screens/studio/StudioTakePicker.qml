// The take picker (NATIVE_RECORDING_UX.md §4: "a compact take-picker row above the waveform;
// a popover ... holds extended history"): the selected take in a short line that fits the
// smallest window, and, open, every take of the prompt from this machine and the PC, newest
// first, with its date, length, status (changed elsewhere, imported from the Mac) and where
// it lives; then the selected take's actions: Remove, Reject (with a reason) and Restore.
// The model is the studio's `takes` (screens/studio_takes.py).
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
    readonly property bool menuOpen: combo.popup.visible || rejectDialog.visible

    function indexOf(key) {
        const rows = takes.history
        for (let i = 0; i < rows.length; ++i)
            if (rows[i].key === key) return i
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
            model: picker.takes.history
            textRole: "label"
            valueRole: "key"
            currentIndex: picker.indexOf(picker.takes.selectedKey)
            enabled: !picker.busy && (count > 0 || picker.takes.historyNote !== "")
            opacity: enabled ? 1 : SuiTheme.disabledOpacity
            displayText: count === 0 ? "No takes of this prompt yet"
                : currentIndex < 0 ? count + (count === 1 ? " take" : " takes") + " · none selected"
                : currentText
            Accessible.role: Accessible.ComboBox
            Accessible.name: "Take"
            Accessible.description: displayText

            onActivated: (index) => {
                currentIndex = Qt.binding(() => picker.indexOf(picker.takes.selectedKey))
                picker.takes.selectTake(picker.takes.history[index].key)
            }

            contentItem: Text {
                objectName: "studioTakeSelectText"
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
            // The history: every take, two lines each.
            delegate: T.ItemDelegate {
                id: entry
                required property int index
                required property var modelData
                objectName: "studioHistoryRow-" + modelData.key
                width: ListView.view ? ListView.view.width : implicitWidth
                implicitHeight: entryColumn.implicitHeight + 2 * SuiTheme.spaceXs
                padding: SuiTheme.spaceXs
                leftPadding: SuiTheme.spaceSm
                highlighted: combo.highlightedIndex === index
                Accessible.role: Accessible.ListItem
                Accessible.name: modelData.when + ", " + modelData.status + ", " + modelData.where
                contentItem: ColumnLayout {
                    id: entryColumn
                    spacing: 0
                    SuiText {
                        Layout.fillWidth: true
                        text: entry.modelData.when + " · " + entry.modelData.length
                        font.weight: entry.modelData.selected ? SuiTheme.fontWeightStrong
                                                              : SuiTheme.fontWeightNormal
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
                background: Rectangle {
                    radius: SuiTheme.radiusSm
                    color: entry.modelData.selected ? SuiTheme.accentSubtle
                           : entry.highlighted ? SuiTheme.bgHover : "transparent"
                }
            }
            popup: T.Popup {
                objectName: "studioHistoryPopup"
                y: combo.height + 2
                width: Math.max(combo.width, Math.min(picker.width, 520))
                implicitHeight: Math.min(contentItem.implicitHeight + 2 * padding, 360)
                padding: 2
                contentItem: ListView {
                    clip: true
                    implicitHeight: contentHeight
                    model: combo.popup.visible ? combo.delegateModel : null
                    currentIndex: combo.highlightedIndex
                    boundsBehavior: Flickable.StopAtBounds
                    header: SuiText {
                        objectName: "studioHistoryNote"
                        width: ListView.view ? ListView.view.width : 0
                        visible: text !== ""
                        height: visible ? implicitHeight + SuiTheme.spaceXs : 0
                        leftPadding: SuiTheme.spaceSm
                        text: picker.takes.historyNote
                        variant: "caption"
                        tone: "warn"
                        wrapMode: Text.Wrap
                    }
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
