import assert from "node:assert/strict";
import { test } from "node:test";

import { createGoal, evaluateGoal, GOAL_METRICS, goalContext, goalsPayload, validateGoalSpec } from "../src/goals.mjs";

const CREATED = "2026-08-01T00:00:00Z";

function vibratoTake(recordingId, capturedAt, meanRateHz) {
  return { recordingId, capturedAt, status: "completed", features: { vibrato: { meanRateHz } } };
}

function goalOf(overrides = {}) {
  return {
    goalId: "g",
    createdAt: CREATED,
    status: "open",
    metric: "vibratoRateHz",
    direction: "at_least",
    target: 5.5,
    takesRequired: 3,
    clusterId: null,
    title: "t",
    ...overrides,
  };
}

test("validateGoalSpec rejects bad specs with 400s and applies defaults", () => {
  assert.throws(() => validateGoalSpec({ metric: "nope", direction: "at_least", target: 1 }), (error) => error.statusCode === 400 && /unknown_metric/u.test(error.message));
  assert.throws(() => validateGoalSpec({ metric: "cpps", direction: "sideways", target: 1 }), (error) => /invalid_direction/u.test(error.message));
  assert.throws(() => validateGoalSpec({ metric: "cpps", direction: "at_most", target: "wat" }), (error) => /invalid_target/u.test(error.message));
  assert.throws(() => validateGoalSpec({ metric: "cpps", direction: "at_most", target: 1, takesRequired: 2 }), (error) => /invalid_takes_required/u.test(error.message));
  const spec = validateGoalSpec({ metric: "cpps", direction: "at_most", target: "11.5", title: `  padded  ` });
  assert.equal(spec.takesRequired, 10);
  assert.equal(spec.target, 11.5);
  assert.equal(spec.title, "padded");
  assert.equal(spec.clusterId, null);
});

test("createGoal freezes the spec with a generated title and id", () => {
  const goal = createGoal(validateGoalSpec({ metric: "vibratoRateHz", direction: "at_least", target: 5.5, clusterId: "lyric-9" }), { now: Date.parse(CREATED) });
  assert.match(goal.goalId, /^[0-9a-f]{16}$/u);
  assert.equal(goal.createdAt, new Date(Date.parse(CREATED)).toISOString());
  assert.equal(goal.status, "open");
  assert.equal(goal.title, "vibrato rate (Hz) ≥ 5.5 on lyric-9");
});

test("evaluation counts only qualifying takes after creation, in chronological order", () => {
  const featureRows = [
    vibratoTake("r-late", "2026-08-20T00:00:00Z", 5.7),
    vibratoTake("r-before", "2026-07-01T00:00:00Z", 9.9),
    vibratoTake("r-early", "2026-08-05T00:00:00Z", 5.6),
    vibratoTake("r-mid", "2026-08-10T00:00:00Z", 5.4),
    { recordingId: "r-failed", capturedAt: "2026-08-06T00:00:00Z", status: "failed", features: { vibrato: { meanRateHz: 1 } } },
  ];
  const progress = evaluateGoal(goalOf(), { featureRows, clusterByRecording: new Map() });
  assert.deepEqual(progress.perTake.map((take) => take.recordingId), ["r-early", "r-mid", "r-late"]);
  assert.equal(progress.takesCounted, 3);
  assert.equal(progress.runningMedian, 5.6);
  assert.equal(progress.verdict, "achieved");
});

test("pre-registration means takes beyond the first N never count", () => {
  const featureRows = [
    vibratoTake("a", "2026-08-02T00:00:00Z", 4.0),
    vibratoTake("b", "2026-08-03T00:00:00Z", 4.1),
    vibratoTake("c", "2026-08-04T00:00:00Z", 4.2),
    // Spectacular later takes that would flip the verdict if peeking were allowed.
    vibratoTake("d", "2026-08-05T00:00:00Z", 6.5),
    vibratoTake("e", "2026-08-06T00:00:00Z", 6.6),
  ];
  const progress = evaluateGoal(goalOf(), { featureRows, clusterByRecording: new Map() });
  assert.equal(progress.takesCounted, 3);
  assert.equal(progress.verdict, "missed");
  assert.deepEqual(progress.perTake.map((take) => take.recordingId), ["a", "b", "c"]);
});

test("direction at_most, incomplete goals, and cluster scoping behave", () => {
  const featureRows = [
    vibratoTake("x1", "2026-08-02T00:00:00Z", 4.0),
    vibratoTake("x2", "2026-08-03T00:00:00Z", 4.4),
    vibratoTake("y1", "2026-08-04T00:00:00Z", 9.0),
  ];
  const clusterByRecording = new Map([["x1", "song-x"], ["x2", "song-x"], ["y1", "song-y"]]);
  const scoped = evaluateGoal(goalOf({ direction: "at_most", target: 4.5, clusterId: "song-x", takesRequired: 3 }), { featureRows, clusterByRecording });
  assert.equal(scoped.takesCounted, 2, "other clusters never qualify");
  assert.equal(scoped.verdict, null, "incomplete goals hold no verdict");
  assert.equal(scoped.runningMedian, 4.0);
  const complete = evaluateGoal(goalOf({ direction: "at_most", target: 4.5, clusterId: "song-x", takesRequired: 2 }), { featureRows, clusterByRecording });
  assert.equal(complete.verdict, "achieved");
});

test("goalsPayload derives statuses, keeps abandoned goals frozen, and stays aggregate-only", () => {
  const state = {
    goals: [
      goalOf({ goalId: "g-old", createdAt: "2026-07-01T00:00:00Z" }),
      goalOf({ goalId: "g-gone", createdAt: "2026-07-15T00:00:00Z", status: "abandoned", abandonedAt: "2026-08-01T00:00:00Z" }),
    ],
  };
  const rows = [
    { recordingId: "x1", lyricMatchCluster: "song-x", lyricMatchClusterLabel: "song x label", capturedAt: "2026-08-02T00:00:00Z", bucket: "clean_singing" },
    { recordingId: "x2", lyricMatchCluster: "song-x", lyricMatchClusterLabel: "song x label", capturedAt: "2026-08-03T00:00:00Z", bucket: "clean_singing" },
    { recordingId: "x3", lyricMatchCluster: "song-x", lyricMatchClusterLabel: "song x label", capturedAt: "2026-08-04T00:00:00Z", bucket: "clean_singing" },
  ];
  const featuresManifest = { results: rows.map((row, i) => vibratoTake(row.recordingId, row.capturedAt, 5.6 + i * 0.1)) };
  const payload = goalsPayload(state, goalContext(rows, featuresManifest));
  assert.deepEqual(payload.goals.map((goal) => goal.goalId), ["g-gone", "g-old"], "newest first");
  const gone = payload.goals[0];
  assert.equal(gone.derivedStatus, "abandoned");
  assert.equal(gone.progress, null);
  const active = payload.goals[1];
  assert.equal(active.derivedStatus, "achieved");
  assert.equal(active.metricLabel, GOAL_METRICS.vibratoRateHz.label);
  assert.ok(payload.metrics.some((metric) => metric.key === "cpps"));
  assert.deepEqual(payload.clusterOptions, [{ clusterId: "song-x", label: "song x label", takeCount: 3 }]);
  assert.equal(payload.readScope.transcriptText, false);
});
