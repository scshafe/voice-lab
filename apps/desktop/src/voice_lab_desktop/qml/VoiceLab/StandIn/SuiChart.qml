// STAND-IN, marked for removal: scshafe-qt's charts gap (design §6.4: line,
// band/area, scatter, histogram, themed from the tokens, no Qt Graphs). The API is
// the one asked of scshafe-qt in docs/design/PHASE-4-LOG.md ("SuiChart"); when
// scshafe-qt releases it, delete this file and its qmldir line.
//
// SuiChart: a layered chart of one or more panels stacked on a shared x axis.
// Everything is in data coordinates; the chart owns the pixels. `spec`:
//   { label, xMin, xMax, xTicks: [{ value, label }], xTitle, padLeft, padRight,
//     compact: bool (no room for titles or the x axis: a sparkline),
//     panels: [{ height, title, yMin, yMax, yScale: "linear" | "log2",
//                yTicks: [{ value, label }], yTicksRight: [...], grid: bool,
//                layers: [layer, ...] }] }
// Layers (`tone` is a SuiTheme tone: "bucket1".."bucket8", "info", "ok", "warn",
// "danger", "muted", "line"; `opacity` in [0, 1]):
//   { kind: "band",   tone, opacity, points: [[x, lo, hi], ...] }      a filled band
//   { kind: "line",   tone, width, points: [[x, y], ...] }             null y breaks it
//   { kind: "points", tone, radius, points: [{ x, y, r, tone, shade, hollow,
//                                              opacity, id, tooltip }] }
//   { kind: "rects",  tone, opacity, rects: [{ x0, x1, y0, y1, tone, id, tooltip }] }
//                                    (y0/y1 null: the panel's bottom/top)
//   { kind: "rule",   y, tone, label, labelSide: "left" | "right" }    horizontal
//   { kind: "vrules", xs: [...], tone }                                 vertical
//   { kind: "zone",   y0, y1, tone, opacity, label }                    a y range
//   { kind: "regions", regions: [{ x0, x1, tooltip }] }                 hover only
//   { kind: "text",   items: [{ x, y, text, align: "left"|"center"|"right" }] }
// `shade` in [0, 1] darkens a point's tone toward the panel (older = darker).
// Hover shows the tooltip of the point, rect or region under the pointer;
// a click on a point or rect with an `id` emits `activated(id)`.
// `tooltipAt(x, y)` and `idAt(x, y)` answer for a position (tests, keyboard).
import QtQuick
import Scshafe.Ui

