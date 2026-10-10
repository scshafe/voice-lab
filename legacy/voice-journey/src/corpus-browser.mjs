#!/usr/bin/env node
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const VJ = resolvePaths();
const DEFAULT_INDEX = VJ.manifest("recording-index.json");
const DEFAULT_RESULTS = VJ.manifest("full-corpus-filter-results.json");
const DEFAULT_STATE = VJ.artifact("corpus-browser-state.json");
const DEFAULT_TRANSCRIPTS = VJ.manifest("local-stt-transcripts.json");
const DEFAULT_LYRICS = VJ.manifest("local-lyric-indicators.json");
const DEFAULT_FEATURES = VJ.manifest("local-voice-features.json");
const DEFAULT_JOURNEYS = VJ.manifest("song-journeys.json");
const DEFAULT_PORT = 8787;
const TRENDS_SCHEMA_VERSION = "voice-journey.voice-trends.v1";
const RELIABLE_REJECTION_MAX = 0.10;
const DEFAULT_HOSTS = ["127.0.0.1"];
const DEFAULT_QUEUE_SIZE = 56;
const ROWS_SCHEMA_VERSION = "voice-journey.corpus-browser-rows.v1";
const STATE_SCHEMA_VERSION = "voice-journey.corpus-browser-state.v1";
import { createWatchd } from "./watchd.mjs";
import { createIntake } from "./intake.mjs";
import { assertBindAllowed, resolvePaths } from "./paths.mjs";
import { buildCoachPayload } from "./coach.mjs";
import { createGoal, GOALS_STATE_SCHEMA_VERSION, goalContext, goalsPayload, validateGoalSpec } from "./goals.mjs";

const REFEREE_STATE_SCHEMA_VERSION = "voice-journey.referee-state.v1";
const DEFAULT_REFEREE_STATE = VJ.artifact("referee-state.json");
const DEFAULT_GOALS_STATE = VJ.artifact("goals-state.json");
const DEFAULT_WEB_DIST = path.join("web", "dist");
const DEFAULT_REFEREE_SEED = 2026;
const REFEREE_PAIRS_PER_CLUSTER = 30;
const REFEREE_CHOICES = new Set(["a", "b", "too_close", "not_same_song", "skip"]);
const ALLOWED_VERDICTS = new Set([
  "clean_singing",
  "noise_contaminated_singing",
  "music_contaminated_singing",
  "non_singing",
  "still_uncertain",
]);
const SORTABLE_FIELDS = new Set([
  "filename",
  "capturedAt",
  "year",
  "durationSeconds",
  "sizeBytes",
  "bucket",
  "confidence",
  "spotCheckStatus",
  "transcriptStatus",
  "lyricMatchStatus",
  "reviewQueueOrder",
]);

class CorpusBrowserError extends Error {}

function parseArgs(argv, env = process.env) {
  const args = [...argv];
  let command = "serve";
  if (args[0] && !args[0].startsWith("--") && args[0] !== "help") {
    command = args.shift();
  }

  const options = {
    command,
    corpusRoot: null,
    hosts: [],
    index: DEFAULT_INDEX,
    once: false,
    out: null,
    playbackApproval: null,
    port: DEFAULT_PORT,
    queueSize: DEFAULT_QUEUE_SIZE,
    results: DEFAULT_RESULTS,
    state: DEFAULT_STATE,
    transcriptDir: null,
    transcripts: DEFAULT_TRANSCRIPTS,
    lyrics: DEFAULT_LYRICS,
    features: DEFAULT_FEATURES,
    featureDir: null,
    journeys: DEFAULT_JOURNEYS,
    refereeSeed: DEFAULT_REFEREE_SEED,
    refereeState: DEFAULT_REFEREE_STATE,
    goalsState: DEFAULT_GOALS_STATE,
    report: VJ.artifact("evidence-report.html"),
    webDist: DEFAULT_WEB_DIST,
    watch: false,
    watchInterval: 60,
    watchApproval: null,
    watchState: VJ.artifact("watchd-state.json"),
    ntfy: null,
    digestDay: "sunday",
    digestHour: 18,
    explicit: new Set(),
    dataRoot: null,
    allowEmptyManifests: false,
  };

  while (args.length > 0) {
    const next = args.shift();
    options.explicit.add(next);
    if (next === "--corpus-root") {
      options.corpusRoot = requireValue(args, next);
    } else if (next === "--host") {
      options.hosts.push(requireValue(args, next));
    } else if (next === "--index") {
      options.index = requireValue(args, next);
    } else if (next === "--once") {
      options.once = true;
    } else if (next === "--out") {
      options.out = requireValue(args, next);
    } else if (next === "--playback-approval") {
      options.playbackApproval = requireValue(args, next);
    } else if (next === "--port") {
      options.port = parsePort(requireValue(args, next));
    } else if (next === "--queue-size") {
      options.queueSize = parsePositiveInteger(requireValue(args, next), next);
    } else if (next === "--results") {
      options.results = requireValue(args, next);
    } else if (next === "--state") {
      options.state = requireValue(args, next);
    } else if (next === "--transcript-dir") {
      options.transcriptDir = requireValue(args, next);
    } else if (next === "--transcripts") {
      options.transcripts = requireValue(args, next);
    } else if (next === "--lyrics") {
      options.lyrics = requireValue(args, next);
    } else if (next === "--features") {
      options.features = requireValue(args, next);
    } else if (next === "--feature-dir") {
      options.featureDir = requireValue(args, next);
    } else if (next === "--journeys") {
      options.journeys = requireValue(args, next);
    } else if (next === "--report") {
      options.report = requireValue(args, next);
    } else if (next === "--referee-seed") {
      options.refereeSeed = parsePositiveInteger(requireValue(args, next), next);
    } else if (next === "--referee-state") {
      options.refereeState = requireValue(args, next);
    } else if (next === "--goals-state") {
      options.goalsState = requireValue(args, next);
    } else if (next === "--web-dist") {
      options.webDist = requireValue(args, next);
    } else if (next === "--watch") {
      options.watch = true;
    } else if (next === "--watch-interval") {
      options.watchInterval = parsePositiveInteger(requireValue(args, next), next);
    } else if (next === "--watch-approval") {
      options.watchApproval = requireValue(args, next);
    } else if (next === "--watch-state") {
      options.watchState = requireValue(args, next);
    } else if (next === "--ntfy") {
      options.ntfy = requireValue(args, next);
    } else if (next === "--digest-day") {
      options.digestDay = requireValue(args, next);
    } else if (next === "--digest-hour") {
      options.digestHour = parseDigestHour(requireValue(args, next));
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new CorpusBrowserError(`unsupported argument: ${next}`);
    }
  }
  applyEnvDefaults(options, env);
  if (options.hosts.length === 0) options.hosts = [...DEFAULT_HOSTS];
  for (const host of options.hosts) {
    try {
      assertBindAllowed(host, env);
    } catch (error) {
      throw new CorpusBrowserError(error.message);
    }
  }
  return options;
}

// Server deployment config comes from the environment (the container's
// compose file sets it); explicit flags always win. With a data root, the
// stores and the phone mirror default to their places under it. Playback and
// the audio-reading watcher stay off until their approval strings are set.
function applyEnvDefaults(options, env) {
  const paths = resolvePaths(env);
  const explicit = (flag) => options.explicit.has(flag);
  if (options.hosts.length === 0 && env.VOICE_JOURNEY_HOST) {
    options.hosts = env.VOICE_JOURNEY_HOST.split(",").map((host) => host.trim()).filter(Boolean);
  }
  if (!explicit("--port") && env.VOICE_JOURNEY_PORT) options.port = parsePort(env.VOICE_JOURNEY_PORT);
  if (paths.dataRoot) {
    options.dataRoot = paths.dataRoot;
    options.corpusRoot ??= paths.phoneCorpusRoot;
    options.transcriptDir ??= paths.artifact("stt-transcripts");
    options.featureDir ??= paths.artifact("voice-features");
    options.allowEmptyManifests = true;
  }
  options.playbackApproval ??= env.VOICE_JOURNEY_PLAYBACK_APPROVAL || null;
  options.watchApproval ??= env.VOICE_JOURNEY_WATCH_APPROVAL || null;
  options.ntfy ??= env.VOICE_JOURNEY_NTFY || null;
  if (env.VOICE_JOURNEY_WATCH === "1") options.watch = true;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new CorpusBrowserError(`${flag} requires a value`);
  return value;
}

function parsePort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new CorpusBrowserError("--port must be an integer from 0 to 65535");
  }
  return port;
}

function parsePositiveInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new CorpusBrowserError(`${flag} must be a positive integer`);
  return parsed;
}

function parseDigestHour(value) {
  const hour = Number(value);
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) throw new CorpusBrowserError("--digest-hour must be an integer from 0 to 23");
  return hour;
}

