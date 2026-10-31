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
    readonly property bool menuOpen: libraryPane.menuOpen || detailsPopup.visible
                                     || takePicker.menuOpen || trimConfirm.visible
                                     || restoreAsk.visible || checkDialog.visible
                                     || flagsPopup.visible || shortcutOverlay.visible
                                     || sizeSelect.popup.visible || approval.dialogOpen
    // Typing in a field (the search, a dialog's) keeps the keys for the field.
    readonly property bool typing: {
        const item = studio.Window.activeFocusItem
        return item !== null && item.cursorPosition !== undefined
    }
    // The page's keys (§5): on this screen only, never while typing, with a menu or dialog
    // open, or on a held key's repeats.
    readonly property bool keysEnabled: shown && visible && !menuOpen && !typing
    // The menu bar's commands (menus.py): on this screen, with nothing of its own open (typing
    // aside: a menu item is a click, and ⌘ keys work in a field as in any Mac app).
    readonly property bool commandsOn: shown && visible && !menuOpen
    onCommandsOnChanged: rec.setCommandsEnabled(commandsOn)
    Component.onCompleted: rec.setCommandsEnabled(commandsOn)

    Shortcut {
        objectName: "studioKeyRecord"
        sequence: "R"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.toggleRecord()
    }
    Shortcut {
        objectName: "studioKeySpace"
        sequence: "Space"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.spaceKey()
    }
    Shortcut {
        objectName: "studioKeySaveNext"
        sequences: ["Return", "Enter"]
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.saveNext()
    }
    Shortcut {
        objectName: "studioKeySave"
        sequences: [StandardKey.Save]
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.save()
    }
    // The Recording and View menus' keys (D-058, item 4): the menu shows them; these take them.
    Shortcut {
        objectName: "studioKeyContinue"
        sequence: "Ctrl+J"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.continueMissing()
    }
    Shortcut {
        objectName: "studioKeyNextFlagged"
        sequence: "Ctrl+Shift+J"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.nextFlagged()
    }
    Shortcut {
        objectName: "studioKeyPrevious"
        sequence: "Ctrl+Up"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.previousPrompt()
    }
    Shortcut {
        objectName: "studioKeyNext"
        sequence: "Ctrl+Down"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.nextPrompt()
    }
    Shortcut {
        objectName: "studioKeyBigger"
        sequences: ["Ctrl+=", "Ctrl++"]
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.stepReadingSize(1)
    }
    Shortcut {
        objectName: "studioKeySmaller"
        sequence: "Ctrl+-"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.stepReadingSize(-1)
    }
    Shortcut {
        objectName: "studioKeyStandardSize"
        sequence: "Ctrl+0"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: studio.rec.setReadingSize(studio.rec.readingSizes[0])
    }
    // ⌘/ opens the shortcuts (Escape closes them: the overlay is a dialog).
    Shortcut {
        objectName: "studioKeyShortcuts"
        sequence: "Ctrl+/"
        autoRepeat: false
        enabled: studio.keysEnabled
        onActivated: shortcutOverlay.open()
    }
    Connections {
        target: studio.rec
        function onShortcutsRequested() { shortcutOverlay.open() }
    }
    SuiShortcutOverlay {
        id: shortcutOverlay
        objectName: "studioShortcuts"
        shortcuts: studio.rec.shortcuts
        toggleShortcut: ""
    }

    // Leaving a saved take with a trim edit not saved: asked first.
    Connections {
        target: studio.takes
        function onConfirmChanged() {
            // open() reverses a closing dialog (RowDetailSheet.qml says why).
            if (studio.takes.confirmOpen)
                trimConfirm.open()
            else if (trimConfirm.visible)
                trimConfirm.close()
        }
    }
    SuiDialog {
        id: trimConfirm
        objectName: "studioTrimConfirm"
        title: "Save the trim?"
        description: "This take's In and Out were changed and not saved."
        onRejected: if (studio.takes.confirmOpen) studio.takes.confirmCancel()
        actions: [
            SuiButton {
                objectName: "studioConfirmCancel"
                text: "Cancel"
                onClicked: studio.takes.confirmCancel()
            },
            SuiButton {
                objectName: "studioConfirmDiscard"
                text: "Discard edit"
                onClicked: studio.takes.confirmDiscardEdit()
            },
            SuiButton {
                objectName: "studioConfirmSave"
                text: "Save trim"
                variant: "primary"
                onClicked: studio.takes.confirmSaveTrim()
            }
        ]
    }

    // Restore, when what the take was before is not known here: asked, never guessed.
    Connections {
        target: studio.takes
        function onRestoreChanged() {
            // open() reverses a closing dialog (RowDetailSheet.qml says why).
            if (studio.takes.restoreAskOpen)
                restoreAsk.open()
            else if (restoreAsk.visible)
                restoreAsk.close()
        }
    }
    SuiDialog {
        id: restoreAsk
        objectName: "studioRestoreAsk"
        title: "Restore this take as?"
        description: "What this take was before it was set aside is not known on this machine."
        onRejected: if (studio.takes.restoreAskOpen) studio.takes.restoreCancel()
        actions: [
            SuiButton {
                objectName: "studioRestoreCancel"
                text: "Cancel"
                onClicked: studio.takes.restoreCancel()
            },
            SuiButton {
                objectName: "studioRestoreAsDraft"
                text: "Restore as draft"
                onClicked: studio.takes.restoreAs("draft")
            },
            SuiButton {
                objectName: "studioRestoreAsKept"
                text: "Restore as kept"
                variant: "primary"
                onClicked: studio.takes.restoreAs("kept")
            }
        ]
    }

    // Check Input (D-058): the room and a spoken line measured on demand. The microphone
    // opens on Start only, closes by itself, and nothing enters the corpus.
    readonly property var check: rec.check
    Connections {
        target: studio.check
        function onCheckChanged() {
            // open() reverses a closing dialog (RowDetailSheet.qml says why).
            if (studio.check.dialogOpen)
                checkDialog.open()
            else if (checkDialog.visible)
                checkDialog.close()
        }
    }
    SuiDialog {
        id: checkDialog
        objectName: "studioCheckDialog"
        title: "Check input"
        description: "About ten seconds: two of quiet, then the line below read aloud, as you"
                     + " would read a prompt. Not a take: the audio is measured, then deleted."
        dismissOnOutsideClick: false
        onRejected: if (studio.check.dialogOpen) studio.check.cancel()
        ColumnLayout {
            width: checkDialog.availableWidth
            spacing: SuiTheme.spaceSm
            SuiText {
                objectName: "studioCheckInstruction"
                Layout.fillWidth: true
                visible: text !== ""
                text: studio.check.instruction
                variant: "subheading"
            }
            SuiText {
                objectName: "studioCheckLine"
                Layout.fillWidth: true
                text: "“" + studio.check.line + "”"
                tone: studio.check.stage === "speak" ? "" : "muted"
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                visible: studio.check.running
                spacing: SuiTheme.spaceSm
                SuiLevelMeter {
                    objectName: "studioCheckLevel"
                    Layout.fillWidth: true
                    label: "Input level"
                    from: studio.check.levelFloorDb
                    level: studio.check.levelPeakDb
                    showScale: false
                    showValue: false
                    showClip: false
                }
                SuiText {
                    objectName: "studioCheckRemaining"
                    text: studio.check.remainingText
                    variant: "caption"
                }
            }
            Repeater {
                model: studio.check.results
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: SuiTheme.spaceSm
                    SuiText {
                        Layout.preferredWidth: 168
                        text: modelData.label
                        variant: "label"
                    }
                    SuiText {
                        objectName: "studioCheckResult"
                        Layout.fillWidth: true
                        text: modelData.value
                        tone: modelData.tone
                        wrapMode: Text.Wrap
                    }
                }
            }
            SuiText {
                objectName: "studioCheckSummary"
                Layout.fillWidth: true
                visible: text !== ""
                text: studio.check.summary
                tone: studio.check.summaryTone === "neutral" ? "" : studio.check.summaryTone
                wrapMode: Text.Wrap
            }
            // The match line (D-058, item 6), off by default.
            SuiCheckBox {
                objectName: "studioMatchLine"
                Layout.fillWidth: true
                text: "Compare with my match line"
                description: "Keeps your first reading of this line on this machine (never sent)"
                             + " and compares each later check with it: level, SNR, length."
                checked: studio.rec.matchLine
                enabled: !studio.check.running
                onToggled: studio.rec.setMatchLine(checked)
            }
            RowLayout {
                Layout.fillWidth: true
                visible: studio.check.referenceText !== ""
                spacing: SuiTheme.spaceSm
                SuiText {
                    objectName: "studioMatchReference"
                    Layout.fillWidth: true
                    text: studio.check.referenceText
                    variant: "caption"
                }
                SuiButton {
                    objectName: "studioPlayReference"
                    text: "Play reference"
                    variant: "ghost"
                    enabled: !studio.check.running
                    onClicked: studio.check.playReference()
                }
                SuiButton {
                    objectName: "studioForgetReference"
                    text: "Forget reference"
                    variant: "ghost"
                    enabled: !studio.check.running
                    onClicked: studio.check.forgetReference()
                }
            }
        }
        actions: [
            SuiButton {
                objectName: "studioCheckClose"
                text: studio.check.running ? "Stop" : "Close"
                onClicked: studio.check.cancel()
            },
            SuiButton {
                objectName: "studioCheckStart"
                text: studio.check.stage === "done" || studio.check.stage === "failed"
                      ? "Check again" : "Start"
                variant: "primary"
                enabled: !studio.check.running && studio.rec.canCheck
                onClicked: studio.rec.startCheck()
            }
        ]
    }

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
                anchors.right: checkButton.left
                anchors.rightMargin: SuiTheme.spaceMd
                y: 2
                text: studio.rec.promptTitle
                variant: "heading"
                elide: Text.ElideRight
            }
            StudioLabelChips {
                anchors.left: parent.left
                anchors.right: checkButton.left
                anchors.rightMargin: SuiTheme.spaceMd
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 2
                titles: studio.rec.labelChips
                pinned: studio.rec.pinnedChips
                spokenLabel: studio.rec.labelsSpoken
            }
            SuiButton {
                id: checkButton
                objectName: "studioCheckInput"
                anchors.right: sizeSelect.left
                anchors.rightMargin: SuiTheme.spaceSm
                anchors.verticalCenter: parent.verticalCenter
                height: 24
                topPadding: 0
                bottomPadding: 0
                text: "Check input"
                variant: "ghost"
                focusPolicy: Qt.NoFocus
                enabled: studio.rec.canCheck
                onClicked: studio.rec.openCheck()
            }
            // The reading size (§5's text-size choice), kept per viewer.
            T.ComboBox {
                id: sizeSelect
                objectName: "studioReadingSize"
                anchors.right: deviceChip.left
                anchors.rightMargin: SuiTheme.spaceSm
                anchors.verticalCenter: parent.verticalCenter
                width: 76
                height: 24
                leftPadding: SuiTheme.spaceSm
                font.pixelSize: SuiTheme.fontSizeSm
                model: studio.rec.readingSizes
                currentIndex: studio.rec.readingSizes.indexOf(studio.rec.readingSize)
                displayText: "Aa " + studio.rec.readingSize
                Accessible.role: Accessible.ComboBox
                Accessible.name: "Reading text size"
                Accessible.description: studio.rec.readingSize + " points"
                onActivated: (index) => {
                    currentIndex = Qt.binding(() => studio.rec.readingSizes.indexOf(studio.rec.readingSize))
                    studio.rec.setReadingSize(studio.rec.readingSizes[index])
                }
                contentItem: Text {
                    textFormat: Text.PlainText
                    text: sizeSelect.displayText
                    font: sizeSelect.font
                    color: SuiTheme.text
                    verticalAlignment: Text.AlignVCenter
                }
                indicator: Text {
                    textFormat: Text.PlainText
                    x: sizeSelect.width - width - SuiTheme.spaceSm
                    y: (sizeSelect.height - height) / 2
                    text: "▾"
                    color: SuiTheme.muted
                    font.pixelSize: SuiTheme.fontSizeSm
                    Accessible.ignored: true
                }
                background: Rectangle {
                    radius: SuiTheme.radiusSm
                    color: SuiTheme.field
                    border.width: 1
                    border.color: sizeSelect.activeFocus ? SuiTheme.accent : SuiTheme.fieldBorder
                }
                delegate: T.ItemDelegate {
                    id: sizeOption
                    required property int index
                    required property var modelData
                    objectName: "studioReadingSize-" + modelData
                    width: 150
                    implicitHeight: SuiTheme.rowHeight
                    leftPadding: SuiTheme.spaceSm
                    highlighted: sizeSelect.highlightedIndex === index
                    contentItem: Text {
                        textFormat: Text.PlainText
                        text: ["Standard", "Large", "Extra large"][sizeOption.index] + " · " + sizeOption.modelData + " pt"
                        color: SuiTheme.text
                        font.pixelSize: SuiTheme.fontSizeSm
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle { color: sizeOption.highlighted ? SuiTheme.bgHover : "transparent" }
                }
                popup: T.Popup {
                    y: sizeSelect.height + 2
                    x: sizeSelect.width - width
                    width: 150
                    implicitHeight: contentItem.implicitHeight + 2 * padding
                    padding: 2
                    contentItem: ListView {
                        implicitHeight: contentHeight
                        model: sizeSelect.popup.visible ? sizeSelect.delegateModel : null
                        currentIndex: sizeSelect.highlightedIndex
                    }
                    background: Rectangle {
                        radius: SuiTheme.radiusSm
                        color: SuiTheme.popover
                        border.width: 1
                        border.color: SuiTheme.borderStrong
                    }
                }
            }
            SuiChip {
                id: deviceChip
                objectName: "studioDevice"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: 120
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
                onStepRequested: (frames) => studio.takes.seekBy(frames)
                onEdgeRequested: (end) => end ? studio.takes.seekToEnd() : studio.takes.seekToStart()
            }
            // The take's quality flags (D-058): a chip each, in words (never colour alone), in
            // the waveform's corner, never in the way; a click says why, with the levels.
            T.AbstractButton {
                id: flagsBadge
                objectName: "studioFlags"
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.margins: SuiTheme.spaceXs
                visible: studio.takes.qualityState !== "" && studio.rec.problemText === ""
                enabled: studio.takes.qualityState === "checked"
                focusPolicy: Qt.NoFocus
                implicitWidth: implicitContentWidth + leftPadding + rightPadding
                implicitHeight: implicitContentHeight + topPadding + bottomPadding
                padding: 0
                Accessible.role: Accessible.Button
                Accessible.name: studio.takes.flags.length === 0 ? flagsNone.text
                    : "Flags: " + studio.takes.flags.map((flag) => flag.title).join(", ")
                onClicked: flagsPopup.open()
                contentItem: Row {
                    spacing: SuiTheme.spaceXs
                    Repeater {
                        model: studio.takes.flags
                        delegate: SuiChip {
                            required property var modelData
                            objectName: "studioFlag-" + modelData.code
                            text: modelData.title
                            tone: modelData.tone
                            dot: true
                        }
                    }
                    SuiChip {
                        id: flagsNone
                        objectName: "studioFlagsNone"
                        visible: studio.takes.flags.length === 0
                        text: studio.takes.qualityState === "checking" ? "Checking…" : "No flags"
                    }
                }
                T.Popup {
                    id: flagsPopup
                    objectName: "studioFlagsPopup"
                    x: flagsBadge.width - width
                    y: flagsBadge.height + SuiTheme.spaceXs
                    width: 400
                    padding: SuiTheme.spaceMd
                    closePolicy: T.Popup.CloseOnEscape | T.Popup.CloseOnPressOutside
                    background: Rectangle {
                        radius: SuiTheme.radiusMd
                        color: SuiTheme.popover
                        border.width: 1
                        border.color: SuiTheme.borderStrong
                    }
                    contentItem: SuiText {
                        objectName: "studioFlagsDetail"
                        text: studio.takes.qualityDetail
                        wrapMode: Text.Wrap
                    }
                }
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
                    tone: studio.rec.statusTone === "neutral" ? "" : studio.rec.statusTone
                    wrapMode: Text.Wrap
                }
            }
        }
        // In and Out from the playhead, Save trim, Reset. The take under review keeps its edit
        // until Save; a take recorded elsewhere shows its trim.
        Item {
            id: trimRow
            objectName: "studioTrimRow"
            x: studio.inset
            y: studio.trimY
            width: studio.contentWidth
            height: studio.trimHeight
            // The In and Out captions only where the row has room for them (they depend on
            // the window's size alone): on the smallest window, Set In and Set Out name the
            // times beside them.
            readonly property bool captions: width >= 520
            RowLayout {
                anchors.fill: parent
                spacing: SuiTheme.spaceXs
                SuiText { text: "In"; variant: "caption"; visible: trimRow.captions }
                SuiText {
                    objectName: "studioTrimIn"
                    text: studio.takes.inText
                    variant: "code"
                    tone: studio.takes.trimDirty ? "warn" : ""
                    Accessible.name: "In " + text
                }
                CompactButton {
                    objectName: "studioSetIn"
                    text: "Set In"
                    enabled: studio.takes.canEditTrim
                    onClicked: studio.takes.setIn()
                }
                SuiText {
                    Layout.leftMargin: SuiTheme.spaceSm
                    text: "Out"
                    variant: "caption"
                    visible: trimRow.captions
                }
                SuiText {
                    objectName: "studioTrimOut"
                    Layout.leftMargin: trimRow.captions ? 0 : SuiTheme.spaceSm
                    text: studio.takes.outText
                    variant: "code"
                    tone: studio.takes.trimDirty ? "warn" : ""
                    Accessible.name: "Out " + text
                }
                CompactButton {
                    objectName: "studioSetOut"
                    text: "Set Out"
                    enabled: studio.takes.canEditTrim
                    onClicked: studio.takes.setOut()
                }
                Item { id: trimGap; Layout.fillWidth: true }
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
            // "edited" sits in the gap and never takes room from the row: shown when it
            // fits there (the times turn amber either way).
            SuiText {
                objectName: "studioTrimEdited"
                x: trimGap.x + Math.round((trimGap.width - width) / 2)
                anchors.verticalCenter: parent.verticalCenter
                text: "edited"
                variant: "caption"
                tone: "warn"
                visible: studio.takes.trimDirty && trimGap.width >= width + 2 * SuiTheme.spaceXs
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
                id: elapsedLabel
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
            // The take's own caption (D-058, item 9): what it is, then the scale; errors are
            // the status line's and never take its place.
            SuiText {
                objectName: "studioWaveNote"
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                width: Math.max(0, parent.width / 2 - elapsedLabel.width / 2 - SuiTheme.spaceSm)
                visible: studio.takes.bins.length > 0 || studio.takes.takeCaption !== ""
                text: studio.takes.takeCaption !== ""
                      ? studio.takes.takeCaption + " · " + studio.takes.waveNote
                      : studio.takes.waveNote
                variant: "caption"
                elide: Text.ElideRight
            }
            SuiLevelMeter {
                objectName: "studioLevel"
                anchors.right: levelText.left
                anchors.rightMargin: SuiTheme.spaceXs
                anchors.verticalCenter: parent.verticalCenter
                width: 64
                visible: studio.takes.live && studio.takes.levelText !== ""
                label: "Recording level"
                from: studio.takes.levelFloorDb
                level: studio.takes.levelPeakDb
                showScale: false
                showValue: false
                showClip: false
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
                StudioKeyButton {
                    objectName: "studioPlay"
                    Layout.preferredWidth: implicitWidth
                    text: studio.takes.playing ? "Pause" : "Play"
                    texts: ["Play", "Pause"]
                    key: studio.rec.keys.play
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
                Accessible.description: studio.rec.recordBlocker
                // Disabled, it says why (D-058, item 9; the Swift app's transport does).
                HoverHandler {
                    id: recordHover
                }
                T.ToolTip {
                    id: recordTip
                    objectName: "studioRecordTip"
                    parent: recordButton
                    visible: recordHover.hovered && studio.rec.recordBlocker !== ""
                    delay: 400
                    y: -implicitHeight - SuiTheme.spaceXs
                    padding: SuiTheme.spaceSm
                    contentItem: SuiText {
                        text: studio.rec.recordBlocker
                        variant: "caption"
                    }
                    background: Rectangle {
                        radius: SuiTheme.radiusSm
                        color: SuiTheme.popover
                        border.width: 1
                        border.color: SuiTheme.borderStrong
                    }
                }
            }
            // As wide as its widest word, so R beside it never moves with the transport.
            FontMetrics {
                id: recordMetrics
                font: recordLabel.font
            }
            SuiText {
                id: recordLabel
                objectName: "studioRecordLabel"
                anchors.left: recordButton.right
                anchors.leftMargin: SuiTheme.spaceMd
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(
                    Math.ceil(Math.max(...["Record", "Stop", "Starting", "Stopping", "Saving"]
                                       .map((t) => recordMetrics.advanceWidth(t)))),
                    actions.x - x - recordKey.width - 2 * SuiTheme.spaceSm)
                text: studio.rec.phase === "recording" ? "Stop"
                    : studio.rec.phase === "starting" ? "Starting"
                    : studio.rec.phase === "stopping" ? "Stopping"
                    : studio.rec.phase === "saving" ? "Saving" : "Record"
                variant: "subheading"
                elide: Text.ElideRight
            }
            // R records and stops (its key, D-058).
            SuiKbd {
                id: recordKey
                objectName: "studioRecordKey"
                anchors.left: recordLabel.right
                anchors.leftMargin: SuiTheme.spaceXs
                anchors.verticalCenter: parent.verticalCenter
                text: studio.rec.keys.record
            }
            Row {
                id: actions
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: SuiTheme.spaceSm
                StudioKeyButton {
                    objectName: "studioRetake"
                    text: "Retake"
                    key: studio.rec.keys.record
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
                StudioKeyButton {
                    objectName: "studioSave"
                    text: "Save"
                    key: studio.rec.keys.save
                    enabled: studio.rec.canSave
                    onClicked: studio.rec.save()
                }
                StudioKeyButton {
                    objectName: "studioSaveNext"
                    text: "Save & Next"
                    key: studio.rec.keys.saveNext
                    variant: "primary"
                    enabled: studio.rec.canSaveNext
                    onClicked: studio.rec.saveNext()
                }
            }
        }

        // -- the status line: the take on the left, the sync (or a notice) on the right ----------
        // Each half wraps to two lines, so the cause of a failure or a refusal stays in view.
        // Beside the sync, one button at most (studio/status.py), so the line stays whole on the
        // smallest window: Approve while uploads wait for the home-host approval (the app's
        // `approval` model opens its dialog), Sync now while operations wait or the PC did not
        // answer, or Details when the line leaves something out. Approve and Sync now wait
        // while a take is being recorded or saved. The split is 40/60: the sync half carries the
        // button, and on macOS's wider system font 45/55 cut its longer lines at 960 × 600.
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
                // The room the sync badge and its action leave.
                width: Math.max(0, syncActions.x - SuiTheme.spaceMd)
                height: parent.height
                text: studio.rec.statusText
                tone: studio.rec.statusTone === "neutral" ? "" : studio.rec.statusTone
                variant: "caption"
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            Row {
                id: syncActions
                objectName: "studioSyncActions"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: SuiTheme.spaceXs
                // The sync badge (D-058, item 9): always there, a word or two; a click shows
                // the whole line (and a notice's Dismiss).
                T.AbstractButton {
                    id: syncBadge
                    objectName: "studioSyncBadge"
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: implicitContentWidth
                    implicitHeight: implicitContentHeight
                    padding: 0
                    focusPolicy: Qt.NoFocus
                    visible: studio.rec.syncBadge !== ""
                    Accessible.role: Accessible.Button
                    Accessible.name: "Sync: " + studio.rec.syncBadge
                    Accessible.description: studio.rec.syncText
                    onClicked: detailsPopup.open()
                    contentItem: SuiChip {
                        objectName: "studioSyncText"
                        text: studio.rec.syncBadge
                        tone: studio.rec.syncTone
                        dot: true
                    }
                }
                SuiButton {
                    objectName: "studioApprove"
                    text: "Approve"
                    variant: "primary"
                    visible: studio.rec.syncAction === "approve"
                    enabled: !approval.busy && !studio.busy
                    onClicked: approval.begin()
                }
                SuiButton {
                    objectName: "studioSyncNow"
                    text: "Sync now"
                    variant: "ghost"
                    visible: studio.rec.syncAction === "sync"
                    enabled: !studio.busy
                    onClicked: studio.rec.syncNow()
                }
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
                        objectName: "studioDetailsLine"
                        Layout.fillWidth: true
                        text: studio.rec.syncText
                        tone: studio.rec.syncTone === "neutral" ? "" : studio.rec.syncTone
                        wrapMode: Text.Wrap
                    }
                    SuiText {
                        objectName: "studioDetailsText"
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: studio.rec.syncDetail
                        variant: "caption"
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
