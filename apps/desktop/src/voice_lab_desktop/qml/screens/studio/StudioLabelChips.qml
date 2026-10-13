// The header's label chips (NATIVE_RECORDING_UX.md §4, "labels"): one non-wrapping row in
// effective-label order. The pinned chips (the category, and Test in a physical-test
// catalog) always show; later chips that do not fit collapse into a trailing "+N". The row
// is read aloud as every label ("Labels: Reference, Reference Candidate").
import QtQuick
import Scshafe.Ui

Item {
    id: chips
    objectName: "studioLabelChips"

    property var titles: []
    property int pinned: 1
    property string spokenLabel: ""
    readonly property int spacing: SuiTheme.spaceXs
    readonly property int shown: fitting(width)

    implicitHeight: SuiTheme.chipHeight
    clip: true
    Accessible.role: Accessible.StaticText
    Accessible.name: chips.spokenLabel

    FontMetrics {
        id: metrics
        font.pixelSize: SuiTheme.fontSizeSm
        font.weight: SuiTheme.fontWeightStrong
    }

    function chipWidth(text) {
        return Math.ceil(metrics.advanceWidth(text)) + 2 * SuiTheme.spaceSm
    }

    // The most chips (widest row first) that fit `available`, the "+N" chip included.
    function fitting(available) {
        const count = titles.length
        const floor = Math.min(pinned, count)
        for (let shown = count; shown > floor; --shown) {
            let used = 0
            for (let i = 0; i < shown; ++i)
                used += chipWidth(titles[i]) + (i > 0 ? spacing : 0)
            if (shown < count)
                used += spacing + chipWidth("+" + (count - shown))
            if (used <= available)
                return shown
        }
        return floor
    }

    Row {
        spacing: chips.spacing
        Repeater {
            model: chips.titles.slice(0, chips.shown)
            delegate: SuiChip {
                required property string modelData
                text: modelData
            }
        }
        SuiChip {
            objectName: "studioMoreLabels"
            visible: chips.shown < chips.titles.length
            text: "+" + (chips.titles.length - chips.shown)
        }
    }
}
