// voice-lab-desktop: the main window. Every state and action lives in the Python
// models (app.py): `app` (account, settings, navigation) and one model per screen
// (`status`, `uploads`, `measurement`, `corpus`, `journey`, `practice`, `referee`, `player`;
// Record: prompts is the prompt studio, whose model is `app.studioModel`, or with
// `prompts_mode = "sessions"` phase 4c's screen over `app.promptsModel`).
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

    SuiToastHost {
        id: toasts
        objectName: "toasts"
        anchors.fill: parent
        z: 100
    }
}
