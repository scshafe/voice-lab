import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { showToastThunk } from "@scshafe/ui/state";
import { fetchNextPairThunk, fetchRefereeResultsThunk, selectReferee, submitRefereeChoiceThunk, trialAdvanced } from "../../state/RefereeManager.js";
import { BradleyTerryChart } from "../charts/SmallCharts.jsx";

const CHOICES = [
  { choice: "a", label: "A sounds better", primary: true },
  { choice: "b", label: "B sounds better", primary: true },
  { choice: "too_close", label: "Too close", primary: false },
  { choice: "not_same_song", label: "Not the same song", primary: false },
  { choice: "skip", label: "Skip", primary: false }
];

const REVEAL_ADVANCE_MS = 1400;

function TrialCard() {
  const dispatch = useDispatch();
  const referee = useSelector(selectReferee);
  const audioARef = React.useRef(null);
  const audioBRef = React.useRef(null);
  const audioErrorPairRef = React.useRef(null);
  const audioChainRef = React.useRef({ context: null, gains: null });

  // Loudness-matched, band-limited playback: the server sends per-side gains
  // (from committed RMS aggregates); a WebAudio chain — lowpass 8 kHz then
  // gain — applies them at playback so a 2019 phone take and a 2026 take stop
  // telegraphing their year through the recording chain. Served audio is
  // untouched; without WebAudio support playback falls back to plain.
  const wireMatchedPlayback = React.useCallback((element, gainDb) => {
    if (!element || typeof window === "undefined") return;
    const AudioContextCtor = window.AudioContext ?? window.webkitAudioContext;
    if (!AudioContextCtor) return;
    const chain = audioChainRef.current;
    chain.context ??= new AudioContextCtor();
    chain.gains ??= new WeakMap();
    if (!chain.gains.has(element)) {
      const source = chain.context.createMediaElementSource(element);
      const lowpass = chain.context.createBiquadFilter();
      lowpass.type = "lowpass";
      lowpass.frequency.value = 8000;
      const gain = chain.context.createGain();
      source.connect(lowpass);
      lowpass.connect(gain);
      gain.connect(chain.context.destination);
      element.addEventListener("play", () => { chain.context.resume().catch(() => {}); });
      chain.gains.set(element, gain);
    }
    chain.gains.get(element).gain.value = Math.pow(10, (gainDb ?? 0) / 20);
  }, []);

  React.useEffect(() => {
    if (!referee.pair) return;
    wireMatchedPlayback(audioARef.current, referee.pair.a?.gainDb);
    wireMatchedPlayback(audioBRef.current, referee.pair.b?.gainDb);
  }, [referee.pair, wireMatchedPlayback]);

  // The <audio> element's native "error" chip says nothing about WHY. The usual
  // cause here is the server losing corpus access (macOS TCC / Full Disk
  // Access), so translate the failure into an actionable toast — once per pair.
  const onAudioError = () => {
    const pairId = referee.pair?.pairId ?? null;
    if (!pairId || audioErrorPairRef.current === pairId) return;
    audioErrorPairRef.current = pairId;
    dispatch(showToastThunk({
      kind: "error",
      message: "Track failed to load — the server can't read the Voice Memos corpus (usually macOS Full Disk Access for node). Fix the grant, then reload."
    }));
  };

  // After the reveal, advance to the next blind pair (legacy 1.4 s pause).
  React.useEffect(() => {
    if (!referee.reveal) return undefined;
    const timer = setTimeout(() => {
      dispatch(trialAdvanced());
      dispatch(fetchNextPairThunk());
    }, REVEAL_ADVANCE_MS);
    return () => clearTimeout(timer);
  }, [referee.reveal, dispatch]);

  const judge = (choice) => {
    if (!referee.pair || referee.submitting || referee.reveal) return;
    audioARef.current?.pause();
    audioBRef.current?.pause();
    dispatch(submitRefereeChoiceThunk({ pairId: referee.pair.pairId, choice }))
      .unwrap()
      .then(() => dispatch(fetchRefereeResultsThunk()))
      .catch(() => {});
  };

  if (!referee.pair && !referee.reveal) {
    return (
      <section className="vj-card">
        <h2>This pair</h2>
        <p className="vj-sub">Pool complete: {referee.progress?.judged ?? 0} of {referee.progress?.poolSize ?? 0} pairs judged.</p>
      </section>
    );
  }

  return (
    <section className="vj-card">
      <h2>This pair</h2>
      <p className="vj-sub">Listen to both, then call it. "Not the same song" also helps — it flags a chained cluster for repair. Playback is loudness-matched and band-limited (8 kHz) to blunt recording-chain tells.</p>
      <div className="vj-pair">
        <div className="vj-side"><h3>A</h3>{referee.pair ? <audio ref={audioARef} controls preload="none" src={referee.pair.a.audioUrl} onError={onAudioError} /> : null}</div>
        <div className="vj-side"><h3>B</h3>{referee.pair ? <audio ref={audioBRef} controls preload="none" src={referee.pair.b.audioUrl} onError={onAudioError} /> : null}</div>
      </div>
      <div className="vj-choices">
        {CHOICES.map((entry) => (
          <button
            key={entry.choice}
            type="button"
            className={entry.primary ? "vj-choice-primary" : undefined}
            disabled={!referee.pair || referee.submitting || Boolean(referee.reveal)}
            onClick={() => judge(entry.choice)}
          >
            {entry.label}
          </button>
        ))}
      </div>
      {referee.reveal ? (
        <div className="vj-reveal">
          A was <b>{referee.reveal.aCapturedAt.slice(0, 10)}</b> · B was <b>{referee.reveal.bCapturedAt.slice(0, 10)}</b> · {referee.reveal.clusterId}
        </div>
      ) : null}
      <div className="vj-progress">{referee.progress ? `${referee.progress.judged} of ${referee.progress.poolSize} pairs judged.` : ""}</div>
    </section>
  );
}

