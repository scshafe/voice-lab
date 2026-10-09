import React from "react";
import { useSelector } from "react-redux";
import { rowsQuerySearch, selectRowsSlice } from "../../state/RowsManager.js";
import { CorpusFiltersComponent } from "./CorpusFiltersComponent.jsx";
import { CorpusTableComponent } from "./CorpusTableComponent.jsx";
import { RowDetailSheetComponent } from "./RowDetailSheetComponent.jsx";
import { selectView } from "../../state/NavigationManager.js";

export function CorpusPageComponent() {
  const slice = useSelector(selectRowsSlice);
  const view = useSelector(selectView);

  // Keep filter + sort state in the URL search (replaceState — no history spam) so the
  // table stays shareable/deep-linkable, matching the legacy page's ?bucket=…&q=… links.
  React.useEffect(() => {
    if (view !== "corpus") return;
    const search = rowsQuerySearch(slice);
    const target = `${location.pathname}${search ? `?${search}` : ""}`;
    const current = `${location.pathname}${location.search}`;
    if (current !== target) history.replaceState(null, "", target);
  }, [view, slice.filters, slice.sort, slice.direction]);

  return (
    <>
      <CorpusFiltersComponent />
      <CorpusTableComponent />
      <RowDetailSheetComponent />
    </>
  );
}
