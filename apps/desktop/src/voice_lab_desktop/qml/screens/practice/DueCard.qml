// "Due for a take" (checklist P-02): the coach's due songs.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import VoiceLab.Controls
import ".."

Card {
    objectName: "dueCard"
    heading: "Due for a take"
    info: "Spaced re-recording for the same-song engine: recurring songs whose next take buys the most evidence — stale threads, judged songs whose span a take today extends, songs one clean take from eligibility. Always framed as adding data, never as chasing a verdict."

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
                SuiText { objectName: "dueTakes"; text: due.modelData.takes; tabular: true }
                SuiText { objectName: "dueYears"; text: due.modelData.years; variant: "caption" }
            }
            ColumnLayout {
                Layout.preferredWidth: 110
                spacing: 0
                SuiText { text: "last sung"; tabular: true }
                SuiText { objectName: "dueLastSung"; text: due.modelData.lastSung; variant: "caption" }
            }
            Flow {
                Layout.fillWidth: true
                spacing: SuiTheme.spaceXs
                // The coach's first two reasons are the last-sung and takes columns'; the
                // chips say only the rest (the same-song index, eligibility; D-059).
                Repeater {
                    model: due.modelData.why.slice(2)
                    delegate: SuiChip {
                        required property var modelData
                        objectName: "whyChip"
                        text: modelData
                    }
                }
            }
        }
    }
    // Nothing due: the line shows; the coach's window is behind its ⓘ (D-059).
    RowLayout {
        Layout.fillWidth: true
        visible: practice.dueSongs.length === 0
        spacing: SuiTheme.spaceXs
        SuiText {
            objectName: "dueEmpty"
            text: "Nothing due — go sing something new."
            variant: "caption"
        }
        InfoTip {
            objectName: "dueEmptyInfo"
            title: "Nothing due"
            text: practice.dueEmptyText
        }
        Item { Layout.fillWidth: true }
    }
}
