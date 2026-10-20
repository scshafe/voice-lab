// The row detail (RowDetailSheetComponent): the full row, the classifier's rationale,
// the player, the local transcript text on request only, and the spot-check verdict.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui
import VoiceLab.StandIn
import ".."

SuiSheet {
    id: sheet
    title: corpus.detailTitle
    description: corpus.detailState === "loaded" ? corpus.detailDescription : ""
    width: Math.min(480, _parentWidth)

    property bool _syncing: false

    // The sheet follows the model. A closing sheet stays visible until its exit transition
    // ends; open() then reverses the transition (no closed follows), and does nothing to a
    // sheet that is open or opening. So a row opened just after the last one closed is shown,
    // instead of being skipped and closed again when the transition ends.
    Connections {
        target: corpus
        function onDetailChanged() {
            sheet._syncing = true
            if (corpus.detailOpen)
                sheet.open()
            else if (sheet.visible)
                sheet.close()
            sheet._syncing = false
        }
    }
    onAboutToShow: note.text = ""
    // Closed here (Escape, a click outside): the detail closes as the sheet starts to, not
    // when its transition ends, when the model may already show another row.
    onAboutToHide: if (!_syncing && corpus.detailOpen) corpus.closeDetail()

    Flickable {
        id: body
        objectName: "detailBody"
        clip: true
        implicitHeight: content.implicitHeight
        contentWidth: width
        contentHeight: content.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        T.ScrollBar.vertical: T.ScrollBar {
            width: 8
            policy: T.ScrollBar.AsNeeded
            contentItem: Rectangle { radius: 3; color: SuiTheme.borderStrong }
        }

        ColumnLayout {
            id: content
            width: body.width - SuiTheme.spaceSm
            spacing: SuiTheme.spaceMd

            SuiText {
                objectName: "detailLoading"
                visible: corpus.detailState === "loading"
                text: "Loading…"
                tone: "muted"
            }
            SuiText {
                objectName: "detailError"
                Layout.fillWidth: true
                visible: corpus.detailState === "failed"
                text: corpus.detailError
                tone: "muted"
                wrapMode: Text.Wrap
            }

            ColumnLayout {
                objectName: "detailLoaded"
                visible: corpus.detailState === "loaded"
                Layout.fillWidth: true
                spacing: SuiTheme.spaceMd

                ColumnLayout {
                    objectName: "detailMeta"
                    Layout.fillWidth: true
                    spacing: 2
                    Repeater {
                        objectName: "detailEntries"
                        model: corpus.detailEntries
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: SuiTheme.spaceSm
                            SuiText {
                                Layout.preferredWidth: 120
                                text: modelData.label
                                variant: "label"
                                tone: "muted"
                            }
                            SuiText {
                                objectName: "detail-" + modelData.label
                                Layout.fillWidth: true
                                text: modelData.value
                                wrapMode: Text.Wrap
                            }
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    visible: corpus.detailRationale !== ""
                    spacing: 2
                    SuiText { text: "Classifier rationale"; variant: "subheading" }
                    SuiText {
                        objectName: "detailRationale"
                        Layout.fillWidth: true
                        text: corpus.detailRationale
                        wrapMode: Text.Wrap
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    SuiText { text: "Listen"; variant: "subheading" }
                    PlayerBar {
                        objectName: "detailPlayer"
                        Layout.fillWidth: true
                        key: corpus.detailId !== "" ? "corpus:" + corpus.detailId : ""
                        source: corpus.detailId
                        available: corpus.playbackEnabled
                        unavailableText: "Playback requires the release-gated launch."
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    SuiText { text: "Transcript text"; variant: "subheading" }
                    SuiButton {
                        objectName: "viewTranscript"
                        visible: corpus.transcriptAllowed && corpus.transcriptState === "idle"
                        text: "View local text"
                        variant: "secondary"
                        onClicked: corpus.loadTranscript()
                    }
                    SuiText {
                        objectName: "transcriptLoading"
                        visible: corpus.transcriptAllowed && corpus.transcriptState === "loading"
                        text: "Loading…"
                        tone: "muted"
                    }
                    SuiText {
                        objectName: "transcriptMessage"
                        Layout.fillWidth: true
                        visible: corpus.transcriptMessage !== ""
                            && (!corpus.transcriptAllowed || corpus.transcriptState === "failed")
                        text: corpus.transcriptMessage
                        tone: "muted"
                        wrapMode: Text.Wrap
                    }
                    SuiText {
                        objectName: "transcriptText"
                        Layout.fillWidth: true
                        visible: corpus.transcriptState === "loaded"
                        text: corpus.transcriptText
                        variant: "code"
                        wrapMode: Text.Wrap
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: SuiTheme.spaceXs
                    SuiText { text: "Verdict"; variant: "subheading" }
                    SuiText { text: "Spot-check note (optional)"; variant: "label"; tone: "muted" }
                    SuiTextField {
                        id: note
                        objectName: "verdictNote"
                        Layout.fillWidth: true
                        label: "Spot-check note (optional)"
                    }
                    Flow {
                        Layout.fillWidth: true
                        spacing: SuiTheme.spaceXs
                        Repeater {
                            model: corpus.verdictChoices
                            delegate: SuiButton {
                                required property var modelData
                                objectName: "verdict-" + modelData.value
                                text: modelData.label
                                variant: corpus.detailVerdict === modelData.value ? "primary" : "secondary"
                                enabled: !corpus.verdictBusy
                                onClicked: corpus.submitVerdict(modelData.value, note.text)
                            }
                        }
                    }
                }
            }
        }
    }

    actions: [
        SuiButton {
            objectName: "closeDetail"
            text: "Close"
            onClicked: sheet.close()
        }
    ]
}
