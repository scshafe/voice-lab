import { createDetailSlice, webApiJson } from "@scshafe/ui/state";

// ============================================================================
// TakeInspectorManager — the per-take inspector on the Journey page
// (fingerprint-dot click → GET /api/feature-detail/:id from the gated local
// feature store), on @scshafe/ui/state's createDetailSlice (S2).
// ============================================================================

const inspector = createDetailSlice({
  name: "TakeInspectorManager",
  fetch: ({ id }) => webApiJson(`/api/feature-detail/${encodeURIComponent(id)}`, { label: "Take detail" })
});

export const TakeInspectorManager = inspector.slice;
export const openTakeInspectorThunk = inspector.openThunk;
export const takeInspectorClosed = inspector.slice.actions.closed;
export const selectTakeInspector = inspector.select;
