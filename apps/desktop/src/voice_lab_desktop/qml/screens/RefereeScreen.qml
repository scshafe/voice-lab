// Skeleton: filled in by the 4a work on this screen.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    objectName: "refereeScreen"
    title: "Referee"
    NeedsServer { what: "the referee"; visible: !app.signedIn }
}
