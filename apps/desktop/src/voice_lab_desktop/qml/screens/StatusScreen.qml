// Sign-in and status (design §6.1; checklist S-01 to S-05, A-02): the account, the
// settings, the server and its gates, the job runner, the upload approval (D-021: Approve
// uploads to my PC, the app's `approval`), the spool, the evidence report.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    id: page
    objectName: "statusScreen"
    title: "Status"
    tagline: "Sign-in, the voice-lab server, and the sessions waiting on this machine."

    SuiBanner {
        objectName: "setupBanner"
        Layout.fillWidth: true
        visible: !app.configured
        tone: "warn"
        title: "Set up the server"
        text: "Set the voice-lab server and the Pocket ID issuer before signing in."
        SuiButton { text: "Settings…"; variant: "secondary"; onClicked: settings.open() }
    }

    Card {
        heading: "Account"
        RowLayout {
            spacing: SuiTheme.spaceSm
            SuiText {
                objectName: "accountText"
                Layout.fillWidth: true
                text: app.signedIn ? "Signed in as " + app.account
                    : app.authBusy ? "Waiting for the browser…" : "Not signed in."
            }
            SuiButton {
                objectName: "signInButton"
                text: app.signedIn ? "Sign out" : "Sign in"
                variant: app.signedIn ? "ghost" : "primary"
                enabled: !app.authBusy && (app.signedIn || app.configured)
                onClicked: app.signedIn ? app.signOut() : app.signIn()
            }
            SuiButton {
                objectName: "cancelSignInButton"
                visible: app.authBusy
                text: "Cancel"
                variant: "ghost"
                onClicked: app.cancelSignIn()
            }
            SuiButton {
                objectName: "settingsButton"
                text: "Settings…"
                variant: "ghost"
                onClicked: settings.open()
            }
        }
        SuiText {
            Layout.fillWidth: true
            text: "Settings from " + app.settingsSource + "."
            variant: "caption"
            wrapMode: Text.Wrap
        }
    }

    Card {
        heading: "Server"
        GridLayout {
            columns: 2
            columnSpacing: SuiTheme.spaceLg
            rowSpacing: SuiTheme.spaceXs
            SuiText { text: "Address"; variant: "label" }
            SuiText { objectName: "serverAddress"; text: app.server || "not set"; variant: "code" }
            SuiText { text: "Build"; variant: "label" }
            SuiText { objectName: "serverBuild"; text: status.serverBuild || "—"; variant: "code" }
            SuiText { text: "API versions"; variant: "label" }
            SuiText { objectName: "apiVersions"; text: status.apiVersions || "—" }
            SuiText { text: "Minimum client"; variant: "label" }
            SuiText { objectName: "minimumClient"; text: status.minimumClientVersion || "—" }
            SuiText { text: "This app"; variant: "label" }
            SuiText { objectName: "appVersion"; text: app.version }
        }
        SuiBanner {
            objectName: "clientProblem"
            Layout.fillWidth: true
            visible: status.clientProblem !== ""
            tone: "danger"
            title: "Update the app"
            text: status.clientProblem
        }
        SuiText {
            objectName: "statusError"
            Layout.fillWidth: true
            visible: status.error !== ""
            text: status.error
            tone: "danger"
            wrapMode: Text.Wrap
        }
        RowLayout {
            SuiText {
                text: status.state === "loading" ? "Asking the server…" : ""
                variant: "caption"
                Layout.fillWidth: true
            }
            SuiButton {
                objectName: "refreshStatus"
                text: "Refresh"
                variant: "secondary"
                enabled: app.signedIn && status.state !== "loading"
                onClicked: status.refresh()
            }
        }
    }

    Card {
        heading: "Uploads to the PC"
        subheading: "Recordings go only to the PC you approve, for a year at most (D-021). The app looks the PC up in Tailscale; pressing Approve in its dialog is your review."
        RowLayout {
            spacing: SuiTheme.spaceSm
            SuiText {
                objectName: "approvalSummary"
                Layout.fillWidth: true
                text: approval.summary
                tone: approval.needed ? "warn" : ""
                wrapMode: Text.Wrap
            }
            SuiButton {
                objectName: "statusApprove"
                text: "Approve uploads to my PC"
                variant: approval.needed || approval.expiringSoon ? "primary" : "secondary"
                enabled: !approval.busy && app.server !== ""
                onClicked: approval.begin()
            }
        }
    }

    Card {
        heading: "The server's gates"
        subheading: "What the operator enabled on the server. Playback needs a recorded approval there; transcript text and feature detail need their local stores."
        RowLayout {
            spacing: SuiTheme.spaceSm
            SuiChip {
                objectName: "gatePlayback"
                text: "Playback: " + (status.playbackEnabled ? "on" : "off")
                tone: status.playbackEnabled ? "ok" : "neutral"
                dot: true
            }
            SuiChip {
                objectName: "gateTranscript"
                text: "Transcript text: " + (status.transcriptTextEnabled ? "on" : "off")
                tone: status.transcriptTextEnabled ? "ok" : "neutral"
                dot: true
            }
            SuiChip {
                objectName: "gateFeatures"
                text: "Feature detail: " + (status.featureDetailEnabled ? "on" : "off")
                tone: status.featureDetailEnabled ? "ok" : "neutral"
                dot: true
            }
        }
    }

    Card {
        heading: "Job runner"
        subheading: "The PC's journey pipeline (GET /api/v1/watch)."
        Repeater {
            model: status.watchLines
            delegate: SuiText {
                required property var modelData
                objectName: "watchLine"
                text: modelData
            }
        }
        SuiText { visible: status.watchLines.length === 0; text: "—"; variant: "caption" }
    }

    Card {
        heading: "Spool"
        subheading: "Measurement sessions stay on this machine until the server accepts them."
        RowLayout {
            SuiText { objectName: "spoolText"; Layout.fillWidth: true; text: status.spoolText; wrapMode: Text.Wrap }
            SuiButton { text: "Open Uploads"; variant: "secondary"; onClicked: app.navigate("uploads") }
        }
    }

    Card {
        heading: "Evidence report"
        subheading: "The web view renders the evidence report; it opens in your browser, behind the same sign-in."
        SuiButton {
            objectName: "openReport"
            text: "Open the evidence report"
            enabled: app.configured
            onClicked: app.openReport()
        }
    }

    SuiDialog {
        id: settings
        objectName: "settingsDialog"
        parent: page
        title: "Settings"
        description: "Where the app finds the voice-lab server and Pocket ID. Values set in the environment win and cannot be changed here."
        onOpened: {
            serverField.text = app.server
            issuerField.text = app.issuer
            clientField.text = app.clientId
            distanceField.text = String(app.distanceCm)
        }
        ColumnLayout {
            spacing: SuiTheme.spaceSm
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
        }
        actions: [
            SuiButton { text: "Cancel"; onClicked: settings.reject() },
            SuiButton {
                objectName: "saveSettings"
                text: "Save"
                variant: "primary"
                onClicked: if (app.saveSettings(serverField.text, issuerField.text, clientField.text,
                                                parseInt(distanceField.text) || 0)) settings.accept()
            }
        ]
    }
}