function printHelp() {
  process.stdout.write(`Voice Journey corpus browser.

Usage:
  voice-journey-browser [--index PATH] [--results PATH] [--transcripts PATH] [--lyrics PATH] [--features PATH] [--state PATH] [--host HOST]... [--port PORT] [--once]
  voice-journey-browser --corpus-root PATH --playback-approval TEXT [--transcript-dir PATH] [--feature-dir PATH] [--state PATH] [--host HOST]... [--port PORT]
  voice-journey-browser merge-verdicts --results PATH --state PATH [--out PATH]

Playback is disabled unless --corpus-root and --playback-approval are supplied.
Verdicts are written to a local app-state JSON file. merge-verdicts writes only
repo-safe humanSpotCheck metadata into the results manifest.
Transcript text is served only from --transcript-dir and is never included in row JSON.
The Journey page (/journey) renders longitudinal voice trends from the optional
repo-safe voice-features manifest; it serves summary aggregates only. Per-take
f0 contours are served only from --feature-dir via /api/feature-detail.
The Referee page (/referee) runs blind same-song A/B trials (cross-year pairs,
seeded via --referee-seed) behind the playback gate; judgments persist to the
gitignored --referee-state file and fit a Bradley-Terry perceived-quality
curve by year.
The Practice page (/practice) serves the coaching layer from /api/coach —
due-for-a-take rankings and frontier ("one step away") flags, computed from
committed manifests only. Goals (pre-registered n=1 experiments) persist to
the local --goals-state file; a goal's verdict derives from the first N
qualifying takes after its creation — no optional stopping. Referee pairs
carry loudness-match gains (from committed RMS aggregates) that the client
applies at playback; served audio is untouched.
--watch runs the breathing loop inside this process (requires --corpus-root and
--watch-approval, the recorded operator approval for audio-reading stages): the
corpus is scanned every --watch-interval seconds (default 60), and when new
recordings appear and stabilize, the incremental pipeline runs (index → filter
→ stt → lyric → reclassify → features → journeys) and the served manifests
hot-reload. --ntfy URL enables run/failure notifications plus a weekly digest
(--digest-day, --digest-hour). Status: GET /api/watch/status.
`);
}

async function readJson(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}

// A fresh data volume has no generated manifests until the watcher's first
// run; the server then serves an empty corpus instead of refusing to start.
async function readManifest(filePath, empty, options) {
  if (!options.allowEmptyManifests) return readJson(filePath);
  return (await readOptionalJson(filePath)) ?? empty;
}

