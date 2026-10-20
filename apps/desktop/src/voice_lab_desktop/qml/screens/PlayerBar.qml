// One take's controls over the app's one player (`player`, playback.py): Play / Pause,
// the position (a slider to seek) and the time. `key` names this place in the app
// ("corpus:<id>", "referee:a", ...); starting it stops whatever else was playing.
// Without playback (`available` false) the button is disabled and `unavailableText`
// shows instead. `gainDb` and `lowpassHz` (0: none) shape the take (the referee).
import QtQuick
import QtQuick.Layouts
import Scshafe.Ui
import VoiceLab.StandIn

RowLayout {
    id: bar
    objectName: "playerBar"
    property string key: ""
    property string source: ""
    property bool available: true
    property string unavailableText: ""
    property real gainDb: 0
    property real lowpassHz: 0
    readonly property bool active: player.activeKey === key && key !== ""
    readonly property string playerState: active ? player.state : "idle"

    function clock(seconds) {
        const s = Math.max(0, Math.floor(seconds))
        return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0")
    }
    function start() { player.playShaped(key, source, gainDb, lowpassHz) }

    spacing: SuiTheme.spaceSm

    SuiButton {
        objectName: "playButton"
        text: bar.playerState === "playing" ? "Pause"
            : bar.playerState === "loading" ? "Loading…"
            : bar.playerState === "paused" ? "Resume" : "Play"
        enabled: bar.available && bar.playerState !== "loading"
        onClicked: {
            if (bar.playerState === "playing" || bar.playerState === "paused") player.toggle(bar.key)
            else bar.start()
        }
    }
    SuiSlider {
        objectName: "playerPosition"
        Layout.fillWidth: true
        visible: bar.active && (bar.playerState === "playing" || bar.playerState === "paused")
        label: "Position"
        from: 0
        to: Math.max(0.01, player.duration)
        value: bar.active ? player.position : 0
        onMoved: player.seek(value)
    }
    SuiText {
        objectName: "playerTime"
        visible: bar.active && (bar.playerState === "playing" || bar.playerState === "paused")
        text: bar.clock(player.position) + " / " + bar.clock(player.duration)
        tabular: true
    }
    SuiText {
        objectName: "playerError"
        Layout.fillWidth: true
        visible: bar.playerState === "failed"
        text: player.error
        tone: "danger"
        wrapMode: Text.Wrap
    }
    SuiText {
        objectName: "playerUnavailable"
        Layout.fillWidth: true
        visible: !bar.available && bar.unavailableText !== ""
        text: bar.unavailableText
        variant: "caption"
        wrapMode: Text.Wrap
    }
}
