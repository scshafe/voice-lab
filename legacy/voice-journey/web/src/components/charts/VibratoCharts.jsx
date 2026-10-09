import React from "react";
import { domainOf, linearY, quarterMidIdx, quarterSeries, yearColor } from "./chartUtils.js";
import { AxisTicks, BandAndLine, Baseline, PanelTitle } from "./chartPrimitives.jsx";
import { tipHandlers, useChartTooltip } from "./ChartTooltip.jsx";

// The Vibrato Story — the quarterly triptych (rate / extent / time-share with IQR
// bands and the settled 5–6 Hz zone) and the per-take fingerprint scatter.
// Ported from journeyHtml()'s renderTriptych + renderFingerprint.

export function VibratoTriptychChart({ quarters }) {
  const tooltip = useChartTooltip();
  if (!quarters.length) return null;
  const minI = quarterMidIdx(quarters[0].quarter) - 1;
  const maxI = quarterMidIdx(quarters[quarters.length - 1].quarter) + 1;
  const mw = 12;
  const padL = 50;
  const padR = 90;
  const W = padL + padR + (maxI - minI + 1) * mw;
  const specs = [
    { title: "rate (Hz)", key: "rate", pick: (q) => q.vibrato.rateHz, fmt: (v) => v.toFixed(1), zone: true, scale: 1 },
    { title: "extent (cents)", key: "extent", pick: (q) => q.vibrato.extentCents, fmt: (v) => `${Math.round(v)}c`, zone: false, scale: 1 },
    { title: "share of sustained time", key: "timeShare", pick: (q) => (q.vibrato.timeShare === null ? null : q.vibrato.timeShare * 100), fmt: (v) => `${Math.round(v)}%`, zone: false, scale: 100 }
  ];
  const panelH = 84;
  const gap = 34;
  const top0 = 22;
  const H = top0 + specs.length * (panelH + gap);

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Vibrato rate, extent, and time share per quarter with interquartile bands">
      {specs.map((spec, index) => {
        const top = top0 + index * (panelH + gap);
        const values = quarters.map(spec.pick);
        const spreadValues = [
          ...quarters.map((q) => (q.spread?.[spec.key]?.[0] === null || q.spread?.[spec.key]?.[0] === undefined ? null : q.spread[spec.key][0] * spec.scale)),
          ...quarters.map((q) => (q.spread?.[spec.key]?.[1] === null || q.spread?.[spec.key]?.[1] === undefined ? null : q.spread[spec.key][1] * spec.scale))
        ];
        const domain = domainOf([...values, ...spreadValues, ...(spec.zone ? [6.4] : [])], 0.12, 0);
        const series = quarterSeries(quarters, minI, mw, padL, spec.pick, spec.key, domain[0], domain[1], top, panelH, spec.scale);
        const zoneTop = spec.zone ? linearY(6, domain[0], domain[1], top, panelH) : null;
        const zoneBottom = spec.zone ? linearY(5, domain[0], domain[1], top, panelH) : null;
        return (
          <React.Fragment key={spec.key}>
            <PanelTitle x={padL} y={top - 8}>{spec.title}</PanelTitle>
            <Baseline x1={padL} x2={W - padR} y={top + panelH} />
            {spec.zone ? (
              <>
                <rect x={padL} y={zoneTop} width={W - padL - padR} height={zoneBottom - zoneTop} fill="rgb(91 142 222 / 0.12)" />
                <text x={W - padR + 6} y={(zoneTop + zoneBottom) / 2 + 3}>settled 5–6 Hz</text>
              </>
            ) : null}
            <BandAndLine points={series} color="#d9ad45" />
            {series.map((p) => (p.y === null ? null : (
              <circle
                key={p.q.quarter}
                cx={p.x}
                cy={p.y}
                r={3}
                fill="#d9ad45"
                stroke="#151a24"
                strokeWidth={2}
                {...tipHandlers(tooltip, () => <><b>{p.q.quarter}</b> · {spec.fmt(p.value)} · {p.q.nReliable} takes</>)}
              />
            )))}
            <AxisTicks xLeft={padL} lo={domain[0]} hi={domain[1]} top={top} height={panelH} format={spec.fmt} />
          </React.Fragment>
        );
      })}
      {quarters.map((q) => (!q.quarter.endsWith("Q1") ? null : (
        <text key={`yr:${q.quarter}`} x={padL + (quarterMidIdx(q.quarter) - minI) * mw + mw / 2} y={H - 10} textAnchor="middle">
          {q.quarter.slice(0, 4)}
        </text>
      )))}
    </svg>
  );
}

const FINGERPRINT = { W: 540, H: 330, padL: 46, padR: 16, padT: 14, padB: 36 };

export function VibratoFingerprintChart({ takes, yearSpan, onOpenTake }) {
  const tooltip = useChartTooltip();
  if (!takes.length) return null;
  const { W, H, padL, padR, padT, padB } = FINGERPRINT;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const xHi = Math.min(150, Math.max(...takes.map((t) => t.extent)) * 1.04);
  const yLo = 2.5;
  const yHi = 8.5;
  const zoneTop = linearY(6, yLo, yHi, padT, plotH);
  const zoneBottom = linearY(5, yLo, yHi, padT, plotH);
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Scatter of vibrato extent versus rate for every take, colored by year">
      <rect x={padL} y={zoneTop} width={plotW} height={zoneBottom - zoneTop} fill="rgb(91 142 222 / 0.10)" />
      <Baseline x1={padL} x2={padL + plotW} y={padT + plotH} />
      {[0, 50, 100, 150].map((v) => (v > xHi ? null : (
        <text key={v} x={padL + (v / xHi) * plotW} y={H - 18} textAnchor="middle" className="vj-notelabel">{v}</text>
      )))}
      {[3, 4, 5, 6, 7, 8].map((v) => (
        <React.Fragment key={v}>
          <line x1={padL} x2={padL + plotW} y1={linearY(v, yLo, yHi, padT, plotH)} y2={linearY(v, yLo, yHi, padT, plotH)} stroke="#242b38" strokeWidth={1} />
          <text x={padL - 6} y={linearY(v, yLo, yHi, padT, plotH) + 3} textAnchor="end" className="vj-notelabel">{v}</text>
        </React.Fragment>
      ))}
      <text x={padL + plotW / 2} y={H - 4} textAnchor="middle">extent (cents)</text>
      <text x={10} y={padT + 8}>rate (Hz)</text>
      {takes.map((t) => {
        if (t.rate < yLo || t.rate > yHi || t.extent > xHi) return null;
        const color = yearColor(t.year, yearSpan.min, yearSpan.max);
        const shape = t.reliable
          ? { fill: color, fillOpacity: 0.8 }
          : { fill: "none", stroke: color, strokeWidth: 1.4 };
        return (
          <circle
            key={t.id}
            cx={padL + (t.extent / xHi) * plotW}
            cy={linearY(t.rate, yLo, yHi, padT, plotH)}
            r={3.2}
            style={{ cursor: "pointer" }}
            {...shape}
            {...tipHandlers(tooltip, () => <><b>{t.date}</b> · {t.rate} Hz · {Math.round(t.extent)}c{t.reliable ? "" : " · flagged"}<br />click to inspect</>)}
            onClick={() => onOpenTake(t.id)}
          />
        );
      })}
    </svg>
  );
}

export function YearLegend({ years, yearSpan }) {
  return (
    <div className="vj-yearlegend">
      {years.map((year) => (
        <span key={year}>
          <span className="vj-swatch" style={{ background: yearColor(year, yearSpan.min, yearSpan.max) }} />
          {year}
        </span>
      ))}
    </div>
  );
}
