// Shared chart math for the Journey/Referee SVG components — ported from the inline
// journeyHtml() renderers (the geometry is the spec; only the rendering moved to React).

export const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
export const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function noteName(hz) {
  if (!hz || hz <= 0) return "";
  const midi = Math.round(69 + 12 * Math.log2(hz / 440));
  const name = NOTE_NAMES[((midi % 12) + 12) % 12];
  return name + (Math.floor(midi / 12) - 1);
}

export function midiHz(midi) {
  return 440 * 2 ** ((midi - 69) / 12);
}

export function monthIdx(month) {
  return Number(month.slice(0, 4)) * 12 + Number(month.slice(5, 7)) - 1;
}

export function monthShort(month) {
  return MONTH_NAMES[Number(month.slice(5, 7)) - 1];
}

export function quarterMidIdx(quarter) {
  return Number(quarter.slice(0, 4)) * 12 + (Number(quarter.slice(6, 7)) - 1) * 3 + 1;
}

export function eraLabel(era) {
  const y1 = era.start.slice(0, 4);
  const y2 = era.end.slice(0, 4);
  if (y1 === y2) return `${monthShort(era.start)}–${monthShort(era.end)} ${y1}`;
  return `${monthShort(era.start)} ${y1} – ${monthShort(era.end)} ${y2}`;
}

export function linearY(value, lo, hi, top, height) {
  return top + height - ((value - lo) / (hi - lo)) * height;
}

export function domainOf(values, padShare, floor) {
  const present = values.filter((value) => value !== null && value !== undefined);
  if (!present.length) return [0, 1];
  let lo = Math.min(...present);
  let hi = Math.max(...present);
  const pad = Math.max((hi - lo) * padShare, 0.001);
  lo -= pad;
  hi += pad;
  if (floor !== undefined) lo = Math.max(lo, floor);
  return [lo, hi];
}

export function pct(share) {
  return share === null || share === undefined ? "" : `${Math.round(share * 100)}%`;
}

// Year → brass gradient (older = darker), over the year span the quarters cover.
export function yearColor(year, minYear, maxYear) {
  const t = maxYear === minYear ? 1 : (Number(year) - minYear) / (maxYear - minYear);
  const lo = [107, 90, 43];
  const hi = [242, 208, 115];
  const rgb = lo.map((c, i) => Math.round(c + (hi[i] - c) * Math.max(0, Math.min(1, t))));
  return `rgb(${rgb.join(" ")})`;
}

export function quarterSeries(quarters, minI, mw, padL, pick, spreadKey, lo, hi, top, height, spreadScale = 1) {
  return quarters.map((q) => {
    const value = pick(q);
    const spread = q.spread?.[spreadKey] ?? [null, null];
    const sLo = spread[0] === null ? null : spread[0] * spreadScale;
    const sHi = spread[1] === null ? null : spread[1] * spreadScale;
    const x = padL + (quarterMidIdx(q.quarter) - minI) * mw + mw / 2;
    return {
      x,
      q,
      value,
      y: value === null || value === undefined ? null : linearY(value, lo, hi, top, height),
      lo: sLo,
      hi: sHi,
      yLo: sLo === null ? null : linearY(sLo, lo, hi, top, height),
      yHi: sHi === null ? null : linearY(sHi, lo, hi, top, height)
    };
  });
}
