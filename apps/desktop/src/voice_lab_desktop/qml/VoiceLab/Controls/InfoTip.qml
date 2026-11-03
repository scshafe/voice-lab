// InfoTip: scshafe-qt's SuiInfoTip (0.10.0) as the app shows it (D-059, docs/design/INFO-TIPS.md).
// - Plain text by default: a tip often holds a string from the server or the catalog.
// - With the focus (only Tab gives it: a click never does), Space and Return are the tip's,
//   and so is Escape while it is open. SuiInfoTip takes them as key presses, which a window
//   Shortcut gets first (the studio's Space and Return, Settings' Escape, which closes the
//   window) unless the focused item claims them as a shortcut override. The next Escape is
//   the window's again.
import QtQuick
import Scshafe.Ui

SuiInfoTip {
    id: tip
    textFormat: Text.PlainText
    Keys.onShortcutOverride: (event) => {
        const plain = !(event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))
        event.accepted = plain && (event.key === Qt.Key_Space || event.key === Qt.Key_Return
                                   || event.key === Qt.Key_Enter
                                   || (event.key === Qt.Key_Escape && tip.opened))
    }
}
