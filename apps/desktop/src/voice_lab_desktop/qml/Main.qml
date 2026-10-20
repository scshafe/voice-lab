// voice-lab-desktop: the main window. Every state and action lives in the Python
// models (app.py): `app` (account, settings, navigation) and one model per screen
// (`status`, `uploads`, `measurement`, `corpus`, `journey`, `practice`, `referee`, `player`;
// Record: prompts is the prompt studio, whose model is `app.studioModel`, or with
// `prompts_mode = "sessions"` phase 4c's screen over `app.promptsModel`), and `approval`:
// Approve uploads to my PC, whose one dialog lives here (Status, Uploads and the studio open it).
// This file lays out scshafe-qt's shell around them.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import "screens"

Window {
    id: root
    objectName: "mainWindow"
    width: 1280
    height: 820
    minimumWidth: 960
    minimumHeight: 600
    visible: true
    color: SuiTheme.bg
    title: "voice-lab"

    Connections {
        target: app
        function onToast(text, tone) { toasts.show(text, { tone: tone }) }
        function onCloseApproved() {
            root.closeApproved = true
            root.close()
        }
    }

    // Closing (the window, or Quit) asks first about an unsaved trim edit in the studio.
    property bool closeApproved: false
    onClosing: (close) => {
        if (!root.closeApproved && !app.mayClose())
            close.accepted = false
    }

    SuiAppShell {
        id: shell
        anchors.fill: parent
        sidebarLabel: "Screens"
        contentLabel: "Screen"

        sidebar: Rectangle {
            color: SuiTheme.panel
            ColumnLayout {
                anchors.fill: parent
                anchors.topMargin: SuiTheme.spaceMd
                spacing: SuiTheme.spaceSm
                SuiText {
                    Layout.leftMargin: SuiTheme.spaceLg
                    text: "voice-lab"
                    variant: "heading"
                }
                SuiText {
                    objectName: "sidebarAccount"
                    Layout.leftMargin: SuiTheme.spaceLg
                    Layout.rightMargin: SuiTheme.spaceLg
                    Layout.fillWidth: true
                    text: app.signedIn ? app.account : (app.configured ? "Not signed in" : "Not set up")
                    variant: "caption"
                    elide: Text.ElideRight
                }
                SuiSidebarList {
                    id: sidebarList
                    objectName: "sidebarList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    label: "Screens"
                    model: app.sidebarModel
                    selectedIndex: app.screenIndex
                    onActivated: (index) => app.navigateIndex(index)
                }
            }
        }

        content: StackLayout {
            objectName: "screens"
            currentIndex: app.screenIndex
            StatusScreen {}
            UploadsScreen {}
            Loader {
                objectName: "recordPrompts"
                source: app.promptsMode === "studio" ? "screens/StudioScreen.qml"
                                                     : "screens/PromptsScreen.qml"
            }
            MeasurementScreen {}
            CorpusScreen {}
            JourneyScreen {}
            PracticeScreen {}
            RefereeScreen {}
        }
    }

    // Approve uploads to my PC (D-021): what Approve writes, from Tailscale and the settings;
    // or, when it cannot be approved, why. Pressing Approve is the review.
    Connections {
        target: approval
        function onDialogChanged() {
            // open() reverses a closing dialog (RowDetailSheet.qml says why).
            if (approval.dialogOpen)
                approveDialog.open()
            else if (approveDialog.visible)
                approveDialog.close()
        }
    }
    SuiDialog {
        id: approveDialog
        objectName: "approveDialog"
        title: approval.dialogTitle
        description: approval.dialogText
        onRejected: if (approval.dialogOpen) approval.cancel()
        ColumnLayout {
            spacing: SuiTheme.spaceSm
            GridLayout {
                objectName: "approveFields"
                visible: approval.canApprove
                Layout.fillWidth: true
                columns: 2
                columnSpacing: SuiTheme.spaceLg
                rowSpacing: SuiTheme.spaceXs
                SuiText { text: "PC"; variant: "label" }
                SuiText { objectName: "approveEndpoint"; text: approval.endpoint; variant: "code" }
                SuiText { text: "Tailscale node"; variant: "label" }
                SuiText { objectName: "approveNode"; text: approval.nodeId; variant: "code" }
                SuiText { text: "What"; variant: "label" }
                SuiText {
                    objectName: "approveClasses"
                    Layout.fillWidth: true
                    text: approval.classesText
                    wrapMode: Text.Wrap
                }
                SuiText { text: "Until"; variant: "label" }
                SuiText { objectName: "approveExpires"; text: approval.expiresText }
                SuiText { text: "Approved by"; variant: "label" }
                SuiText { objectName: "approveBy"; text: approval.approvedBy }
            }
            SuiText {
                objectName: "approveReason"
                Layout.fillWidth: true
                visible: approval.canApprove && approval.reasonText !== ""
                text: approval.reasonText
                variant: "caption"
                wrapMode: Text.Wrap
            }
        }
        actions: [
            SuiButton {
                objectName: "approveCancel"
                text: approval.canApprove ? "Cancel" : "Close"
                onClicked: approval.cancel()
            },
            SuiButton {
                objectName: "approveConfirm"
                visible: approval.canApprove
                enabled: !approval.busy
                text: "Approve"
                variant: "primary"
                onClicked: approval.confirm()
            }
        ]
    }

    // Top right: at the bottom, a toast would cover the Studio's status line and its Sync now,
    // Approve or Details button for as long as it shows.
    SuiToastHost {
        id: toasts
        objectName: "toasts"
        anchors.fill: parent
        alignment: Qt.AlignTop | Qt.AlignRight
        z: 100
    }
}
