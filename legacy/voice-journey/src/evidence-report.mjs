#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { bradleyTerryByYear, buildVoiceTrends, readRefereeState } from "./corpus-browser.mjs";
import { resolvePaths } from "./paths.mjs";

const VJ = resolvePaths();
const DEFAULT_RESULTS = VJ.manifest("full-corpus-filter-results.json");
const DEFAULT_FEATURES = VJ.manifest("local-voice-features.json");
const DEFAULT_LYRICS = VJ.manifest("local-lyric-indicators.json");
const DEFAULT_JOURNEYS = VJ.manifest("song-journeys.json");
const DEFAULT_REFEREE_STATE = VJ.artifact("referee-state.json");
const DEFAULT_OUT = VJ.artifact("evidence-report.html");
const MIN_JUDGMENTS_FOR_EAR_VERDICT = 30;

class EvidenceReportError extends Error {}

function parseArgs(argv) {
  const [maybeCommand, ...rest] = argv;
  const hasCommand = maybeCommand && !maybeCommand.startsWith("--") && maybeCommand !== "help";
  const command = hasCommand ? maybeCommand : "generate";
  const args = hasCommand ? rest : argv;
  const options = {
    command,
    dryRun: false,
    features: DEFAULT_FEATURES,
    generatedAt: null,
    journeys: DEFAULT_JOURNEYS,
    lyrics: DEFAULT_LYRICS,
    out: DEFAULT_OUT,
    refereeState: DEFAULT_REFEREE_STATE,
    results: DEFAULT_RESULTS,
  };
  while (args.length > 0) {
    const next = args.shift();
    if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--features") {
      options.features = requireValue(args, next);
    } else if (next === "--generated-at") {
      options.generatedAt = requireValue(args, next);
    } else if (next === "--journeys") {
      options.journeys = requireValue(args, next);
    } else if (next === "--lyrics") {
      options.lyrics = requireValue(args, next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
    } else if (next === "--referee-state") {
      options.refereeState = requireValue(args, next);
    } else if (next === "--results") {
      options.results = requireValue(args, next);
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new EvidenceReportError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new EvidenceReportError(`${flag} requires a value`);
  return value;
}

function printHelp() {
  process.stdout.write(`Voice Journey evidence report generator.

Usage:
  voice-journey-report generate --dry-run
  voice-journey-report generate [--results PATH] [--features PATH] [--lyrics PATH] [--journeys PATH] [--referee-state PATH] [--out PATH] [--generated-at ISO]

Renders the regenerable "did I get better" document from the committed
manifests plus the optional local referee judgments: executive verdict,
same-song improvement index, perceived-quality curve, yearly transformation
charts, note-core tuning, song scorecards, limitations, and method appendix.
Self-contained static HTML (inline SVG, no scripts); numeric aggregates and
generic labels only. Default output is gitignored (repo = code + manifests).
`);
}

async function readJson(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}

async function readOptionalJson(filePath) {
  try {
    return await readJson(filePath);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function roundTo(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
}

function medianOf(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((left, right) => left - right);
  if (!sorted.length) return null;
  return sorted[Math.floor((sorted.length - 1) * 0.5)];
}

const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function noteName(hz) {
  if (!hz || hz <= 0) return "";
  const midi = Math.round(69 + 12 * Math.log2(hz / 440));
  return NOTE_NAMES[((midi % 12) + 12) % 12] + (Math.floor(midi / 12) - 1);
}

function escapeHtml(text) {
  return String(text).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

// --- static SVG builders (server-side, no client scripts) ---

function svgOpen(width, height, label) {
  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}">`;
}

function lineChartSvg({ label, points, width = 560, height = 190, format = (v) => String(v), color = "#d9ad45", zeroLine = false }) {
  const usable = points.filter((point) => point.value !== null && point.value !== undefined);
  if (usable.length < 2) return "";
  const padL = 56, padR = 18, padT = 14, padB = 30;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const values = usable.map((point) => point.value);
  let lo = Math.min(...values, zeroLine ? 0 : Infinity);
  let hi = Math.max(...values, zeroLine ? 0 : -Infinity);
  const pad = Math.max((hi - lo) * 0.15, 0.001);
  lo -= pad;
  hi += pad;
  const x = (index) => padL + (usable.length === 1 ? plotW / 2 : (index / (usable.length - 1)) * plotW);
  const y = (value) => padT + plotH - ((value - lo) / (hi - lo)) * plotH;
  let body = svgOpen(width, height, label);
  if (zeroLine && lo < 0 && hi > 0) {
    body += `<line x1="${padL}" x2="${width - padR}" y1="${y(0)}" y2="${y(0)}" stroke="#3a4254" stroke-width="1"/>`;
    body += `<text x="${padL - 8}" y="${y(0) + 3}" text-anchor="end">0</text>`;
  }
  body += `<line x1="${padL}" x2="${width - padR}" y1="${padT + plotH}" y2="${padT + plotH}" stroke="#3a4254" stroke-width="1"/>`;
  body += `<polyline points="${usable.map((point, index) => `${x(index)},${y(point.value)}`).join(" ")}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  usable.forEach((point, index) => {
    body += `<circle cx="${x(index)}" cy="${y(point.value)}" r="3" fill="${color}" stroke="#151a24" stroke-width="2"/>`;
    body += `<text x="${x(index)}" y="${height - 8}" text-anchor="middle">${escapeHtml(point.label)}</text>`;
    body += `<text x="${x(index)}" y="${y(point.value) - 10}" text-anchor="middle" class="num">${escapeHtml(format(point.value))}</text>`;
  });
  return `${body}</svg>`;
}

function columnsSvg({ label, columns, width = 760, height = 130, color = "#d9ad45" }) {
  if (!columns.length) return "";
  const padL = 8, padR = 8, padT = 8, padB = 22;
  const plotH = height - padT - padB;
  const slot = (width - padL - padR) / columns.length;
  const max = Math.max(...columns.map((column) => column.value), 1);
  let body = svgOpen(width, height, label);
  body += `<line x1="${padL}" x2="${width - padR}" y1="${padT + plotH}" y2="${padT + plotH}" stroke="#3a4254" stroke-width="1"/>`;
  columns.forEach((column, index) => {
    const h = (column.value / max) * plotH;
    if (h > 0) {
      body += `<rect x="${padL + index * slot + 1}" y="${padT + plotH - h}" width="${Math.max(slot - 2, 1)}" height="${h}" fill="${color}" opacity="0.75"/>`;
    }
    if (column.tick) {
      body += `<text x="${padL + index * slot}" y="${height - 6}" text-anchor="start">${escapeHtml(column.tick)}</text>`;
    }
  });
  return `${body}</svg>`;
}

function rangeBarsSvg({ label, years, width = 560, height = 210 }) {
  const usable = years.filter((year) => year.topHz);
  if (usable.length < 2) return "";
  const padL = 56, padR = 60, padT = 12, padB = 28;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const tops = usable.map((year) => year.topHz);
  const logLo = Math.log2(Math.min(...tops) * 0.85);
  const logHi = Math.log2(Math.max(...tops) * 1.08);
  const y = (hz) => padT + plotH - ((Math.log2(hz) - logLo) / (logHi - logLo)) * plotH;
  const slot = plotW / usable.length;
  let body = svgOpen(width, height, label);
  for (let midi = Math.ceil(69 + 12 * Math.log2(Math.pow(2, logLo) / 440)); midi <= Math.floor(69 + 12 * Math.log2(Math.pow(2, logHi) / 440)); midi += 1) {
    const name = NOTE_NAMES[((midi % 12) + 12) % 12];
    if (name !== "C" && name !== "G") continue;
    const hz = 440 * Math.pow(2, (midi - 69) / 12);
    body += `<line x1="${padL}" x2="${width - padR}" y1="${y(hz)}" y2="${y(hz)}" stroke="#242b38" stroke-width="1"/>`;
    body += `<text x="${padL - 8}" y="${y(hz) + 3}" text-anchor="end" class="num">${name}${Math.floor(midi / 12) - 1}</text>`;
  }
  usable.forEach((year, index) => {
    const cx = padL + index * slot + slot / 2;
    const top = y(year.topHz);
    body += `<line x1="${cx}" x2="${cx}" y1="${padT + plotH}" y2="${top}" stroke="#d9ad45" stroke-width="6" stroke-linecap="round" opacity="0.75"/>`;
    body += `<text x="${cx}" y="${top - 8}" text-anchor="middle" class="num">${escapeHtml(noteName(year.topHz))}</text>`;
    body += `<text x="${cx}" y="${height - 8}" text-anchor="middle">${escapeHtml(year.year)}</text>`;
  });
  return `${body}</svg>`;
}

// --- report assembly ---

function verdictChipHtml(verdict) {
  const labels = { improving: "improving", declining: "declining", flat: "flat", trending_up: "trending up", trending_down: "trending down", insufficient_data: "insufficient data" };
  return `<span class="chip ${escapeHtml(verdict)}">${escapeHtml(labels[verdict] ?? verdict)}</span>`;
}

function executiveVerdict({ trends, journeys, bt }) {
  const sentences = [];
  const workingTop = trends?.headline?.workingTop;
  if (workingTop) {
    sentences.push(`Between ${workingTop.baselineYear} and ${workingTop.peakYear} your working top expanded from ${noteName(workingTop.baselineTopHz)} to ${noteName(workingTop.peakTopHz)} — ${workingTop.gainSemitones >= 0 ? "+" : ""}${workingTop.gainSemitones} semitones of reach — and that territory has stayed accessible since.`);
  }
  if (journeys) {
    const index = journeys.improvementIndex ?? [];
    const improving = index.filter((dimension) => dimension.verdict === "improving").map((dimension) => dimension.label);
    const declining = index.filter((dimension) => dimension.verdict === "declining").map((dimension) => dimension.label);
    if (!improving.length && !declining.length) {
      sentences.push(`On unchanged material — ${journeys.totals?.clustersEligible ?? "the eligible"} recurring songs compared only against themselves — execution has held steady: no measured dimension moves once repertoire is controlled. The transformation this archive records is what you sing, more than how the same song is sung.`);
    } else {
      if (improving.length) sentences.push(`Same-song evidence shows improvement in ${improving.join(", ")}.`);
      if (declining.length) sentences.push(`Same-song evidence shows decline in ${declining.join(", ")}.`);
    }
  }
  if (bt && bt.judgedTotal >= MIN_JUDGMENTS_FOR_EAR_VERDICT && bt.years.length >= 2) {
    const latest = bt.years[bt.years.length - 1].strengthLog2;
    if (latest > 0.3) sentences.push(`Your blind ear disagrees with the flat metrics in the best way: across ${bt.judgedTotal} judgments, recent years win the listening test (log2 strength ${latest >= 0 ? "+" : ""}${latest} vs the earliest year).`);
    else if (latest < -0.3) sentences.push(`Across ${bt.judgedTotal} blind judgments, earlier years currently win the listening test — worth sitting with.`);
    else sentences.push(`Across ${bt.judgedTotal} blind judgments, your ear agrees with the metrics: no strong drift between eras.`);
  } else {
    sentences.push(`The blind listening experiment is still open — its verdict will be added here as judgments accumulate.`);
  }
  return sentences;
}

function buildReportModel({ resultsManifest, featuresManifest, lyricsManifest, journeysManifest, refereeState }) {
  const trends = featuresManifest ? buildVoiceTrends(featuresManifest, resultsManifest?.results ?? [], { lyricsManifest }) : null;
  const bt = refereeState ? bradleyTerryByYear(refereeState.judgments ?? []) : null;
  const noteCoreByYear = [];
  if (journeysManifest?.noteCore?.takes) {
    const byYear = new Map();
    for (const take of journeysManifest.noteCore.takes) {
      if (!byYear.has(take.year)) byYear.set(take.year, []);
      byYear.get(take.year).push(take.centErrorMedian);
    }
    for (const year of [...byYear.keys()].sort()) {
      noteCoreByYear.push({ year, centError: roundTo(medianOf(byYear.get(year)), 1), n: byYear.get(year).length });
    }
  }
  const buckets = {};
  for (const row of resultsManifest?.results ?? []) {
    const bucket = row.classification?.finalBucket ?? "unclassified";
    buckets[bucket] = (buckets[bucket] ?? 0) + 1;
  }
  return { trends, bt, noteCoreByYear, buckets };
}

function renderReport({ options, generatedAt, model, journeysManifest, sources }) {
  const { trends, bt, noteCoreByYear, buckets } = model;
  const headline = trends?.headline ?? null;
  const sentences = executiveVerdict({ trends, journeys: journeysManifest, bt });

  const statTiles = [];
  if (headline) {
    statTiles.push([String(headline.takesAnalyzed), "takes measured"]);
    statTiles.push([`${headline.hoursAnalyzed} h`, "singing analyzed"]);
    if (headline.workingTop) statTiles.push([`+${headline.workingTop.gainSemitones} st`, `working top, ${headline.workingTop.baselineYear} → ${headline.workingTop.peakYear}`]);
    statTiles.push([String(headline.reliableTakes), "reliable takes"]);
  }
  statTiles.push([String((buckets.clean_singing ?? 0) + (buckets.noise_contaminated_singing ?? 0) + (buckets.music_contaminated_singing ?? 0)), "classified singing"]);

  const indexRows = (journeysManifest?.improvementIndex ?? []).map((dimension) => `
      <div class="vrow"><span>${escapeHtml(dimension.label)}</span>
        <span class="num">${dimension.medianSlopePerYear === null ? "—" : `${dimension.medianSlopePerYear}/yr`}</span>
        <span class="num"><small>CI</small> ${dimension.ci95 ? `${dimension.ci95[0]} … ${dimension.ci95[1]}` : "—"}</span>
        <span><small>${dimension.clustersUsed} songs</small></span>
        ${verdictChipHtml(dimension.verdict)}</div>`).join("");

  const songRows = (journeysManifest?.journeys ?? [])
    .slice()
    .sort((left, right) => right.takesUsed - left.takesUsed)
    .slice(0, 8)
    .map((journey) => {
      const years = Object.keys(journey.perYear).sort();
      const first = journey.perYear[years[0]];
      const last = journey.perYear[years[years.length - 1]];
      return `<tr><td>${escapeHtml(journey.clusterId)}</td><td class="num">${journey.takesUsed}</td><td class="num">${escapeHtml(journey.firstYear)}–${escapeHtml(journey.lastYear)}</td><td class="num">${first?.noteCoreCentError ?? "—"} → ${last?.noteCoreCentError ?? "—"}</td><td class="num">${journey.slopesPerYear.noteCoreCentError ?? "—"}</td><td class="num">${journey.slopesPerYear.cpps ?? "—"}</td></tr>`;
    })
    .join("");

  const btSection = bt && bt.judgedTotal > 0
    ? `<p>${bt.judgedTotal} blind judgments so far (${bt.byChoice.a + bt.byChoice.b} decisive, ${bt.byChoice.too_close} too close, ${bt.byChoice.not_same_song} not-same-song flags).</p>
       ${lineChartSvg({ label: "Perceived quality by year", points: bt.years.map((row) => ({ label: row.year, value: row.strengthLog2 })), format: (v) => (v >= 0 ? `+${v}` : String(v)), color: "#8fd9a0", zeroLine: true })}
       ${bt.notSameSongFlags.length ? `<p class="note">Chained-cluster flags from listening: ${bt.notSameSongFlags.map((flag) => `${escapeHtml(flag.clusterId)} ×${flag.count}`).join(", ")}.</p>` : ""}`
    : `<p class="note">No judgments yet. The Referee page serves ${trends ? "blind same-song pairs" : "the experiment"} whenever the browser runs with release-gated playback; this section regenerates as verdicts accumulate.</p>`;

  const quarterly = trends?.quarters ?? [];
  const sustainChart = lineChartSvg({ label: "Longest sustained note by quarter", points: quarterly.filter((q) => q.sustainSeconds !== null).map((q) => ({ label: q.quarter.endsWith("Q1") ? q.quarter.slice(0, 4) : "", value: q.sustainSeconds })), format: (v) => `${v}s`, width: 760 });
  const cppsChart = lineChartSvg({ label: "CPPS by quarter", points: quarterly.filter((q) => q.cpps !== null).map((q) => ({ label: q.quarter.endsWith("Q1") ? q.quarter.slice(0, 4) : "", value: q.cpps })), format: (v) => String(v), width: 760 });
  const cadenceChart = columnsSvg({
    label: "Recordings per month",
    columns: (trends?.monthlyCadence ?? []).map((entry) => ({ value: entry.count, tick: entry.month.endsWith("-01") ? entry.month.slice(0, 4) : null })),
  });
  const centChart = lineChartSvg({ label: "Note-core tuning error by year", points: noteCoreByYear.map((row) => ({ label: row.year, value: row.centError })), format: (v) => `${v}c`, color: "#8fd9a0" });

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Journey — Evidence Report</title>
<style>
  :root { color-scheme: dark; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  body { margin: 0; background: #10131a; color: #f5f7fb; }
  header { padding: 32px 24px; background: linear-gradient(135deg, #182033, #3d2c17); border-bottom: 1px solid #30384a; }
  h1 { margin: 0 0 6px; font-size: clamp(28px, 4vw, 42px); letter-spacing: -0.04em; }
  h2 { margin: 0 0 4px; font-size: 1.25rem; }
  .meta { color: #9aa5b8; font-size: 0.82rem; }
  main { padding: 24px; display: grid; gap: 20px; max-width: 900px; margin: 0 auto; }
  .card { background: #151a24; border: 1px solid #2a3141; border-radius: 12px; padding: 18px 20px; }
  .card p { color: #c5ccda; line-height: 1.55; }
  .card p.note { color: #9aa5b8; font-size: 0.85rem; }
  .lede { font-size: 1.02rem; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: 10px; margin-top: 14px; }
  .tile { background: rgb(255 255 255 / 0.05); border: 1px solid #30384a; border-radius: 10px; padding: 10px 14px; }
  .tile .v { font-size: 1.4rem; font-weight: 650; }
  .tile .l { font-size: 0.72rem; color: #aeb8ca; margin-top: 2px; }
  .vrow { display: grid; grid-template-columns: minmax(14rem, 2fr) minmax(6rem, 1fr) minmax(9rem, 1fr) minmax(5rem, 1fr) auto; gap: 10px; align-items: center; background: rgb(255 255 255 / 0.03); border: 1px solid #2a3141; border-radius: 9px; padding: 8px 12px; font-size: 0.85rem; color: #c5ccda; margin-bottom: 6px; }
  .vrow small, .note small { color: #8b93a5; }
  .num { font-variant-numeric: tabular-nums; }
  .chip { display: inline-block; font-size: 0.72rem; font-weight: 650; padding: 3px 10px; border-radius: 99px; border: 1px solid #3a4254; }
  .chip.improving { color: #8fd9a0; border-color: #3d6a4a; }
  .chip.declining { color: #e08585; border-color: #6d4040; }
  .chip.flat, .chip.trending_up, .chip.trending_down { color: #aeb8ca; }
  table { border-collapse: collapse; font-size: 0.84rem; }
  th, td { border: 1px solid #2a3141; padding: 5px 10px; text-align: right; color: #c5ccda; }
  th { text-align: left; color: #aeb8ca; }
  td:first-child { text-align: left; }
  svg { max-width: 100%; height: auto; }
  svg text { font: 11px ui-sans-serif, system-ui, sans-serif; fill: #9aa5b8; }
  ul { color: #c5ccda; line-height: 1.55; }
  .scroller { overflow-x: auto; }
  @media print { body { background: #ffffff; } }
</style>
</head>
<body>
<header>
  <h1>Voice Journey — Evidence Report</h1>
  <div class="meta">Generated ${escapeHtml(generatedAt)} · ${escapeHtml(sources)} · regenerate any time with <code>npm run report</code></div>
</header>
<main>
  <section class="card">
    <h2>Did I get better, and in what ways?</h2>
    ${sentences.map((sentence) => `<p class="lede">${sentence}</p>`).join("\n    ")}
    <div class="stats">${statTiles.map(([value, label]) => `<div class="tile"><div class="v num">${escapeHtml(value)}</div><div class="l">${escapeHtml(label)}</div></div>`).join("")}</div>
  </section>

  <section class="card">
    <h2>The verdict — same-song controlled</h2>
    <p class="note">Slopes per year inside recurring songs (Theil–Sen per song; bootstrap 95% CI across songs). The strictest lens the archive allows.</p>
    ${indexRows || '<p class="note">Run <code>npm run journeys -- analyze</code> to populate this section.</p>'}
  </section>

  <section class="card">
    <h2>The listening test — your ear as referee</h2>
    ${btSection}
  </section>

  <section class="card">
    <h2>The yearly transformation</h2>
    <p class="note">Uncontrolled for repertoire — this is the story of where your voice went, not same-song execution.</p>
    ${rangeBarsSvg({ label: "Working top by year (95th percentile of take tops)", years: trends?.yearlyTops ?? [] })}
    ${centChart}
    <div class="scroller">${sustainChart}</div>
    <div class="scroller">${cppsChart}</div>
    <div class="scroller">${cadenceChart}</div>
  </section>

  <section class="card">
    <h2>Song scorecards</h2>
    <div class="scroller"><table>
      <tr><th>Cluster</th><th>Takes</th><th>Span</th><th>Tuning error, first → last year (c)</th><th>Tuning slope /yr</th><th>CPPS slope /yr</th></tr>
      ${songRows || ""}
    </table></div>
  </section>

  <section class="card">
    <h2>Read honestly — limitations</h2>
    <ul>
      <li>Recording conditions drifted for seven years (phones, rooms, distance): HNR falls monotonically for that reason, absolute loudness is untrustworthy, and CPPS is the preferred clarity measure.</li>
      <li>Song clusters come from transcript similarity; some are chained across distinct songs. Not-same-song verdicts from the Referee accumulate as repair evidence; an acoustic verification pass is the planned fix.</li>
      <li>Note-core tuning is measured on stored 50 ms contours against each take's own inferred reference — robust to a-cappella drift, but its noise floor is a few cents; small true changes can hide inside it.</li>
      <li>Same-song eligibility (&ge;6 takes over &ge;1.5 years) favors songs you kept returning to; songs mastered-and-retired are underrepresented by construction.</li>
      <li>114 takes carry high f0-rejection diagnostics and are excluded from every aggregate here.</li>
    </ul>
  </section>

  <section class="card">
    <h2>Method appendix</h2>
    <ul>
      <li>Features: praat-parselmouth + librosa in a pinned local venv (Praat-ac f0, two-layer outlier suppression); versions and thresholds ride in each manifest's config fingerprint.</li>
      <li>Reliability rule: takes with total f0 frame rejection &le; 10% enter aggregates.</li>
      <li>Improvement index: ${escapeHtml(journeysManifest?.method?.slopes?.id ?? "theil_sen_per_cluster.v1")} with ${escapeHtml(String(journeysManifest?.method?.bootstrap?.resamples ?? 1000))} bootstrap resamples, seed ${escapeHtml(String(journeysManifest?.method?.bootstrap?.seed ?? "—"))}.</li>
      <li>Listening test: ${escapeHtml(bt?.method?.id ?? "bradley_terry_by_year.v1")}; ${escapeHtml(bt?.method?.ties ?? "ties as half-wins")}; ${escapeHtml(bt?.method?.regularization ?? "+0.25 pseudo-wins")}.</li>
      <li>Privacy: everything in this report is a numeric aggregate or generic label; no audio, lyric text, or transcript text — and nothing ever leaves the machine.</li>
    </ul>
  </section>
</main>
</body>
</html>`;
}

function buildReportDryRun(options, model, journeysManifest, refereeState) {
  return {
    dryRun: true,
    operation: "evidence-report-generation",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["filter-results-manifest", "voice-feature-manifest", "lyric-indicator-manifest", "song-journeys-manifest", "referee-state (optional, local)"],
      writes: ["evidence-report-html (gitignored by default)"],
    },
    outputPath: options.out,
    inputs: {
      trendsAvailable: Boolean(model.trends?.available),
      journeysAvailable: Boolean(journeysManifest),
      refereeJudgments: refereeState?.judgments?.length ?? 0,
      noteCoreYears: model.noteCoreByYear.length,
    },
    readScope: {
      committedManifests: true,
      localRefereeState: true,
      audioBytes: false,
      transcriptText: false,
      contourArrays: false,
      upload: false,
    },
  };
}

async function generateReport(options) {
  const [resultsManifest, featuresManifest, lyricsManifest, journeysManifest] = await Promise.all([
    readJson(options.results),
    readOptionalJson(options.features),
    readOptionalJson(options.lyrics),
    readOptionalJson(options.journeys),
  ]);
  const refereeState = await readRefereeState(options.refereeState);
  const model = buildReportModel({ resultsManifest, featuresManifest, lyricsManifest, journeysManifest, refereeState });
  if (options.dryRun) {
    process.stdout.write(`${JSON.stringify(buildReportDryRun(options, model, journeysManifest, refereeState), null, 2)}\n`);
    return null;
  }
  const generatedAt = options.generatedAt ?? new Date().toISOString();
  const sources = `${featuresManifest?.results?.length ?? 0} feature takes · ${journeysManifest?.journeys?.length ?? 0} song journeys · ${refereeState.judgments.length} listening judgments`;
  const html = renderReport({ options, generatedAt, model, journeysManifest, sources });
  await mkdir(path.dirname(options.out), { recursive: true });
  await writeFile(options.out, `${html}\n`, "utf8");
  process.stdout.write(`${options.out}\n`);
  return options.out;
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (!options.command || options.help) {
    printHelp();
    return 0;
  }
  if (options.command === "generate") {
    await generateReport(options);
    return 0;
  }
  throw new EvidenceReportError(`unsupported command: ${options.command}`);
}

async function main() {
  try {
    process.exitCode = await run();
  } catch (error) {
    if (error instanceof Error) {
      process.stderr.write(`error: ${error.message}\n`);
      process.exitCode = 1;
      return;
    }
    throw error;
  }
}

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  await main();
}

export {
  buildReportModel,
  executiveVerdict,
  generateReport,
  renderReport,
  run,
};
