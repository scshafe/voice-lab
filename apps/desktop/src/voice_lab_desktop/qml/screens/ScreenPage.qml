// A scrolling page: header, then the screen's content (cards), with the page margins.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui

Flickable {
    id: page
    default property alias content: column.data
    property string title: ""
    property string tagline: ""
    clip: true
    contentWidth: width
    contentHeight: column.implicitHeight + 2 * SuiTheme.spaceXl
    boundsBehavior: Flickable.StopAtBounds
    T.ScrollBar.vertical: T.ScrollBar {
        width: 8
        policy: T.ScrollBar.AsNeeded
        contentItem: Rectangle { radius: 3; color: SuiTheme.borderStrong }
    }
    ColumnLayout {
        id: column
        x: SuiTheme.spaceXl
        y: SuiTheme.spaceXl
        width: page.width - 2 * SuiTheme.spaceXl
        spacing: SuiTheme.spaceLg
        ScreenHeader { title: page.title; tagline: page.tagline }
    }
}
