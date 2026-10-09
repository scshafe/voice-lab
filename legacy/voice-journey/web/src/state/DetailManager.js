import { createDetailSlice, webApiJson } from "@scshafe/ui/state";
import { submitVerdictThunk } from "./RowsManager.js";

// ============================================================================
// DetailManager + TranscriptManager — the row-detail Sheet, on @scshafe/ui/state's
// createDetailSlice, used TWICE (S2): the full row (GET /api/row/:id) and the
// gated local transcript text are both one-open detail lifecycles with the
// stale-response drop. The transcript follows the row: opening or closing the
// row detail resets it, so transcript text never outlives its sheet (mirrors
// the server's explicit-request-only scope).
// ============================================================================

const detail = createDetailSlice({
  name: "DetailManager",
  fetch: ({ id }) => webApiJson(`/api/row/${encodeURIComponent(id)}`, { label: "Row detail" }).then((payload) => payload?.row ?? null),
  extraReducers: (builder) => {
    builder.addCase(submitVerdictThunk.fulfilled, (state, action) => {
      if (!state.data || state.data.recordingId !== action.payload.recordingId) return;
      state.data.spotCheckStatus = "reviewed";
      const verdict = action.payload.verdict;
      state.data.humanVerdict = verdict?.verdict ?? verdict ?? state.data.humanVerdict;
      state.data.reviewedBy = verdict?.reviewedBy ?? "operator";
      state.data.reviewedAt = verdict?.reviewedAt ?? state.data.reviewedAt;
    });
  }
});

export const DetailManager = detail.slice;
export const openRowDetailThunk = detail.openThunk;
export const detailClosed = detail.slice.actions.closed;
export const selectDetail = detail.select;

const transcript = createDetailSlice({
  name: "TranscriptManager",
  fetch: ({ id }) => webApiJson(`/api/transcript/${encodeURIComponent(id)}`, { label: "Transcript" }).then((payload) => payload?.text ?? ""),
  extraReducers: (builder) => {
    // The transcript belongs to the open row: a new row opening, or the sheet
    // closing, resets it.
    const reset = () => ({ openId: null, status: "idle", error: null, data: null });
    builder.addCase(detail.openThunk.pending, reset);
    builder.addCase(detail.slice.actions.closed, reset);
  }
});

export const TranscriptManager = transcript.slice;
export const fetchTranscriptTextThunk = transcript.openThunk;
export const selectTranscript = transcript.select;
