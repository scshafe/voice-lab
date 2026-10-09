import { createSelector, createSlice } from "@reduxjs/toolkit";
import { createResourceSlice, webApiJson } from "@scshafe/ui/state";

// ============================================================================
// TrendsManager — the /api/trends aggregate payload behind the Journey page, on
// @scshafe/ui/state's createResourceSlice (S2). The include-flagged-takes toggle is
// VIEW state, not resource state, so it lives in its own tiny JourneyView slice
// and the selectors compose across the two.
// ============================================================================

const trends = createResourceSlice({
  name: "TrendsManager",
  fetch: () => webApiJson("/api/trends", { label: "Trends" })
});

export const TrendsManager = trends.slice;
export const fetchTrendsThunk = trends.fetchThunk;
export const selectTrends = trends.select;

// The Journey page's subject tabs. Order is display order; availability is
// decided at render time (journeys-dependent tabs hide until the manifest is
// served). The active subject deep-links via ?subject= so a tab can be shared.
export const JOURNEY_SUBJECTS = Object.freeze(["verdict", "range", "practice", "vibrato", "trust", "songs", "threads"]);

function initialSubject() {
  if (typeof window === "undefined") return "verdict";
  const requested = new URLSearchParams(window.location.search).get("subject");
  return JOURNEY_SUBJECTS.includes(requested) ? requested : "verdict";
}

export const JourneyView = createSlice({
  name: "JourneyView",
  initialState: { includeFlagged: false, subject: initialSubject() },
  reducers: {
    includeFlaggedToggled(state, action) {
      state.includeFlagged = Boolean(action.payload);
    },
    subjectSelected(state, action) {
      if (JOURNEY_SUBJECTS.includes(action.payload)) state.subject = action.payload;
    }
  }
});

export const { includeFlaggedToggled, subjectSelected } = JourneyView.actions;

export function selectIncludeFlagged(state = {}) {
  return Boolean(state.JourneyView?.includeFlagged);
}

export function selectJourneySubject(state = {}) {
  return state.JourneyView?.subject ?? "verdict";
}

// The quarters every Journey panel renders: reliable-only by default, the
// all-takes rollup when the toggle is on.
export const selectActiveQuarters = createSelector(
  [trends.selectData, selectIncludeFlagged],
  (data, includeFlagged) => {
    if (!data?.available) return [];
    return (includeFlagged && data.quartersAllTakes ? data.quartersAllTakes : data.quarters) ?? [];
  }
);

export const selectYearSpan = createSelector(
  [trends.selectData],
  (data) => {
    const years = (data?.quarters ?? []).map((q) => Number(q.quarter.slice(0, 4)));
    if (!years.length) return { min: 0, max: 0 };
    return { min: Math.min(...years), max: Math.max(...years) };
  }
);

export const selectFingerprintTakes = createSelector(
  [trends.selectData, selectIncludeFlagged],
  (data, includeFlagged) => (data?.vibratoTakes ?? []).filter((take) => includeFlagged || take.reliable)
);

export const selectFingerprintYears = createSelector(
  [trends.selectData],
  (data) => [...new Set((data?.vibratoTakes ?? []).map((take) => take.year))].sort()
);