async function readOptionalJson(filePath) {
  try {
    return await readJson(filePath);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function writeJson(filePath, payload) {
  const body = `${JSON.stringify(payload, null, 2)}\n`;
  if (filePath === "-") {
    process.stdout.write(body);
    return;
  }
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, body, "utf8");
}

function emptyState() {
  return { schemaVersion: STATE_SCHEMA_VERSION, updatedAt: null, verdicts: [] };
}

async function readState(filePath) {
  try {
    const state = await readJson(filePath);
    if (state.schemaVersion !== STATE_SCHEMA_VERSION || !Array.isArray(state.verdicts)) return emptyState();
    return state;
  } catch (error) {
    if (error?.code === "ENOENT") return emptyState();
    throw error;
  }
}

async function writeState(filePath, state) {
  await writeJson(filePath, state);
}

function verdictMapFor(state) {
  const map = new Map();
  for (const verdict of state.verdicts ?? []) {
    if (verdict.recordingId) map.set(verdict.recordingId, verdict);
  }
  return map;
}

function upsertVerdict(state, verdict) {
  const next = {
    schemaVersion: STATE_SCHEMA_VERSION,
    updatedAt: verdict.reviewedAt,
    verdicts: (state.verdicts ?? []).filter((entry) => entry.recordingId !== verdict.recordingId),
  };
  next.verdicts.push(verdict);
  next.verdicts.sort((left, right) => left.recordingId.localeCompare(right.recordingId));
  return next;
}

function buildCorpusRows(indexManifest, resultsManifest, { state = emptyState(), queueSize = DEFAULT_QUEUE_SIZE, transcriptsManifest = null, lyricsManifest = null } = {}) {
  const resultById = new Map();
  const stateById = verdictMapFor(state);
  const transcriptById = manifestResultsByRecordingId(transcriptsManifest);
  const lyricById = manifestResultsByRecordingId(lyricsManifest);
  for (const result of resultsManifest.results ?? []) {
    if (result.recordingId) resultById.set(result.recordingId, result);
  }

  const rows = (indexManifest.recordings ?? []).map((recording) => {
    const result = resultById.get(recording.recordingId) ?? null;
    const classification = result?.classification ?? null;
    const humanSpotCheck = result?.humanSpotCheck ?? null;
    const localVerdict = stateById.get(recording.recordingId) ?? null;
    const transcript = transcriptById.get(recording.recordingId) ?? null;
    const lyric = lyricById.get(recording.recordingId)?.lyricMatch ?? null;
    const wordlessSignal = lyric?.signals?.wordlessVocalise ?? null;
    const transcriptWordCount = transcript?.counts?.words ?? null;
    const capturedAt = result?.capturedAt ?? recording.capturedAt ?? null;
    const year = result?.year ?? yearFor(capturedAt);
    return {
      recordingId: recording.recordingId,
      filename: recording.filename,
      capturedAt,
      year,
      durationSeconds: result?.features?.durationSeconds ?? recording.durationSeconds ?? null,
      sizeBytes: recording.file?.sizeBytes ?? null,
      bucket: classification?.finalBucket ?? "unclassified",
      confidence: classification?.confidence ?? null,
      rationale: classification?.rationale ?? null,
      spotCheckRecommended: humanSpotCheck?.recommended ?? false,
      spotCheckStatus: localVerdict ? "reviewed" : humanSpotCheck?.status ?? "not_reviewed",
      humanVerdict: localVerdict?.verdict ?? humanSpotCheck?.verdict ?? null,
      reviewedBy: localVerdict?.reviewedBy ?? humanSpotCheck?.reviewedBy ?? null,
      reviewedAt: localVerdict?.reviewedAt ?? humanSpotCheck?.reviewedAt ?? null,
      transcriptStatus: transcript?.status ?? "not_available",
      transcriptLanguage: transcript?.language?.detected ?? null,
      transcriptWordCount,
      transcriptSegmentCount: transcript?.counts?.segments ?? null,
      transcriptCharacterCount: transcript?.counts?.characters ?? null,
      transcriptHasWords: transcript?.derivedFeatures?.hasTranscribedWords ?? (transcriptWordCount === null ? null : transcriptWordCount > 0),
      transcriptTextLocalOnly: Boolean(transcript?.localArtifacts?.transcriptTextPath && transcript?.localArtifacts?.textIncludedInManifest === false),
      lyricMatchStatus: lyric?.status ?? "not_available",
      lyricMatchMarked: Boolean(lyric?.marked),
      lyricMatchMethod: lyric?.method ?? null,
      lyricMatchConfidence: lyric?.confidence ?? null,
      lyricMatchCluster: lyric?.matchedCluster ?? null,
      lyricMatchClusterLabel: lyric?.matchedClusterLabel ?? null,
      wordlessVocaliseMarked: Boolean(wordlessSignal?.marked ?? (transcript?.status === "completed" && transcriptWordCount === 0)),
      wordlessVocaliseConfidence: wordlessSignal?.confidence ?? null,
      wordlessVocaliseLanguage: wordlessSignal?.detectedLanguage ?? transcript?.language?.detected ?? null,
      reviewQueueSelected: false,
      reviewQueueOrder: null,
      reviewQueueReason: null,
    };
  });

  const queue = selectReviewQueue(rows, queueSize);
  for (const [index, selection] of queue.entries()) {
    selection.row.reviewQueueSelected = true;
    selection.row.reviewQueueOrder = index + 1;
    selection.row.reviewQueueReason = selection.reason;
  }

  return {
    schemaVersion: ROWS_SCHEMA_VERSION,
    generatedAt: new Date().toISOString(),
    source: {
      index: {
        schemaVersion: indexManifest.schemaVersion,
        recordingCount: indexManifest.totals?.recordings ?? indexManifest.recordings?.length ?? null,
      },
      results: {
        schemaVersion: resultsManifest.schemaVersion,
        recordingCount: resultsManifest.results?.length ?? null,
      },
      transcripts: manifestSourceSummary(transcriptsManifest),
      lyrics: manifestSourceSummary(lyricsManifest),
      state: {
        schemaVersion: state.schemaVersion,
        verdictCount: state.verdicts?.length ?? 0,
      },
      readScope: {
        committedManifestsOnly: true,
        audioBytes: false,
        transcriptText: false,
        verdictWrites: true,
        upload: false,
        corpusMutation: false,
      },
    },
    reviewQueue: {
      strategy: "uncertain_year_confidence_stratified_with_bucket_controls.v1",
      targetSize: queueSize,
      selectedCount: queue.length,
      recordingIds: queue.map(({ row }) => row.recordingId),
    },
    totals: summarizeRows(rows),
    rows,
  };
}

function manifestResultsByRecordingId(manifest) {
  const map = new Map();
  for (const result of manifest?.results ?? []) {
    if (result.recordingId) map.set(result.recordingId, result);
  }
  return map;
}

function manifestSourceSummary(manifest) {
  if (!manifest) return { available: false, schemaVersion: null, recordingCount: null };
  return {
    available: true,
    schemaVersion: manifest.schemaVersion ?? null,
    recordingCount: manifest.results?.length ?? manifest.totals?.recordings ?? manifest.sourceTranscripts?.recordingCount ?? null,
  };
}

function selectReviewQueue(rows, targetSize = DEFAULT_QUEUE_SIZE) {
  const selected = [];
  const seen = new Set();
  const add = (candidates, count, reason) => {
    for (const row of stratifiedPick(candidates.filter((candidate) => !seen.has(candidate.recordingId)), count)) {
      seen.add(row.recordingId);
      selected.push({ row, reason });
    }
  };

  const controls = [
    ["music_contaminated_singing", 1],
    ["noise_contaminated_singing", 4],
    ["non_singing", 4],
    ["clean_singing", 6],
  ];
  const controlBudget = Math.min(15, Math.max(0, targetSize - 40));
  let usedControlBudget = 0;
  for (const [bucket, preferredCount] of controls) {
    const count = Math.min(preferredCount, Math.max(0, controlBudget - usedControlBudget));
    if (count > 0) {
      add(rows.filter((row) => row.bucket === bucket), count, `${bucket}_control`);
      usedControlBudget += count;
    }
  }

  add(rows.filter((row) => row.bucket === "uncertain_manual_review"), targetSize - selected.length, "uncertain_year_confidence_stratified");
  if (selected.length < targetSize) {
    add(rows, targetSize - selected.length, "backfill_stratified");
  }
  selected.sort((left, right) => compareQueueRows(left.row, right.row));
  return selected.slice(0, targetSize);
}

function stratifiedPick(rows, count) {
  if (count <= 0 || rows.length === 0) return [];
  const sorted = [...rows].sort(compareQueueRows);
  if (sorted.length <= count) return sorted;
  const picked = [];
  const seen = new Set();
  for (let index = 0; index < count; index += 1) {
    const position = Math.round((index * (sorted.length - 1)) / (count - 1 || 1));
    const row = sorted[position];
    if (!seen.has(row.recordingId)) {
      seen.add(row.recordingId);
      picked.push(row);
    }
  }
  for (const row of sorted) {
    if (picked.length >= count) break;
    if (!seen.has(row.recordingId)) {
      seen.add(row.recordingId);
      picked.push(row);
    }
  }
  return picked;
}

function compareQueueRows(left, right) {
  return compareValues(left.year, right.year) || compareValues(left.confidence, right.confidence) || left.recordingId.localeCompare(right.recordingId);
}

function summarizeRows(rows) {
  const buckets = {};
  const years = {};
  let reviewQueueSelected = 0;
  let reviewed = 0;
  for (const row of rows) {
    buckets[row.bucket] = (buckets[row.bucket] ?? 0) + 1;
    years[row.year ?? "unknown"] = (years[row.year ?? "unknown"] ?? 0) + 1;
    if (row.reviewQueueSelected) reviewQueueSelected += 1;
    if (row.spotCheckStatus === "reviewed") reviewed += 1;
  }
  return { rows: rows.length, buckets, years, reviewQueueSelected, reviewed };
}

function yearFor(capturedAt) {
  return capturedAt ? capturedAt.slice(0, 4) : "unknown";
}

function queryRows(rows, searchParams) {
  const filtered = filterRows(rows, Object.fromEntries(searchParams.entries()));
  const sorted = sortRows(filtered, searchParams.get("sort") ?? "capturedAt", searchParams.get("direction") ?? "asc");
  return sorted;
}

function filterRows(rows, filters = {}) {
  const q = normalize(filters.q);
  return rows.filter((row) => {
    if (filters.bucket && row.bucket !== filters.bucket) return false;
    if (filters.year && row.year !== filters.year) return false;
    if (filters.spotCheckStatus && row.spotCheckStatus !== filters.spotCheckStatus) return false;
    if (filters.transcriptStatus && row.transcriptStatus !== filters.transcriptStatus) return false;
    if (filters.lyricMatchStatus && row.lyricMatchStatus !== filters.lyricMatchStatus) return false;
    if (filters.reviewQueue === "selected" && !row.reviewQueueSelected) return false;
    if (!q) return true;
    return [row.filename, row.recordingId, row.bucket, row.rationale, row.transcriptLanguage, row.lyricMatchCluster, row.lyricMatchClusterLabel].some((value) => normalize(value).includes(q));
  });
}

function sortRows(rows, field = "capturedAt", direction = "asc") {
  const sortField = SORTABLE_FIELDS.has(field) ? field : "capturedAt";
  const multiplier = direction === "desc" ? -1 : 1;
  return [...rows].sort((left, right) => compareValues(left[sortField], right[sortField]) * multiplier);
}

function compareValues(left, right) {
  if (left === right) return 0;
  if (left === null || left === undefined) return 1;
  if (right === null || right === undefined) return -1;
  if (typeof left === "number" && typeof right === "number") return left - right;
  return String(left).localeCompare(String(right));
}

function normalize(value) {
  return String(value ?? "").trim().toLowerCase();
}

const ROWS_PAGE_LIMIT_MAX = 500;
const ROWS_PAGE_LIMIT_DEFAULT = 100;

// The lean projection behind `fields=list`: the scalar columns the corpus table renders.
// Everything heavy (rationale, transcript counts, wordless/lyric diagnostics) stays behind
// the full projection and GET /api/row/:recordingId.
const LIST_PROJECTION_FIELDS = Object.freeze([
  "recordingId",
  "filename",
  "capturedAt",
  "year",
  "durationSeconds",
  "bucket",
  "confidence",
  "spotCheckRecommended",
  "spotCheckStatus",
  "humanVerdict",
  "transcriptStatus",
  "transcriptHasWords",
  "transcriptWordCount",
  "lyricMatchCluster",
  "lyricMatchClusterLabel",
  "reviewQueueSelected",
  "reviewQueueOrder",
]);

function clampInteger(value, fallback, min, max) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

function paginateRows(rows, searchParams) {
  const limitParam = searchParams.get("limit");
  const offsetParam = searchParams.get("offset");
  if (limitParam === null && offsetParam === null) return { rows, page: null };
  const limit = clampInteger(limitParam, ROWS_PAGE_LIMIT_DEFAULT, 1, ROWS_PAGE_LIMIT_MAX);
  const offset = clampInteger(offsetParam, 0, 0, Number.MAX_SAFE_INTEGER);
  const pageRows = rows.slice(offset, offset + limit);
  return { rows: pageRows, page: { offset, limit, returned: pageRows.length, totalFiltered: rows.length } };
}

function projectRows(rows, fieldsParam) {
  if (fieldsParam === null) return rows;
  if (fieldsParam !== "list") throw httpError(400, "unsupported_fields_projection");
  return rows.map((row) => {
    const lean = {};
    for (const field of LIST_PROJECTION_FIELDS) lean[field] = row[field];
    return lean;
  });
}

function buildServeState(rowsPayload, options = {}) {
  return {
    rowsPayload,
    rowsById: new Map(rowsPayload.rows.map((row) => [row.recordingId, row])),
    voiceTrends: options.voiceTrends ?? null,
    songJourneys: options.songJourneys ?? null,
    refereePool: options.refereePool ?? [],
    featuresManifest: options.featuresManifest ?? null,
  };
}

// Everything the routes serve, reloadable in place: after a successful watchd
// pipeline run the holder's state is swapped for freshly-loaded manifests, so
// the running server never serves stale rows and never needs a restart.
async function loadServeState(options) {
  const rowsPayload = await loadRowsPayload(options);
  const featuresManifest = await readOptionalJson(options.features);
  const lyricsManifest = await readOptionalJson(options.lyrics);
  const voiceTrends = buildVoiceTrends(featuresManifest, rowsPayload.rows, { lyricsManifest });
  const journeysManifest = await readOptionalJson(options.journeys);
  const songJourneys = journeysManifest ? { available: true, ...journeysManifest } : null;
  const refereePool = buildRefereePairs(lyricsManifest, featuresManifest, { seed: options.refereeSeed });
  return buildServeState(rowsPayload, { voiceTrends, songJourneys, refereePool, featuresManifest });
}

function createRequestHandler(rowsPayload, options = {}) {
  const holder = options.serveState ?? { state: buildServeState(rowsPayload, options) };
  const playback = {
    enabled: Boolean(options.corpusRoot && options.playbackApproval),
    corpusRoot: options.corpusRoot,
    approval: options.playbackApproval,
  };
  const transcriptText = {
    enabled: Boolean(options.transcriptDir),
    transcriptDir: options.transcriptDir,
  };
  const featureDetail = {
    enabled: Boolean(options.featureDir),
    featureDir: options.featureDir,
  };

  const webDist = options.webDist ?? DEFAULT_WEB_DIST;
  const intake = options.intake ?? null;

  return async (request, response) => {
    try {
      const { rowsPayload, rowsById, voiceTrends, songJourneys, refereePool, featuresManifest } = holder.state;
      const url = new URL(request.url ?? "/", "http://voice-journey.local");
      if (request.method === "GET" && url.pathname === "/healthz") {
        sendJson(response, 200, { ok: true });
        return;
      }
      if (intake && await intake.handle(request, response, url)) return;
      if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/journey" || url.pathname === "/referee" || url.pathname === "/practice")) {
        await sendSpaShell(response, webDist);
        return;
      }
      if (request.method === "GET" && url.pathname === "/assets/app.js") {
        await sendWebAsset(response, path.join(webDist, "app.js"), "text/javascript; charset=utf-8");
        return;
      }
      if (request.method === "GET" && url.pathname === "/assets/app.css") {
        await sendWebAsset(response, path.join(webDist, "app.css"), "text/css; charset=utf-8");
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/trends") {
        sendJson(response, 200, voiceTrends ?? { available: false, reason: "trends_not_computed" });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/journeys") {
        sendJson(response, 200, songJourneys ?? { available: false, reason: "song_journeys_manifest_not_loaded", launchHint: "run npm run journeys -- analyze on the host, or relaunch with --journeys manifests/song-journeys.json" });
        return;
      }
      if (request.method === "GET" && url.pathname === "/report") {
        try {
          sendHtml(response, await readFile(options.report ?? VJ.artifact("evidence-report.html"), "utf8"));
        } catch (error) {
          if (error?.code !== "ENOENT") throw error;
          sendHtml(response, `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Evidence Report</title><style>body{background:#10131a;color:#c5ccda;font-family:system-ui;padding:40px}code{background:#0f131b;border-radius:5px;padding:2px 7px}a{color:#8fb3e8}</style></head><body><p>No evidence report generated yet. Run <code>npm run report</code> on the host, then reload — the page serves the file live, no relaunch needed.</p><p><a href="/journey">&larr; back to Journey</a></p></body></html>`);
        }
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/referee/next") {
        const pool = refereePool ?? [];
        if (!playback.enabled) {
          sendJson(response, 200, { enabled: false, reason: "playback_requires_release_gate_approval", poolSize: pool.length });
          return;
        }
        const state = await readRefereeState(options.refereeState ?? DEFAULT_REFEREE_STATE);
        const judged = new Set(state.judgments.map((judgment) => judgment.pairId));
        const nextPair = pool.find((pair) => !judged.has(pair.pairId)) ?? null;
        sendJson(response, 200, {
          enabled: true,
          progress: { judged: judged.size, poolSize: pool.length },
          pair: nextPair ? {
            pairId: nextPair.pairId,
            a: { audioUrl: `/api/audio/${encodeURIComponent(nextPair.aId)}`, gainDb: nextPair.aGainDb ?? 0 },
            b: { audioUrl: `/api/audio/${encodeURIComponent(nextPair.bId)}`, gainDb: nextPair.bGainDb ?? 0 },
          } : null,
        });
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/referee/verdicts") {
        const body = JSON.parse(await readRequestBody(request) || "{}");
        const pool = refereePool ?? [];
        const pair = pool.find((candidate) => candidate.pairId === String(body.pairId ?? ""));
        if (!pair) throw httpError(404, "pair_not_found");
        const choice = String(body.choice ?? "");
        if (!REFEREE_CHOICES.has(choice)) throw httpError(400, "invalid_choice");
        const judgment = {
          pairId: pair.pairId,
          aId: pair.aId,
          bId: pair.bId,
          clusterId: pair.clusterId,
          aYear: pair.aYear,
          bYear: pair.bYear,
          choice,
          judgedAt: body.judgedAt ? String(body.judgedAt) : new Date().toISOString(),
          source: "referee",
        };
        const statePath = options.refereeState ?? DEFAULT_REFEREE_STATE;
        const state = upsertRefereeJudgment(await readRefereeState(statePath), judgment);
        await writeJson(statePath, state);
        sendJson(response, 200, {
          ok: true,
          reveal: {
            aYear: pair.aYear,
            bYear: pair.bYear,
            aCapturedAt: pair.aCapturedAt,
            bCapturedAt: pair.bCapturedAt,
            clusterId: pair.clusterId,
            clusterLabel: pair.clusterLabel,
          },
          progress: { judged: state.judgments.length, poolSize: pool.length },
        });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/referee/results") {
        const state = await readRefereeState(options.refereeState ?? DEFAULT_REFEREE_STATE);
        sendJson(response, 200, { enabled: Boolean(playback.enabled), poolSize: (refereePool ?? []).length, ...bradleyTerryByYear(state.judgments) });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/rows") {
        const filtered = queryRows(rowsPayload.rows, url.searchParams);
        const { rows: pageRows, page } = paginateRows(filtered, url.searchParams);
        const rows = projectRows(pageRows, url.searchParams.get("fields"));
        sendJson(response, 200, { ...rowsPayload, playback: playbackSummary(playback), transcriptText: transcriptTextSummary(transcriptText), totals: summarizeRows(filtered), ...(page ? { page } : {}), rows });
        return;
      }
      if (request.method === "GET" && url.pathname.startsWith("/api/row/")) {
        const recordingId = decodeURIComponent(url.pathname.slice("/api/row/".length));
        const row = rowsById.get(recordingId);
        if (!row) throw httpError(404, "row_not_found");
        sendJson(response, 200, { schemaVersion: rowsPayload.schemaVersion, row });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/review-queue") {
        const rows = sortRows(rowsPayload.rows.filter((row) => row.reviewQueueSelected), "reviewQueueOrder", "asc");
        sendJson(response, 200, { strategy: rowsPayload.reviewQueue.strategy, rows });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/summary") {
        sendJson(response, 200, {
          schemaVersion: rowsPayload.schemaVersion,
          source: rowsPayload.source,
          playback: playbackSummary(playback),
          transcriptText: transcriptTextSummary(transcriptText),
          featureDetail: featureDetailSummary(featureDetail),
          reviewQueue: rowsPayload.reviewQueue,
          totals: rowsPayload.totals,
          filterOptions: filterOptionsFor(rowsPayload.rows),
        });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/goals") {
        const state = await readGoalsState(options.goalsState ?? DEFAULT_GOALS_STATE);
        sendJson(response, 200, goalsPayload(state, goalContext(rowsPayload.rows, featuresManifest)));
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/goals") {
        const body = JSON.parse(await readRequestBody(request) || "{}");
        const spec = validateGoalSpec(body);
        const statePath = options.goalsState ?? DEFAULT_GOALS_STATE;
        const state = await readGoalsState(statePath);
        const goal = createGoal(spec, { now: Date.now() });
        state.goals.push(goal);
        await writeJson(statePath, state);
        sendJson(response, 200, { ok: true, goal });
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/goals/abandon") {
        const body = JSON.parse(await readRequestBody(request) || "{}");
        const statePath = options.goalsState ?? DEFAULT_GOALS_STATE;
        const state = await readGoalsState(statePath);
        const goal = state.goals.find((candidate) => candidate.goalId === String(body.goalId ?? ""));
        if (!goal) throw httpError(404, "goal_not_found");
        goal.status = "abandoned";
        goal.abandonedAt = new Date().toISOString();
        await writeJson(statePath, state);
        sendJson(response, 200, { ok: true });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/coach") {
        sendJson(response, 200, buildCoachPayload({ rows: rowsPayload.rows, featuresManifest, journeysManifest: songJourneys, voiceTrends }));
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/watch/status") {
        sendJson(response, 200, options.watchd ? options.watchd.statusSnapshot() : { enabled: false, reason: "watch_not_enabled" });
        return;
      }
      if (request.method === "GET" && url.pathname.startsWith("/api/audio/")) {
        await handleAudioRequest(response, decodeURIComponent(url.pathname.slice("/api/audio/".length)), rowsById, playback, request.headers.range);
        return;
      }
      if (request.method === "GET" && url.pathname.startsWith("/api/transcript/")) {
        await handleTranscriptRequest(response, decodeURIComponent(url.pathname.slice("/api/transcript/".length)), rowsById, transcriptText);
        return;
      }
      if (request.method === "GET" && url.pathname.startsWith("/api/feature-detail/")) {
        await handleFeatureDetailRequest(response, decodeURIComponent(url.pathname.slice("/api/feature-detail/".length)), rowsById, featureDetail);
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/verdicts") {
        const verdict = await parseVerdictRequest(request, rowsById);
        const state = upsertVerdict(await readState(options.state), verdict);
        await writeState(options.state, state);
        applyVerdictToRows(rowsPayload.rows, verdict);
        rowsPayload.source.state.verdictCount = state.verdicts.length;
        rowsPayload.totals = summarizeRows(rowsPayload.rows);
        sendJson(response, 200, { ok: true, verdict, totals: rowsPayload.totals });
        return;
      }
      if (!["GET", "POST"].includes(request.method)) {
        sendJson(response, 405, { error: "method_not_allowed" });
        return;
      }
      sendJson(response, 404, { error: "not_found" });
    } catch (error) {
      sendJson(response, error.statusCode ?? 500, { error: error.message });
    }
  };
}

