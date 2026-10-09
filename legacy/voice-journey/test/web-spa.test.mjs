// SPA render smoke — the web/ React app server-renders its three pages from a
// preloaded store (no network, no DOM). This is the coverage the legacy inline-page
// tests carried (section suite, gate notices, table axes), now against the real
// bundle graph: a broken import, selector, or render path fails here. esbuild
// resolves react/@scshafe/ui from web/node_modules; the test skips (loudly) if web/ has
// not been installed on this host.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const webRoot = path.join(repoRoot, "web");
const webInstalled = existsSync(path.join(webRoot, "node_modules", "react", "package.json"));

// The bundled app graph holds Node's event loop open once imported (a scheduler
// handle somewhere under react-dom/server), which wedges `node --test` after the
// assertions pass. So the render happens in a CHILD process that exits explicitly:
// this process builds the bundle once, the child imports + renders + prints + exits.
let bundlePromise = null;

async function bundleApp() {
  bundlePromise ??= (async () => {
    const { build } = await import(pathToFileURL(path.join(webRoot, "node_modules", "esbuild", "lib", "main.js")).href);
    // The entry must live under web/ so esbuild resolves react/@scshafe/ui from web/node_modules.
    const tmp = mkdtempSync(path.join(webRoot, ".spa-smoke-"));
    process.on("exit", () => rmSync(tmp, { recursive: true, force: true }));
    const entryPath = path.join(tmp, "entry.mjs");
    const outPath = path.join(tmp, "entry.bundle.mjs");
    const runnerPath = path.join(tmp, "runner.mjs");
    const storePath = path.join(webRoot, "src", "state", "StoreManager.js");
    const appPath = path.join(webRoot, "src", "AppComponent.jsx");
    writeFileSync(entryPath, `
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Provider } from "react-redux";
import { LocalPopoverProvider } from "@scshafe/ui";
import { createVoiceJourneyStore } from ${JSON.stringify(storePath)};
import { AppComponent } from ${JSON.stringify(appPath)};
export function renderApp(preloadedState) {
  const store = createVoiceJourneyStore({ preloadedState });
  return renderToStaticMarkup(
    React.createElement(Provider, { store },
      React.createElement(LocalPopoverProvider, null,
        React.createElement(AppComponent, null))));
}
`);
    writeFileSync(runnerPath, `
import { renderApp } from "./entry.bundle.mjs";
let data = "";
for await (const chunk of process.stdin) data += chunk;
const html = renderApp(JSON.parse(data));
process.stdout.write(JSON.stringify({ html }), () => process.exit(0));
`);
    await build({
      entryPoints: [entryPath],
      bundle: true,
      format: "esm",
      platform: "browser",
      absWorkingDir: webRoot,
      outfile: outPath,
      logLevel: "silent",
    });
    return runnerPath;
  })();
  const runnerPath = await bundlePromise;
  return {
    renderApp: (preloadedState) => new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [runnerPath], { stdio: ["pipe", "pipe", "pipe"] });
      let out = "";
      let err = "";
      child.stdout.on("data", (chunk) => { out += chunk; });
      child.stderr.on("data", (chunk) => { err += chunk; });
      child.on("close", (code) => {
        if (code !== 0) return reject(new Error(`render child exited ${code}: ${err.slice(0, 800)}`));
        try {
          resolve(JSON.parse(out).html);
        } catch (error) {
          reject(new Error(`render child produced unparseable output: ${out.slice(0, 200)}`));
        }
      });
      child.stdin.end(JSON.stringify(preloadedState));
    })
  };
}

function leanRow(recordingId, overrides = {}) {
  return {
    recordingId,
    filename: `${recordingId}.m4a`,
    capturedAt: "2021-03-01T20:00:00Z",
    year: "2021",
    durationSeconds: 42,
    bucket: "clean_singing",
    confidence: 0.9,
    spotCheckRecommended: false,
    spotCheckStatus: "not_reviewed",
    humanVerdict: null,
    transcriptStatus: "completed",
    transcriptHasWords: true,
    transcriptWordCount: 12,
    lyricMatchCluster: "lyric-1",
    lyricMatchClusterLabel: "cluster one",
    reviewQueueSelected: false,
    reviewQueueOrder: null,
    ...overrides,
  };
}

