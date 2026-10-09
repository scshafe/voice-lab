import { createSuiStore, Popovers, Toasts } from "@scshafe/ui/state";
import { CoachManager } from "./CoachManager.js";
import { DetailManager, TranscriptManager } from "./DetailManager.js";
import { GoalsManager } from "./GoalsManager.js";
import { JourneysManager } from "./JourneysManager.js";
import { NavigationManager } from "./NavigationManager.js";
import { PlaybackManager } from "./PlaybackManager.js";
import { RefereeManager } from "./RefereeManager.js";
import { RowsManager } from "./RowsManager.js";
import { SummaryManager } from "./SummaryManager.js";
import { JourneyView, TrendsManager } from "./TrendsManager.js";
import { TakeInspectorManager } from "./TakeInspectorManager.js";

export function createVoiceJourneyStore(options = {}) {
  return createSuiStore({
    slices: [
      CoachManager,
      DetailManager,
      GoalsManager,
      TranscriptManager,
      JourneysManager,
      JourneyView,
      NavigationManager,
      PlaybackManager,
      Popovers,
      RefereeManager,
      RowsManager,
      SummaryManager,
      TakeInspectorManager,
      Toasts,
      TrendsManager
    ],
    preloadedState: options.preloadedState
  });
}