function playbackSummary(playback) {
  return {
    enabled: playback.enabled,
    releaseGateRequired: !playback.enabled,
    readScope: {
      operatorClickedRecordingsOnly: true,
      bulkRead: false,
      retainedAudioBytes: false,
      audibleDerivedArtifacts: false,
      upload: false,
      corpusMutation: false,
    },
  };
}

function transcriptTextSummary(transcriptText) {
  return {
    enabled: transcriptText.enabled,
    releaseGateRequired: false,
    readScope: {
      localTranscriptText: transcriptText.enabled,
      explicitRecordingRequestsOnly: true,
      bulkRead: false,
      transcriptTextInRowJson: false,
      transcriptTextCommittedToRepo: false,
      audioBytes: false,
      upload: false,
      corpusMutation: false,
    },
  };
}

function featureDetailSummary(featureDetail) {
  return {
    enabled: featureDetail.enabled,
    releaseGateRequired: false,
    readScope: {
      localFeatureArrays: featureDetail.enabled,
      explicitRecordingRequestsOnly: true,
      bulkRead: false,
      featureArraysCommittedToRepo: false,
      audioBytes: false,
      transcriptText: false,
      upload: false,
      corpusMutation: false,
    },
  };
}

async function handleFeatureDetailRequest(response, recordingId, rowsById, featureDetail) {
  const row = rowsById.get(recordingId);
  if (!row) {
    sendJson(response, 404, { error: "recording_not_indexed" });
    return;
  }
  if (!featureDetail.enabled) {
    sendJson(response, 403, { error: "feature_dir_not_configured", recordingId });
    return;
  }
  const root = path.resolve(featureDetail.featureDir);
  const detailPath = path.resolve(root, `${recordingId}.json`);
  const relative = path.relative(root, detailPath);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    sendJson(response, 400, { error: "invalid_feature_detail_path", recordingId });
    return;
  }
  try {
    const detail = JSON.parse(await readFile(detailPath, "utf8"));
    sendJson(response, 200, {
      recordingId,
      filename: row.filename,
      capturedAt: row.capturedAt,
      year: row.year,
      bucket: row.bucket,
      methodId: detail.methodId ?? null,
      summary: detail.summary ?? null,
      detail: detail.detail ?? null,
      localOnly: true,
      repoSafe: { featureArraysCommittedToRepo: false, audioBytes: false, transcriptText: false, upload: false },
    });
  } catch (error) {
    if (error?.code === "ENOENT") {
      sendJson(response, 404, { error: "feature_detail_missing", recordingId });
      return;
    }
    throw error;
  }
}

