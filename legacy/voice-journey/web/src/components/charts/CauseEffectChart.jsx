import React from "react";
import { domainOf, linearY, monthIdx, monthShort, quarterSeries } from "./chartUtils.js";
import { AxisTicks, BandAndLine, Baseline, PanelTitle } from "./chartPrimitives.jsx";
import { tipHandlers, useChartTooltip } from "./ChartTooltip.jsx";

// Cause & Effect — monthly practice cadence over quarterly sustain + CPPS on one
// shared time axis, with the auto-derived high-practice eras as shaded bands.
// Ported from journeyHtml()'s renderCause (era listen buttons render page-side).

const MW = 10;
const PAD_L = 50;
const PAD_R = 18;
const CAD_TOP = 24;
const CAD_H = 104;
const SUS_TOP = CAD_TOP + CAD_H + 34;
const SUS_H = 82;
const CPP_TOP = SUS_TOP + SUS_H + 34;
const CPP_H = 82;

export function CauseEffectChart({ months, quarters, eras }) {
  const tooltip = useChartTooltip();
  if (!months.length || !quarters.length) return null;

  const minI = monthIdx(months[0].month);
  const span = monthIdx(months[months.length - 1].month) - minI + 1;
  const W = PAD_L + PAD_R + span * MW;
  const H = CPP_TOP + CPP_H + 30;
  const maxCount = Math.max(...months.map((m) => m.count));

  const panels = [
    { top: SUS_TOP, height: SUS_H, key: "sustain", pick: (q) => q.sustainSeconds, fmt: (v) => `${v.toFixed(1)}s`, color: "#8fb3e8", title: "longest sustained note (s, quarterly median)" },
    { top: CPP_TOP, height: CPP_H, key: "cpps", pick: (q) => q.cpps, fmt: (v) => v.toFixed(1), color: "#d9ad45", title: "voice clarity — CPPS (dB, quarterly median)" }
  ];

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Practice cadence, longest sustained note, and CPPS voice clarity on one shared time axis">
      {(eras ?? []).map((era) => {
        const x1 = PAD_L + (monthIdx(era.start) - minI) * MW;
        const x2 = PAD_L + (monthIdx(era.end) - minI + 1) * MW;
        return <rect key={era.start} x={x1} y={CAD_TOP} width={x2 - x1} height={CPP_TOP + CPP_H - CAD_TOP} fill="rgb(217 173 69 / 0.07)" />;
      })}
      <PanelTitle x={PAD_L} y={CAD_TOP - 8}>recordings per month</PanelTitle>
      <Baseline x1={PAD_L} x2={W - PAD_R} y={CAD_TOP + CAD_H} />
      {months.map((entry) => {
        const h = (entry.count / maxCount) * CAD_H;
        if (h <= 0) return null;
        const x = PAD_L + (monthIdx(entry.month) - minI) * MW;
        return (
          <rect
            key={entry.month}
            x={x + 1}
            y={CAD_TOP + CAD_H - h}
            width={MW - 2}
            height={h}
            fill="#d9ad45"
            opacity={0.75}
            {...tipHandlers(tooltip, () => <><b>{monthShort(entry.month)} {entry.month.slice(0, 4)}</b> · {entry.count} recordings</>)}
          />
        );
      })}
      <AxisTicks xLeft={PAD_L} lo={0} hi={maxCount} top={CAD_TOP} height={CAD_H} format={(v) => String(Math.round(v))} />
      {panels.map((panel) => {
        const domain = domainOf(
          [
            ...quarters.map(panel.pick),
            ...quarters.map((q) => q.spread?.[panel.key]?.[0] ?? null),
            ...quarters.map((q) => q.spread?.[panel.key]?.[1] ?? null)
          ],
          0.12,
          0
        );
        const series = quarterSeries(quarters, minI, MW, PAD_L, panel.pick, panel.key, domain[0], domain[1], panel.top, panel.height);
        return (
          <React.Fragment key={panel.key}>
            <PanelTitle x={PAD_L} y={panel.top - 8}>{panel.title}</PanelTitle>
            <Baseline x1={PAD_L} x2={W - PAD_R} y={panel.top + panel.height} />
            <BandAndLine points={series} color={panel.color} />
            {series.map((p) => (p.y === null ? null : (
              <circle
                key={p.q.quarter}
                cx={p.x}
                cy={p.y}
                r={3}
                fill={panel.color}
                stroke="#151a24"
                strokeWidth={2}
                {...tipHandlers(tooltip, () => <><b>{p.q.quarter}</b> · {panel.fmt(p.value)} · {p.q.nReliable} takes</>)}
              />
            )))}
            <AxisTicks xLeft={PAD_L} lo={domain[0]} hi={domain[1]} top={panel.top} height={panel.height} format={panel.fmt} />
          </React.Fragment>
        );
      })}
      {months.map((entry) => (entry.month.slice(5, 7) !== "01" ? null : (
        <text key={`yr:${entry.month}`} x={PAD_L + (monthIdx(entry.month) - minI) * MW} y={H - 8} textAnchor="start">
          {entry.month.slice(0, 4)}
        </text>
      )))}
    </svg>
  );
}

export function CauseEffectTable({ quarters }) {
  return (
    <table>
      <thead>
        <tr><th>Quarter</th><th>Takes</th><th>Sustain s</th><th>CPPS</th><th>HNR</th></tr>
      </thead>
      <tbody>
        {quarters.map((q) => (
          <tr key={q.quarter}>
            <td>{q.quarter}</td>
            <td>{q.nReliable}</td>
            <td>{q.sustainSeconds ?? ""}</td>
            <td>{q.cpps ?? ""}</td>
            <td>{q.hnr ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
