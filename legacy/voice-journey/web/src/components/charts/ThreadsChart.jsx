import React from "react";
import { monthIdx } from "./chartUtils.js";
import { tipHandlers, useChartTooltip } from "./ChartTooltip.jsx";

// Repertoire Threads — the biggest recurring song clusters as timeline threads,
// dot area = takes that year, click-through to the Corpus table pre-filtered to the
// cluster. Labels stay privacy-generic ("Song A"). Ported from renderThreads.
export function ThreadsChart({ threads, onOpenCluster }) {
  const tooltip = useChartTooltip();
  if (!threads.length) return null;
  const minI = Math.min(...threads.map((t) => monthIdx(t.first))) - 2;
  const maxI = Math.max(...threads.map((t) => monthIdx(t.last))) + 2;
  const mw = Math.max(760 / (maxI - minI + 1), 6);
  const padL = 78;
  const padR = 150;
  const rowH = 40;
  const padT = 26;
  const W = padL + padR + (maxI - minI + 1) * mw;
  const H = padT + threads.length * rowH + 12;

  const yearLines = [];
  for (let year = Math.ceil(minI / 12); year * 12 <= maxI; year += 1) {
    yearLines.push(year);
  }

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Recurring song clusters as timeline threads with takes per year">
      {yearLines.map((year) => (
        <React.Fragment key={year}>
          <line x1={padL + (year * 12 - minI) * mw} x2={padL + (year * 12 - minI) * mw} y1={padT - 8} y2={H - 8} stroke="#242b38" strokeWidth={1} />
          <text x={padL + (year * 12 - minI) * mw} y={padT - 12} textAnchor="middle">{year}</text>
        </React.Fragment>
      ))}
      {threads.map((thread, index) => {
        const cy = padT + index * rowH + rowH / 2;
        const songLabel = `Song ${String.fromCharCode(65 + index)}`;
        return (
          <React.Fragment key={thread.id}>
            <text x={padL - 10} y={cy + 4} textAnchor="end" className="vj-notelabel">{songLabel}</text>
            <line
              x1={padL + (monthIdx(thread.first) - minI) * mw}
              x2={padL + (monthIdx(thread.last) - minI) * mw}
              y1={cy}
              y2={cy}
              stroke="#3a4254"
              strokeWidth={2}
              strokeLinecap="round"
            />
            {Object.keys(thread.perYear).sort().map((year) => {
              const count = thread.perYear[year];
              return (
                <circle
                  key={year}
                  cx={padL + ((Number(year) * 12 + 5.5) - minI) * mw}
                  cy={cy}
                  r={Math.max(3.5, Math.sqrt(count) * 1.9)}
                  fill="#d9ad45"
                  stroke="#151a24"
                  strokeWidth={2}
                  style={{ cursor: "pointer" }}
                  {...tipHandlers(tooltip, () => <><b>{songLabel}</b> · {year} · {count} take{count === 1 ? "" : "s"}<br />click to open in Corpus</>)}
                  onClick={() => onOpenCluster(thread.id)}
                />
              );
            })}
            <text x={W - padR + 10} y={cy + 4} className="vj-notelabel">
              {thread.size} takes · {thread.first.slice(0, 4)}–{thread.last.slice(2, 4)}
            </text>
          </React.Fragment>
        );
      })}
    </svg>
  );
}
