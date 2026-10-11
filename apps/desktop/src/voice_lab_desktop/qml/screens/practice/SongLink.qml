// A song's privacy-generic label and cluster id; activating it opens the Corpus
// filtered to that song (the SPA's `.vj-songname` link).
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui
import VoiceLab.StandIn

T.AbstractButton {
    id: link
    property string label: ""
    property string clusterId: ""
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    implicitWidth: column.implicitWidth
    implicitHeight: column.implicitHeight
    Accessible.role: Accessible.Link
    Accessible.name: label + " " + clusterId
    onClicked: practice.openCluster(clusterId)
    Keys.onReturnPressed: practice.openCluster(clusterId)

    contentItem: ColumnLayout {
        id: column
        spacing: 0
        SuiText {
            text: link.label
            color: link.hovered ? SuiTheme.accentHover : SuiTheme.accent
            font.underline: link.hovered
            elide: Text.ElideRight
            Layout.maximumWidth: 280
        }
        SuiText { text: link.clusterId; variant: "caption" }
    }
    background: Rectangle {
        color: "transparent"
        Rectangle {
            visible: link.visualFocus
            anchors.fill: parent
            anchors.margins: -(SuiTheme.focusRingOffset + SuiTheme.focusRingWidth)
            radius: SuiTheme.radiusSm
            color: "transparent"
            border.width: SuiTheme.focusRingWidth
            border.color: SuiTheme.focusRing
        }
    }
}
