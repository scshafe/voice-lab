import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { Button, Sheet, SheetBody, SheetFooter, SheetHeader } from "@scshafe/ui";
import { fetchTrendsThunk, includeFlaggedToggled, selectActiveQuarters, selectFingerprintTakes, selectFingerprintYears, selectIncludeFlagged, selectJourneySubject, selectTrends, selectYearSpan, subjectSelected } from "../../state/TrendsManager.js";
import { fetchJourneysThunk, selectJourneys, selectNoteCoreOverall, selectSongScorecards, selectTuningPseudoQuarters } from "../../state/JourneysManager.js";
import { fetchSummaryThunk, selectPlaybackEnabled } from "../../state/SummaryManager.js";
import { openCorpusWithQueryThunk } from "../../state/RowsManager.js";
import { openTakeInspectorThunk, selectTakeInspector, takeInspectorClosed } from "../../state/TakeInspectorManager.js";
import { ChartTooltipProvider } from "../charts/ChartTooltip.jsx";
import { RangeRiverChart, RangeRiverTable } from "../charts/RangeRiverChart.jsx";
import { CauseEffectChart, CauseEffectTable } from "../charts/CauseEffectChart.jsx";
import { VibratoFingerprintChart, VibratoTriptychChart, YearLegend } from "../charts/VibratoCharts.jsx";
import { MiniLinePanel } from "../charts/MiniLinePanel.jsx";
import { ThreadsChart } from "../charts/ThreadsChart.jsx";
import { F0ContourChart, SparklineChart } from "../charts/SmallCharts.jsx";
import { eraLabel, noteName } from "../charts/chartUtils.js";
import { JourneyEducationCard } from "./journeyEducation.jsx";

// The subject tabs, in display order. Availability is decided at render time:
// journeys-dependent subjects hide until /api/journeys serves, practice hides
// without a cadence series, threads without clusters.
const SUBJECT_TABS = [
  { key: "verdict", label: "The Verdict" },
  { key: "range", label: "Range River" },
  { key: "practice", label: "Cause & Effect" },
  { key: "vibrato", label: "Vibrato Story" },
  { key: "trust", label: "What Not To Trust" },
  { key: "songs", label: "Song Scorecards" },
  { key: "threads", label: "Repertoire Threads" }
];

function StatTile({ value, label }) {
  return (
    <div className="vj-tile">
      <div className="vj-tile-v">{value}</div>
      <div className="vj-tile-l">{label}</div>
    </div>
  );
}

function StatBand({ trends }) {
  const head = trends.headline;
  return (
    <div className="vj-stats">
      <StatTile value={head.takesAnalyzed.toLocaleString()} label="takes analyzed" />
      <StatTile value={`${head.hoursAnalyzed} h`} label="singing measured" />
      {head.workingTop ? <StatTile value={`+${head.workingTop.gainSemitones} st`} label={`working top, ${head.workingTop.baselineYear} → ${head.workingTop.peakYear}`} /> : null}
      <StatTile value={head.reliableTakes.toLocaleString()} label="reliable takes" />
      <StatTile value={head.flaggedTakes.toLocaleString()} label="flagged for listening" />
    </div>
  );
}

function VerdictChip({ verdict }) {
  const labels = { improving: "improving", declining: "declining", flat: "flat", trending_up: "trending up", trending_down: "trending down", insufficient_data: "insufficient data" };
  return <span className={`vj-chip vj-chip-${verdict}`}>{labels[verdict] ?? verdict}</span>;
}

