import assert from "node:assert/strict";
import { test } from "node:test";

import { buildCoachPayload, buildDueSongs, buildFrontier, clusterRollup, DUE_AFTER_DAYS } from "../src/coach.mjs";

const NOW = Date.parse("2026-08-12T00:00:00Z");

function take(recordingId, cluster, capturedAt, overrides = {}) {
  return {
    recordingId,
    lyricMatchCluster: cluster,
    lyricMatchClusterLabel: cluster ? `${cluster} label` : null,
    capturedAt,
    bucket: "clean_singing",
    ...overrides,
  };
}

function featRow(recordingId, capturedAt, features) {
  return { recordingId, capturedAt, status: "completed", features };
}

test("clusterRollup groups rows by cluster with privacy-generic labels only", () => {
  const rollup = clusterRollup([
    take("r1", "song-a", "2020-01-01T00:00:00Z"),
    take("r2", "song-a", "2021-01-01T00:00:00Z", { bucket: "uncertain_manual_review" }),
    take("r3", null, "2021-01-01T00:00:00Z"),
  ]);
  assert.equal(rollup.length, 1);
  assert.equal(rollup[0].clusterId, "song-a");
  assert.equal(rollup[0].label, "song-a label");
  assert.equal(rollup[0].takeCount, 2);
  assert.equal(rollup[0].cleanCount, 1);
});

test("due songs respect the interval and the recurring minimum", () => {
  const rows = [
    // stale recurring cluster — due
    ...[0, 1, 2, 3, 4, 5].map((i) => take(`a${i}`, "stale", `202${Math.min(i, 4)}-0${i + 1}-01T00:00:00Z`)),
    // fresh cluster — inside the window
    take("b1", "fresh", "2026-06-01T00:00:00Z"),
    take("b2", "fresh", "2026-07-01T00:00:00Z"),
    take("b3", "fresh", "2026-08-01T00:00:00Z"),
    // too small to count as a song you do
    take("c1", "tiny", "2020-01-01T00:00:00Z"),
    take("c2", "tiny", "2020-02-01T00:00:00Z"),
  ];
  const due = buildDueSongs({ rows, journeysManifest: null, now: NOW });
  assert.deepEqual(due.map((entry) => entry.clusterId), ["stale"]);
  assert.equal(due[0].label, "stale label");
  assert.match(due[0].why[0], /^last sung \d+ d ago$/u);
  assert.ok(due[0].daysSinceLast >= DUE_AFTER_DAYS);
});

test("due ranking favors deep history, marks same-song index membership, and flags near-eligibility", () => {
  const rows = [
    ...Array.from({ length: 24 }, (_, i) => take(`big${i}`, "big", `${2020 + (i % 5)}-03-01T00:00:00Z`)),
    ...[0, 1, 2].map((i) => take(`sm${i}`, "small", `2020-0${i + 1}-01T00:00:00Z`)),
    // four clean takes across >1.5y and no journey entry: two takes short of eligibility
    take("n1", "near", "2020-01-01T00:00:00Z"),
    take("n2", "near", "2021-01-01T00:00:00Z"),
    take("n3", "near", "2022-01-01T00:00:00Z"),
    take("n4", "near", "2024-01-01T00:00:00Z"),
  ];
  const journeysManifest = { journeys: [{ clusterId: "big", takesUsed: 20 }] };
  const due = buildDueSongs({ rows, journeysManifest, now: NOW });
  assert.equal(due[0].clusterId === "big" || due[0].clusterId === "near", true, "deep or boosted clusters lead");
  const big = due.find((entry) => entry.clusterId === "big");
  assert.equal(big.inSameSongIndex, true);
  assert.ok(big.why.some((reason) => reason.includes("same-song index")));
  const near = due.find((entry) => entry.clusterId === "near");
  assert.equal(near.inSameSongIndex, false);
  assert.ok(near.why.some((reason) => reason.includes("short of same-song eligibility")));
  const small = due.find((entry) => entry.clusterId === "small");
  assert.ok(big.score > small.score, "history depth outranks a shallow thread at similar staleness");
});

