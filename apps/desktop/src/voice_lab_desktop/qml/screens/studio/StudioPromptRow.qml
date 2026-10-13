// One prompt in the studio's library: its title and status (and "Silent" or "Step" for a
// prompt that is not read aloud). Selecting it makes it the recording target; rows stay
// put while a take is recorded or saved, and only the current one is enabled then.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui
import VoiceLab.StandIn

T.ItemDelegate {
    id: row

    required property string promptId
    required property string title
    required property string statusTitle
    required property string kind
    required property bool current
    property var rec

    implicitHeight: Math.max(44, implicitContentHeight + topPadding + bottomPadding)
    topPadding: SuiTheme.spaceXs
    bottomPadding: SuiTheme.spaceXs
    leftPadding: SuiTheme.spaceSm
    rightPadding: SuiTheme.spaceSm
    hoverEnabled: true
    focusPolicy: Qt.NoFocus
    enabled: row.rec !== null && row.rec !== undefined && (row.rec.canNavigate || row.current)
    opacity: enabled ? 1 : SuiTheme.disabledOpacity
    readonly property string kindTitle: row.kind === "room-tone" ? "Silent · "
                                        : row.kind === "procedure" ? "Step · " : ""

    Accessible.role: Accessible.ListItem
    Accessible.name: row.title + ", " + row.statusTitle
    Accessible.selected: row.current

    onClicked: row.rec.selectPrompt(row.promptId)

    background: Rectangle {
        radius: SuiTheme.radiusSm
        color: row.current ? SuiTheme.accentSubtle : (row.hovered ? SuiTheme.bgHover : "transparent")
    }
    contentItem: ColumnLayout {
        spacing: 1
        SuiText {
            Layout.fillWidth: true
            text: row.title
            font.weight: row.current ? SuiTheme.fontWeightStrong : SuiTheme.fontWeightNormal
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
        }
        SuiText {
            Layout.fillWidth: true
            text: row.kindTitle + row.statusTitle
            variant: "caption"
            elide: Text.ElideRight
        }
    }
}
