import React from "react";
import { createRoot } from "react-dom/client";
import { SuiProviders } from "@scshafe/ui/state";
import "@scshafe/ui/layout.css";
import "@scshafe/ui/components.css";
import "./theme.css";
import { AppComponent } from "./AppComponent.jsx";
import { createVoiceJourneyStore } from "./state/StoreManager.js";
import { attachNavigation } from "./state/NavigationManager.js";
import { filtersInitialized, fetchRowsPageThunk } from "./state/RowsManager.js";
import { fetchSummaryThunk } from "./state/SummaryManager.js";

function mountVoiceJourney() {
  const reactRoot = document.getElementById("app");
  if (!reactRoot) throw new Error("Voice Journey app root #app was not found");
  const store = createVoiceJourneyStore();

  // Boot state from the URL: the route slice parses the live location at store
  // creation; the corpus filters honor the legacy deep-link params
  // (/?bucket=…&q=… — the Journey page's thread + scorecard links).
  store.dispatch(filtersInitialized(Object.fromEntries(new URLSearchParams(location.search).entries())));
  attachNavigation(store);

  store.dispatch(fetchSummaryThunk());
  store.dispatch(fetchRowsPageThunk());

  createRoot(reactRoot).render(
    <SuiProviders theme="dark" store={store}>
      <AppComponent />
    </SuiProviders>
  );
}

mountVoiceJourney();
