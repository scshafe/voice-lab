// goals.mjs — phase E slice 2: goals as pre-registered n=1 experiments.
//
// A goal freezes its full spec at creation: metric, direction, target, sample
// size, scope. The verdict is decided by the FIRST N qualifying takes recorded
// after creation, in chronological order — no optional stopping, no peeking,
// no "just one more take" bias. Status is DERIVED from the manifests on every
// read (the first N takes are the first N takes, so recomputation is stable);
// only the spec and an explicit abandonment are ever stored. Values that flow
// out are numeric aggregates only.

import { createHash } from "node:crypto";
import { clusterRollup } from "./coach.mjs";

export const GOALS_STATE_SCHEMA_VERSION = "voice-journey.goals-state.v1";

export const GOAL_METRICS = Object.freeze({
  vibratoRateHz: { label: "vibrato rate (Hz)", pick: (features) => features.vibrato?.meanRateHz },
  vibratoExtentCents: { label: "vibrato extent (cents)", pick: (features) => features.vibrato?.meanExtentCents },
  cpps: { label: "clarity — CPPS (dB)", pick: (features) => features.quality?.cpps },
  longestSustainSeconds: { label: "longest sustain (s)", pick: (features) => features.phrasing?.longestSustainedSeconds },
  pitchP95Hz: { label: "top of range — p95 (Hz)", pick: (features) => features.pitch?.f0Hz?.p95 },
  tuningMedianCentError: { label: "tuning error (cents)", pick: (features) => features.tuning?.medianAbsCentError },
});

export const GOAL_DIRECTIONS = Object.freeze(["at_least", "at_most"]);
const TAKES_REQUIRED_MIN = 3;
const TAKES_REQUIRED_MAX = 50;
const TAKES_REQUIRED_DEFAULT = 10;

function goalError(message) {
  return Object.assign(new Error(message), { statusCode: 400 });
}

function median(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((left, right) => left - right);
  if (!sorted.length) return null;
  return sorted[Math.floor((sorted.length - 1) / 2)];
}

export function validateGoalSpec(body = {}) {
  const metric = String(body.metric ?? "");
  if (!GOAL_METRICS[metric]) throw goalError(`unknown_metric: expected one of ${Object.keys(GOAL_METRICS).join(", ")}`);
  const direction = String(body.direction ?? "");
  if (!GOAL_DIRECTIONS.includes(direction)) throw goalError("invalid_direction: expected at_least or at_most");
  const target = Number(body.target);
  if (!Number.isFinite(target)) throw goalError("invalid_target: expected a finite number");
  const takesRequired = body.takesRequired === undefined ? TAKES_REQUIRED_DEFAULT : Number(body.takesRequired);
  if (!Number.isInteger(takesRequired) || takesRequired < TAKES_REQUIRED_MIN || takesRequired > TAKES_REQUIRED_MAX) {
    throw goalError(`invalid_takes_required: expected an integer ${TAKES_REQUIRED_MIN}–${TAKES_REQUIRED_MAX}`);
  }
  const clusterId = body.clusterId ? String(body.clusterId) : null;
  const title = body.title ? String(body.title).trim().slice(0, 120) : null;
  return { metric, direction, target, takesRequired, clusterId, title };
}

export function createGoal(spec, { now }) {
  const createdAt = new Date(now).toISOString();
  const metricLabel = GOAL_METRICS[spec.metric].label;
  const title = spec.title
    ?? `${metricLabel} ${spec.direction === "at_least" ? "≥" : "≤"} ${spec.target}${spec.clusterId ? ` on ${spec.clusterId}` : ""}`;
  return {
    goalId: createHash("sha256").update(`${JSON.stringify(spec)}:${createdAt}`).digest("hex").slice(0, 16),
    createdAt,
    status: "open",
    ...spec,
    title,
  };
}

// The evaluation context the routes hand in: per-take feature rows plus the
// recordingId → cluster mapping and privacy-generic cluster options.
export function goalContext(rows, featuresManifest) {
  const clusterByRecording = new Map();
  for (const row of rows ?? []) {
    if (row.lyricMatchCluster) clusterByRecording.set(row.recordingId, row.lyricMatchCluster);
  }
  const clusterOptions = clusterRollup(rows ?? [])
    .sort((left, right) => right.takeCount - left.takeCount)
    .slice(0, 30)
    .map((cluster) => ({ clusterId: cluster.clusterId, label: cluster.label ?? cluster.clusterId, takeCount: cluster.takeCount }));
  return { featureRows: featuresManifest?.results ?? [], clusterByRecording, clusterOptions };
}

export function evaluateGoal(goal, { featureRows, clusterByRecording }) {
  const createdAtMs = Date.parse(goal.createdAt);
  const pick = GOAL_METRICS[goal.metric]?.pick ?? (() => undefined);
  const qualifying = [];
  for (const row of featureRows ?? []) {
    if (row.status !== "completed" || !row.features || !row.capturedAt) continue;
    const at = Date.parse(row.capturedAt);
    if (!Number.isFinite(at) || at <= createdAtMs) continue;
    if (goal.clusterId && clusterByRecording.get(row.recordingId) !== goal.clusterId) continue;
    const value = pick(row.features);
    if (!Number.isFinite(value)) continue;
    qualifying.push({ recordingId: row.recordingId, capturedAt: row.capturedAt, value, at });
  }
  qualifying.sort((left, right) => left.at - right.at || left.recordingId.localeCompare(right.recordingId));
  // Pre-registration: only the first N qualifying takes ever count.
  const counted = qualifying.slice(0, goal.takesRequired).map(({ recordingId, capturedAt, value }) => ({ recordingId, capturedAt, value }));
  const runningMedian = median(counted.map((take) => take.value));
  const complete = counted.length >= goal.takesRequired;
  let verdict = null;
  if (complete) {
    verdict = (goal.direction === "at_least" ? runningMedian >= goal.target : runningMedian <= goal.target) ? "achieved" : "missed";
  }
  return {
    takesCounted: counted.length,
    takesRequired: goal.takesRequired,
    runningMedian,
    latestValue: counted.length ? counted[counted.length - 1].value : null,
    verdict,
    perTake: counted,
  };
}

export function goalsPayload(state, context) {
  const goals = (state?.goals ?? [])
    .slice()
    .sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt))
    .map((goal) => {
      const abandoned = goal.status === "abandoned";
      const progress = abandoned ? null : evaluateGoal(goal, context);
      return {
        ...goal,
        metricLabel: GOAL_METRICS[goal.metric]?.label ?? goal.metric,
        progress,
        derivedStatus: abandoned ? "abandoned" : progress.verdict ?? "active",
      };
    });
  return {
    goals,
    metrics: Object.entries(GOAL_METRICS).map(([key, def]) => ({ key, label: def.label })),
    clusterOptions: context.clusterOptions ?? [],
    readScope: { committedManifestsOnly: true, countsAndAggregatesOnly: true, transcriptText: false, audioBytes: false },
  };
}
