// Skeleton: filled in by the 4a work on this screen.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    objectName: "corpusScreen"
    title: "Corpus"
    NeedsServer { what: "the corpus"; visible: !app.signedIn }
}
