import { createAsyncThunk } from "@reduxjs/toolkit";
import { createResourceSlice, showToastThunk, webApiJson, webApiMutation } from "@scshafe/ui/state";

// ============================================================================
// GoalsManager — pre-registered n=1 experiments (/api/goals). The server owns
// the methodology: specs freeze at creation and verdicts derive from the first
// N qualifying takes. This slice lists them and submits create/abandon; every
// mutation refetches so the derived statuses stay server-truthful.
// ============================================================================

const goals = createResourceSlice({
  name: "GoalsManager",
  fetch: () => webApiJson("/api/goals", { label: "Goals" })
});

export const GoalsManager = goals.slice;
export const fetchGoalsThunk = goals.fetchThunk;
export const selectGoals = goals.select;

export const createGoalThunk = createAsyncThunk(
  "GoalsManager/create",
  async (spec, { dispatch }) => {
    try {
      const result = await webApiMutation("/api/goals", spec, { label: "Goal" });
      dispatch(showToastThunk({ kind: "success", message: "Goal pre-registered — the next qualifying takes decide it." }));
      dispatch(fetchGoalsThunk());
      return result;
    } catch (error) {
      dispatch(showToastThunk({ kind: "error", message: `Goal not created: ${error.message}` }));
      throw error;
    }
  }
);

export const abandonGoalThunk = createAsyncThunk(
  "GoalsManager/abandon",
  async ({ goalId }, { dispatch }) => {
    const result = await webApiMutation("/api/goals/abandon", { goalId }, { label: "Goal" });
    dispatch(fetchGoalsThunk());
    return result;
  }
);
