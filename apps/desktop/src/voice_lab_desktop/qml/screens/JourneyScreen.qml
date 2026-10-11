// Skeleton: filled in by the 4a work on this screen.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    objectName: "journeyScreen"
    title: "Journey"
    NeedsServer { what: "the journey"; visible: !app.signedIn }
}
