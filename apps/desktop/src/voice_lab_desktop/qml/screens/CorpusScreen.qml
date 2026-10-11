// Corpus (design §6.1; checklist C, A-04, A-06): voice-journey's Corpus view. The
// filters, the virtualised table of every take (paged from the server as it scrolls),
// its status line and the row detail. All state lives in `corpus` (screens/corpus.py).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn
import "corpus"

Item {
    id: screen
    objectName: "corpusScreen"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: SuiTheme.spaceXl
        spacing: SuiTheme.spaceMd

        ScreenHeader {
            title: "Voice Journey Corpus Browser"
            tagline: "Spot-check workbench over committed manifests. Playback is release-gated and per-click only."
        }

        NeedsServer { what: "the corpus"; visible: !app.signedIn }

        CorpusFilters {
            Layout.fillWidth: true
            visible: app.signedIn
        }

        SuiBanner {
            objectName: "corpusError"
            Layout.fillWidth: true
            visible: app.signedIn && corpus.error !== ""
            tone: "danger"
            title: "The corpus did not load"
            text: corpus.error
            SuiButton { text: "Retry"; variant: "secondary"; onClicked: corpus.refresh() }
        }

        SuiTable {
            id: table
            objectName: "corpusTable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: app.signedIn
            label: "Corpus takes"
            model: corpus.rowsModel
            columns: corpus.columns
            pinnedColumns: 2
            sortKey: corpus.sortKey
            sortDescending: corpus.sortDescending
            onSortRequested: (key) => corpus.sortBy(key)
            onCellActivated: (index, key) => corpus.cellActivated(index, key)
            onActionTriggered: (index, action) => corpus.actionTriggered(index, action)
            onActivated: (index) => corpus.activateRow(index)
            onEndReached: corpus.loadMore()
        }

        SuiText {
            objectName: "corpusStatusLine"
            visible: app.signedIn
            text: corpus.loading && corpus.totalFiltered >= 0 ? "Loading…" : corpus.statusLine
            variant: "caption"
        }
    }

    RowDetailSheet {
        id: detail
        objectName: "rowDetail"
    }
}