function ResultsCard() {
  const referee = useSelector(selectReferee);
  const results = referee.results.data;
  if (!results?.judgedTotal) return null;
  const years = results.years ?? [];
  return (
    <section className="vj-card">
      <h2>Perceived quality by year</h2>
      <p className="vj-sub">Bradley–Terry strengths from your judgments, log2 relative to the earliest judged year. Positive means those years win your blind ear.</p>
      <div className="vj-chart-scroller"><BradleyTerryChart years={years} /></div>
      <details open>
        <summary>Counts</summary>
        <div className="vj-chart-scroller">
          <table>
            <thead>
              <tr><th>Year</th><th>Strength (log2)</th><th>Wins</th><th>Losses</th><th>Ties</th><th>Trials</th></tr>
            </thead>
            <tbody>
              {years.map((row) => (
                <tr key={row.year}>
                  <td>{row.year}</td>
                  <td>{row.strengthLog2}</td>
                  <td>{row.wins}</td>
                  <td>{row.losses}</td>
                  <td>{row.ties}</td>
                  <td>{row.trials}</td>
                </tr>
              ))}
              <tr>
                <td>choices</td>
                <td colSpan={5}>A {results.byChoice.a} · B {results.byChoice.b} · too close {results.byChoice.too_close} · not same song {results.byChoice.not_same_song} · skip {results.byChoice.skip}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
      {results.notSameSongFlags?.length ? (
        <div className="vj-flagged">
          Chained-cluster flags: {results.notSameSongFlags.map((flag) => `${flag.clusterId} ×${flag.count}`).join(", ")} — these feed the cluster repair pass.
        </div>
      ) : null}
    </section>
  );
}

export function RefereePageComponent() {
  const dispatch = useDispatch();
  const referee = useSelector(selectReferee);

  React.useEffect(() => {
    dispatch(fetchNextPairThunk());
    dispatch(fetchRefereeResultsThunk());
  }, [dispatch]);

  return (
    <div className="vj-page vj-page-narrow">
      {referee.status === "failed" ? <div className="vj-notice">{referee.error}</div> : null}
      {referee.enabled === false ? (
        <div className="vj-notice">
          The referee needs release-gated playback. Relaunch the browser with <code>--corpus-root</code> and <code>--playback-approval</code>; {(referee.progress?.poolSize ?? 0).toLocaleString()} blind pairs are waiting. Existing results still show below.
        </div>
      ) : null}
      {referee.enabled ? <TrialCard /> : null}
      <ResultsCard />
    </div>
  );
}
