// Referee (design §6.1; checklist R-01 to R-11): blind same-song A/B trials, the reveal,
// and the perceived quality by year (Bradley–Terry). The SPA's RefereePageComponent.jsx.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ScreenPage {
    id: page
    objectName: "refereeScreen"
    title: "The Referee"
    tagline: "Blind same-song A/B trials. Years stay hidden until you judge."

    // One of the five choices (referee.choices, in the SPA's order).
    component ChoiceButton: SuiButton {
        property int index: 0
        readonly property var choice: referee.choices.length > index ? referee.choices[index] : null
        objectName: choice ? "choice-" + choice.choice : ""
        text: choice ? choice.label : ""
        variant: choice && choice.primary ? "primary" : "secondary"
        enabled: choiceRow.open
        onClicked: if (choice) referee.judge(choice.choice)
    }

    NeedsServer { what: "the referee"; visible: !app.signedIn }

    SuiBanner {
        objectName: "refereeError"
        Layout.fillWidth: true
        visible: app.signedIn && referee.error !== ""
        tone: "danger"
        title: "The referee could not load"
        text: referee.error
        SuiButton { text: "Retry"; variant: "secondary"; onClicked: referee.refresh() }
    }

    SuiText {
        objectName: "refereeLoading"
        visible: app.signedIn && referee.state === "loading"
        text: "Loading the next pair…"
        variant: "caption"
    }

    SuiBanner {
        objectName: "refereeGate"
        Layout.fillWidth: true
        visible: app.signedIn && referee.gated
        tone: "warn"
        title: "Playback is off on the server"
        text: referee.gateText
    }

    Card {
        objectName: "trialCard"
        visible: app.signedIn && referee.enabled
        heading: "This pair"
        subheading: referee.hasPair || referee.revealed
            ? "Listen to both, then call it. “Not the same song” also helps — it flags a chained cluster for repair. Playback is loudness-matched and band-limited (8 kHz) to blunt recording-chain tells."
            : ""

        SuiText {
            objectName: "poolComplete"
            Layout.fillWidth: true
            visible: !referee.hasPair && !referee.revealed && referee.state === "loaded"
            text: referee.poolCompleteText
            variant: "caption"
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            visible: referee.hasPair || referee.revealed
            spacing: SuiTheme.spaceLg
            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                SuiText { text: "A"; variant: "heading" }
                PlayerBar {
                    objectName: "playerA"
                    Layout.fillWidth: true
                    key: "referee:a"
                    source: referee.aSource
                    available: referee.hasPair
                    gainDb: referee.aGainDb
                    lowpassHz: referee.lowpassHz
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                SuiText { text: "B"; variant: "heading" }
                PlayerBar {
                    objectName: "playerB"
                    Layout.fillWidth: true
                    key: "referee:b"
                    source: referee.bSource
                    available: referee.hasPair
                    gainDb: referee.bGainDb
                    lowpassHz: referee.lowpassHz
                }
            }
        }

        Flow {
            id: choiceRow
            Layout.fillWidth: true
            visible: referee.hasPair || referee.revealed
            spacing: SuiTheme.spaceSm
            readonly property bool open: referee.hasPair && !referee.submitting && !referee.revealed
            ChoiceButton { index: 0 }
            ChoiceButton { index: 1 }
            ChoiceButton { index: 2 }
            ChoiceButton { index: 3 }
            ChoiceButton { index: 4 }
        }

        SuiText {
            objectName: "revealLine"
            Layout.fillWidth: true
            visible: referee.revealed
            text: referee.revealText
            variant: "subheading"
            wrapMode: Text.Wrap
        }
        SuiText {
            objectName: "progressLine"
            Layout.fillWidth: true
            visible: referee.hasPair || referee.revealed
            text: referee.progressText
            variant: "caption"
        }
    }

    Card {
        objectName: "resultsCard"
        visible: app.signedIn && referee.hasResults
        heading: "Perceived quality by year"
        subheading: "Bradley–Terry strengths from your judgments, log2 relative to the earliest judged year. Positive means those years win your blind ear."

        SuiChart {
            objectName: "btChart"
            Layout.fillWidth: true
            Layout.maximumWidth: Math.max(420, 120 + referee.yearCount * 80)
            visible: referee.hasChart
            spec: referee.chartSpec
        }

        SuiText { text: "Counts"; variant: "label" }
        SuiTable {
            objectName: "yearTable"
            Layout.fillWidth: true
            Layout.maximumWidth: 560
            Layout.preferredHeight: headerHeight + rowHeight * referee.yearCount + 2
            label: "Counts by year"
            rowHeight: 30
            model: referee.yearRows
            columns: referee.yearColumns
        }
        SuiText {
            objectName: "choicesLine"
            Layout.fillWidth: true
            text: referee.choicesText
            variant: "caption"
            wrapMode: Text.Wrap
        }
        SuiText {
            objectName: "flagsLine"
            Layout.fillWidth: true
            visible: referee.flagsText !== ""
            text: referee.flagsText
            tone: "warn"
            wrapMode: Text.Wrap
        }
    }
}
