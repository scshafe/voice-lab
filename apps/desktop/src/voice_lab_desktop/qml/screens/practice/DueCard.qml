// "Due for a take" (checklist P-02): the coach's due songs.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import ".."

Card {
    objectName: "dueCard"
    heading: "Due for a take"
    subheading: "Spaced re-recording for the same-song engine: recurring songs whose next take buys the most evidence — stale threads, judged songs whose span a take today extends, songs one clean take from eligibility. Always framed as adding data, never as chasing a verdict."

    Repeater {
        objectName: "dueRows"
        model: practice.dueSongs
        delegate: RowLayout {
            id: due
            required property var modelData
            objectName: "due-" + modelData.clusterId
            Layout.fillWidth: true
            spacing: SuiTheme.spaceLg
            SongLink {
                objectName: "dueSong"
                Layout.preferredWidth: 220
                label: due.modelData.label
                clusterId: due.modelData.clusterId
            }
            ColumnLayout {
                Layout.preferredWidth: 110
                spacing: 0
                SuiText { objectName: "dueTakes"; text: due.modelData.takes; variant: "number" }
                SuiText { objectName: "dueYears"; text: due.modelData.years; variant: "caption" }
            }
            ColumnLayout {
                Layout.preferredWidth: 110
                spacing: 0
                SuiText { text: "last sung"; variant: "number" }
                SuiText { objectName: "dueLastSung"; text: due.modelData.lastSung; variant: "caption" }
            }
            Flow {
                Layout.fillWidth: true
                spacing: SuiTheme.spaceXs
                Repeater {
                    model: due.modelData.why
                    delegate: SuiChip {
                        required property var modelData
                        objectName: "whyChip"
                        text: modelData
                    }
                }
            }
        }
    }
    SuiText {
        objectName: "dueEmpty"
        Layout.fillWidth: true
        visible: practice.dueSongs.length === 0
        text: practice.dueEmptyText
        variant: "caption"
        wrapMode: Text.Wrap
    }
}