async function handleTranscriptRequest(response, recordingId, rowsById, transcriptText) {
  const row = rowsById.get(recordingId);
  if (!row) {
    sendJson(response, 404, { error: "recording_not_indexed" });
    return;
  }
  if (!transcriptText.enabled) {
    sendJson(response, 403, { error: "transcript_dir_not_configured", recordingId });
    return;
  }
  if (row.transcriptStatus !== "completed" || !row.transcriptTextLocalOnly) {
    sendJson(response, 404, { error: "transcript_not_available", recordingId });
    return;
  }
  const root = path.resolve(transcriptText.transcriptDir);
  const transcriptPath = path.resolve(root, `${recordingId}.txt`);
  const relative = path.relative(root, transcriptPath);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    sendJson(response, 400, { error: "invalid_transcript_path", recordingId });
    return;
  }
  try {
    const text = await readFile(transcriptPath, "utf8");
    sendJson(response, 200, {
      recordingId,
      status: row.transcriptStatus,
      language: row.transcriptLanguage,
      wordCount: row.transcriptWordCount,
      text,
      localOnly: true,
      repoSafe: { transcriptTextCommittedToRepo: false, audioBytes: false, upload: false },
    });
  } catch (error) {
    if (error?.code === "ENOENT") {
      sendJson(response, 404, { error: "transcript_text_missing", recordingId });
      return;
    }
    throw error;
  }
}

async function handleAudioRequest(response, recordingId, rowsById, playback, rangeHeader) {
  const row = rowsById.get(recordingId);
  if (!row) {
    sendJson(response, 404, { error: "recording_not_indexed" });
    return;
  }
  if (!playback.enabled) {
    sendJson(response, 403, { error: "playback_requires_release_gate_approval", recordingId });
    return;
  }
  let handle;
  let stats;
  try {
    handle = await readHandleThroughSeam(playback.corpusRoot, recordingId, playback.approval);
    if (handle.recordingId !== recordingId && handle.filename !== row.filename) {
      sendJson(response, 409, { error: "seam_handle_mismatch", recordingId });
      return;
    }
    stats = await stat(handle.hostPath);
  } catch (error) {
    // The OS refusing the corpus root (macOS TCC on the Voice Memos group
    // container, or plain permissions) is an operator-fixable state, not a
    // server bug — surface it as its own status so the browser can say so.
    if (/EPERM|EACCES|operation not permitted|permission denied/iu.test(String(error?.message ?? error))) {
      sendJson(response, 503, {
        error: "corpus_access_denied",
        recordingId,
        hint: "The service cannot read the corpus root. On macOS, grant Full Disk Access to the node binary running this service (re-grant after node upgrades), then retry.",
      });
      return;
    }
    throw error;
  }
  const range = parseRange(rangeHeader, stats.size);
  const headers = {
    "content-type": "audio/mp4",
    "accept-ranges": "bytes",
    "cache-control": "no-store",
  };
  if (range) {
    headers["content-range"] = `bytes ${range.start}-${range.end}/${stats.size}`;
    headers["content-length"] = String(range.end - range.start + 1);
    response.writeHead(206, headers);
    createReadStream(handle.hostPath, { start: range.start, end: range.end }).pipe(response);
    return;
  }
  headers["content-length"] = String(stats.size);
  response.writeHead(200, headers);
  createReadStream(handle.hostPath).pipe(response);
}

async function readHandleThroughSeam(corpusRoot, recordingId, approval) {
  const { stdout } = await execFileAsync(process.execPath, [
    path.join(path.dirname(new URL(import.meta.url).pathname), "host-access.mjs"),
    "--corpus-root",
    corpusRoot,
    "read-handle",
    recordingId,
    "--approval",
    approval,
  ]);
  const payload = JSON.parse(stdout);
  return payload.handle;
}

function parseRange(rangeHeader, size) {
  if (!rangeHeader) return null;
  const match = /^bytes=(\d*)-(\d*)$/u.exec(rangeHeader);
  if (!match) return null;
  const start = match[1] === "" ? 0 : Number(match[1]);
  const end = match[2] === "" ? size - 1 : Number(match[2]);
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end >= size) return null;
  return { start, end };
}

async function parseVerdictRequest(request, rowsById) {
  const body = await readRequestBody(request);
  const payload = JSON.parse(body || "{}");
  const recordingId = String(payload.recordingId ?? "");
  const verdict = String(payload.verdict ?? "");
  if (!rowsById.has(recordingId)) throw httpError(404, "recording_not_indexed");
  if (!ALLOWED_VERDICTS.has(verdict)) throw httpError(400, "invalid_verdict");
  return {
    recordingId,
    verdict,
    note: payload.note ? String(payload.note) : null,
    reviewedBy: payload.reviewedBy ? String(payload.reviewedBy) : "operator",
    reviewedAt: payload.reviewedAt ? String(payload.reviewedAt) : new Date().toISOString(),
    source: "corpus-browser",
  };
}

function applyVerdictToRows(rows, verdict) {
  const row = rows.find((candidate) => candidate.recordingId === verdict.recordingId);
  if (!row) return;
  row.spotCheckStatus = "reviewed";
  row.humanVerdict = verdict.verdict;
  row.reviewedBy = verdict.reviewedBy;
  row.reviewedAt = verdict.reviewedAt;
}

function readRequestBody(request) {
  return new Promise((resolve, reject) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
      if (body.length > 64 * 1024) reject(httpError(413, "request_too_large"));
    });
    request.on("end", () => resolve(body));
    request.on("error", reject);
  });
}

function httpError(statusCode, message) {
  const error = new Error(message);
  error.statusCode = statusCode;
  return error;
}

function filterOptionsFor(rows) {
  return {
    buckets: uniqueSorted(rows.map((row) => row.bucket)),
    years: uniqueSorted(rows.map((row) => row.year)),
    spotCheckStatuses: uniqueSorted(rows.map((row) => row.spotCheckStatus)),
    transcriptStatuses: uniqueSorted(rows.map((row) => row.transcriptStatus)),
    lyricMatchStatuses: uniqueSorted(rows.map((row) => row.lyricMatchStatus)),
  };
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort();
}

function sendHtml(response, html) {
  response.writeHead(200, {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(html);
}

// The React SPA shell for /, /journey and /referee (the bundle routes by pathname).
// The bundle is built into the gitignored web/dist by the host (`npm run build` in web/);
// assets are read request-time, so a rebuild goes live on reload without a relaunch.
function spaHtml() {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Voice Journey</title>
  <link rel="stylesheet" href="/assets/app.css">
</head>
<body>
  <div id="app"></div>
  <script type="module" src="/assets/app.js"></script>
</body>
</html>`;
}

async function sendSpaShell(response, webDist) {
  try {
    await stat(path.join(webDist, "app.js"));
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    sendHtml(response, `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Voice Journey</title><style>body{background:#10131a;color:#c5ccda;font-family:system-ui;padding:40px}code{background:#0f131b;border-radius:5px;padding:2px 7px}a{color:#8fb3e8}</style></head><body><p>The web bundle is not built on this host yet. Run <code>npm install</code> then <code>npm run build</code> inside <code>web/</code>, then reload — assets are served live, no relaunch needed.</p><p>The JSON API (<a href="/api/summary">/api/summary</a>, <code>/api/rows</code>, …) works without it.</p></body></html>`);
    return;
  }
  sendHtml(response, spaHtml());
}

async function sendWebAsset(response, filePath, contentType) {
  try {
    const body = await readFile(filePath);
    response.writeHead(200, { "content-type": contentType, "cache-control": "no-store" });
    response.end(body);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    sendJson(response, 404, { error: "web_bundle_not_built", hint: "run npm install && npm run build inside web/" });
  }
}

function sendJson(response, status, payload) {
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(`${JSON.stringify(payload, null, 2)}\n`);
}

async function startServers(rowsPayload, options = {}) {
  const { hosts = DEFAULT_HOSTS, port = DEFAULT_PORT } = options;
  // One shared serve-state holder across the per-host handlers, so a reload
  // swaps data for every listener at once.
  const serveState = options.serveState ?? { state: buildServeState(rowsPayload, options) };
  if (options.payloadReload) {
    options.payloadReload.fn = async () => {
      serveState.state = await loadServeState(options);
    };
  }
  const handlerOptions = { ...options, serveState };
  const servers = [];
  for (const host of hosts) {
    const server = http.createServer(createRequestHandler(rowsPayload, handlerOptions));
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, host, () => {
        server.off("error", reject);
        resolve();
      });
    });
    servers.push({ host, port: server.address().port, server });
  }
  return servers;
}

