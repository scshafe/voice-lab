// The script card (NATIVE_RECORDING_UX.md §4, "labels, Prompt and Instructions"): one layout
// for every kind, top to bottom: a kind banner (none for a spoken prompt); a Prompt section
// (PROMPT · READ ALOUD, the text at the reading size) only for spoken prompts, a muted "No
// spoken prompt" line otherwise; and an Instructions section that is always there
// (INSTRUCTIONS · NOT READ ALOUD, the instructions, then the guidance and the duration in a
// fixed 32-point row). Under a Prompt the Instructions keep their natural height up to 40%
// of the card and scroll beyond it; without one they fill the card. Long text scrolls inside
// its section; the scroll position resets only for a new prompt. Plain text only.
import QtQuick
import Scshafe.Ui
import VoiceLab.StandIn

Rectangle {
    id: card
    objectName: "studioScriptCard"

    required property var rec
    readonly property int pad: SuiTheme.spaceLg
    readonly property int headingHeight: 18
    readonly property int guidanceHeight: 32
    readonly property bool hasPrompt: rec.promptId !== ""
    readonly property bool spoken: rec.promptText !== ""
    readonly property string shownId: rec.promptId
    readonly property real bodyTop: pad + (bannerText.visible ? headingHeight + SuiTheme.spaceSm : 0)
    readonly property real instructionsNatural: headingHeight + SuiTheme.spaceXs
        + (rec.instructions !== "" ? instructionsText.implicitHeight : 0) + guidanceHeight
    readonly property real instructionsHeight: !hasPrompt ? 0
        : spoken ? Math.min(instructionsNatural, (height - 2 * pad) * 0.4)
        : Math.max(0, height - pad - (bodyTop + headingHeight + SuiTheme.spaceSm))

    radius: SuiTheme.radiusLg
    color: SuiTheme.panel
    border.width: 1
    border.color: SuiTheme.line
    clip: true

    onShownIdChanged: {
        scriptFlick.contentY = 0
        instructionsFlick.contentY = 0
    }

    component Heading: Text {
        textFormat: Text.PlainText
        color: SuiTheme.muted
        font.pixelSize: 13
        font.weight: SuiTheme.fontWeightStrong
        elide: Text.ElideRight
        height: card.headingHeight
        verticalAlignment: Text.AlignVCenter
    }

    Heading {
        id: bannerText
        objectName: "studioBanner"
        x: card.pad
        y: card.pad
        width: card.width - 2 * card.pad
        visible: card.rec.banner !== ""
        text: card.rec.banner
        color: SuiTheme.toneText("warn")
    }

    // The Prompt section, or the line in its place.
    Item {
        id: promptSection
        objectName: "studioPromptSection"
        x: card.pad
        y: card.bodyTop
        width: card.width - 2 * card.pad
        height: card.spoken
            ? Math.max(0, card.height - card.pad - card.bodyTop - card.instructionsHeight - SuiTheme.spaceSm)
            : card.headingHeight
        Heading {
            objectName: "studioPromptHeading"
            width: parent.width
            visible: card.spoken
            text: "PROMPT · READ ALOUD"
        }
        Flickable {
            id: scriptFlick
            objectName: "studioScriptFlick"
            y: card.headingHeight + SuiTheme.spaceXs
            width: parent.width
            height: Math.max(0, parent.height - y)
            visible: card.spoken
            clip: true
            contentHeight: scriptText.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            Accessible.name: "Prompt, read aloud"
            Text {
                id: scriptText
                objectName: "studioScript"
                width: scriptFlick.width
                textFormat: Text.PlainText
                text: card.rec.promptText
                color: SuiTheme.textStrong
                font.pixelSize: 24
                lineHeight: 1.25
                wrapMode: Text.Wrap
            }
        }
        SuiText {
            objectName: "studioPlaceholder"
            width: parent.width
            height: card.headingHeight
            visible: !card.spoken
            text: card.rec.placeholder
            variant: "caption"
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
    }

    // The Instructions section: always there for a selected prompt, never read aloud.
    Item {
        id: instructionsSection
        objectName: "studioInstructionsSection"
        x: card.pad
        y: promptSection.y + promptSection.height + SuiTheme.spaceSm
        width: card.width - 2 * card.pad
        height: card.instructionsHeight
        visible: card.hasPrompt
        Accessible.name: "Instructions, do not read aloud"
        Heading {
            width: parent.width
            text: "INSTRUCTIONS · NOT READ ALOUD"
        }
        Flickable {
            id: instructionsFlick
            objectName: "studioInstructionsFlick"
            y: card.headingHeight + SuiTheme.spaceXs
            width: parent.width
            height: Math.max(0, parent.height - y - card.guidanceHeight)
            clip: true
            contentHeight: instructionsText.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            Text {
                id: instructionsText
                objectName: "studioInstructions"
                width: instructionsFlick.width
                textFormat: Text.PlainText
                text: card.rec.instructions
                color: SuiTheme.text
                font.pixelSize: 17
                wrapMode: Text.Wrap
            }
        }
        Item {
            objectName: "studioGuidanceRow"
            y: parent.height - card.guidanceHeight
            width: parent.width
            height: card.guidanceHeight
            SuiText {
                objectName: "studioGuidance"
                anchors.left: parent.left
                anchors.right: durationText.left
                anchors.rightMargin: SuiTheme.spaceSm
                anchors.verticalCenter: parent.verticalCenter
                text: card.rec.guidance
                variant: "caption"
                font.pixelSize: 13
                elide: Text.ElideRight
                maximumLineCount: 2
                wrapMode: Text.Wrap
            }
            SuiText {
                id: durationText
                objectName: "studioDuration"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: card.rec.duration
                variant: "caption"
                font.pixelSize: 13
            }
        }
    }
}
