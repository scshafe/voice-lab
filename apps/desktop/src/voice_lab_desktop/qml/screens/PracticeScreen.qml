// Skeleton: filled in by the 4a work on this screen.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    objectName: "practiceScreen"
    title: "Practice"
    NeedsServer { what: "the practice"; visible: !app.signedIn }
}
