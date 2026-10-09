import { createResourceSlice, webApiJson } from "@scshafe/ui/state";

// ============================================================================
// SummaryManager — the one-shot /api/summary snapshot (filter options, gate
// states, review-queue info), on @scshafe/ui/state's createResourceSlice (S2). The
// old hand-rolled slice flattened the payload into state fields; the factory
// keeps the raw payload as `data` and these selectors carry the same reads.
// ============================================================================

const EMPTY_FILTER_OPTIONS = Object.freeze({ buckets: [], years: [], spotCheckStatuses: [], transcriptStatuses: [], lyricMatchStatuses: [] });

const summary = createResourceSlice({
  name: "SummaryManager",
  fetch: () => webApiJson("/api/summary", { label: "Summary" })
});

export const SummaryManager = summary.slice;
export const fetchSummaryThunk = summary.fetchThunk;
export const selectSummary = summary.select;

export function selectPlaybackEnabled(state = {}) {
  return Boolean(summary.selectData(state)?.playback?.enabled);
}

export function selectTranscriptTextEnabled(state = {}) {
  return Boolean(summary.selectData(state)?.transcriptText?.enabled);
}

export function selectFilterOptions(state = {}) {
  return summary.selectData(state)?.filterOptions ?? EMPTY_FILTER_OPTIONS;
}
