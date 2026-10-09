import React from "react";
import { midiHz, NOTE_NAMES } from "./chartUtils.js";

// The two standalone small renderers: the scorecard sparkline (note-core tuning by
// year) and the take-inspector f0 contour. Ported from sparkline + openTake's contour.

export function SparklineChart({ perYear, valueKey }) {
  const years = Object.keys(perYear).sort();
  const values = years.map((year) => perYear[year][valueKey]).filter((v) => v !== null && v !== undefined);
  if (values.length < 2) return null;
  const w = 130;
  const h = 26;
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (hi - lo < 0.001) {
    lo -= 1;
    hi += 1;
  }
  const points = [];
  let plotted = 0;
  for (const year of years) {
    const v = perYear[year][valueKey];
    if (v === null || v === undefined) continue;
    const x = 4 + (plotted / (values.length - 1)) * (w - 8);
    const y = 3 + (1 - (v - lo) / (hi - lo)) * (h - 6);
    points.push(`${x.toFixed(1)},${y.toFixed(1)}`);
    plotted += 1;
  }
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label="note-core tuning error by year">
      <polyline points={points.join(" ")} fill="none" stroke="#d9ad45" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function F0ContourChart({ contour }) {
  if (!contour || contour.length < 2) return null;
  const W = 700;
  const H = 190;
  const padL = 44;
  const padR = 12;
  const padT = 10;
  const padB = 22;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const times = contour.map((p) => p[0]);
  const freqs = contour.map((p) => p[1]);
  const tHi = Math.max(...times);
  const fLo = Math.min(...freqs) * 0.94;
  const fHi = Math.max(...freqs) * 1.06;
  const logLo = Math.log2(fLo);
  const logHi = Math.log2(fHi);
  const gridNotes = [];
  for (let midi = Math.ceil(69 + 12 * Math.log2(fLo / 440)); midi <= Math.floor(69 + 12 * Math.log2(fHi / 440)); midi += 1) {
    const name = NOTE_NAMES[((midi % 12) + 12) % 12];
    if (name !== "C" && name !== "G") continue;
    gridNotes.push({ midi, label: name + (Math.floor(midi / 12) - 1) });
  }
  const y = (hz) => padT + plotH - ((Math.log2(hz) - logLo) / (logHi - logLo)) * plotH;
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="f0 contour of this take">
      {gridNotes.map((note) => (
        <React.Fragment key={note.midi}>
          <line x1={padL} x2={W - padR} y1={y(midiHz(note.midi))} y2={y(midiHz(note.midi))} stroke="#242b38" strokeWidth={1} />
          <text x={padL - 6} y={y(midiHz(note.midi)) + 3} textAnchor="end" className="vj-notelabel">{note.label}</text>
        </React.Fragment>
      ))}
      {contour.map((p, i) => (
        <circle key={i} cx={padL + (p[0] / tHi) * plotW} cy={y(p[1])} r={1.6} fill="#d9ad45" />
      ))}
      <text x={W - padR} y={H - 6} textAnchor="end">{Math.round(tHi)} s</text>
    </svg>
  );
}

export function BradleyTerryChart({ years }) {
  if (years.length < 2) return null;
  const W = Math.max(120 + years.length * 80, 420);
  const H = 220;
  const padL = 56;
  const padR = 20;
  const padT = 14;
  const padB = 34;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const values = years.map((row) => row.strengthLog2);
  let lo = Math.min(...values, 0);
  let hi = Math.max(...values, 0);
  const pad = Math.max((hi - lo) * 0.15, 0.2);
  lo -= pad;
  hi += pad;
  const y = (v) => padT + plotH - ((v - lo) / (hi - lo)) * plotH;
  const x = (i) => padL + (years.length === 1 ? plotW / 2 : (i / (years.length - 1)) * plotW);
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Perceived quality by year, Bradley-Terry log2 strengths">
      <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)} stroke="#3a4254" strokeWidth={1} />
      <text x={padL - 8} y={y(0) + 3} textAnchor="end">0</text>
      <polyline
        points={years.map((row, i) => `${x(i)},${y(row.strengthLog2)}`).join(" ")}
        fill="none"
        stroke="#8fd9a0"
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {years.map((row, i) => (
        <React.Fragment key={row.year}>
          <circle cx={x(i)} cy={y(row.strengthLog2)} r={Math.max(3, Math.min(7, 2 + Math.sqrt(row.trials)))} fill="#8fd9a0" stroke="#151a24" strokeWidth={2} />
          <text x={x(i)} y={H - 10} textAnchor="middle">{row.year}</text>
          <text x={x(i)} y={y(row.strengthLog2) - 12} textAnchor="middle">{`${row.strengthLog2 >= 0 ? "+" : ""}${row.strengthLog2}`}</text>
        </React.Fragment>
      ))}
    </svg>
  );
}