function VerdictCard({ journeys }) {
  const counts = { improving: 0, declining: 0, flat: 0 };
  for (const dim of journeys.improvementIndex) {
    if (counts[dim.verdict] !== undefined) counts[dim.verdict] += 1;
  }
  return (
    <section className="vj-card">
      <h2>The Verdict — same-song controlled</h2>
      <p className="vj-sub">Slopes per year measured <em>inside</em> recurring songs (Theil–Sen per song, bootstrap 95% CI across songs). The strictest lens this archive allows: you, compared only to yourself, on the same material.</p>
      <div className="vj-verdict-rows">
        {journeys.improvementIndex.map((dim) => (
          <div className="vj-vrow" key={dim.key ?? dim.label}>
            <span>{dim.label}</span>
            <span className="vj-num">{dim.medianSlopePerYear === null ? "—" : `${dim.medianSlopePerYear}/yr`}</span>
            <span className="vj-num"><small>CI</small> {dim.ci95 ? `${dim.ci95[0]} … ${dim.ci95[1]}` : "—"}</span>
            <span><small>{dim.clustersUsed} songs</small></span>
            <VerdictChip verdict={dim.verdict} />
          </div>
        ))}
      </div>
      <p className="vj-sub" style={{ marginTop: 12 }}>
        {counts.improving === 0 && counts.declining === 0
          ? <>Across {journeys.totals.clustersEligible} recurring songs, execution on unchanged material has held <strong>steady</strong> — no dimension moves once repertoire is controlled. The transformation this archive records lives in the other tabs: <strong>what</strong> you sing (range, register, repertoire) changed far more than how the same song is sung. Individual songs still moved both ways — see the scorecards.</>
          : <>{counts.improving} improving · {counts.flat} flat · {counts.declining} declining across {journeys.totals.clustersEligible} songs. Individual songs vary — see the scorecards.</>}
      </p>
    </section>
  );
}

function SlopeArrow({ value, downGood }) {
  if (value === null || value === undefined) return <span className="vj-steady">—</span>;
  const good = downGood ? value < 0 : value > 0;
  const arrow = value < 0 ? "▼" : value > 0 ? "▲" : "▬";
  const cls = Math.abs(value) < 0.05 ? "vj-steady" : good ? "vj-up" : "vj-down";
  return <span className={cls}>{arrow} {value}/yr</span>;
}

function songNameFor(clusterId, clusters) {
  const index = (clusters ?? []).findIndex((cluster) => cluster.id === clusterId);
  if (index >= 0) return `Song ${String.fromCharCode(65 + index)}`;
  return clusterId.replace("lyric-", "");
}

// Chained era playback (one Audio element, advances on `ended`) — imperative and
// transient, so it lives in a ref, not the store.
function useEraAudio(playbackEnabled) {
  const audioRef = React.useRef(null);
  React.useEffect(() => () => audioRef.current?.pause(), []);
  return React.useCallback((era) => {
    if (!playbackEnabled) return;
    const queue = era.sampleRecordingIds.slice();
    audioRef.current?.pause();
    const audio = new Audio();
    audio.preload = "none";
    audioRef.current = audio;
    const next = () => {
      if (!queue.length) return;
      audio.src = `/api/audio/${encodeURIComponent(queue.shift())}`;
      audio.play().catch(() => {});
    };
    audio.addEventListener("ended", next);
    next();
  }, [playbackEnabled]);
}

function TakeInspectorSheet() {
  const dispatch = useDispatch();
  const inspector = useSelector(selectTakeInspector);
  const playbackEnabled = useSelector(selectPlaybackEnabled);
  if (!inspector.openId) return null;
  const payload = inspector.data;
  const summary = payload?.summary ?? {};
  const pitch = summary.pitch ?? {};
  const vibrato = summary.vibrato ?? {};
  const quality = summary.quality ?? {};
  const phrasing = summary.phrasing ?? {};
  const close = () => dispatch(takeInspectorClosed());
  const tiles = payload ? [
    ["median pitch", pitch.f0Hz?.p50 ? `${noteName(pitch.f0Hz.p50)} (${pitch.f0Hz.p50} Hz)` : "—"],
    ["range p05–p95", pitch.rangeSemitonesP05P95 ?? "—"],
    ["vibrato", vibrato.meanRateHz ? `${vibrato.meanRateHz} Hz · ${Math.round(vibrato.meanExtentCents)}c` : "—"],
    ["CPPS", quality.cpps !== null && quality.cpps !== undefined ? `${quality.cpps} dB` : "—"],
    ["longest sustain", phrasing.longestSustainedSeconds ? `${phrasing.longestSustainedSeconds} s` : "—"],
    ["bucket", payload.bucket ?? "—"]
  ] : [];
  return (
    <Sheet open onClose={close} side="center" ariaLabel="Take inspector" dataSuiComponent="TakeInspectorSheet">
      <SheetHeader title={payload ? `${payload.filename} · ${(payload.capturedAt ?? "").slice(0, 10)}` : `Take ${inspector.openId}`} />
      <SheetBody>
        {inspector.status === "loading" ? <p className="vj-muted">Loading…</p> : null}
        {inspector.status === "failed" ? (
          <p className="vj-muted">
            {inspector.error === "feature_dir_not_configured"
              ? <>Per-take contours need the local feature store. Relaunch with <code>--feature-dir local-artifacts/voice-features</code>.</>
              : `Detail unavailable: ${inspector.error}`}
          </p>
        ) : null}
        {payload ? (
          <>
            <div className="vj-kv">
              {tiles.map(([label, value]) => (
                <div key={label}>{label}<b>{value}</b></div>
              ))}
            </div>
            <div className="vj-chart-scroller"><F0ContourChart contour={payload.detail?.f0ContourVoiced50ms} /></div>
            {playbackEnabled ? <audio controls preload="none" src={`/api/audio/${encodeURIComponent(inspector.openId)}`} style={{ marginTop: 12, width: "100%" }} /> : null}
          </>
        ) : null}
      </SheetBody>
      <SheetFooter>
        <Button label="Close" onClick={close} />
      </SheetFooter>
    </Sheet>
  );
}

