// A button that shows its key after its label (D-058; the recording UX review's item 4: "each
// transport button shows its key glyph"), drawn as SuiKbd. The key is the model's
// (`app.studioModel.keys`: ⌘S on macOS, Ctrl+S elsewhere).
import QtQuick
import Scshafe.Ui

SuiButton {
    id: control

    property string key: ""
    // Every text the button may show (Play and Pause): its width fits the widest, so a
    // change of state never moves the layout.
    property var texts: [text]

    Accessible.description: key !== "" ? "Key: " + key : ""

    FontMetrics {
        id: metrics
        font: control.font
    }

    contentItem: Item {
        readonly property real widest: Math.max(...control.texts.map((t) => metrics.advanceWidth(t)))
        implicitWidth: Math.ceil(widest) + (hint.visible ? row.spacing + hint.implicitWidth : 0)
        implicitHeight: row.implicitHeight
        Row {
            id: row
            anchors.centerIn: parent
            spacing: SuiTheme.spaceXs
            Text {
                objectName: "label"
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: control.text
                font: control.font
                color: control.variant === "ghost" && !control._hot ? SuiTheme.muted : SuiTheme.text
            }
            SuiKbd {
                id: hint
                objectName: "keyHint"
                anchors.verticalCenter: parent.verticalCenter
                visible: control.key !== ""
                text: control.key
            }
        }
    }
}
