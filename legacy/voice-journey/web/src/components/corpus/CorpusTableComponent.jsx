import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { Badge, Button, InfiniteScrollSentinel, PinnedDataTable } from "@scshafe/ui";
import { fetchRowsPageThunk, filterChanged, selectRows, selectRowsSlice, selectRowsStatusLine, sortChanged } from "../../state/RowsManager.js";
import { openRowDetailThunk } from "../../state/DetailManager.js";
import { playbackRequested, selectActivePlaybackId } from "../../state/PlaybackManager.js";
import { selectPlaybackEnabled } from "../../state/SummaryManager.js";
import { bucketTone, fmtCaptured, fmtConfidence, fmtSeconds, labelize } from "../../utils/fmt.js";

function SortHeader({ field, children }) {
  const dispatch = useDispatch();
  const { sort, direction } = useSelector(selectRowsSlice);
  const active = sort === field;
  return (
    <button
      type="button"
      className="vj-sort-header"
      data-active={active ? "true" : "false"}
      onClick={() => {
        dispatch(sortChanged({ field }));
        dispatch(fetchRowsPageThunk());
      }}
    >
      {children}
      <span aria-hidden="true">{active ? (direction === "asc" ? "▲" : "▼") : ""}</span>
    </button>
  );
}

function PlaybackCell({ row }) {
  const dispatch = useDispatch();
  const playbackEnabled = useSelector(selectPlaybackEnabled);
  const activeId = useSelector(selectActivePlaybackId);
  if (activeId === row.recordingId) {
    // The single instantiated player on the page — swapped between rows on demand.
    return <audio className="vj-inline-audio" controls autoPlay preload="none" src={`/api/audio/${encodeURIComponent(row.recordingId)}`} />;
  }
  return (
    <Button
      size="mini"
      label="Play"
      disabled={!playbackEnabled}
      title={playbackEnabled ? "Stream this take (per-click, release-gated)" : "Playback requires the release-gated launch"}
      onClick={() => dispatch(playbackRequested({ recordingId: row.recordingId }))}
    />
  );
}

function buildColumns(dispatch) {
  return [
    {
      id: "queue",
      header: <SortHeader field="reviewQueueOrder">Queue</SortHeader>,
      ariaLabel: "Review queue order",
      width: 64,
      align: "right",
      render: (row) => (row.reviewQueueOrder ? <span className="vj-num">#{row.reviewQueueOrder}</span> : null)
    },
    {
      id: "filename",
      header: <SortHeader field="filename">Filename</SortHeader>,
      ariaLabel: "Filename",
      pinned: "left",
      rowHeader: true,
      minWidth: 220,
      render: (row) => (
        <button type="button" className="vj-sort-header" onClick={() => dispatch(openRowDetailThunk({ id: row.recordingId }))} title="Open row detail">
          {row.filename}
        </button>
      )
    },
    { id: "captured", header: <SortHeader field="capturedAt">Captured</SortHeader>, minWidth: 150, render: (row) => <span className="vj-num">{fmtCaptured(row.capturedAt)}</span> },
    { id: "year", header: <SortHeader field="year">Year</SortHeader>, width: 64, render: (row) => row.year },
    { id: "duration", header: <SortHeader field="durationSeconds">Duration</SortHeader>, width: 84, align: "right", render: (row) => <span className="vj-num">{fmtSeconds(row.durationSeconds)}</span> },
    {
      id: "bucket",
      header: <SortHeader field="bucket">Bucket</SortHeader>,
      minWidth: 150,
      render: (row) => <Badge value={labelize(row.bucket)} tone={bucketTone[row.bucket] ?? "blue"} />
    },
    { id: "confidence", header: <SortHeader field="confidence">Conf.</SortHeader>, width: 66, align: "right", render: (row) => <span className="vj-num">{fmtConfidence(row.confidence)}</span> },
    {
      id: "spotCheck",
      header: <SortHeader field="spotCheckStatus">Spot-check</SortHeader>,
      minWidth: 130,
      render: (row) => (
        <span className="vj-cell-stack">
          <span className={row.spotCheckStatus === "not_reviewed" ? "vj-muted" : undefined}>{labelize(row.spotCheckStatus)}</span>
          {row.humanVerdict ? <small>{labelize(row.humanVerdict)}</small> : null}
        </span>
      )
    },
    {
      id: "transcript",
      header: <SortHeader field="transcriptStatus">Transcript</SortHeader>,
      minWidth: 130,
      render: (row) => (
        <span className="vj-cell-stack">
          <span>{labelize(row.transcriptStatus)}</span>
          {row.transcriptWordCount !== null ? <small>{row.transcriptWordCount} words</small> : null}
        </span>
      )
    },
    {
      id: "cluster",
      header: "Lyric cluster",
      minWidth: 170,
      render: (row) => {
        const label = row.lyricMatchClusterLabel || row.lyricMatchCluster;
        if (!label) return null;
        return (
          <button
            type="button"
            className="vj-sort-header"
            title="Filter the table to this cluster"
            onClick={() => {
              dispatch(filterChanged({ key: "q", value: row.lyricMatchCluster ?? label }));
              dispatch(fetchRowsPageThunk());
            }}
          >
            {label}
          </button>
        );
      }
    },
    {
      id: "actions",
      header: "Listen / review",
      ariaLabel: "Row actions",
      minWidth: 200,
      render: (row) => (
        <span className="vj-row-actions">
          <PlaybackCell row={row} />
          <Button size="mini" label="Review" onClick={() => dispatch(openRowDetailThunk({ id: row.recordingId }))} />
        </span>
      )
    }
  ];
}

export function CorpusTableComponent() {
  const dispatch = useDispatch();
  const rows = useSelector(selectRows);
  const { status, hasMore } = useSelector(selectRowsSlice);
  const statusLine = useSelector(selectRowsStatusLine);
  const columns = React.useMemo(() => buildColumns(dispatch), [dispatch]);
  return (
    <section className="vj-table-region">
      <PinnedDataTable
        ariaLabel="Corpus takes"
        columns={columns}
        rows={rows}
        rowKey={(row) => row.recordingId}
        getRowClassName={(row) => (row.reviewQueueSelected ? "vj-row-queued" : undefined)}
      />
      <InfiniteScrollSentinel
        onLoadMore={() => dispatch(fetchRowsPageThunk())}
        disabled={status === "loading" || !hasMore}
      >
        {status === "loading" ? "Loading…" : statusLine}
      </InfiniteScrollSentinel>
    </section>
  );
}