async function closeServers(servers) {
  await Promise.all(servers.map(({ server }) => new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  })));
}

function mergeVerdictsIntoResults(resultsManifest, state) {
  const stateById = verdictMapFor(state);
  let mergedCount = 0;
  const results = (resultsManifest.results ?? []).map((result) => {
    const verdict = stateById.get(result.recordingId);
    if (!verdict) return result;
    mergedCount += 1;
    return {
      ...result,
      humanSpotCheck: {
        ...(result.humanSpotCheck ?? {}),
        recommended: result.humanSpotCheck?.recommended ?? true,
        status: "reviewed",
        verdict: verdict.verdict,
        reviewedBy: verdict.reviewedBy,
        reviewedAt: verdict.reviewedAt,
        notes: verdict.note,
        source: verdict.source,
      },
    };
  });
  return {
    ...resultsManifest,
    updatedAt: new Date().toISOString(),
    humanSpotCheckMerge: {
      sourceStateSchemaVersion: state.schemaVersion,
      mergedCount,
      mergedAt: new Date().toISOString(),
      repoSafe: true,
      audioBytes: false,
      transcriptText: false,
    },
    results,
  };
}

function quantileOf(values, p) {
  const sorted = values.filter((value) => value !== null && value !== undefined && Number.isFinite(value)).sort((left, right) => left - right);
  if (sorted.length === 0) return null;
  return sorted[Math.floor((sorted.length - 1) * p)];
}

function medianOf(values) {
  return quantileOf(values, 0.5);
}

function roundTo(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
}

function quarterFor(capturedAt) {
  if (!capturedAt || capturedAt.length < 7) return null;
  const month = Number(capturedAt.slice(5, 7));
  if (!Number.isInteger(month) || month < 1 || month > 12) return null;
  return `${capturedAt.slice(0, 4)}-Q${Math.ceil(month / 3)}`;
}

function totalRejectionShareFor(featureRow) {
  const voicing = featureRow.features?.voicing ?? {};
  return (voicing.rejectedOutlierShare ?? 0) + (voicing.rejectedGlobalOutlierShare ?? 0);
}

function quarterRollupsFor(source, completedByQuarter) {
  const byQuarter = new Map();
  for (const row of source) {
    const quarter = quarterFor(row.capturedAt);
    if (!quarter) continue;
    if (!byQuarter.has(quarter)) byQuarter.set(quarter, []);
    byQuarter.get(quarter).push(row);
  }
  return [...byQuarter.keys()].sort().map((quarter) => {
    const group = byQuarter.get(quarter);
    const pitch = (selector) => group.map((row) => row.features.pitch?.f0Hz?.[selector] ?? null);
    const medians = group.map((row) => row.features.pitch?.f0Hz?.p50 ?? null).filter((value) => value !== null);
    const registerCounts = { low: 0, mid: 0, high: 0 };
    for (const median of medians) {
      if (median < 200) registerCounts.low += 1;
      else if (median < 300) registerCounts.mid += 1;
      else registerCounts.high += 1;
    }
    const registerTotal = Math.max(medians.length, 1);
    const series = (selector) => group.map(selector);
    const iqr = (values) => [roundTo(quantileOf(values, 0.25), 2), roundTo(quantileOf(values, 0.75), 2)];
    const rates = series((row) => row.features.vibrato?.meanRateHz ?? null);
    const extents = series((row) => row.features.vibrato?.meanExtentCents ?? null);
    const timeShares = series((row) => row.features.vibrato?.vibratoTimeShare ?? null);
    const sustains = series((row) => row.features.phrasing?.longestSustainedSeconds ?? null);
    const cppsValues = series((row) => row.features.quality?.cpps ?? null);
    const hnrValues = series((row) => row.features.quality?.meanHnrDb ?? null);
    return {
      quarter,
      n: completedByQuarter.get(quarter) ?? group.length,
      nReliable: group.length,
      envelope: { loHz: roundTo(quantileOf(pitch("p05"), 0.05), 1), hiHz: roundTo(quantileOf(pitch("p95"), 0.95), 1) },
      typical: { loHz: roundTo(medianOf(pitch("p25")), 1), hiHz: roundTo(medianOf(pitch("p75")), 1) },
      medianHz: roundTo(medianOf(pitch("p50")), 1),
      registerShares: {
        low: roundTo(registerCounts.low / registerTotal),
        mid: roundTo(registerCounts.mid / registerTotal),
        high: roundTo(registerCounts.high / registerTotal),
      },
      vibrato: {
        rateHz: roundTo(medianOf(rates), 2),
        extentCents: roundTo(medianOf(extents), 1),
        timeShare: roundTo(medianOf(timeShares)),
      },
      sustainSeconds: roundTo(medianOf(sustains), 2),
      cpps: roundTo(medianOf(cppsValues), 2),
      hnr: roundTo(medianOf(hnrValues), 2),
      spread: {
        rate: iqr(rates),
        extent: iqr(extents),
        timeShare: iqr(timeShares),
        sustain: iqr(sustains),
        cpps: iqr(cppsValues),
        hnr: iqr(hnrValues),
      },
    };
  });
}

function monthIndexOf(month) {
  return Number(month.slice(0, 4)) * 12 + Number(month.slice(5, 7)) - 1;
}

function buildEras(monthlyCadence, reliable) {
  const MONTH_MIN = 40;
  const GAP_MAX_MONTHS = 2;
  const TOTAL_MIN = 90;
  const countByIndex = new Map(monthlyCadence.map(({ month, count }) => [monthIndexOf(month), { month, count }]));
  const qualifying = monthlyCadence.filter(({ count }) => count >= MONTH_MIN).map(({ month }) => monthIndexOf(month)).sort((a, b) => a - b);
  const runs = [];
  for (const index of qualifying) {
    const current = runs.at(-1);
    if (current && index - current.end <= GAP_MAX_MONTHS + 1) current.end = index;
    else runs.push({ start: index, end: index });
  }
  return runs.map((run) => {
    let totalTakes = 0;
    for (let index = run.start; index <= run.end; index += 1) totalTakes += countByIndex.get(index)?.count ?? 0;
    const monthAt = (index) => `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, "0")}`;
    const start = monthAt(run.start);
    const end = monthAt(run.end);
    const inEra = reliable
      .filter((row) => {
        const month = row.capturedAt ? row.capturedAt.slice(0, 7) : null;
        return month && month >= start && month <= end;
      })
      .sort((left, right) => String(left.capturedAt).localeCompare(String(right.capturedAt)));
    const sampleRecordingIds = [];
    if (inEra.length > 0) {
      for (let pick = 0; pick < Math.min(3, inEra.length); pick += 1) {
        const position = Math.round((pick * (inEra.length - 1)) / (Math.min(3, inEra.length) - 1 || 1));
        const id = inEra[position].recordingId;
        if (!sampleRecordingIds.includes(id)) sampleRecordingIds.push(id);
      }
    }
    return { start, end, totalTakes, sampleRecordingIds };
  }).filter((era) => era.totalTakes >= TOTAL_MIN);
}

function clusterThreads(lyricsManifest, limit = 8) {
  if (!lyricsManifest || !Array.isArray(lyricsManifest.clusters)) return [];
  const capturedById = new Map((lyricsManifest.results ?? []).map((row) => [row.recordingId, row.capturedAt ?? null]));
  return [...lyricsManifest.clusters]
    .map((cluster) => {
      const dates = (cluster.recordingIds ?? []).map((id) => capturedById.get(id)).filter(Boolean).sort();
      const perYear = {};
      for (const date of dates) {
        const year = date.slice(0, 4);
        perYear[year] = (perYear[year] ?? 0) + 1;
      }
      return {
        id: cluster.clusterId,
        label: cluster.label ?? cluster.clusterId,
        size: cluster.recordingIds?.length ?? 0,
        first: dates.at(0)?.slice(0, 7) ?? null,
        last: dates.at(-1)?.slice(0, 7) ?? null,
        perYear,
      };
    })
    .filter((thread) => thread.size >= 2 && thread.first)
    .sort((left, right) => right.size - left.size || left.id.localeCompare(right.id))
    .slice(0, limit);
}

