// coach.mjs — phase E of docs/breathing-studio-plan.md: the coaching layer.
//
// Turns the committed, repo-safe manifests into practice recommendations:
//   - due-for-a-take: spaced re-recording for the same-song engine, ranked by
//     how much evidence the next take buys (staleness × history, span
//     extension, eligibility proximity);
//   - the frontier: songs sitting one measured step from a threshold, each
//     flag carrying the numbers that say so.
//
// Pure functions over aggregates only — no audio, no transcript text, and
// every suggestion states its reasons as data ("why" chips). Evidence-thin
// findings are framed as "add data", never as verdict-chasing.

const MS_PER_DAY = 86_400_000;
const DAYS_PER_YEAR = 365.25;

export const DUE_AFTER_DAYS = 90;
const MIN_RECURRING_TAKES = 3;
const ELIGIBLE_CLEAN_TAKES = 6;
const ELIGIBLE_SPAN_YEARS = 1.5;
const RECENT_TAKES_WINDOW = 10;
const VIBRATO_SETTLING_BAND = [4.5, 5.2];
const SETTLED_ZONE_HZ = 5.5;

function toTime(value) {
  const time = Date.parse(value ?? "");
  return Number.isNaN(time) ? null : time;
}

function median(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((left, right) => left - right);
  if (!sorted.length) return null;
  return sorted[Math.floor((sorted.length - 1) / 2)];
}

// Rows → per-cluster rollup (id, privacy-generic label, take counts, first/last).
export function clusterRollup(rows) {
  const clusters = new Map();
  for (const row of rows ?? []) {
    const clusterId = row.lyricMatchCluster;
    if (!clusterId) continue;
    if (!clusters.has(clusterId)) {
      clusters.set(clusterId, { clusterId, label: null, takeCount: 0, cleanCount: 0, firstAt: null, lastAt: null, recordingIds: [] });
    }
    const cluster = clusters.get(clusterId);
    cluster.takeCount += 1;
    if (row.bucket === "clean_singing") cluster.cleanCount += 1;
    cluster.label = cluster.label ?? row.lyricMatchClusterLabel ?? null;
    cluster.recordingIds.push(row.recordingId);
    const at = toTime(row.capturedAt);
    if (at !== null) {
      if (cluster.firstAt === null || at < cluster.firstAt) cluster.firstAt = at;
      if (cluster.lastAt === null || at > cluster.lastAt) cluster.lastAt = at;
    }
  }
  return [...clusters.values()];
}

export function buildDueSongs({ rows, journeysManifest, now, dueAfterDays = DUE_AFTER_DAYS, limit = 12 } = {}) {
  const journeysById = new Map((journeysManifest?.journeys ?? []).map((journey) => [journey.clusterId, journey]));
  const due = [];
  for (const cluster of clusterRollup(rows)) {
    if (cluster.takeCount < MIN_RECURRING_TAKES || cluster.lastAt === null || cluster.firstAt === null) continue;
    const daysSinceLast = (now - cluster.lastAt) / MS_PER_DAY;
    if (daysSinceLast < dueAfterDays) continue;

    const journey = journeysById.get(cluster.clusterId) ?? null;
    const spanGainYears = daysSinceLast / DAYS_PER_YEAR;
    const spanIncludingTodayYears = (now - cluster.firstAt) / MS_PER_DAY / DAYS_PER_YEAR;
    const cleanShort = Math.max(0, ELIGIBLE_CLEAN_TAKES - cluster.cleanCount);
    const nearEligible = !journey && cleanShort > 0 && cleanShort <= 2 && spanIncludingTodayYears >= ELIGIBLE_SPAN_YEARS;

    // Evidence-gain heuristic: history depth × staleness, plus a boost when a
    // take today either extends a judged song's span or completes eligibility.
    const score = Math.log2(1 + cluster.takeCount) * Math.min(0.5 + daysSinceLast / DAYS_PER_YEAR, 2.5)
      + (journey ? Math.min(spanGainYears, 1.5) : 0)
      + (nearEligible ? 3 : 0);

    const firstYear = new Date(cluster.firstAt).getUTCFullYear();
    const lastYear = new Date(cluster.lastAt).getUTCFullYear();
    const why = [
      `last sung ${Math.round(daysSinceLast)} d ago`,
      `${cluster.takeCount} takes, ${firstYear}–${lastYear}`,
    ];
    if (journey) why.push(`in the same-song index (${journey.takesUsed} takes used) — a take today extends its span ${spanGainYears.toFixed(1)} y`);
    if (nearEligible) why.push(`${cleanShort} clean take${cleanShort === 1 ? "" : "s"} short of same-song eligibility`);

    due.push({
      clusterId: cluster.clusterId,
      label: cluster.label ?? cluster.clusterId,
      score: Number(score.toFixed(2)),
      daysSinceLast: Math.round(daysSinceLast),
      takeCount: cluster.takeCount,
      firstYear: String(firstYear),
      lastYear: String(lastYear),
      inSameSongIndex: Boolean(journey),
      why,
    });
  }
  return due.sort((left, right) => right.score - left.score).slice(0, limit);
}

