// The prompt studio's library sidebar (NATIVE_RECORDING_UX.md §4, "library filters"): the
// covered count (the catalog and where the count comes from behind its ⓘ, D-059), Continue
// Missing above the search (it ignores search and filters), the search with its filters (Show
// and Labels behind the funnel), the switch the next session makes, and every prompt of the
// catalog with its status. The model is the studio's (screens/studio.py).
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui
import VoiceLab.Controls

Rectangle {
    id: library
    objectName: "studioLibrary"

    required property var rec
    // The filter menu holds the focus while it is open; an ⓘ's popover never takes it.
    readonly property bool menuOpen: {
        for (let item = library.Window.activeFocusItem; item !== null; item = item.parent) {
            if (item === T.Overlay.overlay)
                return true
        }
        return false
    }

    color: SuiTheme.panel

    Rectangle {
        anchors.right: parent.right
        width: 1
        height: parent.height
        color: SuiTheme.line
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: SuiTheme.spaceLg
        anchors.rightMargin: SuiTheme.spaceLg + 1
        spacing: SuiTheme.spaceSm

        RowLayout {
            Layout.fillWidth: true
            spacing: SuiTheme.spaceXs
            SuiText { text: "Library"; variant: "title" }
            Item { Layout.fillWidth: true }
            // Warn: the count is this machine's takes only, or the PC is rebuilding its library.
            SuiText {
                objectName: "studioCoverage"
                text: library.rec.coverageText
                tabular: true
                tone: library.rec.coveragePartial ? "warn" : "muted"
                Accessible.name: library.rec.coverageSpoken
            }
            InfoTip {
                objectName: "studioLibraryInfo"
                title: "Library"
                text: [library.rec.catalogText, library.rec.coverageSource].filter(line => line !== "").join("\n")
            }
        }
        StudioKeyButton {
            objectName: "studioContinueMissing"
            Layout.fillWidth: true
            Layout.preferredHeight: 36
            text: "Continue Missing"
            key: library.rec.keys.continueMissing
            variant: "primary"
            enabled: library.rec.canContinueMissing
            Accessible.description: library.rec.continueHelp
            onClicked: library.rec.continueMissing()
        }
        // The search, and Show and Labels behind the funnel: an exclusive Show (none chosen is
        // all prompts) and the labels (OR among themselves); Clear resets both, not the search.
        SuiListHeader {
            id: header
            objectName: "studioLibraryHeader"
            Layout.fillWidth: true
            label: "Search and filter the library"
            searchLabel: "Search library prompts"
            placeholderText: "Search prompts"
            searchField.objectName: "studioSearch"
            filterLabel: "Filter prompts"
            filters: library.rec.filters
            activeFilters: library.rec.activeFilters
            onTextEdited: library.rec.setSearch(text)
            onCleared: library.rec.setSearch("")
            // The header sets its activeFilters as you choose: the model's, bound again after.
            onFilterToggled: library.applyFilters()
            onFiltersCleared: library.applyFilters()
            // The search the model holds (the last run's, D-058 item 7) shows in the field.
            Component.onCompleted: text = library.rec.searchText
            Connections {
                target: library.rec
                function onLibraryChanged() {
                    if (header.text !== library.rec.searchText)
                        header.text = library.rec.searchText
                }
            }
        }
        // Only while it has something to say: the switch the next session makes, or why
        // nothing can be recorded yet.
        ColumnLayout {
            objectName: "studioSwitchRow"
            Layout.fillWidth: true
            visible: library.rec.switchText !== ""
            spacing: 2
            SuiText {
                objectName: "studioSwitchNotice"
                Layout.fillWidth: true
                text: library.rec.switchText
                variant: "caption"
                tone: library.rec.canSwitch ? "info" : (library.rec.phase === "blocked" ? "warn" : "muted")
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }
            SuiButton {
                objectName: "studioSwitch"
                Layout.preferredHeight: 20
                topPadding: 0
                bottomPadding: 0
                text: "Switch now"
                variant: "ghost"
                visible: library.rec.canSwitch
                enabled: library.rec.phase === "ready"
                onClicked: library.rec.switchCatalog()
            }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: SuiTheme.line }

        ColumnLayout {
            objectName: "studioCurrentOutside"
            Layout.fillWidth: true
            visible: library.rec.currentOutside
            spacing: 2
            SuiText { text: "Current prompt · outside filter"; variant: "caption" }
            StudioPromptRow {
                Layout.fillWidth: true
                rec: library.rec
                promptId: library.rec.currentRow.promptId || ""
                title: library.rec.currentRow.title || ""
                statusTitle: library.rec.currentRow.statusTitle || ""
                kind: library.rec.currentRow.kind || "spoken"
                flag: library.rec.currentRow.flag || ""
                current: true
            }
        }

        ListView {
            id: list
            objectName: "studioLibraryList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 2
            boundsBehavior: Flickable.StopAtBounds
            model: library.rec.rowsModel
            // Every row is made when it scrolls into view, never ahead of it: a filter that
            // shrinks the list then has no row half-made beyond its end.
            cacheBuffer: 0
            T.ScrollBar.vertical: T.ScrollBar {
                width: 8
                visible: list.contentHeight > list.height
                contentItem: Rectangle { radius: 3; color: SuiTheme.borderStrong }
            }
            delegate: StudioPromptRow {
                width: ListView.view.width - 10
                rec: library.rec
                objectName: "studioRow-" + promptId
            }
            SuiText {
                objectName: "studioLibraryEmpty"
                anchors.centerIn: parent
                width: parent.width - 2 * SuiTheme.spaceLg
                horizontalAlignment: Text.AlignHCenter
                visible: library.rec.emptyText !== ""
                text: library.rec.emptyText
                variant: "caption"
                wrapMode: Text.Wrap
            }
        }
    }

    function applyFilters() {
        library.rec.setFilters(header.activeFilters)
        header.activeFilters = Qt.binding(() => library.rec.activeFilters)
    }
}