function RangeSection({ trendsData, quarters }) {
  return (
    <section className="vj-card">
      <h2>The Range River</h2>
      <p className="vj-sub">
        {trendsData.headline.workingTop
          ? <>Working top: <strong>{noteName(trendsData.headline.workingTop.baselineTopHz)}</strong> ({trendsData.headline.workingTop.baselineYear}) → <strong>{noteName(trendsData.headline.workingTop.peakTopHz)}</strong> ({trendsData.headline.workingTop.peakYear}) · <strong>+{trendsData.headline.workingTop.gainSemitones} semitones</strong> of reach, held since.</>
          : "Where your voice lives, quarter by quarter."}
      </p>
      <div className="vj-chart-scroller"><RangeRiverChart quarters={quarters} /></div>
      <div className="vj-legend">
        <span><span className="vj-swatch" style={{ background: "rgb(217 173 69 / 0.14)" }} />reach (p05–p95 envelope)</span>
        <span><span className="vj-swatch" style={{ background: "rgb(217 173 69 / 0.32)" }} />typical (p25–p75)</span>
        <span><span className="vj-swatch" style={{ background: "#d9ad45" }} />median</span>
        <span><span className="vj-swatch" style={{ background: "#5b8ede" }} />register strip: <span style={{ color: "#d9ad45" }}>high ≥300 Hz</span> · <span style={{ color: "#8fb3e8" }}>mid</span> · <span style={{ color: "#55607a" }}>low &lt;200 Hz</span></span>
      </div>
      <details><summary>Data table</summary><div className="vj-chart-scroller"><RangeRiverTable quarters={quarters} /></div></details>
    </section>
  );
}

function PracticeSection({ trendsData, quarters, playbackEnabled, playEra }) {
  return (
    <section className="vj-card">
      <h2>Cause &amp; Effect</h2>
      <p className="vj-sub">Practice cadence above; what it buys below. Shaded bands are your high-practice eras, derived from the cadence itself; the panels share one time axis, so alignment does the arguing.</p>
      <div className="vj-chart-scroller"><CauseEffectChart months={trendsData.monthlyCadence} quarters={quarters} eras={trendsData.eras} /></div>
      <div className="vj-erarow">
        {(trendsData.eras ?? []).map((era) => (
          <button
            key={era.start}
            type="button"
            disabled={!playbackEnabled || !era.sampleRecordingIds.length}
            title={playbackEnabled ? `Hear ${era.sampleRecordingIds.length} takes from this era` : "Playback needs the release-gated launch (corpus root + approval)"}
            onClick={() => playEra(era)}
          >
            ▶ {eraLabel(era)} · {era.totalTakes} takes
          </button>
        ))}
      </div>
      <details><summary>Data table</summary><div className="vj-chart-scroller"><CauseEffectTable quarters={quarters} /></div></details>
    </section>
  );
}

