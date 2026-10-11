// Analysis → Practice (design §6.1; checklist P-01 to P-05): the coach (due songs, the
// frontier) and pre-registered goals, from voice-journey's Practice page.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import "practice"

ScreenPage {
    objectName: "practiceScreen"
    title: "The Practice Room"
    tagline: "Tonight's session, argued from the data — due songs and frontier flags from the coach."

    NeedsServer { what: "the practice room"; visible: !app.signedIn }

    SuiText {
        objectName: "practiceLoading"
        visible: app.signedIn && (practice.state === "loading" || practice.state === "idle")
        text: "Consulting the coach…"
        variant: "caption"
    }
    SuiBanner {
        objectName: "practiceError"
        Layout.fillWidth: true
        visible: app.signedIn && practice.state === "failed"
        tone: "danger"
        title: "The coach did not answer"
        text: practice.error
    }

    DueCard { visible: app.signedIn && practice.state === "loaded" }
    FrontierCard { visible: app.signedIn && practice.state === "loaded" }
    GoalsCard { visible: app.signedIn && practice.state === "loaded" }
}