function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function seededShuffle(items, random) {
  for (let i = items.length - 1; i > 0; i -= 1) {
    const j = Math.floor(random() * (i + 1));
    [items[i], items[j]] = [items[j], items[i]];
  }
  return items;
}

// Playback-side loudness matching for blind pairs: symmetric gains that bring
// both sides to their mutual mean RMS (from the committed per-take dynamics
// aggregates), capped so a missing or absurd stat can't produce a blast. The
// audio files themselves are untouched — the client applies these via WebAudio.
const LOUDNESS_MATCH_CAP_DB = 12;

function loudnessMatchGainsDb(aRmsDb, bRmsDb) {
  if (!Number.isFinite(aRmsDb) || !Number.isFinite(bRmsDb)) return { aGainDb: 0, bGainDb: 0 };
  const mean = (aRmsDb + bRmsDb) / 2;
  const clamp = (value) => Math.max(-LOUDNESS_MATCH_CAP_DB, Math.min(LOUDNESS_MATCH_CAP_DB, value));
  return {
    aGainDb: Number(clamp(mean - aRmsDb).toFixed(1)),
    bGainDb: Number(clamp(mean - bRmsDb).toFixed(1)),
  };
}

function buildRefereePairs(lyricsManifest, featuresManifest, { seed = DEFAULT_REFEREE_SEED, pairsPerCluster = REFEREE_PAIRS_PER_CLUSTER } = {}) {
  const reliableTakes = new Map();
  for (const row of featuresManifest?.results ?? []) {
    if (row.status !== "completed" || !row.features || !row.capturedAt) continue;
    const voicing = row.features.voicing ?? {};
    if (((voicing.rejectedOutlierShare ?? 0) + (voicing.rejectedGlobalOutlierShare ?? 0)) > RELIABLE_REJECTION_MAX) continue;
    reliableTakes.set(row.recordingId, {
      year: row.year ?? row.capturedAt.slice(0, 4),
      capturedAt: row.capturedAt,
      rmsDb: row.features.dynamics?.voicedRmsDbP50 ?? null,
    });
  }
  const random = mulberry32(seed);
  const perCluster = [];
  for (const cluster of lyricsManifest?.clusters ?? []) {
    const members = (cluster.recordingIds ?? []).filter((id) => reliableTakes.has(id));
    const candidates = [];
    for (let i = 0; i < members.length; i += 1) {
      for (let j = i + 1; j < members.length; j += 1) {
        if (reliableTakes.get(members[i]).year === reliableTakes.get(members[j]).year) continue;
        candidates.push([members[i], members[j]]);
      }
    }
    seededShuffle(candidates, random);
    const pairs = candidates.slice(0, pairsPerCluster).map(([left, right]) => {
      const flip = random() < 0.5;
      const aId = flip ? right : left;
      const bId = flip ? left : right;
      const identity = [left, right].sort().join("+");
      return {
        pairId: createHash("sha256").update(`${cluster.clusterId}:${identity}`).digest("hex").slice(0, 16),
        aId,
        bId,
        clusterId: cluster.clusterId,
        clusterLabel: cluster.label ?? cluster.clusterId,
        aYear: reliableTakes.get(aId).year,
        bYear: reliableTakes.get(bId).year,
        aCapturedAt: reliableTakes.get(aId).capturedAt,
        bCapturedAt: reliableTakes.get(bId).capturedAt,
        ...loudnessMatchGainsDb(reliableTakes.get(aId).rmsDb, reliableTakes.get(bId).rmsDb),
      };
    });
    if (pairs.length) perCluster.push(pairs);
  }
  seededShuffle(perCluster, random);
  const pool = [];
  for (let index = 0; ; index += 1) {
    let added = false;
    for (const pairs of perCluster) {
      if (pairs[index]) {
        pool.push(pairs[index]);
        added = true;
      }
    }
    if (!added) break;
  }
  return pool;
}

function emptyRefereeState() {
  return { schemaVersion: REFEREE_STATE_SCHEMA_VERSION, updatedAt: null, judgments: [] };
}

async function readGoalsState(filePath) {
  const fallback = { version: GOALS_STATE_SCHEMA_VERSION, goals: [] };
  try {
    const state = JSON.parse(await readFile(filePath, "utf8"));
    if (state?.version !== GOALS_STATE_SCHEMA_VERSION || !Array.isArray(state.goals)) return fallback;
    return state;
  } catch (error) {
    if (error?.code === "ENOENT") return fallback;
    throw error;
  }
}

async function readRefereeState(filePath) {
  try {
    const state = await readJson(filePath);
    if (state.schemaVersion !== REFEREE_STATE_SCHEMA_VERSION || !Array.isArray(state.judgments)) return emptyRefereeState();
    return state;
  } catch (error) {
    if (error?.code === "ENOENT") return emptyRefereeState();
    throw error;
  }
}

function upsertRefereeJudgment(state, judgment) {
  const next = {
    schemaVersion: REFEREE_STATE_SCHEMA_VERSION,
    updatedAt: judgment.judgedAt,
    judgments: (state.judgments ?? []).filter((entry) => entry.pairId !== judgment.pairId),
  };
  next.judgments.push(judgment);
  next.judgments.sort((left, right) => left.pairId.localeCompare(right.pairId));
  return next;
}

// Bradley–Terry across years via the Zermelo MM iteration. Ties (too_close)
// count as half a win each way; every year-pair with at least one comparison
// gets a +0.25 regularizing pseudo-win each way so one-sided records stay
// finite. Strengths are reported as log2 relative to the earliest year.
function bradleyTerryByYear(judgments) {
  const byChoice = { a: 0, b: 0, too_close: 0, not_same_song: 0, skip: 0 };
  const wins = new Map();
  const perYear = new Map();
  const flags = new Map();
  const addWin = (winner, loser, amount) => {
    const key = `${winner}|${loser}`;
    wins.set(key, (wins.get(key) ?? 0) + amount);
  };
  const yearRow = (year) => {
    if (!perYear.has(year)) perYear.set(year, { year, wins: 0, losses: 0, ties: 0, trials: 0 });
    return perYear.get(year);
  };
  for (const judgment of judgments) {
    if (byChoice[judgment.choice] !== undefined) byChoice[judgment.choice] += 1;
    if (judgment.choice === "not_same_song") {
      flags.set(judgment.clusterId, (flags.get(judgment.clusterId) ?? 0) + 1);
      continue;
    }
    if (judgment.choice === "skip") continue;
    const { aYear, bYear } = judgment;
    if (!aYear || !bYear || aYear === bYear) continue;
    if (judgment.choice === "a") {
      addWin(aYear, bYear, 1);
      yearRow(aYear).wins += 1;
      yearRow(bYear).losses += 1;
    } else if (judgment.choice === "b") {
      addWin(bYear, aYear, 1);
      yearRow(bYear).wins += 1;
      yearRow(aYear).losses += 1;
    } else if (judgment.choice === "too_close") {
      addWin(aYear, bYear, 0.5);
      addWin(bYear, aYear, 0.5);
      yearRow(aYear).ties += 1;
      yearRow(bYear).ties += 1;
    }
    yearRow(aYear).trials += 1;
    yearRow(bYear).trials += 1;
  }
  const years = [...perYear.keys()].sort();
  if (years.length >= 2) {
    for (let i = 0; i < years.length; i += 1) {
      for (let j = i + 1; j < years.length; j += 1) {
        const forward = wins.get(`${years[i]}|${years[j]}`) ?? 0;
        const backward = wins.get(`${years[j]}|${years[i]}`) ?? 0;
        if (forward + backward > 0) {
          addWin(years[i], years[j], 0.25);
          addWin(years[j], years[i], 0.25);
        }
      }
    }
    const strengths = new Map(years.map((year) => [year, 1]));
    for (let iteration = 0; iteration < 200; iteration += 1) {
      const updated = new Map();
      for (const year of years) {
        let totalWins = 0;
        let denominator = 0;
        for (const other of years) {
          if (other === year) continue;
          const won = wins.get(`${year}|${other}`) ?? 0;
          const lost = wins.get(`${other}|${year}`) ?? 0;
          const pairTotal = won + lost;
          if (pairTotal === 0) continue;
          totalWins += won;
          denominator += pairTotal / (strengths.get(year) + strengths.get(other));
        }
        updated.set(year, denominator > 0 ? Math.max(totalWins, 1e-6) / denominator : strengths.get(year));
      }
      const meanLog = [...updated.values()].reduce((sum, value) => sum + Math.log(value), 0) / updated.size;
      for (const year of years) strengths.set(year, updated.get(year) / Math.exp(meanLog));
    }
    const baseline = Math.log2(strengths.get(years[0]));
    for (const year of years) {
      perYear.get(year).strengthLog2 = roundTo(Math.log2(strengths.get(year)) - baseline, 3);
    }
  } else {
    for (const year of years) perYear.get(year).strengthLog2 = 0;
  }
  return {
    method: {
      id: "bradley_terry_by_year.v1",
      ties: "counted as half a win each way",
      regularization: "+0.25 pseudo-win each way per compared year pair",
      strengthScale: "log2 relative to the earliest judged year",
    },
    byChoice,
    judgedTotal: judgments.length,
    years: years.map((year) => perYear.get(year)),
    notSameSongFlags: [...flags.entries()].sort().map(([clusterId, count]) => ({ clusterId, count })),
  };
}