function VibratoSection({ quarters, fingerprintTakes, fingerprintYears, yearSpan, onOpenTake }) {
  return (
    <section className="vj-card">
      <h2>The Vibrato Story</h2>
      <p className="vj-sub">Median pitch-modulation per quarter with interquartile bands. The blue zone marks settled classical vibrato (5–6 Hz) — the trainable gap.</p>
      <div className="vj-chart-scroller"><VibratoTriptychChart quarters={quarters} /></div>
      <h3 style={{ margin: "18px 0 2px", fontSize: "1rem" }}>The fingerprint — every take, walking home</h3>
      <p className="vj-sub">One dot per take: extent across, rate up, brighter = more recent. Click a dot to inspect that take.</p>
      <div className="vj-fingerwrap">
        <VibratoFingerprintChart takes={fingerprintTakes} yearSpan={yearSpan} onOpenTake={onOpenTake} />
        <YearLegend years={fingerprintYears} yearSpan={yearSpan} />
      </div>
    </section>
  );
}

function TrustSection({ quarters, tuningQuarters, noteCoreOverall, journeysData }) {
  return (
    <section className="vj-card">
      <h2>What Not To Trust</h2>
      <p className="vj-sub">Two measures of "voice quality" that disagree — on purpose. <strong>CPPS</strong> is robust to recording conditions and tracks your practice arc; <strong>HNR</strong> falls monotonically because your microphones and rooms changed across seven years, not your voice.</p>
      <div className="vj-duo">
        <div><MiniLinePanel title="CPPS (dB) — tracks the voice" quarters={quarters} seriesKey="cpps" pick={(q) => q.cpps} color="#d9ad45" fmt={(v) => v.toFixed(1)} /></div>
        <div><MiniLinePanel title="HNR (dB) — tracks the microphones" quarters={quarters} seriesKey="hnr" pick={(q) => q.hnr} color="#8fb3e8" fmt={(v) => v.toFixed(1)} /></div>
        {journeysData ? <div><MiniLinePanel title="note-core tuning error (cents) — now measured" quarters={tuningQuarters} seriesKey="nc" pick={(q) => q.nc} color="#8fd9a0" fmt={(v) => `${v.toFixed(1)}c`} /></div> : null}
      </div>
      {journeysData && noteCoreOverall.median !== null ? (
        <div className="vj-mutedtile">
          <strong>Tuning — now measured on note cores.</strong>{" "}
          {noteCoreOverall.takeCount.toLocaleString()} takes scored on stable note medians only (slides and melisma excluded by construction): median error {noteCoreOverall.median.toFixed(1)} cents, steady across seven years. The green panel above is the real instrument; the old every-frame metric is retired.
        </div>
      ) : (
        <div className="vj-mutedtile">
          <strong>Tuning accuracy — not yet meaningful.</strong>{" "}
          Note-core scoring arrives with the same-song analysis (run <code>npm run journeys -- analyze</code>).
        </div>
      )}
    </section>
  );
}

function SongsSection({ scorecards, clusters, openCluster }) {
  return (
    <section className="vj-card">
      <h2>Song Scorecards</h2>
      <p className="vj-sub">The songs with enough history to judge (≥6 reliable takes across ≥1.5 years), ranked by takes. Slopes are per year within that song; the sparkline is note-core tuning error by year (down is better).</p>
      <div>
        {scorecards.map((journey) => (
          <div className="vj-songrow" key={journey.clusterId}>
            <span className="vj-songname" onClick={() => openCluster(journey.clusterId)} role="link" tabIndex={0}>
              {songNameFor(journey.clusterId, clusters)}
              <small>{journey.clusterId}</small>
            </span>
            <span className="vj-num">{journey.takesUsed} takes<small>{journey.firstYear}–{journey.lastYear}</small></span>
            <span><SparklineChart perYear={journey.perYear} valueKey="noteCoreCentError" /></span>
            <span>tuning <SlopeArrow value={journey.slopesPerYear.noteCoreCentError} downGood /></span>
            <span>clarity <SlopeArrow value={journey.slopesPerYear.cpps} downGood={false} /></span>
          </div>
        ))}
      </div>
    </section>
  );
}

function ThreadsSection({ clusters, openCluster }) {
  return (
    <section className="vj-card">
      <h2>Repertoire Threads</h2>
      <p className="vj-sub">Your eight biggest recurring songs across the years — dot area is takes that year. Click a thread to open its takes in the Corpus table. Labels are privacy-generic; no lyric text exists in any manifest.</p>
      <div className="vj-chart-scroller"><ThreadsChart threads={clusters} onOpenCluster={openCluster} /></div>
    </section>
  );
}

