// Small domain formatters for the corpus table. Generic helpers (timestamp, plural, …)
// come from @scshafe/ui/format; these encode voice-journey display choices only.

export function fmtCaptured(value) {
  if (!value) return "";
  return String(value).replace("T", " ").replace(/:\d\d(\.\d+)?Z?$/, "");
}

export function fmtSeconds(value) {
  if (value === null || value === undefined) return "";
  return `${Math.round(value)}s`;
}

export function fmtBytes(value) {
  if (value === null || value === undefined) return "";
  return `${(value / 1048576).toFixed(2)} MB`;
}

export function fmtConfidence(value) {
  if (value === null || value === undefined) return "";
  return Number(value).toFixed(2);
}

// Bucket → @scshafe/ui StatusTone. Voice-journey's classification vocabulary is domain,
// so the map lives here, not in @scshafe/ui/format's toneByState.
export const bucketTone = Object.freeze({
  clean_singing: "green",
  uncertain_manual_review: "yellow",
  noise_contaminated_singing: "orange",
  music_contaminated_singing: "purple",
  non_singing: "red",
  unclassified: "blue"
});

export function labelize(value) {
  return String(value ?? "").replaceAll("_", " ");
}
