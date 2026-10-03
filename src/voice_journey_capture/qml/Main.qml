// voice-journey-capture: the capture window. All state and actions live in the
// Python controller (`app`); this file lays out scshafe-qt components around it.
//
// Left: the checklist before a session (sign-in, the UMIK-1, the pinned input
// level, the cal file, options). Right: the ritual (segments, the prompt, live
// meters, Record / Stop). Space records and stops; R redoes the last segment.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui

Window {
    id: root
    objectName: "mainWindow"
    width: 1100
    height: 720
    minimumWidth: 820
    minimumHeight: 560
    visible: true
    color: SuiTheme.bg
    title: app.phase === "recording" ? "Recording — Voice Journey capture" : "Voice Journey capture"

    readonly property bool inSession: ["segment", "recording", "review"].indexOf(app.phase) >= 0
    readonly property bool modalOpen: abortDialog.visible || settingsDialog.visible || repinDialog.visible
    readonly property var levelTones: ({ ok: "ok", unpinned: "warn", moved: "danger", muted: "danger", unreadable: "danger", unknown: "neutral" })

    Connections {
        target: app
        function onToast(text, tone) { toasts.show(text, { tone: tone }) }
    }

    Shortcut {
        objectName: "spaceShortcut"
        sequence: "Space"
        enabled: !root.modalOpen && (app.phase === "segment" || (app.phase === "recording" && app.currentMode === "manual"))
        onActivated: app.toggleRecording()
    }
    Shortcut { sequence: "R"; enabled: !root.modalOpen && app.canRedo; onActivated: app.redoSegment() }
    Shortcut { sequence: "Escape"; enabled: !root.modalOpen && root.inSession; onActivated: abortDialog.open() }

    component Card: Rectangle {
        id: card
        default property alias content: column.data
        property string heading: ""
        Layout.fillWidth: true
        implicitHeight: column.implicitHeight + 2 * SuiTheme.spaceMd
        radius: SuiTheme.radiusMd
        color: SuiTheme.panel
        border.color: SuiTheme.line
        border.width: 1
        ColumnLayout {
            id: column
            anchors.fill: parent
            anchors.margins: SuiTheme.spaceMd
            spacing: SuiTheme.spaceXs
            Text {
                text: card.heading
                visible: text !== ""
                textFormat: Text.PlainText
                color: SuiTheme.textStrong
                font.pixelSize: SuiTheme.fontSizeMd
                font.weight: SuiTheme.fontWeightStrong
            }
        }
    }

    component Note: Text {
        Layout.fillWidth: true
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: SuiTheme.muted
        font.pixelSize: SuiTheme.fontSizeSm
        visible: text !== ""
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // --- header -------------------------------------------------------------------
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: header.implicitHeight + 2 * SuiTheme.spaceSm
            color: SuiTheme.panel
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: SuiTheme.line }

            RowLayout {
                id: header
                anchors.fill: parent
                anchors.leftMargin: SuiTheme.spaceLg
                anchors.rightMargin: SuiTheme.spaceLg
                spacing: SuiTheme.spaceMd

                Text {
                    text: "Voice Journey capture"
                    textFormat: Text.PlainText
                    color: SuiTheme.textStrong
                    font.pixelSize: SuiTheme.fontSizeLg
                    font.weight: SuiTheme.fontWeightBold
                }
                SuiChip { text: "UMIK-1 · umik1"; tone: "info" }
                Item { Layout.fillWidth: true }
                Text {
                    objectName: "uploadText"
                    text: app.uploadText
                    textFormat: Text.PlainText
                    color: SuiTheme.muted
                    font.pixelSize: SuiTheme.fontSizeSm
                    elide: Text.ElideRight
                    Layout.maximumWidth: 360
                }
                SuiButton {
                    objectName: "uploadButton"
                    text: "Upload now"
                    visible: app.pendingCount > 0
                    enabled: app.signedIn && !app.uploadBusy
                    onClicked: app.uploadPending()
                }
                SuiChip {
                    objectName: "accountChip"
                    visible: app.signedIn
                    text: app.account
                    tone: "ok"
                    dot: true
                }
                SuiButton {
                    objectName: "signInButton"
                    text: app.signedIn ? "Sign out" : (app.authBusy ? "Waiting for the browser…" : "Sign in")
                    variant: app.signedIn ? "ghost" : "primary"
                    enabled: !app.authBusy
                    onClicked: app.signedIn ? app.signOut() : app.signIn()
                }
                SuiButton {
                    objectName: "settingsButton"
                    text: "Settings"
                    variant: "ghost"
                    enabled: !root.inSession
                    onClicked: settingsDialog.open()
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: SuiTheme.spaceLg
            spacing: SuiTheme.spaceLg

            // --- the checklist ---------------------------------------------------------------
            ColumnLayout {
                Layout.preferredWidth: 380
                Layout.maximumWidth: 420
                Layout.fillHeight: true
                spacing: SuiTheme.spaceMd

                Card {
                    heading: "Microphone"
                    RowLayout {
                        Layout.fillWidth: true
                        SuiChip {
                            objectName: "deviceChip"
                            text: app.deviceOk ? app.deviceName : "No UMIK-1"
                            tone: app.deviceOk ? "ok" : "danger"
                            dot: true
                        }
                        Item { Layout.fillWidth: true }
                        SuiButton {
                            objectName: "refreshButton"
                            text: "Refresh"
                            variant: "ghost"
                            enabled: !root.inSession && !app.busy
                            onClicked: app.refreshDevices()
                        }
                    }
                    Note { objectName: "deviceMessage"; text: app.deviceMessage }
                }

                Card {
                    heading: "Input level"
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: SuiTheme.spaceSm
                        Text {
                            objectName: "levelText"
                            text: app.levelText === "" ? "—" : app.levelText
                            textFormat: Text.PlainText
                            color: SuiTheme.textStrong
                            font.pixelSize: SuiTheme.fontSize2xl
                            font.weight: SuiTheme.fontWeightBold
                        }
                        SuiChip {
                            objectName: "levelChip"
                            text: app.levelState
                            tone: root.levelTones[app.levelState] || "neutral"
                        }
                        Item { Layout.fillWidth: true }
                        SuiButton {
                            objectName: "pinButton"
                            text: app.levelState === "moved" ? "Re-pin…" : "Pin level"
                            enabled: app.deviceOk && !root.inSession && app.levelState !== "unreadable" && app.levelState !== "muted"
                            onClicked: app.levelState === "moved" ? repinDialog.open() : app.pinLevel()
                        }
                    }
                    Note { objectName: "pinnedText"; text: app.pinnedText }
                    Note { objectName: "levelMessage"; text: app.levelMessage; color: app.levelState === "ok" ? SuiTheme.muted : SuiTheme.toneText(root.levelTones[app.levelState] || "neutral") }
                }

                Card {
                    heading: "Calibration file"
                    Note { objectName: "calText"; text: app.calText; color: app.calOk ? SuiTheme.muted : SuiTheme.toneText("warn") }
                    RowLayout {
                        Layout.fillWidth: true
                        SuiTextField {
                            id: calPath
                            objectName: "calPath"
                            Layout.fillWidth: true
                            label: "Path to the UMIK-1 cal file"
                            placeholderText: "Path to miniDSP's cal file (e.g. 7012345.txt)"
                            enabled: !root.inSession
                        }
                        SuiButton {
                            objectName: "importCalButton"
                            text: "Import"
                            enabled: calPath.text !== "" && !root.inSession
                            onClicked: { app.importCalFile(calPath.text); calPath.text = "" }
                        }
                    }
                }

                Card {
                    heading: "Session"
                    Note { text: "Mouth to capsule: " + app.distanceCm + " cm (Settings). Same corner, same distance, every session." }
                    Flow {
                        Layout.fillWidth: true
                        spacing: SuiTheme.spaceXs
                        SuiButton {
                            objectName: "calToneButton"
                            text: "94 dB calibrator"
                            checkable: true
                            checked: app.calTone
                            enabled: !root.inSession
                            onClicked: app.setCalTone(checked)
                        }
                        SuiButton {
                            objectName: "skipFreeButton"
                            text: "Skip free practice"
                            checkable: true
                            checked: app.skipFree
                            enabled: !root.inSession
                            onClicked: app.setSkipFree(checked)
                        }
                    }
                    SuiButton {
                        objectName: "startButton"
                        Layout.fillWidth: true
                        text: app.phase === "starting" ? "Checking the input…" : "Start session"
                        variant: "primary"
                        enabled: app.canStart
                        onClicked: app.startSession()
                    }
                    Note { objectName: "startBlocker"; text: app.canStart || root.inSession ? "" : app.startBlocker }
                }
                Item { Layout.fillHeight: true }
            }

            // --- the ritual ------------------------------------------------------------------
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: SuiTheme.radiusMd
                color: SuiTheme.panel
                border.color: SuiTheme.line
                border.width: 1

                SuiEmptyState {
                    objectName: "emptyState"
                    anchors.centerIn: parent
                    width: Math.min(parent.width - 2 * SuiTheme.spaceXl, 420)
                    visible: app.phase === "setup" || app.phase === "starting"
                    iconName: "info"
                    title: "No session yet"
                    description: "Plug in the UMIK-1, pin its input level once, import its cal file, then start a session. The ritual: room silence, (calibrator), /a/ soft and loud, a glide, the anchor song, free practice."
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: SuiTheme.spaceLg
                    spacing: SuiTheme.spaceMd
                    visible: !(app.phase === "setup" || app.phase === "starting")

                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            objectName: "sessionTitle"
                            text: "Session " + app.sessionId
                            textFormat: Text.PlainText
                            color: SuiTheme.textStrong
                            font.pixelSize: SuiTheme.fontSizeLg
                            font.weight: SuiTheme.fontWeightStrong
                        }
                        SuiChip {
                            objectName: "phaseChip"
                            text: app.phase === "done" ? "saved" : app.phase
                            tone: app.phase === "aborted" ? "danger" : app.phase === "done" ? "ok" : app.phase === "recording" ? "warn" : "info"
                            dot: app.phase === "recording"
                        }
                        Item { Layout.fillWidth: true }
                        SuiButton {
                            objectName: "abortButton"
                            text: "Abort…"
                            variant: "ghost"
                            visible: root.inSession
                            onClicked: abortDialog.open()
                        }
                    }

                    SuiBanner {
                        objectName: "abortBanner"
                        Layout.fillWidth: true
                        visible: app.phase === "aborted"
                        tone: "danger"
                        title: "Session aborted (kept locally, not uploaded)"
                        text: app.abortReason
                    }

                    Repeater {
                        objectName: "segmentRows"
                        model: app.segments
                        delegate: Rectangle {
                            required property var modelData
                            required property int index
                            objectName: "segment_" + modelData.key
                            Layout.fillWidth: true
                            implicitHeight: row.implicitHeight + 2 * SuiTheme.spaceXs
                            radius: SuiTheme.radiusSm
                            color: modelData.state === "current" || modelData.state === "recording" ? SuiTheme.accentSubtle : "transparent"
                            RowLayout {
                                id: row
                                anchors.fill: parent
                                anchors.leftMargin: SuiTheme.spaceSm
                                anchors.rightMargin: SuiTheme.spaceSm
                                spacing: SuiTheme.spaceSm
                                Text {
                                    text: (index + 1) + "."
                                    textFormat: Text.PlainText
                                    color: SuiTheme.muted
                                    font.pixelSize: SuiTheme.fontSizeMd
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.label
                                    textFormat: Text.PlainText
                                    elide: Text.ElideRight
                                    color: modelData.state === "pending" ? SuiTheme.muted : SuiTheme.text
                                    font.pixelSize: SuiTheme.fontSizeMd
                                }
                                Text {
                                    text: modelData.detail
                                    textFormat: Text.PlainText
                                    color: SuiTheme.muted
                                    font.pixelSize: SuiTheme.fontSizeSm
                                }
                                SuiChip {
                                    text: modelData.state
                                    tone: modelData.state === "done" ? "ok" : modelData.state === "recording" ? "warn" : modelData.state === "current" ? "info" : "neutral"
                                }
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }

                    // The prompt and the meters.
                    Text {
                        objectName: "prompt"
                        Layout.fillWidth: true
                        visible: root.inSession
                        text: app.phase === "review" ? "All segments recorded. Finish to save the session (or redo the last one)."
                              : app.currentLabel
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: SuiTheme.textStrong
                        font.pixelSize: SuiTheme.fontSizeXl
                        font.weight: SuiTheme.fontWeightStrong
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        visible: app.phase === "recording" || app.phase === "segment"
                        spacing: SuiTheme.spaceXs
                        Rectangle {
                            id: meter
                            objectName: "meter"
                            Layout.fillWidth: true
                            implicitHeight: 14
                            radius: SuiTheme.radiusSm
                            color: SuiTheme.tint
                            border.color: SuiTheme.line
                            Accessible.role: Accessible.ProgressBar
                            Accessible.name: "Input meter: " + app.meterText
                            Rectangle {
                                width: parent.width * app.meterRms
                                height: parent.height
                                radius: SuiTheme.radiusSm
                                color: app.clipping ? SuiTheme.toneRed : app.meterPeak > 0.92 ? SuiTheme.toneYellow : SuiTheme.ok
                            }
                            Rectangle {
                                x: Math.max(0, parent.width * app.meterPeak - width)
                                width: 2
                                height: parent.height
                                color: app.clipping ? SuiTheme.toneRed : SuiTheme.textStrong
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Text { objectName: "meterText"; text: app.meterText; textFormat: Text.PlainText; color: SuiTheme.muted; font.pixelSize: SuiTheme.fontSizeSm }
                            Text { text: app.clipping ? "CLIPPED" : ""; textFormat: Text.PlainText; color: SuiTheme.toneRed; font.pixelSize: SuiTheme.fontSizeSm; font.weight: SuiTheme.fontWeightBold }
                            Item { Layout.fillWidth: true }
                            Text { objectName: "elapsedText"; text: app.elapsedText; textFormat: Text.PlainText; color: SuiTheme.text; font.pixelSize: SuiTheme.fontSizeMd; font.family: SuiTheme.monoFamily }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: SuiTheme.spaceSm
                        SuiButton {
                            objectName: "recordButton"
                            visible: app.phase === "segment" || app.phase === "recording"
                            text: app.phase === "recording"
                                  ? (app.currentMode === "manual" ? "Stop (Space)" : "Recording…")
                                  : "Record (Space)"
                            variant: "primary"
                            enabled: app.phase === "segment" || app.currentMode === "manual"
                            onClicked: app.toggleRecording()
                        }
                        SuiButton {
                            objectName: "redoButton"
                            text: "Redo last (R)"
                            visible: root.inSession
                            enabled: app.canRedo
                            onClicked: app.redoSegment()
                        }
                        SuiButton {
                            objectName: "finishButton"
                            text: "Finish session"
                            variant: "primary"
                            visible: app.phase === "review"
                            onClicked: app.finishSession()
                        }
                        SuiButton {
                            objectName: "newSessionButton"
                            text: "New session"
                            visible: app.phase === "done" || app.phase === "aborted"
                            onClicked: app.newSession()
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }
        }
    }

    SuiDialog {
        id: abortDialog
        objectName: "abortDialog"
        title: "Abort this session?"
        description: "What was recorded is kept on this machine with the reason, and is never uploaded."
        actions: [
            SuiButton { text: "Keep recording"; onClicked: abortDialog.reject() },
            SuiButton { objectName: "confirmAbort"; text: "Abort"; variant: "primary"; onClicked: { abortDialog.accept(); app.abortSession() } }
        ]
    }

    SuiDialog {
        id: repinDialog
        objectName: "repinDialog"
        title: "Re-pin the input level?"
        description: "A re-pin is a chain era event: sessions before and after it are not directly comparable. Restore the old level instead unless you moved it on purpose."
        actions: [
            SuiButton { text: "Cancel"; onClicked: repinDialog.reject() },
            SuiButton { objectName: "confirmRepin"; text: "Re-pin"; variant: "primary"; onClicked: { repinDialog.accept(); app.pinLevel() } }
        ]
    }

    SuiDialog {
        id: settingsDialog
        objectName: "settingsDialog"
        title: "Settings"
        description: "Saved to " + app.settingsSource + ". The environment (VOICE_JOURNEY_CAPTURE_SERVER) wins over this file."
        onAboutToShow: { serverField.text = app.server; distanceField.text = String(app.distanceCm) }
        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: SuiTheme.spaceSm
            Note { text: "Server (sessions upload only here)"; color: SuiTheme.text }
            SuiTextField { id: serverField; objectName: "serverField"; Layout.fillWidth: true; label: "Server address"; enabled: !app.serverLocked }
            Note { text: "Mouth to capsule distance, cm"; color: SuiTheme.text }
            SuiTextField { id: distanceField; objectName: "distanceField"; Layout.fillWidth: true; label: "Distance in centimetres"; inputMethodHints: Qt.ImhDigitsOnly }
        }
        actions: [
            SuiButton { text: "Cancel"; onClicked: settingsDialog.reject() },
            SuiButton {
                objectName: "saveSettings"
                text: "Save"
                variant: "primary"
                onClicked: if (app.saveSettings(serverField.text, parseInt(distanceField.text) || 0)) settingsDialog.accept()
            }
        ]
    }

    SuiToastHost { id: toasts; objectName: "toasts"; anchors.fill: parent; z: 100 }
}