function completedFeatureRows(featuresManifest) {
  return (featuresManifest?.results ?? []).filter((row) => row.status === "completed" && row.features);
}

function latestPerYearEntry(journey) {
  const years = Object.keys(journey?.perYear ?? {}).sort();
  if (!years.length) return null;
  const year = years[years.length - 1];
  return { year, ...journey.perYear[year] };
}

export function buildFrontier({ rows, featuresManifest, journeysManifest, workingTopHz, limit = 12 } = {}) {
  const featureRows = completedFeatureRows(featuresManifest);
  const clusterByRecording = new Map();
  const labelByCluster = new Map();
  for (const row of rows ?? []) {
    if (!row.lyricMatchCluster) continue;
    clusterByRecording.set(row.recordingId, row.lyricMatchCluster);
    if (row.lyricMatchClusterLabel && !labelByCluster.has(row.lyricMatchCluster)) {
      labelByCluster.set(row.lyricMatchCluster, row.lyricMatchClusterLabel);
    }
  }

  const personalBestSustain = featureRows.reduce((best, row) => {
    const sustain = row.features.phrasing?.longestSustainedSeconds;
    return Number.isFinite(sustain) && sustain > best ? sustain : best;
  }, 0);

  const takesByCluster = new Map();
  for (const row of featureRows) {
    const clusterId = clusterByRecording.get(row.recordingId);
    if (!clusterId) continue;
    if (!takesByCluster.has(clusterId)) takesByCluster.set(clusterId, []);
    takesByCluster.get(clusterId).push(row);
  }

  const journeysById = new Map((journeysManifest?.journeys ?? []).map((journey) => [journey.clusterId, journey]));
  const entries = [];
  for (const [clusterId, takes] of takesByCluster) {
    if (takes.length < MIN_RECURRING_TAKES) continue;
    const recent = takes
      .slice()
      .sort((left, right) => (toTime(right.capturedAt) ?? 0) - (toTime(left.capturedAt) ?? 0))
      .slice(0, RECENT_TAKES_WINDOW);
    const flags = [];

    // Range stretch: the song's top notes sit just under the corpus working top.
    if (Number.isFinite(workingTopHz)) {
      const p95 = median(recent.map((row) => row.features.pitch?.f0Hz?.p95));
      if (p95 !== null && p95 > 0) {
        const semitonesUnder = 12 * Math.log2(workingTopHz / p95);
        if (semitonesUnder >= 0.5 && semitonesUnder <= 2) {
          flags.push({ rule: "range-stretch", detail: `top notes sit ${semitonesUnder.toFixed(1)} st under your working top — a stretch, not a leap` });
        }
      }
    }

    // Vibrato settling: recent takes hover just below the settled zone.
    const vibratoTakes = recent.filter((row) => Number.isFinite(row.features.vibrato?.meanRateHz) && (row.features.vibrato?.vibratoSegmentCount ?? 0) > 0);
    if (vibratoTakes.length >= 3) {
      const inBand = vibratoTakes.filter((row) => {
        const rate = row.features.vibrato.meanRateHz;
        return rate >= VIBRATO_SETTLING_BAND[0] && rate <= VIBRATO_SETTLING_BAND[1];
      });
      const share = inBand.length / vibratoTakes.length;
      if (share >= 0.3) {
        const rate = median(inBand.map((row) => row.features.vibrato.meanRateHz));
        flags.push({ rule: "vibrato-settling", detail: `${Math.round(share * 100)}% of recent takes at ${rate.toFixed(1)} Hz — one nudge from the settled ${SETTLED_ZONE_HZ} Hz zone` });
      }
    }

    // Sustain personal best: within striking distance of the corpus record.
    const bestRecent = recent.reduce((best, row) => {
      const sustain = row.features.phrasing?.longestSustainedSeconds;
      return Number.isFinite(sustain) && sustain > best ? sustain : best;
    }, 0);
    if (personalBestSustain > 0 && bestRecent >= 0.85 * personalBestSustain && bestRecent < personalBestSustain) {
      flags.push({ rule: "sustain-pb", detail: `longest recent sustain ${bestRecent.toFixed(1)} s — within ${Math.max(1, Math.round((1 - bestRecent / personalBestSustain) * 100))}% of your ${personalBestSustain.toFixed(1)} s best` });
    }

    const journey = journeysById.get(clusterId) ?? null;
    if (journey) {
      // Nearly in tune: latest-year note cores sit just outside the ±25 c gate.
      const latest = latestPerYearEntry(journey);
      if (latest && Number.isFinite(latest.noteCoreCentError) && latest.noteCoreCentError >= 25 && latest.noteCoreCentError <= 35) {
        flags.push({ rule: "tuning-near", detail: `note cores at ${latest.noteCoreCentError} c in ${latest.year} — nearly in tune, worth targeted drilling` });
      }
      // Evidence-thin clarity: promising slope, not enough takes to trust it.
      const cppsSlope = journey.slopesPerYear?.cpps;
      if (Number.isFinite(cppsSlope) && cppsSlope > 0.15 && journey.takesUsed < 12) {
        flags.push({ rule: "clarity-thin", detail: `clarity trending +${cppsSlope}/yr on only ${journey.takesUsed} takes — promising but evidence-thin; add data` });
      }
    }

    if (flags.length) {
      entries.push({
        clusterId,
        label: labelByCluster.get(clusterId) ?? clusterId,
        featureTakes: takes.length,
        flags,
      });
    }
  }
  return entries
    .sort((left, right) => right.flags.length - left.flags.length || right.featureTakes - left.featureTakes)
    .slice(0, limit);
}

export function buildCoachPayload({ rows, featuresManifest, journeysManifest, voiceTrends, now = Date.now() } = {}) {
  const workingTop = voiceTrends?.headline?.workingTop ?? null;
  return {
    generatedAt: new Date(now).toISOString(),
    params: { dueAfterDays: DUE_AFTER_DAYS, vibratoSettlingBandHz: VIBRATO_SETTLING_BAND, eligibility: { cleanTakes: ELIGIBLE_CLEAN_TAKES, spanYears: ELIGIBLE_SPAN_YEARS } },
    workingTop,
    dueSongs: buildDueSongs({ rows, journeysManifest, now }),
    frontier: buildFrontier({ rows, featuresManifest, journeysManifest, workingTopHz: workingTop?.peakTopHz ?? null }),
    readScope: {
      committedManifestsOnly: true,
      countsAndAggregatesOnly: true,
      transcriptText: false,
      audioBytes: false,
    },
  };
}