function quarterFixture(quarter, overrides = {}) {
  return {
    quarter,
    n: 12,
    nReliable: 9,
    envelope: { loHz: 110, hiHz: 392 },
    typical: { loHz: 150, hiHz: 300 },
    medianHz: 220,
    registerShares: { high: 0.3, mid: 0.5, low: 0.2 },
    sustainSeconds: 2.4,
    cpps: 11.2,
    hnr: 15.1,
    vibrato: { rateHz: 4.1, extentCents: 48, timeShare: 0.4 },
    spread: { sustain: [2.0, 2.9], cpps: [10.1, 12.0], hnr: [14, 16], rate: [3.8, 4.4], extent: [40, 55], timeShare: [0.3, 0.5], nc: [null, null] },
    ...overrides,
  };
}

const trendsFixture = {
  available: true,
  headline: {
    takesAnalyzed: 2145,
    hoursAnalyzed: 30.1,
    reliableTakes: 2031,
    flaggedTakes: 114,
    workingTop: { baselineYear: "2019", peakYear: "2021", baselineTopHz: 266, peakTopHz: 392, gainSemitones: 6.7 },
  },
  quarters: [quarterFixture("2020Q1"), quarterFixture("2020Q2"), quarterFixture("2021Q1"), quarterFixture("2021Q2")],
  quartersAllTakes: [quarterFixture("2020Q1"), quarterFixture("2020Q2"), quarterFixture("2021Q1"), quarterFixture("2021Q2")],
  monthlyCadence: [
    { month: "2020-01", count: 12 }, { month: "2020-02", count: 30 }, { month: "2021-01", count: 44 }, { month: "2021-02", count: 8 },
  ],
  eras: [{ start: "2021-01", end: "2021-02", totalTakes: 52, sampleRecordingIds: ["r1", "r2"] }],
  vibratoTakes: [
    { id: "r1", year: "2020", date: "2020-02-02", rate: 4.2, extent: 50, reliable: true },
    { id: "r2", year: "2021", date: "2021-01-15", rate: 5.4, extent: 62, reliable: false },
  ],
  clusters: [
    { id: "lyric-1", first: "2020-01", last: "2021-02", size: 24, perYear: { 2020: 10, 2021: 14 } },
  ],
};

const journeysFixture = {
  available: true,
  improvementIndex: [
    { key: "noteCoreCentError", label: "Note-core tuning error", verdict: "flat", medianSlopePerYear: -0.4, ci95: [-2.1, 1.4], clustersUsed: 40 },
    { key: "cpps", label: "Voice clarity (CPPS)", verdict: "flat", medianSlopePerYear: 0.05, ci95: [-0.2, 0.3], clustersUsed: 44 },
  ],
  totals: { clustersEligible: 46 },
  journeys: [
    {
      clusterId: "lyric-1",
      takesUsed: 24,
      firstYear: "2020",
      lastYear: "2021",
      perYear: { 2020: { noteCoreCentError: 19.2 }, 2021: { noteCoreCentError: 17.4 } },
      slopesPerYear: { noteCoreCentError: -1.8, cpps: 0.4 },
    },
  ],
  noteCore: {
    takes: [
      { quarter: "2020Q1", centErrorMedian: 18.4 }, { quarter: "2020Q1", centErrorMedian: 17.1 }, { quarter: "2020Q1", centErrorMedian: 19.9 },
      { quarter: "2021Q1", centErrorMedian: 16.8 }, { quarter: "2021Q1", centErrorMedian: 17.5 }, { quarter: "2021Q1", centErrorMedian: 18.2 },
    ],
  },
};

const summaryLoaded = {
  status: "loaded",
  error: null,
  data: {
    playback: { enabled: false },
    transcriptText: { enabled: false },
    featureDetail: { enabled: false },
    filterOptions: { buckets: ["clean_singing"], years: ["2021"], spotCheckStatuses: ["not_reviewed"], transcriptStatuses: ["completed"], lyricMatchStatuses: ["marked"] },
    reviewQueue: { selectedCount: 5 },
  },
};

