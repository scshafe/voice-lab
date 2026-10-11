// STAND-IN, marked for removal: scshafe-qt's form-controls gap (design §6.4). The
// API is the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md ("SuiCheckbox");
// when scshafe-qt releases it, delete this file and its qmldir line.
//
// SuiCheckbox: a labelled check box. `checked`, `text`; `toggled()` fires on a
// user change (Space, click). Focus ring for keyboard focus.
import QtQuick
import QtQuick.Templates as T
import Scshafe.Ui

T.CheckBox {
    id: control

    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding,
                             implicitIndicatorHeight + topPadding + bottomPadding)
    padding: SuiTheme.spaceXs
    spacing: SuiTheme.spaceSm
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    opacity: enabled ? 1 : SuiTheme.disabledOpacity
    font.pixelSize: SuiTheme.fontSizeMd

    Accessible.role: Accessible.CheckBox
    Accessible.name: text
    Accessible.checkable: true
    Accessible.checked: checked

    indicator: Rectangle {
        objectName: "box"
        implicitWidth: 16
        implicitHeight: 16
        x: control.leftPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        radius: 3
        color: control.checked ? SuiTheme.alpha(SuiTheme.accent, 0.85) : SuiTheme.field
        border.width: 1
        border.color: control.checked ? SuiTheme.accent
                      : control.hovered ? SuiTheme.borderHover : SuiTheme.fieldBorder
        Text {
            textFormat: Text.PlainText
            anchors.centerIn: parent
            visible: control.checked
            text: "✓"
            color: SuiTheme.bg
            font.pixelSize: 12
            font.weight: SuiTheme.fontWeightBold
            Accessible.ignored: true
        }
        Rectangle {
            visible: control.visualFocus
            anchors.fill: parent
            anchors.margins: -(SuiTheme.focusRingOffset + SuiTheme.focusRingWidth)
            radius: parent.radius + SuiTheme.focusRingOffset + SuiTheme.focusRingWidth
            color: "transparent"
            border.width: SuiTheme.focusRingWidth
            border.color: SuiTheme.focusRing
        }
    }

    contentItem: Text {
        textFormat: Text.PlainText
        leftPadding: control.indicator.width + control.spacing
        text: control.text
        font: control.font
        color: SuiTheme.text
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.Wrap
        Accessible.ignored: true
    }
}
