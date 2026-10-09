import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { Button, InputField, SelectField } from "@scshafe/ui";
import { fetchRowsPageThunk, filterChanged, filtersCleared, selectRowsSlice } from "../../state/RowsManager.js";
import { selectFilterOptions } from "../../state/SummaryManager.js";

const SELECT_FILTERS = [
  { key: "bucket", label: "Bucket", optionsKey: "buckets" },
  { key: "year", label: "Year", optionsKey: "years" },
  { key: "reviewQueue", label: "Queue", options: [{ value: "selected", label: "Spot-check sample" }] },
  { key: "spotCheckStatus", label: "Spot-check", optionsKey: "spotCheckStatuses" },
  { key: "transcriptStatus", label: "Transcript", optionsKey: "transcriptStatuses" },
  { key: "lyricMatchStatus", label: "Lyric mark", optionsKey: "lyricMatchStatuses" }
];

export function CorpusFiltersComponent() {
  const dispatch = useDispatch();
  const { filters } = useSelector(selectRowsSlice);
  const filterOptions = useSelector(selectFilterOptions);
  const anyActive = Object.values(filters).some(Boolean);

  const applyFilter = (key, value) => {
    dispatch(filterChanged({ key, value }));
    dispatch(fetchRowsPageThunk());
  };

  return (
    <section className="vj-filters" aria-label="Filters">
      <InputField
        id="corpus-search"
        label="Search"
        type="search"
        placeholder="filename, bucket, rationale"
        value={filters.q}
        onChange={(value) => applyFilter("q", value)}
      />
      {SELECT_FILTERS.map((filter) => {
        const values = filter.options ?? (filterOptions[filter.optionsKey] ?? []).map((value) => ({ value, label: value }));
        return (
          <SelectField
            key={filter.key}
            id={`corpus-filter-${filter.key}`}
            field={filter.key}
            label={filter.label}
            value={filters[filter.key]}
            options={[{ value: "", label: "All" }, ...values]}
            onChange={(value) => applyFilter(filter.key, value)}
          />
        );
      })}
      {anyActive ? (
        <Button
          size="mini"
          label="Clear filters"
          onClick={() => {
            dispatch(filtersCleared());
            dispatch(fetchRowsPageThunk());
          }}
        />
      ) : null}
    </section>
  );
}
