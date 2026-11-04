// Analysis → Journey (design §6.1; checklist J-01 to J-22): the stat band, the subject
// tabs and their charts, the education card, and the take inspector. Every number and
// chart spec comes from the `journey` model (screens/journey.py, journey_charts.py).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.Controls
import VoiceLab.StandIn
import "journey"

ScreenPage {
    id: page
    objectName: "journeyScreen"
    title: "The Voice Journey"
    tagline: "Seven years of practice, measured: range, sustain, timbre, vibrato, and the same-song verdict."

    readonly property bool ready: app.signedIn && journey.state === "loaded" && journey.available

    NeedsServer { what: "the journey"; visible: !app.signedIn }

    SuiText {
        objectName: "journeyLoading"
        visible: app.signedIn && (journey.state === "loading" || journey.state === "idle")
        text: "Loading trends…"
        variant: "caption"
    }
    SuiBanner {
        objectName: "journeyError"
        Layout.fillWidth: true
        visible: app.signedIn && journey.state === "failed"
        tone: "danger"
        title: "The trends could not be loaded"
        text: journey.error
    }
    SuiBanner {
        objectName: "journeyNotice"
        Layout.fillWidth: true
        visible: app.signedIn && journey.state === "loaded" && !journey.available
        tone: "info"
        title: "No voice trends yet"
        InfoTip {
            objectName: "journeyNoticeInfo"
            title: "No voice trends yet"
            text: journey.notice
        }
    }

    // ---- the stat band and the toggle (J-02, J-03) -----------------------------------------
    Flow {
        objectName: "statBand"
        Layout.fillWidth: true
        visible: page.ready
        spacing: SuiTheme.spaceSm
        Repeater {
            model: journey.statTiles
            delegate: Rectangle {
                required property var modelData
                objectName: "statTile"
                width: 168
                height: tileColumn.implicitHeight + 2 * SuiTheme.spaceMd
                radius: SuiTheme.radiusMd
                color: SuiTheme.panel
                border.color: SuiTheme.line
                ColumnLayout {
                    id: tileColumn
                    anchors.fill: parent
                    anchors.margins: SuiTheme.spaceMd
                    spacing: 2
                    SuiText { objectName: "statValue"; text: modelData.value; variant: "title" }
                    SuiText {
                        objectName: "statLabel"
                        Layout.fillWidth: true
                        text: modelData.label
                        variant: "caption"
                        wrapMode: Text.Wrap
                    }
                }
            }
        }
    }
    RowLayout {
        visible: page.ready
        spacing: SuiTheme.spaceXs
        SuiCheckBox {
            objectName: "includeFlagged"
            text: "include the flagged low-reliability takes"
            checked: journey.includeFlagged
            onToggled: journey.setIncludeFlagged(checked)
        }
        InfoTip {
            objectName: "includeFlaggedInfo"
            title: "Flagged takes"
            text: "Included, they show hollow in the scatters."
        }
    }

    // ---- the subject tabs (J-04) --------------------------------------------------------------
    Flow {
        objectName: "subjectTabs"
        Layout.fillWidth: true
        visible: page.ready
        spacing: SuiTheme.spaceXs
        Repeater {
            model: journey.tabs
            delegate: SuiButton {
                required property var modelData
                objectName: "tab-" + modelData.key
                text: modelData.label
                variant: modelData.key === journey.subject ? "primary" : "ghost"
                Accessible.role: Accessible.PageTab
                onClicked: journey.selectSubject(modelData.key)
            }
        }
    }

    // ---- The Verdict (J-05) --------------------------------------------------------------------
    Card {
        objectName: "verdictCard"
        visible: page.ready && journey.subject === "verdict"
        heading: "The Verdict — same-song controlled"
        info: "Slopes per year measured inside recurring songs (Theil–Sen per song, bootstrap 95% CI across songs). The strictest lens this archive allows: you, compared only to yourself, on the same material."
        Repeater {
            model: journey.verdictRows
            delegate: RowLayout {
                required property var modelData
                objectName: "verdictRow"
                Layout.fillWidth: true
                spacing: SuiTheme.spaceMd
                SuiText { Layout.fillWidth: true; text: modelData.label }
                SuiText { objectName: "verdictSlope"; text: modelData.slope; tabular: true }
                SuiText { objectName: "verdictCi"; text: modelData.ci; tabular: true }
                SuiText { text: modelData.songs; variant: "caption" }
                SuiChip { objectName: "verdictChip"; text: modelData.verdict; tone: modelData.tone }
            }
        }
        // The counts show; how to read them is behind the ⓘ (D-059).
        RowLayout {
            Layout.fillWidth: true
            spacing: SuiTheme.spaceXs
            SuiText {
                objectName: "verdictSummary"
                Layout.fillWidth: true
                text: journey.verdictLine
                variant: "caption"
                wrapMode: Text.Wrap
            }
            InfoTip {
                objectName: "verdictReadingInfo"
                visible: text !== ""
                title: "Reading the verdict"
                text: journey.verdictReading
            }
        }
    }

    // ---- The Range River (J-06 to J-10) --------------------------------------------------------
    Card {
        objectName: "rangeCard"
        visible: page.ready && journey.subject === "range"
        heading: "The Range River"
        // The headline shows once there is one; the description is the ⓘ's (D-059).
        subheading: journey.rangeHeadline !== journey.rangeAbout ? journey.rangeHeadline : ""
        info: journey.rangeAbout
        SuiChart {
            objectName: "rangeChart"
            Layout.fillWidth: true
            spec: page.ready && journey.subject === "range" ? journey.rangeSpec : null
        }
        Flow {
            objectName: "rangeLegend"
            Layout.fillWidth: true
            spacing: SuiTheme.spaceMd
            Repeater {
                model: journey.rangeLegend
                delegate: Swatch {
                    required property var modelData
                    objectName: "rangeLegendItem"
                    text: modelData.name
                    tone: modelData.tone
                    fill: modelData.opacity
                }
            }
            // The percentiles and the register strip's bounds (D-059).
            InfoTip {
                objectName: "rangeLegendInfo"
                title: "The legend"
                text: journey.rangeLegend.filter((item) => item.detail !== item.name)
                                         .map((item) => item.detail).join("\n")
            }
        }
        DataTable {
            objectName: "rangeTable"
            model: journey.rangeTable
            columns: journey.rangeColumns
        }
    }

    // ---- Cause & Effect (J-11 to J-13) ---------------------------------------------------------
    Card {
        objectName: "practiceCard"
        visible: page.ready && journey.subject === "practice"
        heading: "Cause & Effect"
        info: "Practice cadence above; what it buys below. Shaded bands are your high-practice eras, derived from the cadence itself; the panels share one time axis, so alignment does the arguing."
        SuiChart {
            objectName: "causeChart"
            Layout.fillWidth: true
            spec: page.ready && journey.subject === "practice" ? journey.causeSpec : null
        }
        Flow {
            objectName: "eraButtons"
            Layout.fillWidth: true
            spacing: SuiTheme.spaceXs
            Repeater {
                model: journey.eras
                delegate: SuiButton {
                    required property var modelData
                    objectName: "era-" + modelData.index
                    text: modelData.text
                    enabled: modelData.enabled
                    variant: "secondary"
                    Accessible.description: modelData.tooltip
                    onClicked: journey.playEra(modelData.index)
                }
            }
            // Why the eras cannot play, while they cannot (D-059).
            InfoTip {
                objectName: "eraInfo"
                readonly property var off: journey.eras.filter((era) => !era.enabled)
                visible: off.length > 0
                title: "Hearing an era"
                text: off.length > 0 ? off[0].tooltip : ""
            }
        }
        DataTable {
            objectName: "causeTable"
            model: journey.causeTable
            columns: journey.causeColumns
        }
    }

    // ---- The Vibrato Story (J-14 to J-16) ------------------------------------------------------
    Card {
        objectName: "vibratoCard"
        visible: page.ready && journey.subject === "vibrato"
        heading: "The Vibrato Story"
        info: "Median pitch-modulation per quarter with interquartile bands. The blue zone marks settled classical vibrato (5–6 Hz) — the trainable gap."
        SuiChart {
            objectName: "vibratoChart"
            Layout.fillWidth: true
            spec: page.ready && journey.subject === "vibrato" ? journey.vibratoSpec : null
        }
        RowLayout {
            spacing: SuiTheme.spaceXs
            SuiText { text: "The fingerprint — every take, walking home"; variant: "subheading" }
            InfoTip {
                objectName: "fingerprintInfo"
                title: "The fingerprint"
                text: "One dot per take: extent across, rate up, brighter = more recent. Click a dot to inspect that take."
            }
        }
        SuiChart {
            objectName: "fingerprintChart"
            Layout.fillWidth: true
            Layout.maximumWidth: 640
            spec: page.ready && journey.subject === "vibrato" ? journey.fingerprintSpec : null
            onActivated: (id) => { journey.openTake(id); inspector.open() }
        }
        Flow {
            objectName: "yearLegend"
            Layout.fillWidth: true
            spacing: SuiTheme.spaceMd
            Repeater {
                model: journey.yearLegend
                delegate: Swatch {
                    required property var modelData
                    objectName: "yearLegendItem"
                    text: modelData.year
                    tone: modelData.tone
                    shade: modelData.shade
                }
            }
        }
    }

    // ---- What Not To Trust (J-18) --------------------------------------------------------------
    Card {
        objectName: "trustCard"
        visible: page.ready && journey.subject === "trust"
        heading: "What Not To Trust"
        info: "Two measures of \"voice quality\" that disagree — on purpose. CPPS is robust to recording conditions and tracks your practice arc; HNR falls monotonically because your microphones and rooms changed across seven years, not your voice."
        GridLayout {
            Layout.fillWidth: true
            columns: page.width > 1100 ? 3 : 1
            columnSpacing: SuiTheme.spaceLg
            Repeater {
                model: journey.trustPanels
                delegate: SuiChart {
                    required property var modelData
                    objectName: "trustChart"
                    Layout.fillWidth: true
                    spec: page.ready && journey.subject === "trust" ? modelData.spec : null
                }
            }
        }
        Rectangle {
            objectName: "tuningTile"
            Layout.fillWidth: true
            implicitHeight: tuningText.implicitHeight + 2 * SuiTheme.spaceMd
            radius: SuiTheme.radiusMd
            color: SuiTheme.tint
            // The title and the reading show; the method, or where it comes from, is the ⓘ's.
            RowLayout {
                anchors.fill: parent
                anchors.margins: SuiTheme.spaceMd
                spacing: SuiTheme.spaceXs
                SuiText {
                    id: tuningText
                    objectName: "tuningTileText"
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    text: [journey.tuningTile.title, journey.tuningTile.line]
                          .filter((part) => !!part).join(" ")
                    wrapMode: Text.Wrap
                }
                InfoTip {
                    objectName: "tuningTileInfo"
                    Layout.alignment: Qt.AlignTop
                    visible: !!journey.tuningTile.more
                    title: "Tuning"
                    text: journey.tuningTile.more || ""
                }
            }
        }
    }

    // ---- Song Scorecards (J-19) ----------------------------------------------------------------
    Card {
        objectName: "songsCard"
        visible: page.ready && journey.subject === "songs"
        heading: "Song Scorecards"
        info: "The songs with enough history to judge (≥6 reliable takes across ≥1.5 years), ranked by takes. Slopes are per year within that song; the sparkline is note-core tuning error by year (down is better)."
        Repeater {
            model: journey.scorecards
            delegate: RowLayout {
                required property var modelData
                objectName: "scorecard"
                Layout.fillWidth: true
                spacing: SuiTheme.spaceMd
                ColumnLayout {
                    Layout.preferredWidth: 200
                    spacing: 0
                    SuiButton {
                        objectName: "songName"
                        text: modelData.name
                        variant: "ghost"
                        Accessible.role: Accessible.Link
                        onClicked: journey.openCluster(modelData.clusterId)
                    }
                    SuiText { text: modelData.clusterId; variant: "caption" }
                }
                ColumnLayout {
                    Layout.preferredWidth: 100
                    spacing: 0
                    SuiText { objectName: "songTakes"; text: modelData.takes; tabular: true }
                    SuiText { text: modelData.years; variant: "caption" }
                }
                SuiChart {
                    objectName: "sparkline"
                    Layout.preferredWidth: 130
                    visible: !!modelData.spark
                    spec: page.ready && journey.subject === "songs" ? modelData.spark : null
                }
                SuiText {
                    objectName: "songTuning"
                    text: "tuning " + modelData.tuning.text
                    tone: modelData.tuning.tone
                }
                SuiText {
                    objectName: "songClarity"
                    text: "clarity " + modelData.clarity.text
                    tone: modelData.clarity.tone
                }
                Item { Layout.fillWidth: true }
            }
        }
    }

    // ---- Repertoire Threads (J-20) -------------------------------------------------------------
    Card {
        objectName: "threadsCard"
        visible: page.ready && journey.subject === "threads"
        heading: "Repertoire Threads"
        info: "Your eight biggest recurring songs across the years — dot area is takes that year. Click a thread to open its takes in the Corpus table. Labels are privacy-generic; no lyric text exists in any manifest."
        SuiChart {
            objectName: "threadsChart"
            Layout.fillWidth: true
            spec: page.ready && journey.subject === "threads" ? journey.threadsSpec : null
            onActivated: (id) => journey.openCluster(id)
        }
    }

    // ---- the education card (J-21): too long for a popover, so a disclosure, closed until you
    // open it (D-059) ---------------------------------------------------------------------------
    Card {
        id: educationCard
        objectName: "educationCard"
        property bool expanded: false
        visible: page.ready && !!journey.education
        RowLayout {
            Layout.fillWidth: true
            spacing: SuiTheme.spaceXs
            SuiText {
                objectName: "educationTitle"
                Layout.fillWidth: true
                text: journey.education ? journey.education.title : ""
                variant: "heading"
            }
            SuiButton {
                objectName: "educationDisclosure"
                text: educationCard.expanded ? "Hide" : "Show"
                variant: "ghost"
                Accessible.name: (educationCard.expanded ? "Hide " : "Show ")
                                 + (journey.education ? journey.education.title : "")
                Accessible.role: Accessible.Button
                onClicked: educationCard.expanded = !educationCard.expanded
            }
        }
        SuiText {
            objectName: "educationIntro"
            Layout.fillWidth: true
            visible: educationCard.expanded && text !== ""
            text: journey.education ? journey.education.intro : ""
            variant: "caption"
            wrapMode: Text.Wrap
        }
        Repeater {
            model: educationCard.expanded && journey.education ? journey.education.sections : []
            delegate: ColumnLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: SuiTheme.spaceXs
                SuiText { objectName: "educationHeading"; text: modelData.heading; variant: "subheading" }
                // The one StyledText in the app: static text written in education.py (its
                // <b>/<i> emphasis), never data from the server.
                Text {
                    objectName: "educationText"
                    Layout.fillWidth: true
                    textFormat: Text.StyledText
                    text: modelData.text
                    wrapMode: Text.Wrap
                    color: SuiTheme.text
                    font.pixelSize: SuiTheme.fontSizeMd
                    lineHeight: 1.15
                }
            }
        }
    }

    // ---- the take inspector (J-17) -------------------------------------------------------------
    SuiDialog {
        id: inspector
        objectName: "takeInspector"
        title: journey.inspectorTitle
        width: Math.min(720, parent ? parent.width - 2 * SuiTheme.spaceXl : 720)
        onClosed: journey.closeTake()
        ColumnLayout {
            spacing: SuiTheme.spaceSm
            SuiText {
                objectName: "inspectorLoading"
                visible: journey.inspectorState === "loading"
                text: "Loading…"
                variant: "caption"
            }
            SuiText {
                objectName: "inspectorError"
                Layout.fillWidth: true
                visible: journey.inspectorState === "failed"
                text: journey.inspectorError
                variant: "caption"
                wrapMode: Text.Wrap
            }
            GridLayout {
                objectName: "inspectorTiles"
                visible: journey.inspectorState === "loaded"
                columns: 3
                columnSpacing: SuiTheme.spaceLg
                rowSpacing: SuiTheme.spaceXs
                Repeater {
                    model: journey.inspectorTiles
                    delegate: ColumnLayout {
                        required property var modelData
                        spacing: 0
                        SuiText { text: modelData.label; variant: "caption" }
                        SuiText { objectName: "inspectorTileValue"; text: modelData.value; variant: "subheading" }
                    }
                }
            }
            SuiChart {
                objectName: "contourChart"
                Layout.fillWidth: true
                Layout.preferredWidth: 640
                visible: journey.inspectorState === "loaded" && !!journey.inspectorContour
                spec: journey.inspectorContour
            }
            PlayerBar {
                objectName: "inspectorPlayer"
                Layout.fillWidth: true
                visible: journey.inspectorState === "loaded" && journey.playbackEnabled
                key: "inspector:" + journey.inspectorId
                source: journey.inspectorId
                available: journey.playbackEnabled
            }
        }
        actions: [
            SuiButton { objectName: "closeInspector"; text: "Close"; onClicked: inspector.close() }
        ]
    }
}
