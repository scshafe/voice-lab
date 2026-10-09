import { createSelector } from "@reduxjs/toolkit";
import { createResourceSlice, webApiJson } from "@scshafe/ui/state";

// ============================================================================
// JourneysManager — the /api/journeys payload (same-song improvement index +
// scorecards), on @scshafe/ui/state's createResourceSlice (S2). Domain selectors
// (note-core rollups, scorecard ranking) compose over the factory's selectData.
// ============================================================================

const journeys = createResourceSlice({
  name: "JourneysManager",
  fetch: () => webApiJson("/api/journeys", { label: "Journeys" })
});

export const JourneysManager = journeys.slice;
export const fetchJourneysThunk = journeys.fetchThunk;
export const selectJourneys = journeys.select;

// The note-core tuning instrument: per-quarter pseudo-rollups (median + IQR when
// n≥3) over the per-take centErrorMedian values.
export const selectTuningPseudoQuarters = createSelector(
  [journeys.selectData],
  (data) => {
    const takes = data?.noteCore?.takes ?? [];
    const byQuarter = new Map();
    for (const take of takes) {
      if (!take.quarter || take.centErrorMedian === null) continue;
      if (!byQuarter.has(take.quarter)) byQuarter.set(take.quarter, []);
      byQuarter.get(take.quarter).push(take.centErrorMedian);
    }
    return [...byQuarter.keys()].sort().map((quarter) => {
      const values = byQuarter.get(quarter).slice().sort((a, b) => a - b);
      const at = (p) => values[Math.floor((values.length - 1) * p)];
      return { quarter, nc: at(0.5), nReliable: values.length, spread: { nc: values.length >= 3 ? [at(0.25), at(0.75)] : [null, null] } };
    }).filter((q) => q.nReliable >= 3);
  }
);

export const selectNoteCoreOverall = createSelector(
  [journeys.selectData],
  (data) => {
    const takes = data?.noteCore?.takes ?? [];
    const values = takes.map((take) => take.centErrorMedian).filter((v) => v !== null).sort((a, b) => a - b);
    if (!values.length) return { takeCount: takes.length, median: null };
    return { takeCount: takes.length, median: values[Math.floor((values.length - 1) / 2)] };
  }
);

export const selectSongScorecards = createSelector(
  [journeys.selectData],
  (data) => (data?.journeys ?? []).slice().sort((a, b) => b.takesUsed - a.takesUsed).slice(0, 10)
);
