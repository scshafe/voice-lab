import React from "react";
import { linearY } from "./chartUtils.js";

// The small SVG fragments every panel shares — band+line series, panel titles,
// two-point axis ticks (the legacy renderers' bandAndLine/panelTitle/axisTicks).

export function BandAndLine({ points, color }) {
  const banded = points.filter((p) => p.lo !== null && p.hi !== null);
  const lined = points.filter((p) => p.y !== null);
  return (
    <>
      {banded.length >= 2 ? (
        <polygon
          points={[
            ...banded.map((p) => `${p.x},${p.yHi}`),
            ...banded.slice().reverse().map((p) => `${p.x},${p.yLo}`)
          ].join(" ")}
          fill={color}
          opacity={0.16}
        />
      ) : null}
      {lined.length >= 2 ? (
        <polyline
          points={lined.map((p) => `${p.x},${p.y}`).join(" ")}
          fill="none"
          stroke={color}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      ) : null}
    </>
  );
}

export function PanelTitle({ x, y, children }) {
  return <text x={x} y={y} className="vj-panel-title">{children}</text>;
}

export function AxisTicks({ xLeft, lo, hi, top, height, format }) {
  return (
    <>
      {[lo, hi].map((value, i) => (
        <text
          key={i}
          x={xLeft - 6}
          y={linearY(value, lo, hi, top, height) + (i === 0 ? 0 : 8)}
          textAnchor="end"
          className="vj-notelabel"
        >
          {format(value)}
        </text>
      ))}
    </>
  );
}

export function Baseline({ x1, x2, y }) {
  return <line x1={x1} x2={x2} y1={y} y2={y} stroke="#3a4254" strokeWidth={1} />;
}
