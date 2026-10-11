// "The frontier" (checklist P-03): songs one measured step from a threshold.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import ".."

Card {
    objectName: "frontierCard"
    heading: "The frontier"
    subheading: "One step away, measured: songs sitting just under a threshold, each flag carrying the numbers that say so. These are the takes where tonight's practice moves a chart."

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
    SuiText {
        objectName: "frontierEmpty"
        Layout.fillWidth: true
        visible: practice.frontier.length === 0
        text: "No frontier flags right now — thresholds move as you do; check back after the next ingest."
        variant: "caption"
        wrapMode: Text.Wrap
    }
}
