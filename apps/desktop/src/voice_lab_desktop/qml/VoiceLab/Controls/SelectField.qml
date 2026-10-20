// SelectField: a captioned, controlled choice over scshafe-qt's SuiSelect (0.4.0), the API
// docs/design/PHASE-4-LOG.md asked for. SuiSelect is Qt's ComboBox: it moves its own index
// and leaves its caption outside, while the app's filters and forms take a choice only
// through their owner's model.
// - `options`: [{ value: string, label: string }]; `value`: the chosen value (one of the
//   options' values; "" is a normal value, e.g. "All");
// - `label`: the visible caption above the select (also its accessible name);
// - `valueSelected(value)` fires on a user choice. Controlled: it never assigns `value`
//   itself. The owner updates its model on `valueSelected`, and its binding on `value`
//   carries the choice back; a choice the owner does not take leaves the shown value
//   unchanged. `currentIndex` and `currentLabel` always follow `value`; `select(value)`
//   makes the same choice from code.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui

ColumnLayout {
    id: root

    property var options: []
    property string value: ""
    property string label: ""
    readonly property int currentIndex: _indexOf(value)
    readonly property string currentLabel: currentIndex >= 0 ? options[currentIndex].label : ""
    readonly property bool popupOpen: combo.popup.visible

    signal valueSelected(string value)

    function _indexOf(v) {
        for (let i = 0; i < options.length; i++)
            if (options[i].value === v) return i
        return -1
    }
    function select(v) {
        if (_indexOf(v) < 0 || v === value) return
        valueSelected(v)
    }

    spacing: 2

    SuiText {
        visible: root.label !== ""
        text: root.label
        variant: "label"
        Accessible.ignored: true
    }

    SuiSelect {
        id: combo
        objectName: "combo"
        Layout.fillWidth: true
        label: root.label
        model: root.options
        textRole: "label"
        valueRole: "value"
        currentIndex: root.currentIndex

        onActivated: (index) => {
            // The combo box moved its own index: give it back to `value` (controlled).
            currentIndex = Qt.binding(() => root.currentIndex)
            root.select(root.options[index].value)
        }
    }
}
