// "The frontier" (checklist P-03): songs one measured step from a threshold.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import VoiceLab.Controls
import ".."

Card {
    objectName: "frontierCard"
    heading: "The frontier"
    info: "One step away, measured: songs sitting just under a threshold, each flag carrying the numbers that say so. These are the takes where tonight's practice moves a chart."

    Repeater {
        objectName: "frontierRows"
        model: practice.frontier
        delegate: ColumnLayout {
            id: row
            required property var modelData
            objectName: "frontier-" + modelData.clusterId
            Layout.fillWidth: true
            spacing: SuiTheme.spaceXs
            Flow {
                Layout.fillWidth: true
                spacing: SuiTheme.spaceSm
                SongLink {
                    objectName: "frontierSong"
                    label: row.modelData.label
                    clusterId: row.modelData.clusterId
                }
                Repeater {
                    model: row.modelData.rules
                    delegate: SuiChip {
                        required property var modelData
                        objectName: "ruleChip"
                        text: modelData
                        tone: "ok"
                    }
                }
            }
            Repeater {
                model: row.modelData.details
                delegate: SuiText {
                    required property var modelData
                    objectName: "frontierDetail"
                    Layout.fillWidth: true
                    text: modelData
                    variant: "caption"
                    wrapMode: Text.Wrap
                }
            }
        }
    }
    // No flags: the line shows; why they come and go is behind its ⓘ (D-059).
    RowLayout {
        Layout.fillWidth: true
        visible: practice.frontier.length === 0
        spacing: SuiTheme.spaceXs
        SuiText {
            objectName: "frontierEmpty"
            text: "No frontier flags right now."
            variant: "caption"
        }
        InfoTip {
            objectName: "frontierEmptyInfo"
            title: "Frontier flags"
            text: "Thresholds move as you do; check back after the next ingest."
        }
        Item { Layout.fillWidth: true }
    }
}