Item {
    id: chart

    property var spec: ({ panels: [] })
    property string hoverText: ""
    property int paintCount: 0
    readonly property var panels: spec && spec.panels ? spec.panels : []
    readonly property real padLeft: spec && spec.padLeft !== undefined ? spec.padLeft : 52
    readonly property real padRight: spec && spec.padRight !== undefined ? spec.padRight : 16
    readonly property bool compact: !!(spec && spec.compact)
    readonly property int titleHeight: compact ? 0 : 18
    readonly property int panelGap: compact ? 0 : 14
    readonly property int axisHeight: compact ? 0 : (spec && spec.xTitle ? 34 : 20)

    signal activated(string id)

    implicitWidth: 480
    implicitHeight: _totalHeight()
    Accessible.role: Accessible.Chart
    Accessible.name: spec && spec.label ? spec.label : ""

    function _totalHeight() {
        let h = axisHeight
        for (let i = 0; i < panels.length; i++)
            h += titleHeight + (panels[i].height || 120) + (i > 0 ? panelGap : 0)
        return h
    }
    function _panelTop(index) {
        let top = 0
        for (let i = 0; i < index; i++)
            top += titleHeight + (panels[i].height || 120) + panelGap
        return top + titleHeight
    }
    function _x(value) {
        const lo = spec.xMin, hi = spec.xMax
        const w = width - padLeft - padRight
        return padLeft + (hi === lo ? w / 2 : (value - lo) / (hi - lo) * w)
    }
    function _y(panel, top, value) {
        const h = panel.height || 120
        if (panel.yScale === "log2") {
            const lo = Math.log2(panel.yMin), hi = Math.log2(panel.yMax)
            return top + h - (Math.log2(value) - lo) / (hi - lo) * h
        }
        const lo = panel.yMin, hi = panel.yMax
        return top + h - (hi === lo ? 0.5 : (value - lo) / (hi - lo)) * h
    }
    function _color(tone, opacity) {
        let c
        if (tone === "line") c = SuiTheme.line
        else if (tone === "muted") c = SuiTheme.muted
        else if (tone === "accent") c = SuiTheme.accent
        else if (tone === "text") c = SuiTheme.text
        else c = SuiTheme.toneBase(tone || "info")
        return Qt.rgba(c.r, c.g, c.b, c.a * (opacity === undefined ? 1 : opacity))
    }
    function _shaded(tone, shade) {
        const c = _color(tone, 1)
        if (shade === undefined || shade === null) return c
        const t = 0.45 + 0.55 * Math.max(0, Math.min(1, shade))
        return Qt.rgba(c.r * t, c.g * t, c.b * t, 1)
    }
    function _panelAt(py) {
        for (let i = 0; i < panels.length; i++) {
            const top = _panelTop(i), h = panels[i].height || 120
            if (py >= top - 4 && py <= top + h + 4) return i
        }
        return -1
    }
    // The topmost thing under (px, py): { tooltip, id } or null.
    function _hit(px, py) {
        const p = _panelAt(py)
        if (p < 0) return null
        const panel = panels[p], top = _panelTop(p), h = panel.height || 120
        const layers = panel.layers || []
        for (let l = layers.length - 1; l >= 0; l--) {
            const layer = layers[l]
            if (layer.kind === "points") {
                let best = null, bestD = 1e9
                for (const pt of layer.points || []) {
                    if (pt.y === null || pt.y === undefined) continue
                    const dx = _x(pt.x) - px, dy = _y(panel, top, pt.y) - py
                    const d = Math.sqrt(dx * dx + dy * dy)
                    const r = (pt.r || layer.radius || 3) + 3
                    if (d <= r && d < bestD) { best = pt; bestD = d }
                }
                if (best) return { tooltip: best.tooltip || "", id: best.id || "" }
            } else if (layer.kind === "rects") {
                for (const r of layer.rects || []) {
                    if (!r.tooltip && !r.id) continue
                    const x0 = _x(r.x0), x1 = _x(r.x1)
                    const y0 = r.y0 === null || r.y0 === undefined ? top + h : _y(panel, top, r.y0)
                    const y1 = r.y1 === null || r.y1 === undefined ? top : _y(panel, top, r.y1)
                    if (px >= Math.min(x0, x1) && px <= Math.max(x0, x1)
                            && py >= Math.min(y0, y1) && py <= Math.max(y0, y1))
                        return { tooltip: r.tooltip || "", id: r.id || "" }
                }
            } else if (layer.kind === "regions") {
                for (const r of layer.regions || [])
                    if (px >= _x(r.x0) && px <= _x(r.x1)) return { tooltip: r.tooltip || "", id: "" }
            }
        }
        return null
    }
    function tooltipAt(px, py) { const hit = _hit(px, py); return hit ? hit.tooltip : "" }
    function idAt(px, py) { const hit = _hit(px, py); return hit ? hit.id : "" }
    // Pixel position of a data point in panel `p` (for tests and keyboard focus).
    function pointPosition(p, x, y) { return Qt.point(_x(x), _y(panels[p], _panelTop(p), y)) }

    onSpecChanged: canvas.requestPaint()
    onWidthChanged: canvas.requestPaint()
    onHeightChanged: canvas.requestPaint()
    Connections {
        target: SuiTheme
        function onDarkChanged() { canvas.requestPaint() }
    }

    Canvas {
        id: canvas
        anchors.fill: parent
        renderTarget: Canvas.Image
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            chart.paintCount += 1
            if (!chart.panels.length || chart.width <= chart.padLeft + chart.padRight) return
            const font = SuiTheme.fontSizeXs + "px sans-serif"
            ctx.font = font
            for (let p = 0; p < chart.panels.length; p++) {
                const panel = chart.panels[p]
                const top = chart._panelTop(p), h = panel.height || 120
                const left = chart.padLeft, right = chart.width - chart.padRight
                // title
                if (panel.title) {
                    ctx.fillStyle = chart._color("muted", 1)
                    ctx.textAlign = "left"
                    ctx.fillText(panel.title, left, top - 6)
                }
                // grid and ticks
                ctx.textAlign = "right"
                for (const t of panel.yTicks || []) {
                    const y = chart._y(panel, top, t.value)
                    if (panel.grid) {
                        ctx.strokeStyle = chart._color("line", 0.6)
                        ctx.lineWidth = 1
                        ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke()
                    }
                    ctx.fillStyle = chart._color("muted", 1)
                    ctx.fillText(t.label, left - 6, y + 3)
                }
                ctx.textAlign = "left"
                for (const t of panel.yTicksRight || []) {
                    const y = chart._y(panel, top, t.value)
                    ctx.fillStyle = chart._color("muted", 1)
                    ctx.fillText(t.label, right + 6, y + 3)
                }
                if (!chart.compact) {
                    ctx.strokeStyle = chart._color("line", 1)
                    ctx.lineWidth = 1
                    ctx.beginPath(); ctx.moveTo(left, top + h); ctx.lineTo(right, top + h); ctx.stroke()
                }
                for (const layer of panel.layers || [])
                    chart._paintLayer(ctx, layer, panel, top, h, left, right)
            }
            // x axis labels under the last panel
            const last = chart.panels.length - 1
            const bottom = chart._panelTop(last) + (chart.panels[last].height || 120)
            ctx.fillStyle = chart._color("muted", 1)
            ctx.textAlign = "center"
            for (const t of chart.spec.xTicks || [])
                ctx.fillText(t.label, chart._x(t.value), bottom + 14)
            if (chart.spec.xTitle)
                ctx.fillText(chart.spec.xTitle, (chart.padLeft + chart.width - chart.padRight) / 2, bottom + 28)
        }
    }

    function _paintLayer(ctx, layer, panel, top, h, left, right) {
        switch (layer.kind) {
        case "zone": {
            const y0 = _y(panel, top, layer.y0), y1 = _y(panel, top, layer.y1)
            ctx.fillStyle = _color(layer.tone, layer.opacity === undefined ? 0.12 : layer.opacity)
            ctx.fillRect(left, Math.min(y0, y1), right - left, Math.abs(y1 - y0))
            if (layer.label) {
                ctx.fillStyle = _color("muted", 1)
                ctx.textAlign = "left"
                ctx.fillText(layer.label, right + 6, (y0 + y1) / 2 + 3)
            }
            break
        }
        case "rects":
            for (const r of layer.rects || []) {
                const x0 = _x(r.x0), x1 = _x(r.x1)
                const y0 = r.y0 === null || r.y0 === undefined ? top + h : _y(panel, top, r.y0)
                const y1 = r.y1 === null || r.y1 === undefined ? top : _y(panel, top, r.y1)
                ctx.fillStyle = _color(r.tone || layer.tone, layer.opacity === undefined ? 1 : layer.opacity)
                ctx.fillRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0))
            }
            break
        case "band": {
            const pts = layer.points || []
            if (pts.length < 2) break
            ctx.fillStyle = _color(layer.tone, layer.opacity === undefined ? 0.16 : layer.opacity)
            ctx.beginPath()
            ctx.moveTo(_x(pts[0][0]), _y(panel, top, pts[0][2]))
            for (let i = 1; i < pts.length; i++) ctx.lineTo(_x(pts[i][0]), _y(panel, top, pts[i][2]))
            for (let i = pts.length - 1; i >= 0; i--) ctx.lineTo(_x(pts[i][0]), _y(panel, top, pts[i][1]))
            ctx.closePath()
            ctx.fill()
            break
        }
        case "line": {
            ctx.strokeStyle = _color(layer.tone, layer.opacity === undefined ? 1 : layer.opacity)
            ctx.lineWidth = layer.width || 2
            ctx.lineJoin = "round"
            ctx.lineCap = "round"
            let drawing = false
            ctx.beginPath()
            for (const pt of layer.points || []) {
                if (pt[1] === null || pt[1] === undefined) { drawing = false; continue }
                const x = _x(pt[0]), y = _y(panel, top, pt[1])
                if (drawing) ctx.lineTo(x, y)
                else { ctx.moveTo(x, y); drawing = true }
            }
            ctx.stroke()
            break
        }
        case "points":
            for (const pt of layer.points || []) {
                if (pt.y === null || pt.y === undefined) continue
                const color = _shaded(pt.tone || layer.tone, pt.shade)
                const r = pt.r || layer.radius || 3
                ctx.beginPath()
                ctx.arc(_x(pt.x), _y(panel, top, pt.y), r, 0, 2 * Math.PI)
                if (pt.hollow) {
                    ctx.strokeStyle = color
                    ctx.lineWidth = 1.4
                    ctx.stroke()
                } else {
                    ctx.fillStyle = Qt.rgba(color.r, color.g, color.b, pt.opacity === undefined ? 0.85 : pt.opacity)
                    ctx.fill()
                }
            }
            break
        case "rule": {
            const y = _y(panel, top, layer.y)
            ctx.strokeStyle = _color(layer.tone || "line", 1)
            ctx.lineWidth = 1
            ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke()
            if (layer.label) {
                ctx.fillStyle = _color("muted", 1)
                ctx.textAlign = layer.labelSide === "right" ? "left" : "right"
                ctx.fillText(layer.label, layer.labelSide === "right" ? right + 6 : left - 6, y + 3)
            }
            break
        }
        case "vrules":
            ctx.strokeStyle = _color(layer.tone || "line", 1)
            ctx.lineWidth = 1
            for (const x of layer.xs || []) {
                ctx.beginPath(); ctx.moveTo(_x(x), top); ctx.lineTo(_x(x), top + h); ctx.stroke()
            }
            break
        case "text":
            ctx.fillStyle = _color("muted", 1)
            for (const item of layer.items || []) {
                ctx.textAlign = item.align || "center"
                ctx.fillText(item.text, _x(item.x), _y(panel, top, item.y) + 3)
            }
            break
        default:
            break
        }
    }

    MouseArea {
        objectName: "chartPointer"
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton
        cursorShape: chart.idAt(mouseX, mouseY) !== "" ? Qt.PointingHandCursor : Qt.ArrowCursor
        onPositionChanged: (mouse) => {
            tooltipBox.hoverPoint = Qt.point(mouse.x, mouse.y)
            chart.hoverText = chart.tooltipAt(mouse.x, mouse.y)
        }
        onExited: chart.hoverText = ""
        onClicked: (mouse) => {
            const id = chart.idAt(mouse.x, mouse.y)
            if (id !== "") chart.activated(id)
        }
    }

    Rectangle {
        id: tooltipBox
        objectName: "tooltip"
        visible: chart.hoverText !== ""
        z: 10
        x: Math.min(chart.width - width, Math.max(0, hoverPoint.x + 12))
        y: Math.max(0, hoverPoint.y - height - 8)
        width: Math.min(320, tipText.implicitWidth + 2 * SuiTheme.spaceSm)
        height: tipText.implicitHeight + SuiTheme.spaceSm
        radius: SuiTheme.radiusSm
        color: SuiTheme.popover
        border.color: SuiTheme.borderStrong
        property point hoverPoint: Qt.point(0, 0)
        Text {
            id: tipText
            textFormat: Text.PlainText
            anchors.centerIn: parent
            width: Math.min(304, implicitWidth)
            wrapMode: Text.Wrap
            text: chart.hoverText
            color: SuiTheme.text
            font.pixelSize: SuiTheme.fontSizeSm
        }
    }
}
