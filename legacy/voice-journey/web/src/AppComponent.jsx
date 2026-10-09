import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { navigateThunk, selectView } from "./state/NavigationManager.js";
import { selectRowsStatusLine } from "./state/RowsManager.js";
import { CorpusPageComponent } from "./components/corpus/CorpusPageComponent.jsx";
import { JourneyPageComponent } from "./components/journey/JourneyPageComponent.jsx";
import { PracticePageComponent } from "./components/practice/PracticePageComponent.jsx";
import { RefereePageComponent } from "./components/referee/RefereePageComponent.jsx";
import { ToastTrayComponent } from "./components/app/ToastTrayComponent.jsx";

const NAV_ITEMS = [
  { view: "corpus", label: "Corpus" },
  { view: "journey", label: "Journey" },
  { view: "practice", label: "Practice" },
  { view: "referee", label: "Referee" }
];

const HEADER_BY_VIEW = {
  corpus: {
    title: "Voice Journey Corpus Browser",
    tagline: "Spot-check workbench over committed manifests. Playback is release-gated and per-click only."
  },
  journey: {
    title: "The Voice Journey",
    tagline: "Seven years of practice, measured: range, sustain, timbre, vibrato, and the same-song verdict."
  },
  practice: {
    title: "The Practice Room",
    tagline: "Tonight's session, argued from the data — due songs and frontier flags from the coach."
  },
  referee: {
    title: "The Referee",
    tagline: "Blind same-song A/B trials. Years stay hidden until you judge."
  }
};

export function AppComponent() {
  const dispatch = useDispatch();
  const view = useSelector(selectView);
  const rowsStatusLine = useSelector(selectRowsStatusLine);
  const header = HEADER_BY_VIEW[view] ?? HEADER_BY_VIEW.corpus;
  return (
    <div className="vj-app">
      <header className="vj-header">
        <nav className="vj-nav" aria-label="Pages">
          {NAV_ITEMS.map((item) => (
            item.view === view
              ? <span key={item.view} className="vj-nav-active">{item.label}</span>
              : <button key={item.view} type="button" onClick={() => dispatch(navigateThunk({ view: item.view }))}>{item.label}</button>
          ))}
          <a href="/report">Report</a>
        </nav>
        <h1>{header.title}</h1>
        <p>{header.tagline}</p>
        {view === "corpus" ? <div className="vj-status-line">{rowsStatusLine}</div> : null}
      </header>
      <main className="vj-main">
        {view === "corpus" ? <CorpusPageComponent /> : null}
        {view === "journey" ? <JourneyPageComponent /> : null}
        {view === "practice" ? <PracticePageComponent /> : null}
        {view === "referee" ? <RefereePageComponent /> : null}
      </main>
      <ToastTrayComponent />
    </div>
  );
}
