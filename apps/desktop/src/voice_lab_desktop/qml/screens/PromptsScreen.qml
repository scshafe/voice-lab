// Record → Prompts (design §6.1, phase 4c; D-010 to D-016, D-042): prompted recording.
// The model is `app.promptsModel` (screens/prompts.py).
//
// Before a session: the catalog, the prompts to record (room tone always comes along), the
// device. In a session: the batch on the left; on the right the script (or a "Do not
// speak" card), the transport, the take under review with its waveform and trim, and the
// prompt's take history. On this screen only, and never while typing in a field or with a
// dialog open: R records and stops, Space plays and pauses the trimmed take, Return saves.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import VoiceLab.Controls

ScreenPage {
    id: page
    objectName: "promptsScreen"
    title: "Prompts"
    tagline: "Read each prompt into the UMIK-1, keep the take you like, and finish the session as a package. Takes, trims and their history stay on this machine."

    readonly property var rec: app.promptsModel
    readonly property bool shown: app.screen === "prompts"
    readonly property bool inSession: rec.phase !== "setup"
    readonly property bool busy: rec.phase === "starting" || rec.phase === "recording" || rec.phase === "stopping"
    readonly property bool modalOpen: abandonDialog.visible || catalogSelect.popupOpen
    readonly property bool typing: {
        const item = page.Window.activeFocusItem
        return item !== null && item.cursorPosition !== undefined
    }
    readonly property bool keysEnabled: shown && inSession && !modalOpen && !typing
    readonly property bool previewing: player.activeKey === rec.previewKey && rec.previewKey !== ""
    readonly property bool playing: previewing && player.state === "playing"

    Shortcut {
        objectName: "recordShortcut"
        sequence: "R"
        autoRepeat: false
        enabled: page.keysEnabled && (rec.phase === "ready" || rec.phase === "starting" || rec.phase === "recording")
        onActivated: rec.toggleRecord()
    }
    Shortcut {
        objectName: "playShortcut"
        sequence: "Space"
        autoRepeat: false
        enabled: page.keysEnabled && rec.phase === "ready" && rec.reviewNumber > 0
        onActivated: rec.togglePlay()
    }
    Shortcut {
        objectName: "saveShortcut"
        sequences: ["Return", "Enter"]
        autoRepeat: false
        enabled: page.keysEnabled && rec.phase === "ready" && rec.reviewNumber > 0
        onActivated: rec.save()
    }

    component Note: SuiText {
        Layout.fillWidth: true
        variant: "caption"
        wrapMode: Text.Wrap
        visible: text !== ""
    }

    // --- before a session ----------------------------------------------------------------
    Card {
        objectName: "setupCard"
        visible: !page.inSession
        heading: "New session"
        subheading: "Choose a catalog and the prompts to record. Every room tone of the catalog is recorded too."
        RowLayout {
            Layout.fillWidth: true
            spacing: SuiTheme.spaceMd
            SelectField {
                id: catalogSelect
                objectName: "catalogSelect"
                Layout.preferredWidth: 320
                label: "Catalog"
                options: rec.catalogOptions
                value: rec.catalogValue
                onValueSelected: (value) => rec.setCatalog(value)
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                SuiText { text: "Microphone"; variant: "label" }
                RowLayout {
                    SuiChip {
                        objectName: "promptsDeviceChip"
                        text: rec.deviceOk ? rec.deviceLabel : "No UMIK-1"
                        tone: rec.deviceOk ? "ok" : "danger"
                        dot: true
                    }
                    SuiButton {
                        objectName: "checkDeviceButton"
                        text: "Check"
                        variant: "ghost"
                        onClicked: rec.checkDevice()
                    }
                }
            }
        }
        Note { objectName: "promptsDeviceMessage"; text: rec.deviceOk ? "" : rec.deviceLabel; tone: "danger" }
        SuiBanner {
            objectName: "catalogRefused"
            Layout.fillWidth: true
            visible: rec.catalogMessage !== ""
            tone: "danger"
            title: "This catalog cannot be recorded here"
            text: rec.catalogMessage
        }
        RowLayout {
            Layout.fillWidth: true
            visible: rec.catalogPrompts.length > 0
            SuiText {
                objectName: "selectedCount"
                Layout.fillWidth: true
                text: rec.selectedCount + " prompt(s) chosen, room tone included"
            }
            SuiButton { objectName: "chooseAll"; text: "All"; variant: "ghost"; onClicked: rec.chooseAll(true) }
            SuiButton { objectName: "chooseNone"; text: "None"; variant: "ghost"; onClicked: rec.chooseAll(false) }
        }
        Flow {
            Layout.fillWidth: true
            spacing: SuiTheme.spaceXs
            Repeater {
                objectName: "catalogPromptRows"
                model: rec.catalogPrompts
                delegate: SuiCheckBox {
                    required property var modelData
                    objectName: "choose-" + modelData.promptId
                    width: 360
                    text: modelData.promptId + " — " + modelData.title
                    checked: modelData.selected
                    enabled: !modelData.always
                    onToggled: rec.choosePrompt(modelData.promptId, checked)
                }
            }
        }
        SuiButton {
            objectName: "startPromptsButton"
            Layout.fillWidth: true
            text: "Start session"
            variant: "primary"
            enabled: rec.canStart
            onClicked: rec.startSession()
        }
    }

    // --- the session ---------------------------------------------------------------------
    RowLayout {
        Layout.fillWidth: true
        visible: page.inSession
        spacing: SuiTheme.spaceSm
        SuiText {
            objectName: "sessionLabel"
            Layout.fillWidth: true
            text: rec.sessionLabel
            variant: "subheading"
            elide: Text.ElideRight
        }
        SuiChip {
            objectName: "keptChip"
            text: rec.keptCount + " of " + rec.batchCount + " kept"
            tone: rec.batchCount > 0 && rec.keptCount === rec.batchCount ? "ok" : "info"
        }
        SuiButton {
            objectName: "finishPromptsButton"
            text: rec.phase === "finished" ? "Build the package again" : "Finish session"
            variant: "primary"
            visible: rec.phase === "ready" || rec.phase === "finished"
            enabled: rec.finishBlocker === "" || rec.phase === "finished"
            onClicked: rec.finishSession()
        }
        SuiButton {
            objectName: "abandonButton"
            text: "Set aside…"
            variant: "ghost"
            visible: rec.phase === "ready"
            onClicked: abandonDialog.open()
        }
        SuiButton {
            objectName: "newPromptsSession"
            text: "New session"
            visible: rec.phase === "packaged"
            onClicked: rec.newSession()
        }
    }
    Note { objectName: "finishBlocker"; text: page.inSession && rec.phase === "ready" ? rec.finishBlocker : "" }
    SuiBanner {
        objectName: "packageBanner"
        Layout.fillWidth: true
        visible: page.inSession && rec.packageText !== ""
        tone: rec.phase === "packaged" ? "ok" : "danger"
        title: rec.phase === "packaged" ? "Packaged" : "Not packaged"
        text: rec.packageText
    }

    RowLayout {
        Layout.fillWidth: true
        visible: page.inSession
        spacing: SuiTheme.spaceLg

        Card {
            objectName: "batchCard"
            Layout.preferredWidth: 320
            Layout.maximumWidth: 360
            Layout.alignment: Qt.AlignTop
            heading: "Prompts"
            Repeater {
                objectName: "batchRows"
                model: rec.batch
                delegate: Rectangle {
                    required property var modelData
                    objectName: "batch-" + modelData.promptId
                    Layout.fillWidth: true
                    implicitHeight: batchRow.implicitHeight + 2 * SuiTheme.spaceXs
                    radius: SuiTheme.radiusSm
                    color: modelData.promptId === rec.currentPromptId ? SuiTheme.accentSubtle : "transparent"
                    RowLayout {
                        id: batchRow
                        anchors.fill: parent
                        anchors.leftMargin: SuiTheme.spaceSm
                        anchors.rightMargin: SuiTheme.spaceSm
                        spacing: SuiTheme.spaceSm
                        SuiText { text: (modelData.index + 1) + "."; tone: "muted" }
                        SuiText {
                            Layout.fillWidth: true
                            text: modelData.title
                            elide: Text.ElideRight
                        }
                        SuiChip { text: modelData.statusLabel; tone: modelData.tone }
                    }
                    MouseArea {
                        anchors.fill: parent
                        enabled: !page.busy
                        onClicked: rec.selectPrompt(modelData.promptId)
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            spacing: SuiTheme.spaceMd

            Card {
                objectName: "scriptCard"
                heading: (rec.currentIndex + 1) + ". " + rec.currentPromptId
                subheading: rec.currentStyle + " · " + rec.currentGuide
                SuiText {
                    objectName: "scriptText"
                    Layout.fillWidth: true
                    visible: !rec.currentSilent
                    text: rec.currentText
                    variant: "display"
                    wrapMode: Text.Wrap
                }
                Rectangle {
                    objectName: "silentCard"
                    Layout.fillWidth: true
                    visible: rec.currentSilent
                    implicitHeight: silentColumn.implicitHeight + 2 * SuiTheme.spaceLg
                    radius: SuiTheme.radiusMd
                    color: SuiTheme.tint
                    border.color: SuiTheme.line
                    ColumnLayout {
                        id: silentColumn
                        anchors.fill: parent
                        anchors.margins: SuiTheme.spaceLg
                        SuiText { text: "Do not speak"; variant: "title"; tone: "warn" }
                        SuiText {
                            objectName: "silentInstructions"
                            Layout.fillWidth: true
                            text: rec.currentInstructions
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }

            Card {
                objectName: "transportCard"
                RowLayout {
                    Layout.fillWidth: true
                    spacing: SuiTheme.spaceSm
                    SuiChip {
                        objectName: "stateChip"
                        visible: rec.stateLabel !== ""
                        text: rec.stateLabel
                        tone: rec.phase === "recording" ? "warn" : "info"
                        dot: rec.phase === "recording"
                    }
                    SuiText { objectName: "promptsElapsed"; text: rec.elapsedText; variant: "code" }
                    Item { Layout.fillWidth: true }
                    SuiButton {
                        objectName: "recordTakeButton"
                        text: rec.phase === "starting" || rec.phase === "recording" ? "Stop (R)"
                            : rec.reviewNumber > 0 ? "Retake (R)" : "Record (R)"
                        variant: "primary"
                        enabled: rec.phase === "ready" || rec.phase === "starting" || rec.phase === "recording"
                        onClicked: rec.toggleRecord()
                    }
                    SuiButton {
                        objectName: "playTakeButton"
                        text: page.playing ? "Pause (Space)" : "Play (Space)"
                        enabled: rec.phase === "ready" && rec.reviewNumber > 0
                        onClicked: rec.togglePlay()
                    }
                    SuiButton {
                        objectName: "saveTakeButton"
                        text: "Save (Return)"
                        enabled: rec.phase === "ready" && rec.reviewNumber > 0
                        onClicked: rec.save()
                    }
                    SuiButton {
                        objectName: "saveNextButton"
                        text: "Save & next"
                        variant: "primary"
                        enabled: rec.phase === "ready" && rec.reviewNumber > 0
                        onClicked: rec.saveNext()
                    }
                }
                Note { objectName: "takeError"; text: rec.lastError; tone: "danger" }
            }

            Card {
                objectName: "reviewCard"
                heading: rec.reviewNumber > 0 ? "Take " + rec.reviewNumber + " · " + rec.reviewStatus : "No take yet"
                subheading: rec.reviewNumber > 0
                    ? "The trim is kept here as a recipe; the package carries the take as recorded."
                    : "Record a take of this prompt (R)."
                SuiWaveform {
                    id: waveform
                    objectName: "waveform"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 120
                    visible: rec.reviewNumber > 0
                    label: "Waveform of the take under review"
                    bins: rec.bins
                    frameCount: rec.frameCount
                    trimStart: rec.trimStart
                    trimEnd: rec.trimEnd
                    editable: rec.phase === "ready"
                    playhead: page.previewing ? Math.round(rec.trimStart + player.position * 48000) : -1
                    onTrimChanged: (start, end) => rec.setTrim(start, end)
                    onSeekRequested: (frame) => rec.seek(frame)
                    onPlayRequested: (_frame) => rec.togglePlay()
                }
                RowLayout {
                    Layout.fillWidth: true
                    visible: rec.reviewNumber > 0
                    spacing: SuiTheme.spaceSm
                    SuiTextField {
                        id: trimIn
                        objectName: "trimInField"
                        Layout.preferredWidth: 160
                        label: "In (frame)"
                        text: String(rec.trimStart)
                        enabled: rec.phase === "ready"
                        onAccepted: {
                            rec.setTrim(parseInt(text), rec.trimEnd)
                            text = Qt.binding(() => String(rec.trimStart))
                        }
                        onActiveFocusChanged: if (!activeFocus) text = Qt.binding(() => String(rec.trimStart))
                    }
                    SuiTextField {
                        id: trimOut
                        objectName: "trimOutField"
                        Layout.preferredWidth: 160
                        label: "Out (frame)"
                        text: String(rec.trimEnd)
                        enabled: rec.phase === "ready"
                        onAccepted: {
                            rec.setTrim(rec.trimStart, parseInt(text))
                            text = Qt.binding(() => String(rec.trimEnd))
                        }
                        onActiveFocusChanged: if (!activeFocus) text = Qt.binding(() => String(rec.trimEnd))
                    }
                    SuiText {
                        objectName: "trimSeconds"
                        Layout.fillWidth: true
                        text: ((rec.trimEnd - rec.trimStart) / 48000).toFixed(2) + " s kept of "
                            + (rec.frameCount / 48000).toFixed(2) + " s"
                        variant: "caption"
                    }
                    SuiButton {
                        objectName: "resetTrimButton"
                        text: "Reset trim"
                        variant: "ghost"
                        enabled: rec.phase === "ready" && (rec.trimStart > 0 || rec.trimEnd < rec.frameCount)
                        onClicked: rec.resetTrim()
                    }
                }
            }

            Card {
                objectName: "historyCard"
                heading: "Takes of this prompt"
                SuiText {
                    Layout.fillWidth: true
                    visible: rec.takes.length === 0
                    text: "None yet."
                    variant: "caption"
                }
                Repeater {
                    objectName: "takeRows"
                    model: rec.takes
                    delegate: RowLayout {
                        required property var modelData
                        objectName: "take-" + modelData.number
                        Layout.fillWidth: true
                        spacing: SuiTheme.spaceSm
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0
                            SuiText {
                                text: modelData.label + (modelData.trimmed ? " · trimmed" : "")
                                tone: modelData.selected ? "strong" : ""
                            }
                            SuiText {
                                Layout.fillWidth: true
                                visible: modelData.message !== ""
                                text: modelData.message
                                variant: "caption"
                                tone: "danger"
                                wrapMode: Text.Wrap
                            }
                        }
                        SuiChip { text: modelData.statusLabel; tone: modelData.tone }
                        SuiButton {
                            objectName: "reviewTake"
                            text: "Review"
                            variant: "ghost"
                            visible: modelData.usable && !modelData.selected
                            enabled: rec.phase === "ready"
                            onClicked: rec.selectTake(modelData.number)
                        }
                        SuiButton {
                            objectName: "removeTake"
                            text: "Remove"
                            variant: "ghost"
                            visible: modelData.usable && modelData.status !== "removed"
                            enabled: rec.phase === "ready"
                            onClicked: rec.removeTake(modelData.number)
                        }
                        SuiButton {
                            objectName: "restoreTake"
                            text: "Restore"
                            variant: "ghost"
                            visible: modelData.status === "removed"
                            enabled: rec.phase === "ready"
                            onClicked: rec.restoreTake(modelData.number)
                        }
                    }
                }
            }
        }
    }

    SuiDialog {
        id: abandonDialog
        objectName: "abandonDialog"
        parent: page
        title: "Set this session aside?"
        description: "Its takes stay on this machine; it is not packaged or uploaded. A new session can start."
        actions: [
            SuiButton { text: "Keep recording"; onClicked: abandonDialog.reject() },
            SuiButton {
                objectName: "confirmAbandon"
                text: "Set aside"
                variant: "primary"
                onClicked: { abandonDialog.accept(); rec.abandonSession() }
            }
        ]
    }
}
