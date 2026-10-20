// Uploads (design §6.1; checklist U-01 to U-04): the local spool, per-session progress,
// resume, retry and receipts. Measurement sessions and prompted packages (phase 4c; held
// here until prompted uploads are on, D-042). While uploads wait for the home-host approval
// (D-021), or it expires within 30 days, Approve uploads to my PC (the app's `approval`).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    objectName: "uploadsScreen"
    title: "Uploads"
    tagline: "Sessions waiting for the server's receipt, and what it accepted."

    Card {
        heading: "Spool"
        SuiBanner {
            objectName: "uploadsNotReady"
            Layout.fillWidth: true
            visible: !uploads.ready && uploads.readiness !== ""
            tone: "warn"
            title: "This machine cannot upload yet"
            text: uploads.readiness
        }
        SuiText {
            objectName: "uploadsReadiness"
            Layout.fillWidth: true
            visible: uploads.ready
            text: uploads.readiness
            variant: "caption"
            wrapMode: Text.Wrap
        }
        RowLayout {
            objectName: "uploadsApproval"
            Layout.fillWidth: true
            visible: approval.needed || approval.expiringSoon
            spacing: SuiTheme.spaceSm
            SuiText {
                objectName: "uploadsApprovalText"
                Layout.fillWidth: true
                text: approval.summary
                tone: approval.needed ? "warn" : ""
                wrapMode: Text.Wrap
            }
            SuiButton {
                objectName: "uploadsApprove"
                text: "Approve uploads to my PC"
                variant: "primary"
                enabled: !approval.busy
                onClicked: approval.begin()
            }
        }
        RowLayout {
            spacing: SuiTheme.spaceSm
            SuiText {
                objectName: "uploadsSummary"
                Layout.fillWidth: true
                text: uploads.pendingCount + " waiting · " + uploads.failedCount + " refused"
                      + (uploads.heldCount > 0 ? " · " + uploads.heldCount + " held here" : "")
            }
            SuiButton {
                objectName: "uploadAll"
                text: uploads.busy ? "Uploading…" : "Upload now"
                variant: "primary"
                enabled: !uploads.busy && uploads.pendingCount > 0 && app.signedIn && uploads.ready
                onClicked: uploads.uploadAll()
            }
        }
        SuiProgressBar {
            objectName: "uploadProgress"
            Layout.fillWidth: true
            visible: uploads.busy
            value: uploads.progressValue
            label: uploads.progressLabel
            // The bytes sent ("12.4 MB of 48 MB"), else the percentage.
            valueText: uploads.progressDetail !== "" ? uploads.progressDetail
                                                     : Math.round(position * 100) + " %"
        }
    }

    Card {
        heading: "Sessions"
        SuiEmptyState {
            Layout.fillWidth: true
            visible: uploads.rows.length === 0
            iconName: "inbox"
            title: "No local sessions"
            description: "Measurement and prompted sessions recorded on this machine wait here until the server accepts them."
        }
        Repeater {
            objectName: "sessionRows"
            model: uploads.rows
            delegate: RowLayout {
                required property var modelData
                objectName: "session-" + modelData.sessionId
                Layout.fillWidth: true
                spacing: SuiTheme.spaceSm
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    SuiText { text: modelData.sessionId; variant: "code" }
                    SuiText {
                        objectName: "sessionDetail"
                        Layout.fillWidth: true
                        text: modelData.detail
                        variant: "caption"
                        wrapMode: Text.Wrap
                    }
                }
                SuiChip { objectName: "sessionState"; text: modelData.stateLabel; tone: modelData.tone; dot: true }
                SuiButton {
                    objectName: "retry"
                    visible: modelData.canRetry
                    text: "Retry"
                    variant: "secondary"
                    onClicked: uploads.retry(modelData.sessionId)
                }
            }
        }
    }
}