test("frontier rules fire with the numbers that argue them", () => {
  const rows = [
    take("f1", "fx", "2025-01-01T00:00:00Z"),
    take("f2", "fx", "2025-02-01T00:00:00Z"),
    take("f3", "fx", "2025-03-01T00:00:00Z"),
    take("pb1", "other", "2024-01-01T00:00:00Z"),
  ];
  const featuresManifest = {
    results: [
      featRow("f1", "2025-01-01T00:00:00Z", {
        pitch: { f0Hz: { p95: 350 } },
        vibrato: { meanRateHz: 4.8, vibratoSegmentCount: 3 },
        phrasing: { longestSustainedSeconds: 9.0 },
      }),
      featRow("f2", "2025-02-01T00:00:00Z", {
        pitch: { f0Hz: { p95: 348 } },
        vibrato: { meanRateHz: 4.9, vibratoSegmentCount: 2 },
        phrasing: { longestSustainedSeconds: 5.0 },
      }),
      featRow("f3", "2025-03-01T00:00:00Z", {
        pitch: { f0Hz: { p95: 352 } },
        vibrato: { meanRateHz: 3.9, vibratoSegmentCount: 4 },
        phrasing: { longestSustainedSeconds: 6.0 },
      }),
      featRow("pb1", "2024-01-01T00:00:00Z", {
        pitch: { f0Hz: { p95: 200 } },
        vibrato: { meanRateHz: 4.0, vibratoSegmentCount: 1 },
        phrasing: { longestSustainedSeconds: 10.0 },
      }),
    ],
  };
  const journeysManifest = {
    journeys: [{
      clusterId: "fx",
      takesUsed: 8,
      perYear: { 2023: { noteCoreCentError: 21 }, 2024: { noteCoreCentError: 28 } },
      slopesPerYear: { cpps: 0.4 },
    }],
  };
  const frontier = buildFrontier({ rows, featuresManifest, journeysManifest, workingTopHz: 392 });
  assert.equal(frontier.length, 1, "only the flagged cluster appears (the PB donor has too few takes)");
  const entry = frontier[0];
  assert.equal(entry.clusterId, "fx");
  assert.equal(entry.label, "fx label");
  const rules = entry.flags.map((flag) => flag.rule).sort();
  assert.deepEqual(rules, ["clarity-thin", "range-stretch", "sustain-pb", "tuning-near", "vibrato-settling"]);
  const byRule = new Map(entry.flags.map((flag) => [flag.rule, flag.detail]));
  assert.match(byRule.get("range-stretch"), /st under your working top/u);
  assert.match(byRule.get("vibrato-settling"), /Hz/u);
  assert.match(byRule.get("sustain-pb"), /of your 10\.0 s best/u);
  assert.match(byRule.get("tuning-near"), /28 c in 2024/u);
  assert.match(byRule.get("clarity-thin"), /add data/u);
});

test("quiet clusters produce no frontier entries", () => {
  const rows = [
    take("q1", "quiet", "2025-01-01T00:00:00Z"),
    take("q2", "quiet", "2025-02-01T00:00:00Z"),
    take("q3", "quiet", "2025-03-01T00:00:00Z"),
  ];
  const featuresManifest = {
    results: ["q1", "q2", "q3"].map((id, i) => featRow(id, `2025-0${i + 1}-01T00:00:00Z`, {
      pitch: { f0Hz: { p95: 200 } },
      vibrato: { meanRateHz: 4.0, vibratoSegmentCount: 2 },
      phrasing: { longestSustainedSeconds: 3.0 },
    })),
  };
  const frontier = buildFrontier({ rows, featuresManifest, journeysManifest: null, workingTopHz: 392 });
  assert.deepEqual(frontier, []);
});

test("coach payload carries aggregates only, with a fixed entry shape", () => {
  const rows = [
    ...[0, 1, 2, 3].map((i) => take(`a${i}`, "song-a", `202${i}-01-01T00:00:00Z`)),
  ];
  const payload = buildCoachPayload({
    rows,
    featuresManifest: null,
    journeysManifest: { available: true, journeys: [] },
    voiceTrends: { headline: { workingTop: { peakTopHz: 392, gainSemitones: 6.7 } } },
    now: NOW,
  });
  assert.equal(payload.workingTop.peakTopHz, 392);
  assert.equal(payload.readScope.transcriptText, false);
  assert.equal(payload.readScope.audioBytes, false);
  assert.ok(Array.isArray(payload.frontier));
  assert.ok(payload.dueSongs.length >= 1);
  assert.deepEqual(
    Object.keys(payload.dueSongs[0]).sort(),
    ["clusterId", "daysSinceLast", "firstYear", "inSameSongIndex", "label", "lastYear", "score", "takeCount", "why"],
  );
});