function buildVoiceTrends(featuresManifest, rows = [], { lyricsManifest = null } = {}) {
  const monthlyCadence = [];
  const cadenceByMonth = new Map();
  for (const row of rows) {
    const month = row.capturedAt ? row.capturedAt.slice(0, 7) : null;
    if (month) cadenceByMonth.set(month, (cadenceByMonth.get(month) ?? 0) + 1);
  }
  for (const month of [...cadenceByMonth.keys()].sort()) {
    monthlyCadence.push({ month, count: cadenceByMonth.get(month) });
  }

  if (!featuresManifest || !Array.isArray(featuresManifest.results)) {
    return {
      available: false,
      reason: "features_manifest_not_loaded",
      launchHint: "relaunch with --features manifests/local-voice-features.json",
      monthlyCadence,
    };
  }

  const completed = featuresManifest.results.filter((row) => row.status === "completed" && row.features);
  const reliable = completed.filter((row) => totalRejectionShareFor(row) <= RELIABLE_REJECTION_MAX);

  const completedByQuarter = new Map();
  for (const row of completed) {
    const quarter = quarterFor(row.capturedAt);
    if (!quarter) continue;
    completedByQuarter.set(quarter, (completedByQuarter.get(quarter) ?? 0) + 1);
  }
  const quarters = quarterRollupsFor(reliable, completedByQuarter);
  const quartersAllTakes = quarterRollupsFor(completed, completedByQuarter);

  const byYear = new Map();
  for (const row of reliable) {
    const year = row.year ?? yearFor(row.capturedAt);
    if (!byYear.has(year)) byYear.set(year, []);
    byYear.get(year).push(row);
  }
  const yearlyTops = [...byYear.keys()].sort()
    .map((year) => ({ year, group: byYear.get(year) }))
    .filter(({ group }) => group.length >= 10)
    .map(({ year, group }) => ({
      year,
      n: group.length,
      topHz: roundTo(quantileOf(group.map((row) => row.features.pitch?.f0Hz?.p95 ?? null), 0.95), 1),
    }))
    .filter(({ topHz }) => topHz !== null);
  let workingTop = null;
  if (yearlyTops.length >= 2) {
    const baseline = yearlyTops[0];
    const peak = yearlyTops.reduce((best, candidate) => (candidate.topHz > best.topHz ? candidate : best), yearlyTops[0]);
    workingTop = {
      baselineYear: baseline.year,
      baselineTopHz: baseline.topHz,
      peakYear: peak.year,
      peakTopHz: peak.topHz,
      gainSemitones: roundTo(12 * Math.log2(peak.topHz / baseline.topHz), 1),
    };
  }

  const vibratoTakes = completed
    .filter((row) => row.features.vibrato?.meanRateHz != null && row.features.vibrato?.meanExtentCents != null && row.capturedAt)
    .map((row) => ({
      id: row.recordingId,
      date: row.capturedAt.slice(0, 10),
      year: row.year ?? yearFor(row.capturedAt),
      rate: row.features.vibrato.meanRateHz,
      extent: row.features.vibrato.meanExtentCents,
      reliable: totalRejectionShareFor(row) <= RELIABLE_REJECTION_MAX,
    }));

  return {
    available: true,
    schemaVersion: TRENDS_SCHEMA_VERSION,
    source: {
      features: {
        schemaVersion: featuresManifest.schemaVersion ?? null,
        generatedAt: featuresManifest.generatedAt ?? null,
        updatedAt: featuresManifest.updatedAt ?? null,
        recordingCount: featuresManifest.results.length,
      },
      reliability: {
        rule: `total f0 frame rejection <= ${RELIABLE_REJECTION_MAX}`,
        reliableCount: reliable.length,
        flaggedCount: completed.length - reliable.length,
      },
    },
    headline: {
      takesAnalyzed: completed.length,
      reliableTakes: reliable.length,
      flaggedTakes: completed.length - reliable.length,
      hoursAnalyzed: roundTo(completed.reduce((sum, row) => sum + (row.features.durationSeconds ?? 0), 0) / 3600, 1),
      workingTop,
    },
    quarters,
    quartersAllTakes,
    yearlyTops,
    monthlyCadence,
    eras: buildEras(monthlyCadence, reliable),
    vibratoTakes,
    clusters: clusterThreads(lyricsManifest),
    readScope: {
      committedManifestsOnly: true,
      summaryAggregatesOnly: true,
      featureArrays: false,
      audioBytes: false,
      transcriptText: false,
      upload: false,
      corpusMutation: false,
    },
  };
}


async function loadRowsPayload(options) {
  const [indexManifest, resultsManifest, state, transcriptsManifest, lyricsManifest] = await Promise.all([
    readManifest(options.index, { recordings: [] }, options),
    readManifest(options.results, { results: [] }, options),
    readState(options.state),
    readOptionalJson(options.transcripts),
    readOptionalJson(options.lyrics),
  ]);
  return buildCorpusRows(indexManifest, resultsManifest, { state, queueSize: options.queueSize, transcriptsManifest, lyricsManifest });
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.help || options.command === "help") {
    printHelp();
    return 0;
  }
  if (options.command === "merge-verdicts") {
    const [resultsManifest, state] = await Promise.all([readJson(options.results), readState(options.state)]);
    const merged = mergeVerdictsIntoResults(resultsManifest, state);
    await writeJson(options.out ?? options.results, merged);
    process.stdout.write(`${JSON.stringify({ mergedCount: merged.humanSpotCheckMerge.mergedCount, out: options.out ?? options.results }, null, 2)}\n`);
    return 0;
  }
  if (options.command !== "serve") throw new CorpusBrowserError(`unsupported command: ${options.command}`);
  const rowsPayload = await loadRowsPayload(options);
  const featuresManifest = await readOptionalJson(options.features);
  const lyricsManifest = await readOptionalJson(options.lyrics);
  const voiceTrends = buildVoiceTrends(featuresManifest, rowsPayload.rows, { lyricsManifest });
  const journeysManifest = await readOptionalJson(options.journeys);
  const songJourneys = journeysManifest ? { available: true, ...journeysManifest } : null;
  const refereePool = buildRefereePairs(lyricsManifest, featuresManifest, { seed: options.refereeSeed });
  const payloadReload = { fn: null };
  let watchd = null;
  if (options.watch) {
    if (!options.corpusRoot || !options.watchApproval) {
      throw new CorpusBrowserError("--watch requires --corpus-root and --watch-approval (the recorded operator approval for the audio-reading pipeline stages)");
    }
    watchd = createWatchd({
      corpusRoot: options.corpusRoot,
      measurementRoot: VJ.measurementDir,
      approval: options.watchApproval,
      intervalMs: options.watchInterval * 1000,
      statePath: options.watchState,
      ntfyUrl: options.ntfy,
      digestDay: options.digestDay,
      digestHour: options.digestHour,
      repoRoot: process.cwd(),
      manifests: { index: options.index, results: options.results, transcripts: options.transcripts, lyrics: options.lyrics, journeys: options.journeys },
    }, {
      reloadServeState: async () => { if (payloadReload.fn) await payloadReload.fn(); },
    });
  }
  const intake = createIntake({
    measurementDir: VJ.measurementDir,
    stagingDir: VJ.stagingDir,
    chainsPath: VJ.chains,
  });
  const servers = await startServers(rowsPayload, { ...options, intake, voiceTrends, songJourneys, refereePool, featuresManifest, payloadReload, watchd });
  if (watchd) watchd.start();
  const endpoints = servers.map(({ host, port }) => `http://${host}:${port}/`);
  process.stdout.write(`${JSON.stringify({ endpoints, rows: rowsPayload.rows.length, reviewQueue: rowsPayload.reviewQueue, playback: playbackSummary({ enabled: Boolean(options.corpusRoot && options.playbackApproval) }), transcriptText: transcriptTextSummary({ enabled: Boolean(options.transcriptDir) }), featureDetail: featureDetailSummary({ enabled: Boolean(options.featureDir) }), voiceTrends: { available: voiceTrends.available, quarters: voiceTrends.quarters?.length ?? 0 }, songJourneys: { available: Boolean(songJourneys), clusters: songJourneys?.journeys?.length ?? 0 }, referee: { poolSize: refereePool.length, statePath: options.refereeState }, watch: watchd ? { enabled: true, intervalSeconds: options.watchInterval, ntfy: Boolean(options.ntfy), statePath: options.watchState } : { enabled: false }, statePath: options.state }, null, 2)}\n`);
  if (options.once) {
    watchd?.stop();
    await closeServers(servers);
  }
  return 0;
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
  bradleyTerryByYear,
  buildCorpusRows,
  loudnessMatchGainsDb,
  buildRefereePairs,
  buildVoiceTrends,
  closeServers,
  createRequestHandler,
  filterRows,
  mergeVerdictsIntoResults,
  paginateRows,
  projectRows,
  queryRows,
  readRefereeState,
  readState,
  run,
  selectReviewQueue,
  sortRows,
  startServers,
};
