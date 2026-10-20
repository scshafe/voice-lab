// Record → Measurement (design §6.1, phase 4b): voice-journey-capture's UMIK-1 ritual,
// unchanged, in the main window. The model is `measurement` (measurement/controller.py).
//
// Left: the checklist before a session (the UMIK-1, the pinned input level, the cal file,
// the options). Right: the ritual (segments, the prompt, the live meter, Record / Stop).
// On this screen only: Space records and stops, R redoes the last segment, Escape asks
// to abort. Sign-in, settings and uploads are the Status and Uploads screens'.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    id: page
    objectName: "measurementScreen"
    title: "Measurement"
    tagline: "The UMIK-1 ritual: a calibrated session, recorded as 48 kHz / 24-bit WAV and kept here until the server accepts it."

    readonly property bool shown: app.screen === "measurement"
    readonly property bool inSession: measurement.inSession
    readonly property bool modalOpen: abortDialog.visible || repinDialog.visible
    readonly property var levelTones: ({ ok: "ok", unpinned: "warn", moved: "danger", muted: "danger", unreadable: "danger", unknown: "neutral" })

    Shortcut {
        objectName: "spaceShortcut"
        sequence: "Space"
        enabled: page.shown && !page.modalOpen
                 && (measurement.phase === "segment"
                     || (measurement.phase === "recording" && measurement.currentMode === "manual"))
        onActivated: measurement.toggleRecording()
    }
    Shortcut {
        objectName: "redoShortcut"
        sequence: "R"
        enabled: page.shown && !page.modalOpen && measurement.canRedo
        onActivated: measurement.redoSegment()
    }
    Shortcut {
        objectName: "escapeShortcut"
        sequence: "Escape"
        enabled: page.shown && !page.modalOpen && page.inSession
        onActivated: abortDialog.open()
    }

    component Note: SuiText {
        Layout.fillWidth: true
        variant: "caption"
        wrapMode: Text.Wrap
        visible: text !== ""
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: SuiTheme.spaceLg

        // --- the checklist -------------------------------------------------------------------
        ColumnLayout {
            Layout.preferredWidth: 380
            Layout.maximumWidth: 420
            Layout.alignment: Qt.AlignTop
            spacing: SuiTheme.spaceMd

            Card {
                heading: "Microphone"
                RowLayout {
                    Layout.fillWidth: true
                    SuiChip {
                        objectName: "deviceChip"
                        text: measurement.deviceOk ? measurement.deviceName : "No UMIK-1"
                        tone: measurement.deviceOk ? "ok" : "danger"
                        dot: true
                    }
                    Item { Layout.fillWidth: true }
                    SuiButton {
                        objectName: "refreshButton"
                        text: "Refresh"
                        variant: "ghost"
                        enabled: !page.inSession && !measurement.busy
                        onClicked: measurement.refreshDevices()
                    }
                }
                Note { objectName: "deviceMessage"; text: measurement.deviceMessage }
            }

            Card {
                heading: "Input level"
                RowLayout {
                    Layout.fillWidth: true
                    spacing: SuiTheme.spaceSm
                    SuiText {
                        objectName: "levelText"
                        text: measurement.levelText === "" ? "—" : measurement.levelText
                        variant: "title"
                    }
                    SuiChip {
                        objectName: "levelChip"
                        text: measurement.levelState
                        tone: page.levelTones[measurement.levelState] || "neutral"
                    }
                    Item { Layout.fillWidth: true }
                    SuiButton {
                        objectName: "pinButton"
                        text: measurement.levelState === "moved" ? "Re-pin…" : "Pin level"
                        enabled: measurement.deviceOk && !page.inSession
                                 && measurement.levelState !== "unreadable"
                                 && measurement.levelState !== "muted"
                        onClicked: measurement.levelState === "moved" ? repinDialog.open() : measurement.pinLevel()
                    }
                }
                Note { objectName: "pinnedText"; text: measurement.pinnedText }
                Note {
                    objectName: "levelMessage"
                    text: measurement.levelMessage
                    tone: measurement.levelState === "ok" ? "muted"
                          : measurement.levelState === "unpinned" ? "warn" : "danger"
                }
            }

            Card {
                heading: "Calibration file"
                Note { objectName: "calText"; text: measurement.calText; tone: measurement.calOk ? "muted" : "warn" }
                RowLayout {
                    Layout.fillWidth: true
                    SuiTextField {
                        id: calPath
                        objectName: "calPath"
                        Layout.fillWidth: true
                        label: "Path to the UMIK-1 cal file"
                        placeholderText: "Path to miniDSP's cal file (e.g. 7012345.txt)"
                        enabled: !page.inSession
                    }
                    SuiButton {
                        objectName: "importCalButton"
                        text: "Import"
                        enabled: calPath.text !== "" && !page.inSession
                        onClicked: { measurement.importCalFile(calPath.text); calPath.text = "" }
                    }
                }
            }

            Card {
                heading: "Session"
                Note {
                    objectName: "distanceText"
                    text: "Mouth to capsule: " + measurement.distanceCm + " cm (Status → Settings). Same corner, same distance, every session."
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: SuiTheme.spaceXs
                    SuiButton {
                        objectName: "calToneButton"
                        text: "94 dB calibrator"
                        checkable: true
                        checked: measurement.calTone
                        enabled: !page.inSession
                        onClicked: measurement.setCalTone(checked)
                    }
                    SuiButton {
                        objectName: "skipFreeButton"
                        text: "Skip free practice"
                        checkable: true
                        checked: measurement.skipFree
                        enabled: !page.inSession
                        onClicked: measurement.setSkipFree(checked)
                    }
                }
                SuiButton {
                    objectName: "startButton"
                    Layout.fillWidth: true
                    text: measurement.phase === "starting" ? "Checking the input…" : "Start session"
                    variant: "primary"
                    enabled: measurement.canStart
                    onClicked: measurement.startSession()
                }
                Note {
                    objectName: "startBlocker"
                    text: measurement.canStart || page.inSession ? "" : measurement.startBlocker
                }
            }
        }

        // --- the ritual ----------------------------------------------------------------------
        Card {
            Layout.alignment: Qt.AlignTop
            Layout.minimumHeight: 420

            SuiEmptyState {
                objectName: "emptyState"
                Layout.fillWidth: true
                visible: measurement.phase === "setup" || measurement.phase === "starting"
                iconName: "info"
                title: "No session yet"
                description: "Plug in the UMIK-1, pin its input level once, import its cal file, then start a session. The ritual: room silence, (calibrator), /a/ soft and loud, a glide, the anchor song, free practice."
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: SuiTheme.spaceMd
                visible: !(measurement.phase === "setup" || measurement.phase === "starting")

                RowLayout {
                    Layout.fillWidth: true
                    SuiText {
                        objectName: "sessionTitle"
                        text: "Session " + measurement.sessionId
                        variant: "heading"
                    }
                    SuiChip {
                        objectName: "phaseChip"
                        text: measurement.phase === "done" ? "saved" : measurement.phase
                        tone: measurement.phase === "aborted" ? "danger"
                              : measurement.phase === "done" ? "ok"
                              : measurement.phase === "recording" ? "warn" : "info"
                        dot: measurement.phase === "recording"
                    }
                    Item { Layout.fillWidth: true }
                    SuiButton {
                        objectName: "abortButton"
                        text: "Abort…"
                        variant: "ghost"
                        visible: page.inSession
                        onClicked: abortDialog.open()
                    }
                }

                SuiBanner {
                    objectName: "abortBanner"
                    Layout.fillWidth: true
                    visible: measurement.phase === "aborted"
                    tone: "danger"
                    title: "Session aborted (kept locally, not uploaded)"
                    text: measurement.abortReason
                }

                Repeater {
                    objectName: "segmentRows"
                    model: measurement.segments
                    delegate: Rectangle {
                        required property var modelData
                        required property int index
                        objectName: "segment_" + modelData.key
                        Layout.fillWidth: true
                        implicitHeight: row.implicitHeight + 2 * SuiTheme.spaceXs
                        radius: SuiTheme.radiusSm
                        color: modelData.state === "current" || modelData.state === "recording"
                               ? SuiTheme.accentSubtle : "transparent"
                        RowLayout {
                            id: row
                            anchors.fill: parent
                            anchors.leftMargin: SuiTheme.spaceSm
                            anchors.rightMargin: SuiTheme.spaceSm
                            spacing: SuiTheme.spaceSm
                            SuiText { text: (index + 1) + "."; tone: "muted" }
                            SuiText {
                                Layout.fillWidth: true
                                text: modelData.label
                                elide: Text.ElideRight
                                tone: modelData.state === "pending" ? "muted" : ""
                            }
                            SuiText { text: modelData.detail; variant: "caption" }
                            SuiChip {
                                text: modelData.state
                                tone: modelData.state === "done" ? "ok"
                                      : modelData.state === "recording" ? "warn"
                                      : modelData.state === "current" ? "info" : "neutral"
                            }
                        }
                    }
                }

                // The prompt and the meter.
                SuiText {
                    objectName: "prompt"
                    Layout.fillWidth: true
                    Layout.topMargin: SuiTheme.spaceSm
                    visible: page.inSession
                    text: measurement.phase === "review"
                          ? "All segments recorded. Finish to save the session (or redo the last one)."
                          : measurement.currentLabel
                    wrapMode: Text.Wrap
                    variant: "title"
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    visible: measurement.phase === "recording" || measurement.phase === "segment"
                    spacing: SuiTheme.spaceXs
                    // The block's sample peak, held by the meter; the reading is beside it,
                    // and the capture's own count of clipped samples lights "CLIPPED".
                    SuiLevelMeter {
                        objectName: "meter"
                        Layout.fillWidth: true
                        label: "Input meter"
                        from: measurement.meterFloorDb
                        level: measurement.meterPeakDb
                        showValue: false
                        showClip: false
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        SuiText { objectName: "meterText"; text: measurement.meterText; variant: "caption" }
                        SuiText {
                            objectName: "clippedText"
                            text: measurement.clipping ? "CLIPPED" : ""
                            variant: "label"
                            tone: "danger"
                        }
                        Item { Layout.fillWidth: true }
                        SuiText { objectName: "elapsedText"; text: measurement.elapsedText; variant: "code" }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: SuiTheme.spaceSm
                    SuiButton {
                        objectName: "recordButton"
                        visible: measurement.phase === "segment" || measurement.phase === "recording"
                        text: measurement.phase === "recording"
                              ? (measurement.currentMode === "manual" ? "Stop (Space)" : "Recording…")
                              : "Record (Space)"
                        variant: "primary"
                        enabled: measurement.phase === "segment" || measurement.currentMode === "manual"
                        onClicked: measurement.toggleRecording()
                    }
                    SuiButton {
                        objectName: "redoButton"
                        text: "Redo last (R)"
                        visible: page.inSession
                        enabled: measurement.canRedo
                        onClicked: measurement.redoSegment()
                    }
                    SuiButton {
                        objectName: "finishButton"
                        text: "Finish session"
                        variant: "primary"
                        visible: measurement.phase === "review"
                        onClicked: measurement.finishSession()
                    }
                    SuiButton {
                        objectName: "newSessionButton"
                        text: "New session"
                        visible: measurement.phase === "done" || measurement.phase === "aborted"
                        onClicked: measurement.newSession()
                    }
                    Item { Layout.fillWidth: true }
                }
            }
        }
    }

    SuiDialog {
        id: abortDialog
        objectName: "abortDialog"
        parent: page
        title: "Abort this session?"
        description: "What was recorded is kept on this machine with the reason, and is never uploaded."
        actions: [
            SuiButton { text: "Keep recording"; onClicked: abortDialog.reject() },
            SuiButton {
                objectName: "confirmAbort"
                text: "Abort"
                variant: "primary"
                onClicked: { abortDialog.accept(); measurement.abortSession() }
            }
        ]
    }

    SuiDialog {
        id: repinDialog
        objectName: "repinDialog"
        parent: page
        title: "Re-pin the input level?"
        description: "A re-pin is a chain era event: sessions before and after it are not directly comparable. Restore the old level instead unless you moved it on purpose."
        actions: [
            SuiButton { text: "Cancel"; onClicked: repinDialog.reject() },
            SuiButton {
                objectName: "confirmRepin"
                text: "Re-pin"
                variant: "primary"
                onClicked: { repinDialog.accept(); measurement.pinLevel() }
            }
        ]
    }
}