test("web SPA renders the corpus table, filters, and sentinel from a preloaded store", { skip: !webInstalled && "web/node_modules not installed" }, async () => {
  const { renderApp } = await bundleApp();
  const html = await renderApp({
    NavigationManager: { view: "corpus" },
    SummaryManager: summaryLoaded,
    RowsManager: {
      entities: [leanRow("r1"), leanRow("r2", { bucket: "uncertain_manual_review", reviewQueueSelected: true, reviewQueueOrder: 3 })],
      hasMore: true,
      totalFiltered: 2145,
      meta: null,
      status: "loaded",
      error: null,
      requestVersion: 0,
      filters: { q: "", bucket: "", year: "", reviewQueue: "", spotCheckStatus: "", transcriptStatus: "", lyricMatchStatus: "" },
      sort: "capturedAt",
      direction: "asc",
    },
  });
  assert.match(html, /data-sui-component="PinnedDataTable"/);
  assert.match(html, /r1\.m4a/);
  assert.match(html, /r2\.m4a/);
  assert.match(html, /data-sui-component="InfiniteScrollSentinel"/);
  assert.match(html, /Showing 2 of 2,145 takes/);
  assert.match(html, /data-sui-component="SelectField"/);
  assert.match(html, /Spot-check sample/);
  assert.match(html, /data-sui-component="Badge"/);
});

test("web SPA renders each journey subject tab with its chart and education card", { skip: !webInstalled && "web/node_modules not installed" }, async () => {
  const { renderApp } = await bundleApp();
  const esc = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const subjects = [
    { subject: "verdict", h2: "The Verdict — same-song controlled", education: "median of all pairwise slopes", minSvg: 0, extra: [] },
    { subject: "range", h2: "The Range River", education: "one octave is a doubling of frequency", minSvg: 1, extra: [] },
    { subject: "practice", h2: "Cause &amp; Effect", education: "Correlation is not causation", minSvg: 1, extra: [] },
    { subject: "vibrato", h2: "The Vibrato Story", education: "settles between 5 and 7 Hz", minSvg: 2, extra: ["settled 5–6 Hz"] },
    { subject: "trust", h2: "What Not To Trust", education: "cepstral peak prominence", minSvg: 2, extra: ["note-core tuning error (cents) — now measured"] },
    { subject: "songs", h2: "Song Scorecards", education: "slopes computed on too few takes are mostly noise", minSvg: 1, extra: ["Song A"] },
    { subject: "threads", h2: "Repertoire Threads", education: "no lyric text ever leaves this machine", minSvg: 1, extra: [] },
  ];
  for (const entry of subjects) {
    const html = await renderApp({
      NavigationManager: { view: "journey" },
      SummaryManager: summaryLoaded,
      TrendsManager: { status: "loaded", error: null, data: trendsFixture },
      JourneyView: { includeFlagged: false, subject: entry.subject },
      JourneysManager: { status: "loaded", error: null, data: journeysFixture },
    });
    assert.match(html, /vj-subtabs/, `${entry.subject}: tab bar renders`);
    assert.match(html, /takes analyzed/, `${entry.subject}: stat band stays global`);
    assert.match(html, /\+6\.7 st/, `${entry.subject}: headline stat present`);
    assert.match(html, new RegExp(`<h2>${esc(entry.h2)}</h2>`), `${entry.subject}: section renders on its tab`);
    assert.match(html, new RegExp(esc(entry.education)), `${entry.subject}: education content renders under the chart`);
    for (const other of subjects) {
      if (other.subject === entry.subject) continue;
      assert.doesNotMatch(html, new RegExp(`<h2>${esc(other.h2)}</h2>`), `${entry.subject}: hides the "${other.h2}" section`);
    }
    for (const marker of entry.extra) {
      assert.match(html, new RegExp(esc(marker)), `${entry.subject}: carries "${marker}"`);
    }
    const svgCount = (html.match(/<svg/g) ?? []).length;
    assert.ok(svgCount >= entry.minSvg, `${entry.subject}: expected ≥${entry.minSvg} charts, saw ${svgCount}`);
  }
});

