// The inspector (D-058, the layout): the studio's right pane, hidden and shown with ⌥⌘I. It holds
// what the centre leaves out, so the centre keeps the prompt, the waveform and Record, Retake
// and Save & Next. Top to bottom: the microphone and Check Input; the take (its history and
// picking, Remove, Reject…, Restore, Save, Discard, and its trim); its quality flags and why;
// the delivery notes of a spoken prompt (its instructions, guidance and duration); the pace
// and the flagged pass; the sync, whole, with its action. It scrolls when it is taller than
// the window. The model is the studio's (`app.studioModel`). The take's picker, its Save and
// Discard, and its trim are the screen's own, moved into `takeSlot`, `saveSlot` and `trimSlot`
// while the inspector shows (StudioScreen.qml): one of each, wherever they are.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui

Rectangle {
    id: inspector
    objectName: "studioInspector"

    required property var rec
    readonly property var takes: rec.takes
    readonly property alias takeSlot: takeSlot
    readonly property alias saveSlot: saveSlot
    readonly property alias trimSlot: trimSlot

    color: SuiTheme.panel

    Rectangle {
        anchors.left: parent.left
        width: 1
        height: parent.height
        color: SuiTheme.line
    }

    component Heading: SuiText {
        Layout.fillWidth: true
        Layout.topMargin: SuiTheme.spaceSm
        variant: "label"
        font.capitalization: Font.AllUppercase
    }
    component CompactButton: SuiButton {
        Layout.preferredHeight: 24
        topPadding: 0
        bottomPadding: 0
        variant: "ghost"
    }

    Flickable {
        id: flick
        objectName: "studioInspectorFlick"
        anchors.fill: parent
        anchors.leftMargin: 1
        contentWidth: width
        contentHeight: column.implicitHeight + 2 * SuiTheme.spaceLg
        interactive: contentHeight > height
        boundsBehavior: Flickable.StopAtBounds
        clip: true

        ColumnLayout {
            id: column
            x: SuiTheme.spaceLg
            y: SuiTheme.spaceLg
            width: flick.width - 2 * SuiTheme.spaceLg
            spacing: SuiTheme.spaceSm

            // -- the microphone, and Check Input -------------------------------------------
            Heading { text: "Microphone" }
            RowLayout {
                Layout.fillWidth: true
                spacing: SuiTheme.spaceSm
                SuiText {
                    objectName: "studioInspectorDevice"
                    Layout.fillWidth: true
                    text: inspector.rec.deviceOk ? inspector.rec.deviceLabel : "No microphone"
                    variant: "caption"
                    elide: Text.ElideRight
                }
                SuiButton {
                    objectName: "studioCheckInput"
                    Layout.preferredHeight: 24
                    topPadding: 0
                    bottomPadding: 0
                    text: "Check input"
                    variant: "ghost"
                    focusPolicy: Qt.NoFocus
                    enabled: inspector.rec.canCheck
                    onClicked: inspector.rec.openCheck()
                }
            }

            // -- the take: its history and picking, its decisions, Save, Discard, its trim -----
            Heading { text: "Take" }
            Item {
                id: takeSlot
                objectName: "studioInspectorTakeSlot"
                Layout.fillWidth: true
                Layout.preferredHeight: childrenRect.height
            }
            Item {
                id: saveSlot
                objectName: "studioInspectorSaveSlot"
                Layout.fillWidth: true
                Layout.preferredHeight: childrenRect.height
            }
            Item {
                id: trimSlot
                objectName: "studioInspectorTrimSlot"
                Layout.fillWidth: true
                Layout.preferredHeight: childrenRect.height
            }

            // -- the selected take's quality flags, and why --------------------------------
            Heading { text: "Flags" }
            SuiText {
                objectName: "studioInspectorFlags"
                Layout.fillWidth: true
                text: inspector.takes.qualityState === "checked" ? inspector.takes.qualityDetail
                    : inspector.takes.qualityState === "checking" ? "Checking the take…"
                    : "No take of this machine selected"
                variant: "caption"
                tone: inspector.takes.flags.length > 0 ? "warn" : "muted"
                wrapMode: Text.Wrap
            }

            // -- a spoken prompt's delivery notes (a silent one's are its card) -------------
            Heading {
                visible: inspector.rec.promptKind === "spoken"
                text: "Delivery notes · not read aloud"
            }
            SuiText {
                objectName: "studioInspectorNotes"
                Layout.fillWidth: true
                visible: inspector.rec.promptKind === "spoken"
                text: inspector.rec.instructions !== "" ? inspector.rec.instructions : "None"
                tone: inspector.rec.instructions !== "" ? "" : "muted"
                wrapMode: Text.Wrap
            }
            SuiText {
                objectName: "studioInspectorGuidance"
                Layout.fillWidth: true
                visible: inspector.rec.promptKind === "spoken"
                         && (inspector.rec.guidance !== "" || inspector.rec.duration !== "")
                text: [inspector.rec.guidance, inspector.rec.duration]
                      .filter((part) => part !== "").join(" · ")
                variant: "caption"
                wrapMode: Text.Wrap
            }

            // -- the pace, the break hint and the flagged pass -----------------------------
            Heading { text: "Pace" }
            SuiText {
                objectName: "studioSessionText"
                Layout.fillWidth: true
                text: inspector.rec.sessionText
                variant: "caption"
                wrapMode: Text.Wrap
            }
            RowLayout {
                objectName: "studioBreakRow"
                Layout.fillWidth: true
                visible: inspector.rec.breakHint !== ""
                spacing: SuiTheme.spaceXs
                SuiText {
                    objectName: "studioBreakHint"
                    Layout.fillWidth: true
                    text: inspector.rec.breakHint
                    variant: "caption"
                    tone: "info"
                    wrapMode: Text.Wrap
                }
                CompactButton {
                    objectName: "studioDismissBreak"
                    text: "OK"
                    onClicked: inspector.rec.dismissBreak()
                }
            }
            RowLayout {
                objectName: "studioFlaggedRow"
                readonly property bool shown: inspector.rec.flaggedText !== ""
                                              || inspector.rec.passText !== ""
                Layout.fillWidth: true
                visible: shown
                spacing: SuiTheme.spaceXs
                SuiText {
                    objectName: "studioFlaggedText"
                    Layout.fillWidth: true
                    text: inspector.rec.passText !== "" ? inspector.rec.passText
                                                        : inspector.rec.flaggedText
                    variant: "caption"
                    tone: "warn"
                    wrapMode: Text.Wrap
                }
                CompactButton {
                    objectName: inspector.rec.passText !== "" ? "studioEndPass"
                                                              : "studioReviewFlagged"
                    text: inspector.rec.passText !== "" ? "End" : "Review now"
                    enabled: inspector.rec.canNavigate
                    onClicked: inspector.rec.passText !== "" ? inspector.rec.endPass()
                                                             : inspector.rec.reviewFlagged()
                }
            }

            // -- the sync, whole --------------------------------------------------------------
            Heading { text: "Sync" }
            SuiText {
                objectName: "studioInspectorSync"
                Layout.fillWidth: true
                text: inspector.rec.syncText
                tone: inspector.rec.syncTone === "neutral" ? "" : inspector.rec.syncTone
                variant: "caption"
                wrapMode: Text.Wrap
            }
            SuiText {
                objectName: "studioInspectorSyncDetail"
                Layout.fillWidth: true
                visible: text !== ""
                text: inspector.rec.syncDetail
                variant: "caption"
                tone: "muted"
                wrapMode: Text.Wrap
            }
        }
    }
}
