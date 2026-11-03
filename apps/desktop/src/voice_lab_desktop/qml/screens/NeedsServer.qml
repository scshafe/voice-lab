// What a screen shows before the app can ask the server: no settings, or signed out.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui

SuiEmptyState {
    objectName: "needsServer"
    property string what: "this screen"
    Layout.fillWidth: true
    Layout.fillHeight: true
    iconName: app.configured ? "user" : "info"
    title: app.configured ? "Sign in to see " + what : "Set up the server first"
    // Signed out, the title says it all (D-059); not set up, where the settings are.
    description: app.configured ? "" : "Set the voice-lab server and the Pocket ID issuer in Settings."
    SuiButton { text: "Go to Status"; variant: "primary"; onClicked: app.navigate("status") }
}
