import React from "react";
import { domainOf, quarterMidIdx, quarterSeries } from "./chartUtils.js";
import { AxisTicks, BandAndLine, Baseline, PanelTitle } from "./chartPrimitives.jsx";
import { tipHandlers, useChartTooltip } from "./ChartTooltip.jsx";

// The compact quarterly line panel behind What Not To Trust (CPPS / HNR) and the
// note-core tuning instrument. Ported from journeyHtml()'s miniLinePanel.
export function MiniLinePanel({ title, quarters, seriesKey, pick, color, fmt }) {
  const tooltip = useChartTooltip();
  if (!quarters.length) return null;
  const minI = quarterMidIdx(quarters[0].quarter) - 1;
  const maxI = quarterMidIdx(quarters[quarters.length - 1].quarter) + 1;
  const mw = Math.max(360 / (maxI - minI + 1), 4);
  const padL = 46;
  const padR = 14;
  const W = padL + padR + (maxI - minI + 1) * mw;
  const top = 22;
  const height = 110;
  const H = top + height + 26;
  const spreadValues = [
    ...quarters.map((q) => q.spread?.[seriesKey]?.[0] ?? null),
    ...quarters.map((q) => q.spread?.[seriesKey]?.[1] ?? null)
  ];
  const domain = domainOf([...quarters.map(pick), ...spreadValues], 0.12, 0);
  const series = quarterSeries(quarters, minI, mw, padL, pick, seriesKey, domain[0], domain[1], top, height);
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
      <PanelTitle x={padL} y={top - 8}>{title}</PanelTitle>
      <Baseline x1={padL} x2={W - padR} y={top + height} />
      <BandAndLine points={series} color={color} />
      {series.map((p) => (p.y === null ? null : (
        <circle
          key={p.q.quarter}
          cx={p.x}
          cy={p.y}
          r={2.6}
          fill={color}
          stroke="#151a24"
          strokeWidth={2}
          {...tipHandlers(tooltip, () => <><b>{p.q.quarter}</b> · {fmt(p.value)}</>)}
        />
      )))}
      <AxisTicks xLeft={padL} lo={domain[0]} hi={domain[1]} top={top} height={height} format={fmt} />
      {quarters.map((q) => (!q.quarter.endsWith("Q1") ? null : (
        <text key={`yr:${q.quarter}`} x={padL + (quarterMidIdx(q.quarter) - minI) * mw + mw / 2} y={H - 8} textAnchor="middle">
          {q.quarter.slice(2, 4)}
        </text>
      )))}
    </svg>
  );
}
