// A titled section of a screen (the SPA's `.vj-card`).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

Rectangle {
    id: card
    default property alias content: column.data
    property string heading: ""
    property string subheading: ""
    Layout.fillWidth: true
    implicitHeight: column.implicitHeight + 2 * SuiTheme.spaceLg
    radius: SuiTheme.radiusMd
    color: SuiTheme.panel
    border.color: SuiTheme.line
    border.width: 1
    ColumnLayout {
        id: column
        anchors.fill: parent
        anchors.margins: SuiTheme.spaceLg
        spacing: SuiTheme.spaceSm
        SuiText { text: card.heading; variant: "heading"; visible: text !== "" }
        SuiText {
            Layout.fillWidth: true
            text: card.subheading
            variant: "caption"
            wrapMode: Text.Wrap
            visible: text !== ""
        }
    }
}
