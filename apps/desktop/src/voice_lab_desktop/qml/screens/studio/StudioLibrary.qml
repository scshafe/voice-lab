// The prompt studio's library sidebar (NATIVE_RECORDING_UX.md §4, "library filters"): the
// covered count, Continue Missing above the search (it ignores search and filters), the
// search, Show and Labels, the catalog version this run records against, and every prompt
// of it with its status. The model is the studio's (screens/studio.py).
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui
import VoiceLab.StandIn

Rectangle {
    id: library
    objectName: "studioLibrary"

    required property var rec
    readonly property bool menuOpen: labelsPopup.visible || showSelect.popupOpen

    color: SuiTheme.panel

    Rectangle {
        anchors.right: parent.right
        width: 1
        height: parent.height
        color: SuiTheme.line
    }

    component Caption: Text {
        textFormat: Text.PlainText
        color: SuiTheme.muted
        font.pixelSize: SuiTheme.fontSizeXs
        font.weight: SuiTheme.fontWeightStrong
        Accessible.ignored: true
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: SuiTheme.spaceLg
        anchors.rightMargin: SuiTheme.spaceLg + 1
        spacing: SuiTheme.spaceSm

        RowLayout {
            Layout.fillWidth: true
            spacing: SuiTheme.spaceSm
            SuiText { text: "Library"; variant: "title" }
            Item { Layout.fillWidth: true }
            SuiText {
                objectName: "studioCoverage"
                text: library.rec.coverageText
                variant: "number"
                tone: "muted"
                Accessible.name: library.rec.coverageSpoken
            }
        }
        SuiText {
            objectName: "studioCoverageSource"
            Layout.fillWidth: true
            Layout.preferredHeight: 36
            text: library.rec.coverageSource
            variant: "caption"
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
        }
        SuiButton {
            objectName: "studioContinueMissing"
            Layout.fillWidth: true
            Layout.preferredHeight: 36
            text: "Continue Missing"
            variant: "primary"
            enabled: library.rec.canContinueMissing
            Accessible.description: library.rec.continueHelp
            onClicked: library.rec.continueMissing()
        }
        SuiSearchField {
            id: search
            objectName: "studioSearch"
            Layout.fillWidth: true
            label: "Search library prompts"
            placeholderText: "Search prompts"
            onTextEdited: library.rec.setSearch(text)
            onCleared: library.rec.setSearch("")
        }
        SuiSelect {
            id: showSelect
            objectName: "studioShow"
            Layout.fillWidth: true
            label: "Show"
            options: library.rec.showOptions
            value: library.rec.show
            onValueSelected: (value) => library.rec.setShow(value)
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2
            Caption { text: "Labels" }
            SuiButton {
                id: labelsButton
                objectName: "studioLabels"
                Layout.fillWidth: true
                implicitHeight: SuiTheme.controlHeight
                text: library.rec.labelsButton
                Accessible.name: library.rec.labelsTitle
                onClicked: labelsPopup.visible ? labelsPopup.close() : labelsPopup.open()
            }
        }
        // The catalog version this run records against, and the switch the next session
        // makes (or why nothing can be recorded): a line and a notice of up to two lines,
        // always there.
        ColumnLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 58
            spacing: 2
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 20
                spacing: SuiTheme.spaceXs
                SuiText {
                    objectName: "studioCatalog"
                    Layout.fillWidth: true
                    text: library.rec.catalogText
                    variant: "caption"
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
            SuiText {
                objectName: "studioSwitchNotice"
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                verticalAlignment: Text.AlignTop
                text: library.rec.switchText
                variant: "caption"
                tone: library.rec.canSwitch ? "info" : (library.rec.phase === "blocked" ? "warn" : "muted")
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
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

    // Labels: one toggle per label in first-appearance catalog order (combined with OR),
    // and "Show all labels". Catalog-derived titles render verbatim.
    T.Popup {
        id: labelsPopup
        objectName: "studioLabelsPopup"
        parent: labelsButton
        y: labelsButton.height + 2
        width: Math.max(labelsButton.width, 220)
        padding: SuiTheme.spaceXs
        implicitHeight: Math.min(labelsColumn.implicitHeight + 2 * padding, 360)
        closePolicy: T.Popup.CloseOnEscape | T.Popup.CloseOnPressOutsideParent
        background: Rectangle {
            radius: SuiTheme.radiusSm
            color: SuiTheme.popover
            border.width: 1
            border.color: SuiTheme.borderStrong
        }
        contentItem: Flickable {
            clip: true
            contentHeight: labelsColumn.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            ColumnLayout {
                id: labelsColumn
                width: parent.width
                spacing: 0
                Repeater {
                    model: library.rec.labelOptions
                    delegate: SuiCheckbox {
                        required property var modelData
                        objectName: "studioLabel-" + modelData.token
                        Layout.fillWidth: true
                        text: modelData.title
                        checked: modelData.selected
                        onToggled: library.rec.setLabel(modelData.token, checked)
                    }
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: SuiTheme.line }
                SuiButton {
                    objectName: "studioAllLabels"
                    Layout.fillWidth: true
                    text: "Show all labels"
                    variant: "ghost"
                    onClicked: library.rec.showAllLabels()
                }
            }
        }
    }
}
