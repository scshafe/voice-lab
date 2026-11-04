// A screen's title, and its tagline behind an ⓘ beside it (the SPA's per-view header,
// checklist A-03; D-059).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.Controls

RowLayout {
    property string title: ""
    property string tagline: ""
    Layout.fillWidth: true
    spacing: SuiTheme.spaceXs
    SuiText { objectName: "screenTitle"; text: parent.title; variant: "title" }
    InfoTip {
        objectName: "screenTagline"
        visible: text !== ""
        title: parent.title
        text: parent.tagline
    }
    Item { Layout.fillWidth: true }
}
