// A titled section of a screen (the SPA's `.vj-card`). `info`: what the section is, behind an ⓘ
// beside the heading (D-059); `subheading`: a line that stays visible (a live summary).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.Controls

Rectangle {
    id: card
    default property alias content: column.data
    property string heading: ""
    property string subheading: ""
    property string info: ""
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
        RowLayout {
            Layout.fillWidth: true
            visible: card.heading !== ""
            spacing: SuiTheme.spaceXs
            SuiText { text: card.heading; variant: "heading" }
            InfoTip {
                objectName: card.objectName !== "" ? card.objectName + "Info" : "cardInfo"
                visible: card.info !== ""
                title: card.heading
                text: card.info
            }
            Item { Layout.fillWidth: true }
        }
        SuiText {
            Layout.fillWidth: true
            text: card.subheading
            variant: "caption"
            wrapMode: Text.Wrap
            visible: text !== ""
        }
    }
}
