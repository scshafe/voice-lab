import { createAsyncThunk, createSelector } from "@reduxjs/toolkit";
import { createPagedListSlice, showToastThunk, webApiJson, webApiMutation } from "@scshafe/ui/state";
import { navigateThunk } from "./NavigationManager.js";

// ============================================================================
// RowsManager — the paged corpus table, on @scshafe/ui/state's createPagedListSlice
// (S2: the hand-rolled accumulation + request-version guard + filter/sort
// lifecycle moved into the library; this file keeps only the DOMAIN — the
// /api/rows contract, the lean projection, verdict writes, and the
// Journey → Corpus click-through).
// ============================================================================

export const PAGE_LIMIT = 100;

export const FILTER_KEYS = Object.freeze(["q", "bucket", "year", "reviewQueue", "spotCheckStatus", "transcriptStatus", "lyricMatchStatus"]);

export const VERDICT_CHOICES = Object.freeze(["clean_singing", "noise_contaminated_singing", "music_contaminated_singing", "non_singing", "still_uncertain"]);

// Shareable-URL serialization for the current filters/sort (drives replaceState
// in CorpusPage and the legacy /?q= deep links).
export function rowsQuerySearch({ filters, sort, direction }) {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    if (filters?.[key]) params.set(key, filters[key]);
  }
  if (sort && sort !== "capturedAt") params.set("sort", sort);
  if (direction && direction !== "asc") params.set("direction", direction);
  return params.toString();
}

export const submitVerdictThunk = createAsyncThunk(
  "RowsManager/submitVerdict",
  async ({ recordingId, verdict, note = null }, { dispatch }) => {
    try {
      const payload = await webApiMutation("/api/verdicts", { recordingId, verdict, note, reviewedBy: "operator" }, { label: "Verdict" });
      dispatch(showToastThunk({ kind: "success", message: `Verdict recorded: ${verdict.replaceAll("_", " ")}` }));
      return { recordingId, verdict: payload.verdict, totals: payload.totals ?? null };
    } catch (error) {
      dispatch(showToastThunk({ kind: "error", message: `Verdict failed: ${error.message}` }));
      throw error;
    }
  }
);

const rowsList = createPagedListSlice({
  name: "RowsManager",
  pageLimit: PAGE_LIMIT,
  filterKeys: FILTER_KEYS,
  initialSort: "capturedAt",
  fetchPage: async ({ offset, limit, filters, sort, direction }) => {
    const params = new URLSearchParams(rowsQuerySearch({ filters, sort, direction }));
    params.set("sort", sort);
    params.set("direction", direction);
    params.set("fields", "list");
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    const payload = await webApiJson(`/api/rows?${params.toString()}`, { label: "Rows" });
    return { rows: payload.rows ?? [], totalFiltered: payload.page?.totalFiltered ?? null, meta: payload.totals ?? null };
  },
  extraReducers: (builder) => {
    builder.addCase(submitVerdictThunk.fulfilled, (state, action) => {
      const { recordingId, verdict, totals } = action.payload;
      const row = state.entities.find((entity) => entity.recordingId === recordingId);
      if (row) {
        row.spotCheckStatus = "reviewed";
        row.humanVerdict = verdict?.verdict ?? verdict ?? row.humanVerdict;
      }
      if (totals) state.meta = totals;
    });
  }
});

export const RowsManager = rowsList.slice;
export const fetchRowsPageThunk = rowsList.fetchPageThunk;
export const { filtersInitialized, filterChanged, filtersCleared, sortChanged } = rowsList.slice.actions;
export const selectRowsSlice = rowsList.select;
export const selectRows = rowsList.selectItems;

export const selectRowsStatusLine = createSelector(
  [(state) => rowsList.select(state).entities.length, (state) => rowsList.select(state).totalFiltered, (state) => rowsList.select(state).status],
  (shown, totalFiltered, status) => {
    if (status === "failed") return "Row fetch failed";
    if (totalFiltered === null || totalFiltered === undefined) return "Loading rows…";
    return `Showing ${shown.toLocaleString()} of ${Number(totalFiltered).toLocaleString()} takes`;
  }
);

// Journey → Corpus click-through (threads, scorecards): reset to a single q filter,
// switch views, and fetch — the SPA equivalent of the legacy "/?q=<clusterId>" links.
// (Navigation pushes "/"; CorpusPage's URL-sync effect then writes the ?q= search.)
export function openCorpusWithQueryThunk(query) {
  return (dispatch) => {
    dispatch(filtersCleared());
    dispatch(filterChanged({ key: "q", value: query }));
    dispatch(navigateThunk({ view: "corpus" }));
    dispatch(fetchRowsPageThunk());
  };
}
