import React from "react";
import { midiHz, noteName, pct } from "./chartUtils.js";
import { tipHandlers, useChartTooltip } from "./ChartTooltip.jsx";

// The Range River — where the voice lives, quarter by quarter, on a log-frequency
// axis labeled in note names. Envelope (p05–p95) + typical (p25–p75) polygons with a
// median polyline, broken across sparse quarters; a register-share strip below;
// faint dots for sparse-quarter medians. Ported from journeyHtml()'s renderRiver.

const SLOT = 34;
const PLOT_L = 64;
const PLOT_R = 78;
const PLOT_T = 16;
const RIVER_H = 300;
const STRIP_GAP = 14;
const STRIP_H = 22;
const AXIS_H = 26;

export function RangeRiverChart({ quarters }) {
  const tooltip = useChartTooltip();
  if (!quarters.length) return null;

  const W = PLOT_L + PLOT_R + quarters.length * SLOT;
  const H = PLOT_T + RIVER_H + STRIP_GAP + STRIP_H + AXIS_H;
  const los = quarters.map((q) => q.envelope.loHz).filter(Boolean);
  const his = quarters.map((q) => q.envelope.hiHz).filter(Boolean);
  const yMin = Math.max(55, Math.min(...los) * 0.93);
  const yMax = Math.max(...his) * 1.07;
  const logMin = Math.log2(yMin);
  const logMax = Math.log2(yMax);
  const y = (hz) => PLOT_T + RIVER_H - ((Math.log2(hz) - logMin) / (logMax - logMin)) * RIVER_H;
  const x = (i) => PLOT_L + i * SLOT + SLOT / 2;

  const gridNotes = [];
  for (let midi = Math.ceil(69 + 12 * Math.log2(yMin / 440)); midi <= Math.floor(69 + 12 * Math.log2(yMax / 440)); midi += 1) {
    const name = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][((midi % 12) + 12) % 12];
    if (name !== "C" && name !== "G") continue;
    gridNotes.push({ midi, label: name + (Math.floor(midi / 12) - 1), hz: midiHz(midi) });
  }

  const segments = [];
  let current = null;
  quarters.forEach((q, i) => {
    const usable = q.nReliable >= 3 && q.envelope.loHz && q.envelope.hiHz && q.typical.loHz && q.typical.hiHz && q.medianHz;
    if (usable) {
      if (!current) {
        current = [];
        segments.push(current);
      }
      current.push({ q, i });
    } else {
      current = null;
    }
  });

  const stripTop = PLOT_T + RIVER_H + STRIP_GAP;
  const registerOrder = [["high", "#d9ad45"], ["mid", "#5b8ede"], ["low", "#55607a"]];
  let lastYear = "";

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Pitch range per quarter on a log-frequency axis labeled in note names">
      {gridNotes.map((note) => (
        <React.Fragment key={note.midi}>
          <line x1={PLOT_L} x2={W - PLOT_R} y1={y(note.hz)} y2={y(note.hz)} stroke="#242b38" strokeWidth={1} />
          <text x={PLOT_L - 8} y={y(note.hz) + 3} textAnchor="end" className="vj-notelabel">{note.label}</text>
          <text x={W - PLOT_R + 8} y={y(note.hz) + 3} className="vj-notelabel">{Math.round(note.hz)} Hz</text>
        </React.Fragment>
      ))}
      {segments.filter((segment) => segment.length >= 2).map((segment, si) => (
        <React.Fragment key={si}>
          <polygon
            points={[
              ...segment.map((item) => `${x(item.i)},${y(item.q.envelope.hiHz)}`),
              ...segment.slice().reverse().map((item) => `${x(item.i)},${y(item.q.envelope.loHz)}`)
            ].join(" ")}
            fill="rgb(217 173 69 / 0.14)"
          />
          <polygon
            points={[
              ...segment.map((item) => `${x(item.i)},${y(item.q.typical.hiHz)}`),
              ...segment.slice().reverse().map((item) => `${x(item.i)},${y(item.q.typical.loHz)}`)
            ].join(" ")}
            fill="rgb(217 173 69 / 0.32)"
          />
          <polyline
            points={segment.map((item) => `${x(item.i)},${y(item.q.medianHz)}`).join(" ")}
            fill="none"
            stroke="#d9ad45"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        </React.Fragment>
      ))}
      {quarters.map((q, i) => (q.nReliable >= 3 || !q.medianHz ? null : (
        <circle key={q.quarter} cx={x(i)} cy={y(q.medianHz)} r={3} fill="#d9ad45" opacity={0.35} />
      )))}
      {quarters.map((q, i) => {
        if (!q.registerShares) return null;
        const bw = Math.max(SLOT - 8, 4);
        const bx = x(i) - bw / 2;
        let offset = 0;
        return registerOrder.map(([key, color]) => {
          const share = q.registerShares[key] || 0;
          const h = share * STRIP_H;
          const rect = h > 0.5
            ? <rect key={`${q.quarter}:${key}`} x={bx} y={stripTop + offset} width={bw} height={Math.max(h - 1, 0.5)} fill={color} />
            : null;
          offset += h;
          return rect;
        });
      })}
      {quarters.map((q, i) => {
        const year = q.quarter.slice(0, 4);
        if (year === lastYear) return null;
        lastYear = year;
        return (
          <React.Fragment key={`yr:${year}`}>
            <text x={x(i)} y={H - 8} textAnchor="middle">{year}</text>
            <line x1={x(i) - SLOT / 2} x2={x(i) - SLOT / 2} y1={stripTop + STRIP_H} y2={stripTop + STRIP_H + 5} stroke="#3a4254" strokeWidth={1} />
          </React.Fragment>
        );
      })}
      {quarters.map((q, i) => (
        <rect
          key={`hit:${q.quarter}`}
          x={x(i) - SLOT / 2}
          y={PLOT_T}
          width={SLOT}
          height={RIVER_H + STRIP_GAP + STRIP_H}
          fill="transparent"
          {...tipHandlers(tooltip, () => (
            <>
              <b>{q.quarter}</b> · {q.nReliable} reliable of {q.n} takes<br />
              {q.envelope.loHz
                ? <>reach {noteName(q.envelope.loHz)}–{noteName(q.envelope.hiHz)} ({q.envelope.loHz}–{q.envelope.hiHz} Hz)</>
                : "sparse quarter"}
              {q.medianHz ? <><br />median {noteName(q.medianHz)} · high-register {pct(q.registerShares?.high)}</> : null}
            </>
          ))}
        />
      ))}
    </svg>
  );
}

export function RangeRiverTable({ quarters }) {
  return (
    <table>
      <thead>
        <tr><th>Quarter</th><th>Reliable</th><th>Total</th><th>Floor</th><th>Top</th><th>Median</th><th>High share</th></tr>
      </thead>
      <tbody>
        {quarters.map((q) => (
          <tr key={q.quarter}>
            <td>{q.quarter}</td>
            <td>{q.nReliable}</td>
            <td>{q.n}</td>
            <td>{q.envelope.loHz ? `${noteName(q.envelope.loHz)} (${q.envelope.loHz})` : ""}</td>
            <td>{q.envelope.hiHz ? `${noteName(q.envelope.hiHz)} (${q.envelope.hiHz})` : ""}</td>
            <td>{q.medianHz ? noteName(q.medianHz) : ""}</td>
            <td>{pct(q.registerShares?.high)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
