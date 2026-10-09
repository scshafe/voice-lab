import { createResourceSlice, webApiJson } from "@scshafe/ui/state";

// ============================================================================
// CoachManager — the /api/coach payload behind the Practice page, on
// @scshafe/ui/state's createResourceSlice. The server computes due-for-a-take
// rankings and frontier flags from the live serve state, so a watchd ingest
// refreshes recommendations on the next fetch — this slice just carries them.
// ============================================================================

const coach = createResourceSlice({
  name: "CoachManager",
  fetch: () => webApiJson("/api/coach", { label: "Coach" })
});

export const CoachManager = coach.slice;
export const fetchCoachThunk = coach.fetchThunk;
export const selectCoach = coach.select;
