// A legend entry: a colour swatch and its text. `shade` darkens the tone as SuiChart's
// points do (the fingerprint's year gradient); `opacity` lightens a band's swatch.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

RowLayout {
    property string text: ""
    property string tone: "bucket5"
    property real fill: 1
    property real shade: -1
    spacing: SuiTheme.spaceXs

    Rectangle {
        implicitWidth: 12
        implicitHeight: 12
        radius: 2
        color: {
            const c = SuiTheme.toneBase(parent.tone)
            if (parent.shade >= 0) {
                const t = 0.45 + 0.55 * Math.max(0, Math.min(1, parent.shade))
                return Qt.rgba(c.r * t, c.g * t, c.b * t, 1)
            }
            return Qt.rgba(c.r, c.g, c.b, c.a * parent.fill)
        }
    }
    SuiText { text: parent.text; variant: "caption" }
}