export function JourneyPageComponent() {
  const dispatch = useDispatch();
  const trends = useSelector(selectTrends);
  const journeys = useSelector(selectJourneys);
  const quarters = useSelector(selectActiveQuarters);
  const includeFlagged = useSelector(selectIncludeFlagged);
  const requestedSubject = useSelector(selectJourneySubject);
  const yearSpan = useSelector(selectYearSpan);
  const fingerprintTakes = useSelector(selectFingerprintTakes);
  const fingerprintYears = useSelector(selectFingerprintYears);
  const tuningQuarters = useSelector(selectTuningPseudoQuarters);
  const noteCoreOverall = useSelector(selectNoteCoreOverall);
  const scorecards = useSelector(selectSongScorecards);
  const playbackEnabled = useSelector(selectPlaybackEnabled);
  const playEra = useEraAudio(playbackEnabled);

  React.useEffect(() => {
    dispatch(fetchSummaryThunk());
    dispatch(fetchTrendsThunk());
    dispatch(fetchJourneysThunk());
  }, [dispatch]);

  const trendsData = trends.data;
  if (trends.status === "loading" || trends.status === "idle") {
    return <div className="vj-page"><p className="vj-empty">Loading trends…</p></div>;
  }
  if (trends.status === "failed") {
    return <div className="vj-page"><div className="vj-notice">{trends.error}</div></div>;
  }
  if (!trendsData?.available) {
    return (
      <div className="vj-page">
        <div className="vj-notice">Voice trends need the features manifest. Relaunch with <code>--features manifests/local-voice-features.json</code>.</div>
      </div>
    );
  }

  const journeysData = journeys.data?.available ? journeys.data : null;
  const clusters = trendsData.clusters ?? [];
  const openCluster = (id) => dispatch(openCorpusWithQueryThunk(id));

  const availability = {
    verdict: Boolean(journeysData),
    range: true,
    practice: (trendsData.monthlyCadence ?? []).length > 0,
    vibrato: true,
    trust: true,
    songs: Boolean(journeysData),
    threads: clusters.length > 0
  };
  const tabs = SUBJECT_TABS.filter((tab) => availability[tab.key]);
  const active = availability[requestedSubject] ? requestedSubject : tabs[0].key;

  const selectSubjectTab = (key) => {
    dispatch(subjectSelected(key));
    if (typeof window !== "undefined" && window.history?.replaceState) {
      const url = new URL(window.location.href);
      url.searchParams.set("subject", key);
      window.history.replaceState(window.history.state, "", url);
    }
  };

  return (
    <ChartTooltipProvider>
      <div className="vj-page">
        <StatBand trends={trendsData} />
        <div className="vj-togglebar">
          <label>
            <input type="checkbox" checked={includeFlagged} onChange={(event) => dispatch(includeFlaggedToggled(event.target.checked))} />
            {" include the flagged low-reliability takes (hollow in scatters)"}
          </label>
        </div>

        <nav className="vj-subtabs" aria-label="Journey subjects">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              type="button"
              data-active={tab.key === active ? "true" : "false"}
              aria-current={tab.key === active ? "page" : undefined}
              onClick={() => selectSubjectTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </nav>

        {active === "verdict" && journeysData ? <VerdictCard journeys={journeysData} /> : null}
        {active === "range" ? <RangeSection trendsData={trendsData} quarters={quarters} /> : null}
        {active === "practice" ? <PracticeSection trendsData={trendsData} quarters={quarters} playbackEnabled={playbackEnabled} playEra={playEra} /> : null}
        {active === "vibrato" ? <VibratoSection quarters={quarters} fingerprintTakes={fingerprintTakes} fingerprintYears={fingerprintYears} yearSpan={yearSpan} onOpenTake={(id) => dispatch(openTakeInspectorThunk({ id }))} /> : null}
        {active === "trust" ? <TrustSection quarters={quarters} tuningQuarters={tuningQuarters} noteCoreOverall={noteCoreOverall} journeysData={journeysData} /> : null}
        {active === "songs" && journeysData ? <SongsSection scorecards={scorecards} clusters={clusters} openCluster={openCluster} /> : null}
        {active === "threads" ? <ThreadsSection clusters={clusters} openCluster={openCluster} /> : null}

        <JourneyEducationCard subject={active} />

        <TakeInspectorSheet />
      </div>
    </ChartTooltipProvider>
  );
}