test("web SPA renders the practice page with due songs and frontier flags", { skip: !webInstalled && "web/node_modules not installed" }, async () => {
  const { renderApp } = await bundleApp();
  const html = await renderApp({
    NavigationManager: { view: "practice" },
    CoachManager: {
      status: "loaded",
      error: null,
      data: {
        generatedAt: "2026-08-12T00:00:00Z",
        params: { dueAfterDays: 90 },
        workingTop: { peakTopHz: 392 },
        dueSongs: [{
          clusterId: "lyric-1",
          label: "cluster one",
          score: 4.2,
          daysSinceLast: 142,
          takeCount: 24,
          firstYear: "2020",
          lastYear: "2024",
          inSameSongIndex: true,
          why: ["last sung 142 d ago", "24 takes, 2020–2024"],
        }],
        frontier: [{
          clusterId: "lyric-1",
          label: "cluster one",
          featureTakes: 9,
          flags: [{ rule: "vibrato-settling", detail: "67% of recent takes at 4.8 Hz — one nudge from the settled 5.5 Hz zone" }],
        }],
        readScope: { transcriptText: false, audioBytes: false },
      },
    },
    GoalsManager: {
      status: "loaded",
      error: null,
      data: {
        goals: [
          { goalId: "g1", title: "vibrato rate (Hz) ≥ 5.5", metric: "vibratoRateHz", metricLabel: "vibrato rate (Hz)", direction: "at_least", target: 5.5, takesRequired: 10, clusterId: null, createdAt: "2026-08-01T00:00:00Z", status: "open", derivedStatus: "active", progress: { takesCounted: 3, takesRequired: 10, runningMedian: 4.9, latestValue: 5.1, verdict: null, perTake: [] } },
          { goalId: "g2", title: "clarity — CPPS (dB) ≥ 12 on lyric-1", metric: "cpps", metricLabel: "clarity — CPPS (dB)", direction: "at_least", target: 12, takesRequired: 5, clusterId: "lyric-1", createdAt: "2026-07-01T00:00:00Z", status: "open", derivedStatus: "achieved", progress: { takesCounted: 5, takesRequired: 5, runningMedian: 12.4, latestValue: 12.6, verdict: "achieved", perTake: [] } },
        ],
        metrics: [{ key: "vibratoRateHz", label: "vibrato rate (Hz)" }],
        clusterOptions: [{ clusterId: "lyric-1", label: "cluster one", takeCount: 24 }],
        readScope: {},
      },
    },
  });
  assert.match(html, /Goals — pre-registered experiments/);
  assert.match(html, /3 of 10 takes/);
  assert.match(html, /achieved/);
  assert.match(html, /Pre-register/);
  assert.match(html, /Due for a take/);
  assert.match(html, /last sung 142 d ago/);
  assert.match(html, /The frontier/);
  assert.match(html, /vibrato settling/);
  assert.match(html, /4\.8 Hz/);
  assert.match(html, /cluster one/);
  assert.match(html, /The Practice Room/);
});

test("web SPA referee page gates without playback and renders blind trials with a pair", { skip: !webInstalled && "web/node_modules not installed" }, async () => {
  const { renderApp } = await bundleApp();
  const gated = await renderApp({
    NavigationManager: { view: "referee" },
    RefereeManager: {
      status: "loaded", error: null, enabled: false, progress: { judged: 0, poolSize: 1657 },
      pair: null, reveal: null, lastChoice: null, submitting: false,
      results: { status: "idle", error: null, data: null },
    },
  });
  assert.match(gated, /release-gated playback/);
  assert.match(gated, /1,657/);
  const live = await renderApp({
    NavigationManager: { view: "referee" },
    RefereeManager: {
      status: "loaded", error: null, enabled: true, progress: { judged: 3, poolSize: 1657 },
      pair: { pairId: "p1", a: { audioUrl: "/api/audio/x" }, b: { audioUrl: "/api/audio/y" } },
      reveal: null, lastChoice: null, submitting: false,
      results: { status: "loaded", error: null, data: { judgedTotal: 3, years: [ { year: "2020", strengthLog2: 0, wins: 1, losses: 1, ties: 0, trials: 2 }, { year: "2021", strengthLog2: 0.8, wins: 2, losses: 0, ties: 1, trials: 3 } ], byChoice: { a: 1, b: 1, too_close: 1, not_same_song: 0, skip: 0 }, notSameSongFlags: [] } },
    },
  });
  assert.match(live, /loudness-matched and band-limited/);
  assert.match(live, /A sounds better/);
  assert.match(live, /Not the same song/);
  assert.match(live, /3 of 1657 pairs judged/);
  assert.match(live, /Perceived quality by year/);
  assert.doesNotMatch(live, /2021-\d\d-\d\d/, "blind trial must not leak capture dates before judgment");
});
