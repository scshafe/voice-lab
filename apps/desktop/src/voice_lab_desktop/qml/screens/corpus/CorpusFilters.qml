// The Corpus's filters (CorpusFiltersComponent): the search, the six selects and
// "Clear filters" (shown only while a filter is set). Each change refetches.
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

Flow {
    id: filters
    objectName: "corpusFilters"
    spacing: SuiTheme.spaceSm

    ColumnLayout {
        spacing: 2
        Text {
            textFormat: Text.PlainText
            text: "Search"
            color: SuiTheme.muted
            font.pixelSize: SuiTheme.fontSizeXs
            font.weight: SuiTheme.fontWeightStrong
            Accessible.ignored: true
        }
        SuiSearchField {
            id: search
            objectName: "corpusSearch"
            implicitWidth: 240
            label: "Search"
            placeholderText: "filename, bucket, rationale"
            onTextEdited: corpus.setSearch(text)
            onCleared: corpus.setFilter("q", "")
            Connections {
                target: corpus
                function onFiltersChanged() {
                    if (search.text !== corpus.searchText) search.text = corpus.searchText
                }
            }
        }
    }

    Repeater {
        model: corpus.filterControls
        delegate: SuiSelect {
            required property var modelData
            objectName: "filter-" + modelData.key
            width: 170
            label: modelData.label
            options: modelData.options
            value: modelData.value
            onValueSelected: (value) => corpus.setFilter(modelData.key, value)
        }
    }

    ColumnLayout {
        visible: corpus.anyFilter
        spacing: 2
        Item { implicitHeight: SuiTheme.fontSizeXs + 4; implicitWidth: 1 }
        SuiButton {
            objectName: "clearFilters"
            text: "Clear filters"
            variant: "ghost"
            onClicked: corpus.clearFilters()
        }
    }
}
