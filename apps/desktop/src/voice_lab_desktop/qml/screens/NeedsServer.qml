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
    description: app.configured
        ? "The app reads the voice-lab server with your Pocket ID sign-in."
        : "Set the voice-lab server and the Pocket ID issuer on the Status screen."
    SuiButton { text: "Go to Status"; variant: "primary"; onClicked: app.navigate("status") }
}
