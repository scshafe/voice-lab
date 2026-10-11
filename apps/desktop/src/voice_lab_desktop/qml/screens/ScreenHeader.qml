// A screen's title and tagline (the SPA's per-view header, checklist A-03).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

ColumnLayout {
    property string title: ""
    property string tagline: ""
    Layout.fillWidth: true
    spacing: 2
    SuiText { objectName: "screenTitle"; text: parent.title; variant: "title" }
    SuiText {
        objectName: "screenTagline"
        Layout.fillWidth: true
        text: parent.tagline
        variant: "caption"
        wrapMode: Text.Wrap
        visible: text !== ""
    }
}
