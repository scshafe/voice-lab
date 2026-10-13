// Record → Prompts as the prompt studio (D-051, D-052; docs/design/RECORDING-V1.md §5, step
// 6a). The model is `app.studioModel` (screens/studio.py); `prompts_mode = "sessions"` in
// config.toml shows phase 4c's PromptsScreen instead.
//
// The Swift app's fixed workspace (docs/NATIVE_RECORDING_UX.md §4): the library on the left;
// on the right, top to bottom, the header (title, label chips, microphone), the script card,
// the take picker, the waveform, the trim row, the timer, the playback row, the 56-point
// Record/Stop footer and the status line. Every region's place and size depend on the
// screen's size alone, never on the transport, the take or the sync: a window shorter than
// the workspace scrolls instead. The take picker, waveform, trim and playback regions are
// held at their final size for step 6b; keys are step 6c's (the model's toggleRecord,
// save and saveNext are what they call).
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import Scshafe.Ui
import VoiceLab.StandIn
import "studio"

Item {
    id: studio
    objectName: "studioScreen"

    readonly property var rec: app.studioModel
    readonly property var takes: rec.takes
    readonly property bool shown: app.screen === "prompts"
    readonly property bool menuOpen: libraryPane.menuOpen || detailsPopup.visible || takePicker.menuOpen

    // Layout tokens: an 8-point scale, 16-point insets, a 280-point library (240 below a
    // 1,000-point screen), a 56-point Record/Stop target.
    readonly property int inset: SuiTheme.spaceLg
    readonly property int gap: SuiTheme.spaceSm
    readonly property int sidebarWidth: width >= 1000 ? 280 : 240
    readonly property int headerHeight: 48
    readonly property int pickerHeight: 24
    readonly property int trimHeight: 24
    readonly property int timerHeight: 24
    readonly property int playbackHeight: 32
    readonly property int footerHeight: 56
    readonly property int statusHeight: 40
    readonly property int scriptBase: 256
    readonly property int waveformBase: 120
    readonly property int scriptFloor: 160
    readonly property int waveformFloor: 64
    readonly property int fixedHeight: headerHeight + pickerHeight + trimHeight + timerHeight
        + playbackHeight + footerHeight + statusHeight + 8 * gap + 2 * inset
    // Height beyond the workspace's base goes 70% to the script and 30% to the waveform.
    // Below the base the prompt comes first: the waveform gives way down to its floor, then
    // the script down to its own; a window shorter still scrolls.
    readonly property real extra: height - fixedHeight - scriptBase - waveformBase
    readonly property real waveformGives: Math.min(Math.max(0, -extra), waveformBase - waveformFloor)
    readonly property real scriptHeight: extra >= 0 ? scriptBase + 0.7 * extra
        : Math.max(scriptFloor, scriptBase + extra + waveformGives)
    readonly property real waveformHeight: extra >= 0 ? waveformBase + 0.3 * extra
                                                      : waveformBase - waveformGives
    readonly property real contentX: sidebarWidth + inset
    readonly property real contentWidth: Math.max(0, width - contentX - inset)

    readonly property real headerY: inset
    readonly property real scriptY: headerY + headerHeight + gap
    readonly property real pickerY: scriptY + scriptHeight + gap
    readonly property real waveformY: pickerY + pickerHeight + gap
    readonly property real trimY: waveformY + waveformHeight + gap
    readonly property real timerY: trimY + trimHeight + gap
    readonly property real playbackY: timerY + timerHeight + gap
    readonly property real footerY: playbackY + playbackHeight + gap
    readonly property real statusY: footerY + footerHeight + gap
    readonly property real workspaceHeight: statusY + statusHeight + inset

    readonly property bool busy: rec.phase === "starting" || rec.phase === "recording"
                                 || rec.phase === "stopping" || rec.phase === "saving"
    readonly property bool live: rec.phase === "starting" || rec.phase === "recording"
    readonly property bool recordingTime: live || rec.phase === "stopping"

    component CompactButton: SuiButton {
        Layout.preferredHeight: 24
        topPadding: 0
        bottomPadding: 0
        variant: "ghost"
    }

    StudioLibrary {
        id: libraryPane
        x: 0
        y: 0
        width: studio.sidebarWidth
        height: studio.height
        rec: studio.rec
    }

    Flickable {
        id: workspace
        objectName: "studioWorkspace"
        x: studio.sidebarWidth
        y: 0
        width: studio.width - studio.sidebarWidth
        height: studio.height
        contentWidth: width
        contentHeight: Math.max(height, studio.workspaceHeight)
        interactive: contentHeight > height
        boundsBehavior: Flickable.StopAtBounds
        clip: true

        // -- header: the prompt's title, its label chips, the microphone ----------------------
        Item {
            objectName: "studioHeader"
            x: studio.inset
            y: studio.headerY
            width: studio.contentWidth
            height: studio.headerHeight
            SuiText {
                objectName: "studioPromptTitle"
                anchors.left: parent.left
                anchors.right: deviceChip.left
                anchors.rightMargin: SuiTheme.spaceMd
                y: 2
                text: studio.rec.promptTitle
                variant: "heading"
                elide: Text.ElideRight
            }
            StudioLabelChips {
                anchors.left: parent.left
                anchors.right: deviceChip.left
                anchors.rightMargin: SuiTheme.spaceMd
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 2
                titles: studio.rec.labelChips
                pinned: studio.rec.pinnedChips
                spokenLabel: studio.rec.labelsSpoken
            }
            SuiChip {
                id: deviceChip
                objectName: "studioDevice"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: 150
                text: studio.rec.deviceOk ? "UMIK-1" : "No microphone"
                tone: studio.rec.deviceOk ? "ok" : "warn"
                dot: true
                Accessible.description: studio.rec.deviceLabel
                // Without a microphone, a click looks again (after plugging the UMIK-1 in).
                TapHandler {
                    enabled: !studio.rec.deviceOk
                    onTapped: studio.rec.checkDevice()
                }
            }
        }

        StudioScriptCard {
            x: studio.inset
            y: studio.scriptY
            width: studio.contentWidth
            height: studio.scriptHeight
            rec: studio.rec
        }

        // -- the take picker, the waveform, the trim row (step 6b) ---------------------------
        StudioTakePicker {
            id: takePicker
            objectName: "studioTakePicker"
            x: studio.inset
            y: studio.pickerY
            width: studio.contentWidth
            height: studio.pickerHeight
            takes: studio.takes
            busy: studio.busy
        }
        Rectangle {
            objectName: "studioWaveform"
            x: studio.inset
            y: studio.waveformY
            width: studio.contentWidth
            height: studio.waveformHeight
            radius: SuiTheme.radiusMd
            color: SuiTheme.tint
            border.width: 1
            border.color: studio.live ? SuiTheme.alpha(SuiTheme.toneRed, 0.6) : SuiTheme.line
            clip: true
            StudioWaveform {
                id: waveform
                objectName: "studioWave"
                anchors.fill: parent
                anchors.margins: 1
                visible: studio.rec.problemText === ""
                bins: studio.takes.bins
                slots: studio.takes.slots
                viewStart: studio.takes.viewStart
                viewEnd: studio.takes.viewEnd
                frameCount: studio.takes.frameCount
                trimStart: studio.takes.trimStart
                trimEnd: studio.takes.trimEnd
                playhead: studio.takes.playhead
                live: studio.takes.live
                editable: studio.takes.canEditTrim
                note: studio.takes.waveNote
                onSeekRequested: (frame) => studio.takes.seekTo(frame)
                onTrimRequested: (start, end) => studio.takes.setTrimRange(start, end)
                onPlayRequested: studio.takes.togglePlay()
            }
            // What went wrong with the take, whole, in the take's place.
            Flickable {
                objectName: "studioProblemFlick"
                anchors.fill: parent
                anchors.margins: SuiTheme.spaceSm
                visible: studio.rec.problemText !== ""
                clip: true
                contentHeight: Math.max(height, problem.implicitHeight)
                interactive: problem.implicitHeight > height
                boundsBehavior: Flickable.StopAtBounds
                SuiText {
                    id: problem
                    objectName: "studioProblem"
                    width: parent.width
                    height: Math.max(implicitHeight, parent.height)
                    verticalAlignment: Text.AlignVCenter
                    horizontalAlignment: Text.AlignHCenter
                    text: studio.rec.problemText
                    tone: studio.rec.statusTone === "neutral" ? "default" : studio.rec.statusTone
                    wrapMode: Text.Wrap
                }
            }
        }
        // In and Out from the playhead, Save trim, Reset. The take under review keeps its edit
        // until Save; a take recorded elsewhere shows its trim.
        Item {
            objectName: "studioTrimRow"
            x: studio.inset
            y: studio.trimY
            width: studio.contentWidth
            height: studio.trimHeight
            RowLayout {
                anchors.fill: parent
                spacing: SuiTheme.spaceXs
                SuiText { text: "In"; variant: "caption" }
                SuiText {
                    objectName: "studioTrimIn"
                    text: studio.takes.inText
                    variant: "mono"
                }
                CompactButton {
                    objectName: "studioSetIn"
                    text: "Set In"
                    enabled: studio.takes.canEditTrim
                    onClicked: studio.takes.setIn()
                }
                Item { Layout.preferredWidth: SuiTheme.spaceSm }
                SuiText { text: "Out"; variant: "caption" }
                SuiText {
                    objectName: "studioTrimOut"
                    text: studio.takes.outText
                    variant: "mono"
                }
                CompactButton {
                    objectName: "studioSetOut"
                    text: "Set Out"
                    enabled: studio.takes.canEditTrim
                    onClicked: studio.takes.setOut()
                }
                Item { Layout.fillWidth: true }
                SuiText {
                    objectName: "studioTrimEdited"
                    text: "edited"
                    variant: "caption"
                    tone: "warn"
                    visible: studio.takes.trimDirty
                }
                CompactButton {
                    objectName: "studioSaveTrim"
                    text: "Save trim"
                    variant: "secondary"
                    enabled: studio.takes.canSaveTrim
                    onClicked: studio.takes.saveTrim()
                }
                CompactButton {
                    objectName: "studioResetTrim"
                    text: "Reset"
                    enabled: studio.takes.canResetTrim
                    onClicked: studio.takes.resetTrim()
                }
            }
        }

        // -- the timer: the take's confirmed frames while recording (D-014), else the playhead
        Item {
            objectName: "studioTimer"
            x: studio.inset
            y: studio.timerY
            width: studio.contentWidth
            height: studio.timerHeight
            Text {
                objectName: "studioElapsed"
                anchors.centerIn: parent
                textFormat: Text.PlainText
                text: studio.recordingTime || studio.takes.timeText === ""
                      ? studio.rec.elapsedText : studio.takes.timeText
                color: studio.rec.phase === "recording" ? SuiTheme.toneText("danger") : SuiTheme.text
                font.family: SuiTheme.monoFamily
                font.pixelSize: SuiTheme.fontSizeLg
                font.features: { "tnum": 1 }
                Accessible.name: (studio.recordingTime ? "Elapsed " : "Position ") + text
            }
            SuiLevelMeter {
                objectName: "studioLevel"
                anchors.right: levelText.left
                anchors.rightMargin: SuiTheme.spaceXs
                anchors.verticalCenter: parent.verticalCenter
                width: 96
                visible: studio.takes.live && studio.takes.levelText !== ""
                peak: studio.takes.levelPeak
                rms: studio.takes.levelPeak
                label: "Recording level"
                text: studio.takes.levelText
            }
            SuiText {
                id: levelText
                objectName: "studioLevelText"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                visible: studio.takes.live
                text: studio.takes.levelText
                variant: "caption"
            }
        }

        // -- playback: Zoom and Fit; −0.1 s, Play/Pause, +0.1 s ------------------------------
        Item {
            objectName: "studioPlaybackRow"
            x: studio.inset
            y: studio.playbackY
            width: studio.contentWidth
            height: studio.playbackHeight
            RowLayout {
                anchors.fill: parent
                spacing: SuiTheme.spaceXs
                SuiButton {
                    objectName: "studioZoomOut"
                    Layout.preferredWidth: 36
                    text: "−"
                    Accessible.name: "Zoom out"
                    enabled: studio.takes.canZoomOut
                    onClicked: studio.takes.zoomOut()
                }
                SuiButton {
                    objectName: "studioZoomIn"
                    Layout.preferredWidth: 36
                    text: "+"
                    Accessible.name: "Zoom in"
                    enabled: studio.takes.canZoomIn
                    onClicked: studio.takes.zoomIn()
                }
                SuiButton {
                    objectName: "studioFit"
                    Layout.preferredWidth: 48
                    text: "Fit"
                    enabled: studio.takes.canZoomOut
                    onClicked: studio.takes.fit()
                }
                Item { Layout.fillWidth: true }
                SuiButton {
                    objectName: "studioStepBack"
                    Layout.preferredWidth: 64
                    text: "−0.1 s"
                    Accessible.name: "Back 0.1 seconds"
                    enabled: studio.takes.canPlay
                    onClicked: studio.takes.stepBack()
                }
                SuiButton {
                    objectName: "studioPlay"
                    Layout.preferredWidth: 72
                    text: studio.takes.playing ? "Pause" : "Play"
                    variant: "secondary"
                    enabled: studio.takes.canPlay
                    onClicked: studio.takes.togglePlay()
                }
                SuiButton {
                    objectName: "studioStepForward"
                    Layout.preferredWidth: 64
                    text: "+0.1 s"
                    Accessible.name: "Forward 0.1 seconds"
                    enabled: studio.takes.canPlay
                    onClicked: studio.takes.stepForward()
                }
                Item { Layout.fillWidth: true }
                // As wide as the zoom buttons, so the transport sits in the middle when there
                // is room (it gives way first on a narrow window).
                Item { Layout.preferredWidth: 128; Layout.minimumWidth: 0 }
            }
        }

        // -- the footer: Record/Stop, Retake, Discard, Save, Save & Next --------------------------
        Item {
            objectName: "studioFooter"
            x: studio.inset
            y: studio.footerY
            width: studio.contentWidth
            height: studio.footerHeight
            StudioRecordButton {
                id: recordButton
                objectName: "studioRecord"
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                stopping: studio.live || studio.rec.phase === "stopping"
                enabled: studio.rec.canRecord || studio.rec.canStop
                onClicked: studio.rec.toggleRecord()
            }
            SuiText {
                objectName: "studioRecordLabel"
                anchors.left: recordButton.right
                anchors.leftMargin: SuiTheme.spaceMd
                anchors.right: actions.left
                anchors.rightMargin: SuiTheme.spaceSm
                anchors.verticalCenter: parent.verticalCenter
                text: studio.rec.phase === "recording" ? "Stop"
                    : studio.rec.phase === "starting" ? "Starting"
                    : studio.rec.phase === "stopping" ? "Stopping"
                    : studio.rec.phase === "saving" ? "Saving" : "Record"
                variant: "subheading"
                elide: Text.ElideRight
            }
            Row {
                id: actions
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: SuiTheme.spaceSm
                SuiButton {
                    objectName: "studioRetake"
                    width: 84
                    text: "Retake"
                    enabled: studio.rec.canRetake
                    onClicked: studio.rec.retake()
                }
                SuiButton {
                    objectName: "studioDiscard"
                    width: 84
                    text: "Discard"
                    variant: "ghost"
                    enabled: studio.rec.canDiscard
                    onClicked: studio.rec.discard()
                }
                SuiButton {
                    objectName: "studioSave"
                    width: 72
                    text: "Save"
                    enabled: studio.rec.canSave
                    onClicked: studio.rec.save()
                }
                SuiButton {
                    objectName: "studioSaveNext"
                    width: 112
                    text: "Save & Next"
                    variant: "primary"
                    enabled: studio.rec.canSaveNext
                    onClicked: studio.rec.saveNext()
                }
            }
        }

        // -- the status line: the take on the left, the sync (or a notice) on the right ----------
        // Each half wraps to two lines, so the cause of a failure or a refusal stays in view.
        Item {
            objectName: "studioStatus"
            x: studio.inset
            y: studio.statusY
            width: studio.contentWidth
            height: studio.statusHeight
            SuiText {
                objectName: "studioStatusText"
                anchors.left: parent.left
                anchors.top: parent.top
                width: Math.floor(parent.width * 0.45)
                height: parent.height
                text: studio.rec.statusText
                tone: studio.rec.statusTone === "neutral" ? "default" : studio.rec.statusTone
                variant: "caption"
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            SuiText {
                objectName: "studioSyncText"
                anchors.right: details.visible ? details.left : parent.right
                anchors.rightMargin: details.visible ? SuiTheme.spaceXs : 0
                anchors.top: parent.top
                width: Math.floor(parent.width * 0.55) - SuiTheme.spaceSm
                    - (details.visible ? details.width + SuiTheme.spaceXs : 0)
                height: parent.height
                horizontalAlignment: Text.AlignRight
                verticalAlignment: Text.AlignVCenter
                text: studio.rec.syncText
                tone: studio.rec.syncTone === "neutral" ? "muted" : studio.rec.syncTone
                variant: "caption"
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }
            SuiButton {
                id: details
                objectName: "studioDetails"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: "Details"
                variant: "ghost"
                visible: studio.rec.syncDetail !== ""
                onClicked: detailsPopup.open()
            }
            // The sync half's whole message (and, for a notice, Dismiss).
            T.Popup {
                id: detailsPopup
                objectName: "studioDetailsPopup"
                x: 0
                y: -height - SuiTheme.spaceXs
                width: parent.width
                padding: SuiTheme.spaceMd
                closePolicy: T.Popup.CloseOnEscape | T.Popup.CloseOnPressOutside
                background: Rectangle {
                    radius: SuiTheme.radiusMd
                    color: SuiTheme.popover
                    border.width: 1
                    border.color: SuiTheme.borderStrong
                }
                contentItem: ColumnLayout {
                    spacing: SuiTheme.spaceSm
                    SuiText {
                        objectName: "studioDetailsText"
                        Layout.fillWidth: true
                        text: studio.rec.syncDetail
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.alignment: Qt.AlignRight
                        spacing: SuiTheme.spaceSm
                        SuiButton {
                            objectName: "studioDismissNotice"
                            text: "Dismiss"
                            visible: studio.rec.noticeId !== ""
                            onClicked: {
                                detailsPopup.close()
                                studio.rec.dismissNotice()
                            }
                        }
                        SuiButton {
                            objectName: "studioCloseDetails"
                            text: "Close"
                            variant: "primary"
                            onClicked: detailsPopup.close()
                        }
                    }
                }
            }
        }
    }
}
