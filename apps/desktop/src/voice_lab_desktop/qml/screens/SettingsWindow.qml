// Settings (⌘,; D-058, the recording UX review's item 9): a window of its own, as on any Mac,
// that opens on the pane last used. General: the server, Pocket ID and the measurement's
// distance (what Status → Settings… held). Uploads: corpus sync, and the home-host approval
// (Approve), so a fresh install reaches a synced take without editing a file. Recording: the
// microphone, Check Input…, the match line and the quality flags' two levels to calibrate.
// The models: `app` (settings, pane), `approval`, and the studio's (`app.studioModel`, null
// with prompts_mode = "sessions").
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui

Window {
    id: win
    objectName: "settingsWindow"
    title: "Settings"
    width: 640
    height: 480
    minimumWidth: 560
    minimumHeight: 400
    color: SuiTheme.bg
    flags: Qt.Dialog

    readonly property var studio: app.studioModel
    readonly property var panes: [
        { key: "general", title: "General" },
        { key: "uploads", title: "Uploads" },
        { key: "recording", title: "Recording" }
    ]
    readonly property int paneIndex: Math.max(0, panes.findIndex((p) => p.key === app.settingsPane))

    function load() {
        serverField.text = app.server
        issuerField.text = app.issuer
        clientField.text = app.clientId
        distanceField.text = String(app.distanceCm)
        if (win.studio) {
            snrField.text = String(win.studio.lowSnrDb)
            driftField.text = String(win.studio.driftDb)
        }
    }
    onVisibleChanged: if (visible) load()

    Shortcut {
        sequences: [StandardKey.Close, "Escape"]
        onActivated: win.close()
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        // The panes.
        Rectangle {
            Layout.fillHeight: true
            Layout.preferredWidth: 160
            color: SuiTheme.panel
            Column {
                anchors.fill: parent
                anchors.margins: SuiTheme.spaceSm
                spacing: 2
                Repeater {
                    model: win.panes
                    delegate: T.ItemDelegate {
                        id: paneRow
                        required property int index
                        required property var modelData
                        objectName: "settingsPane-" + modelData.key
                        width: parent.width
                        implicitHeight: SuiTheme.rowHeight
                        leftPadding: SuiTheme.spaceSm
                        highlighted: win.paneIndex === index
                        Accessible.role: Accessible.PageTab
                        Accessible.name: modelData.title
                        Accessible.selected: highlighted
                        onClicked: app.setSettingsPane(modelData.key)
                        contentItem: SuiText {
                            text: paneRow.modelData.title
                            font.weight: paneRow.highlighted ? SuiTheme.fontWeightStrong
                                                             : SuiTheme.fontWeightNormal
                        }
                        background: Rectangle {
                            radius: SuiTheme.radiusSm
                            color: paneRow.highlighted ? SuiTheme.accentSubtle
                                   : paneRow.hovered ? SuiTheme.bgHover : "transparent"
                        }
                    }
                }
            }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: SuiTheme.spaceLg
            currentIndex: win.paneIndex

            // -- General: where the app finds the server and Pocket ID ----------------------
            ColumnLayout {
                objectName: "settingsGeneral"
                spacing: SuiTheme.spaceSm
                SuiText {
                    Layout.fillWidth: true
                    text: "Where the app finds the voice-lab server and Pocket ID. Values set in"
                          + " the environment win and cannot be changed here."
                    variant: "caption"
                    wrapMode: Text.Wrap
                }
                SuiText { text: "Server (https://voice-lab.<tailnet>.ts.net)"; variant: "label" }
                SuiTextField {
                    id: serverField
                    objectName: "serverField"
                    Layout.fillWidth: true
                    label: "Server"
                    enabled: app.lockedSettings.indexOf("server_url") < 0
                }
                SuiText { text: "Pocket ID issuer (https://id.<tailnet>.ts.net)"; variant: "label" }
                SuiTextField {
                    id: issuerField
                    objectName: "issuerField"
                    Layout.fillWidth: true
                    label: "Issuer"
                    enabled: app.lockedSettings.indexOf("issuer") < 0
                }
                SuiText { text: "Pocket ID client"; variant: "label" }
                SuiTextField {
                    id: clientField
                    objectName: "clientField"
                    Layout.fillWidth: true
                    label: "Client"
                    enabled: app.lockedSettings.indexOf("client_id") < 0
                }
                SuiText { text: "Measurement: mouth to capsule distance, cm"; variant: "label" }
                SuiTextField {
                    id: distanceField
                    objectName: "distanceField"
                    Layout.fillWidth: true
                    label: "Distance in centimetres"
                    inputMethodHints: Qt.ImhDigitsOnly
                }
                Item { Layout.fillHeight: true }
                SuiButton {
                    objectName: "saveSettings"
                    Layout.alignment: Qt.AlignRight
                    text: "Save"
                    variant: "primary"
                    onClicked: app.saveSettings(serverField.text, issuerField.text, clientField.text,
                                                parseInt(distanceField.text) || 0)
                }
            }

            // -- Uploads: corpus sync and the home-host approval ------------------------------
            ColumnLayout {
                objectName: "settingsUploads"
                spacing: SuiTheme.spaceMd
                SuiCheckBox {
                    objectName: "settingsCorpusSync"
                    Layout.fillWidth: true
                    text: "Send takes to the PC"
                    description: "Takes, decisions and trims go to the PC's corpus (corpus_sync)."
                                 + " Off, they stay on this machine until it is on."
                    checked: app.corpusSync
                    enabled: app.lockedSettings.indexOf("corpus_sync") < 0
                    onToggled: app.setCorpusSync(checked)
                }
                SuiText {
                    Layout.fillWidth: true
                    text: "The PC accepts takes only under your approval for it (D-021): Approve"
                          + " uploads to my PC writes it here, from Tailscale and these settings."
                    variant: "caption"
                    wrapMode: Text.Wrap
                }
                SuiButton {
                    objectName: "settingsApprove"
                    text: "Approve uploads to my PC…"
                    enabled: !approval.busy
                    onClicked: approval.begin()
                }
                SuiText {
                    objectName: "settingsSyncLine"
                    Layout.fillWidth: true
                    visible: win.studio !== null
                    text: win.studio ? win.studio.syncText : ""
                    tone: win.studio && win.studio.syncTone !== "neutral" ? win.studio.syncTone : ""
                    wrapMode: Text.Wrap
                }
                Item { Layout.fillHeight: true }
            }

            // -- Recording: the microphone, Check Input, the match line, the flags' levels ---
            ColumnLayout {
                objectName: "settingsRecording"
                spacing: SuiTheme.spaceMd
                enabled: win.studio !== null
                SuiText {
                    objectName: "settingsDevice"
                    Layout.fillWidth: true
                    text: !win.studio ? "Record → Prompts uses the session flow on this machine."
                          : win.studio.deviceOk ? "Microphone: " + win.studio.deviceLabel
                          : "No microphone: " + win.studio.deviceLabel
                    wrapMode: Text.Wrap
                }
                RowLayout {
                    spacing: SuiTheme.spaceSm
                    SuiButton {
                        text: "Look again"
                        enabled: win.studio !== null
                        onClicked: win.studio.checkDevice()
                    }
                    SuiButton {
                        objectName: "settingsCheckInput"
                        text: "Check Input…"
                        enabled: win.studio !== null && win.studio.canCheck
                        onClicked: {
                            app.navigate("prompts")
                            win.studio.openCheck()
                        }
                    }
                }
                SuiCheckBox {
                    objectName: "settingsMatchLine"
                    Layout.fillWidth: true
                    text: "Compare Check Input with my match line"
                    description: "Keeps your first reading of its line on this machine, never"
                                 + " sent, and compares each later check with it."
                    checked: win.studio !== null && win.studio.matchLine
                    onToggled: win.studio.setMatchLine(checked)
                }
                SuiText { text: "Quality flags: starting levels, to calibrate on real takes"; variant: "label" }
                GridLayout {
                    columns: 2
                    columnSpacing: SuiTheme.spaceMd
                    rowSpacing: SuiTheme.spaceXs
                    SuiText { text: "Low SNR under, dB"; variant: "caption" }
                    SuiTextField {
                        id: snrField
                        objectName: "settingsLowSnr"
                        Layout.preferredWidth: 96
                        label: "Low SNR under, dB"
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        onEditingFinished: win.studio.setQualityLevels(parseFloat(snrField.text),
                                                                       parseFloat(driftField.text))
                    }
                    SuiText { text: "Inconsistent over, dB"; variant: "caption" }
                    SuiTextField {
                        id: driftField
                        objectName: "settingsDrift"
                        Layout.preferredWidth: 96
                        label: "Inconsistent over, dB"
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        onEditingFinished: win.studio.setQualityLevels(parseFloat(snrField.text),
                                                                       parseFloat(driftField.text))
                    }
                }
                Item { Layout.fillHeight: true }
            }
        }
    }
}
