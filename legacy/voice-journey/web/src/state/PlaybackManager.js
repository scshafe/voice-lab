import { createSlice } from "@reduxjs/toolkit";

// ============================================================================
// PlaybackManager — which single row has an instantiated <audio> element.
//
// The legacy table mounted 2,433 eager <audio controls> elements — the main
// DOM weight on the page. The SPA instantiates exactly ONE, for the row whose
// play affordance was clicked; clicking another row (or closing) swaps it.
// Playback stays per-click and release-gated server-side; this slice only
// decides which row renders the element.
// ============================================================================

const initialState = { activeRecordingId: null };

export const PlaybackManager = createSlice({
  name: "PlaybackManager",
  initialState,
  reducers: {
    playbackRequested(state, action) {
      state.activeRecordingId = action.payload?.recordingId ?? null;
    },
    playbackCleared(state) {
      state.activeRecordingId = null;
    }
  }
});

export const { playbackRequested, playbackCleared } = PlaybackManager.actions;

export function selectActivePlaybackId(state = {}) {
  return state.PlaybackManager?.activeRecordingId ?? null;
}
