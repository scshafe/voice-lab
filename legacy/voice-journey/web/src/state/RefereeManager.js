import { createAsyncThunk, createSlice } from "@reduxjs/toolkit";
import { showToastThunk, webApiJson, webApiMutation } from "@scshafe/ui/state";

// ============================================================================
// RefereeManager — the blind same-song A/B trial flow.
//
// GET /api/referee/next hands the current pair (audio URLs only — years and
// cluster stay hidden until after judgment). POST /api/referee/verdicts
// persists the choice host-side and returns the reveal; "next trial" advances.
// GET /api/referee/results serves the Bradley–Terry curve. Playback gating is
// server-owned; enabled:false renders the gate notice.
// ============================================================================

export const REFEREE_CHOICES = Object.freeze(["a", "b", "too_close", "not_same_song", "skip"]);

const initialState = {
  status: "idle",
  error: null,
  enabled: null,
  progress: null,
  pair: null,
  reveal: null,
  lastChoice: null,
  submitting: false,
  results: { status: "idle", error: null, data: null }
};

export const fetchNextPairThunk = createAsyncThunk(
  "RefereeManager/fetchNext",
  () => webApiJson("/api/referee/next", { label: "Referee" })
);

export const submitRefereeChoiceThunk = createAsyncThunk(
  "RefereeManager/submitChoice",
  async ({ pairId, choice }, { dispatch }) => {
    try {
      return await webApiMutation("/api/referee/verdicts", { pairId, choice }, { label: "Referee verdict" });
    } catch (error) {
      dispatch(showToastThunk({ kind: "error", message: `Judgment failed: ${error.message}` }));
      throw error;
    }
  }
);

export const fetchRefereeResultsThunk = createAsyncThunk(
  "RefereeManager/fetchResults",
  () => webApiJson("/api/referee/results", { label: "Referee results" })
);

export const RefereeManager = createSlice({
  name: "RefereeManager",
  initialState,
  reducers: {
    trialAdvanced(state) {
      state.pair = null;
      state.reveal = null;
      state.lastChoice = null;
    }
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchNextPairThunk.pending, (state) => {
        state.status = "loading";
        state.error = null;
      })
      .addCase(fetchNextPairThunk.fulfilled, (state, action) => {
        const payload = action.payload ?? {};
        state.status = "loaded";
        state.enabled = Boolean(payload.enabled);
        state.progress = payload.progress ?? { judged: 0, poolSize: payload.poolSize ?? 0 };
        state.pair = payload.pair ?? null;
        state.reveal = null;
        state.lastChoice = null;
      })
      .addCase(fetchNextPairThunk.rejected, (state, action) => {
        state.status = "failed";
        state.error = action.error?.message ?? "referee fetch failed";
      })
      .addCase(submitRefereeChoiceThunk.pending, (state, action) => {
        state.submitting = true;
        state.lastChoice = action.meta.arg.choice;
      })
      .addCase(submitRefereeChoiceThunk.fulfilled, (state, action) => {
        state.submitting = false;
        state.reveal = action.payload?.reveal ?? null;
        state.progress = action.payload?.progress ?? state.progress;
        state.results = { ...initialState.results }; // stale after a new judgment
      })
      .addCase(submitRefereeChoiceThunk.rejected, (state) => {
        state.submitting = false;
        state.lastChoice = null;
      })
      .addCase(fetchRefereeResultsThunk.pending, (state) => {
        state.results = { status: "loading", error: null, data: null };
      })
      .addCase(fetchRefereeResultsThunk.fulfilled, (state, action) => {
        state.results = { status: "loaded", error: null, data: action.payload ?? null };
      })
      .addCase(fetchRefereeResultsThunk.rejected, (state, action) => {
        state.results = { status: "failed", error: action.error?.message ?? "results fetch failed", data: null };
      });
  }
});

export const { trialAdvanced } = RefereeManager.actions;

export function selectReferee(state = {}) {
  return state.RefereeManager ?? initialState;
}
