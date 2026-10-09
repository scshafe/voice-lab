import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { Button, Sheet, SheetBody, SheetFooter, SheetHeader, TextAreaField } from "@scshafe/ui";
import { timestamp } from "@scshafe/ui/format";
import { detailClosed, fetchTranscriptTextThunk, selectDetail, selectTranscript } from "../../state/DetailManager.js";
import { submitVerdictThunk, VERDICT_CHOICES } from "../../state/RowsManager.js";
import { selectPlaybackEnabled, selectTranscriptTextEnabled } from "../../state/SummaryManager.js";
import { fmtBytes, fmtConfidence, fmtSeconds, labelize } from "../../utils/fmt.js";

function MetaList({ entries }) {
  const filtered = entries.filter(([, value]) => value !== null && value !== undefined && value !== "");
  return (
    <dl>
      {filtered.map(([label, value]) => (
        <React.Fragment key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

export function RowDetailSheetComponent() {
  const dispatch = useDispatch();
  const detail = useSelector(selectDetail);
  const transcript = useSelector(selectTranscript);
  const playbackEnabled = useSelector(selectPlaybackEnabled);
  const transcriptTextEnabled = useSelector(selectTranscriptTextEnabled);
  const [note, setNote] = React.useState("");
  const row = detail.data;

  React.useEffect(() => {
    setNote("");
  }, [detail.openId]);

  if (!detail.openId) return null;
  const close = () => dispatch(detailClosed());

  return (
    <Sheet open onClose={close} ariaLabel="Take detail" dataSuiComponent="RowDetailSheet">
      <SheetHeader title={row?.filename ?? detail.openId} description={row ? `${labelize(row.bucket)} · ${row.year ?? ""}` : undefined} />
      <SheetBody>
        {detail.status === "loading" ? <p className="vj-muted">Loading…</p> : null}
        {detail.status === "failed" ? <p className="vj-muted">{detail.error}</p> : null}
        {row ? (
          <div className="vj-detail-grid">
            <MetaList
              entries={[
                ["Captured", timestamp(row.capturedAt)],
                ["Duration", fmtSeconds(row.durationSeconds)],
                ["Size", fmtBytes(row.sizeBytes)],
                ["Bucket", labelize(row.bucket)],
                ["Confidence", fmtConfidence(row.confidence)],
                ["Spot-check", labelize(row.spotCheckStatus)],
                ["Human verdict", row.humanVerdict ? labelize(row.humanVerdict) : null],
                ["Reviewed by", row.reviewedBy],
                ["Reviewed at", row.reviewedAt ? timestamp(row.reviewedAt) : null],
                ["Queue", row.reviewQueueOrder ? `#${row.reviewQueueOrder} — ${row.reviewQueueReason ?? ""}` : null],
                ["Transcript", `${labelize(row.transcriptStatus)}${row.transcriptWordCount !== null ? ` · ${row.transcriptWordCount} words` : ""}${row.transcriptLanguage ? ` · ${row.transcriptLanguage}` : ""}`],
                ["Lyric cluster", row.lyricMatchClusterLabel || row.lyricMatchCluster],
                ["Lyric confidence", row.lyricMatchConfidence],
                ["Wordless vocalise", row.wordlessVocaliseMarked ? "marked" : null]
              ]}
            />
            {row.rationale ? (
              <div className="vj-detail-section">
                <h3>Classifier rationale</h3>
                <p style={{ margin: 0, fontSize: 13 }}>{row.rationale}</p>
              </div>
            ) : null}
            <div className="vj-detail-section">
              <h3>Listen</h3>
              {playbackEnabled
                ? <audio controls preload="none" src={`/api/audio/${encodeURIComponent(row.recordingId)}`} style={{ width: "100%" }} />
                : <p className="vj-muted" style={{ margin: 0 }}>Playback requires the release-gated launch.</p>}
            </div>
            <div className="vj-detail-section">
              <h3>Transcript text</h3>
              {transcriptTextEnabled && row.transcriptStatus === "completed" && row.transcriptTextLocalOnly ? (
                transcript.status === "idle"
                  ? <Button size="mini" label="View local text" onClick={() => dispatch(fetchTranscriptTextThunk({ id: row.recordingId }))} />
                  : transcript.status === "loading"
                    ? <p className="vj-muted" style={{ margin: 0 }}>Loading…</p>
                    : transcript.status === "failed"
                      ? <p className="vj-muted" style={{ margin: 0 }}>{transcript.error}</p>
                      : <pre className="vj-transcript-pre">{transcript.data}</pre>
              ) : (
                <p className="vj-muted" style={{ margin: 0 }}>
                  {row.transcriptStatus !== "completed" ? "No completed transcript." : "Local transcript text is not enabled for this launch."}
                </p>
              )}
            </div>
            <div className="vj-detail-section">
              <h3>Verdict</h3>
              <TextAreaField id="verdict-note" label="Spot-check note (optional)" rows={2} value={note} onChange={setNote} />
              <div className="vj-verdict-buttons">
                {VERDICT_CHOICES.map((choice) => (
                  <Button
                    key={choice}
                    size="mini"
                    variant={row.humanVerdict === choice ? "primary" : "secondary"}
                    label={labelize(choice)}
                    onClick={() => dispatch(submitVerdictThunk({ recordingId: row.recordingId, verdict: choice, note: note || null }))}
                  />
                ))}
              </div>
            </div>
          </div>
        ) : null}
      </SheetBody>
      <SheetFooter>
        <Button label="Close" onClick={close} />
      </SheetFooter>
    </Sheet>
  );
}
