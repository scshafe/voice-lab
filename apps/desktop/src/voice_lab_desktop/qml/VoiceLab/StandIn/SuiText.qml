// STAND-IN, marked for removal: scshafe-qt's typography gap (design §6.4). The API
// is the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md ("SuiText"); when
// scshafe-qt releases it, delete this file and its qmldir line.
//
// SuiText: a text over the type scale. Always plain text (untrusted strings are
// shown literally).
// - `variant`: "display" | "title" | "heading" | "subheading" | "body" (default)
//   | "caption" | "label" | "mono" | "number" (tabular figures);
// - `tone`: "default" | "strong" | "muted" | "info" | "ok" | "warn" | "danger".
// Every other property is Text's (wrapMode, elide, maximumLineCount, ...).
import QtQuick
import Scshafe.Ui

Text {
    id: control

    property string variant: "body"
    property string tone: "default"

    textFormat: Text.PlainText
    color: {
        switch (tone) {
        case "strong": return SuiTheme.textStrong
        case "muted": return SuiTheme.muted
        case "info": return SuiTheme.toneText("info")
        case "ok": return SuiTheme.toneText("ok")
        case "warn": return SuiTheme.toneText("warn")
        case "danger": return SuiTheme.toneText("danger")
        default:
            return variant === "caption" ? SuiTheme.muted
                 : (variant === "display" || variant === "title" || variant === "heading")
                   ? SuiTheme.textStrong : SuiTheme.text
        }
    }
    font.pixelSize: {
        switch (variant) {
        case "display": return SuiTheme.fontSize3xl
        case "title": return SuiTheme.fontSize2xl
        case "heading": return SuiTheme.fontSizeLg
        case "subheading": return SuiTheme.fontSizeMd
        case "caption": return SuiTheme.fontSizeSm
        case "label": return SuiTheme.fontSizeSm
        case "mono": return SuiTheme.fontSizeSm
        default: return SuiTheme.fontSizeBase
        }
    }
    font.weight: {
        switch (variant) {
        case "display":
        case "title": return SuiTheme.fontWeightBold
        case "heading":
        case "subheading":
        case "label": return SuiTheme.fontWeightStrong
        default: return SuiTheme.fontWeightNormal
        }
    }
    font.family: variant === "mono" ? SuiTheme.monoFamily : Qt.application.font.family
    font.features: variant === "number" ? ({ "tnum": 1 }) : ({})
    Accessible.role: Accessible.StaticText
    Accessible.name: text
}
