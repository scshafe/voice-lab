import { createRouteStateSlice } from "@scshafe/ui/state";

// ============================================================================
// NavigationManager — which page is on screen, on @scshafe/ui/state's
// createRouteStateSlice (pathname strategy: the server owns /, /journey and
// /referee as HTML routes, and every existing deep link uses them). This file
// keeps only the domain vocabulary: the view names and their paths.
// ============================================================================

export const VIEWS = Object.freeze(["corpus", "journey", "practice", "referee"]);

export function viewForPathname(pathname) {
  if (pathname === "/journey") return "journey";
  if (pathname === "/practice") return "practice";
  if (pathname === "/referee") return "referee";
  return "corpus";
}

export function pathnameForView(view) {
  if (view === "journey") return "/journey";
  if (view === "practice") return "/practice";
  if (view === "referee") return "/referee";
  return "/";
}

const navigation = createRouteStateSlice({
  name: "NavigationManager",
  strategy: "pathname",
  parse: (location) => ({ view: viewForPathname(location.pathname) }),
  write: (state) => pathnameForView(state.view)
});

export const NavigationManager = navigation.slice;
export const navigateThunk = navigation.navigateThunk;
export const attachNavigation = navigation.attach;
export const { routeChanged } = navigation;

export function selectView(state = {}) {
  return navigation.select(state).view ?? "corpus";
}
