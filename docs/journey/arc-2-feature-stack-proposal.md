# Arc 2 feature-extraction stack — design-board proposal

Status: proposed (design-board consult requested) · Author: agent session, 2026-07-30
Scope: the pinned open-source stack, the extraction seam, and the storage split
for per-recording voice features over the curated singing subset.

## What this decides

The operator roadmap for Arc 2 calls for a design-board consult to "decide and
pin the OSS feature-extraction stack." This proposal pins five things:

1. **The analysis runtime** — a project-local Python virtual environment.
2. **The libraries** — praat-parselmouth + librosa (+ optional torchcrepe later).
3. **The extraction seam** — a Node orchestrator in the established module
   shape driving a long-lived Python worker over ffmpeg-decoded temp WAVs.
4. **The storage split** — detailed per-recording feature JSONs stay in the
   gitignored local store; the committed manifest carries summary aggregates
   only, keyed by `recordingId` with a content digest.
5. **The feature contract v1** — the measured dimensions listed below.

Everything runs on this machine. No audio, feature arrays, or transcript
text leaves the host or enters the repo. The full-corpus run is release-gated
exactly like the STT batch was.

## Runtime and libraries

| Choice | Pinned | Openness | Rationale / swap candidate |
|---|---|---|---|
| Python 3.12 (Homebrew, already installed) | `analysis/.venv` via `python3.12 -m venv` | open | System Python 3.9 stays untouched. Swap: `uv`-managed venv if adopted platform-wide. |
| praat-parselmouth | `analysis/requirements.txt` (pinned) | open (GPL-3) | Praat's validated voice metrics as fast C code: autocorrelation f0, jitter, shimmer, HNR, CPPS, formants. Swap: Essentia. |
| librosa (+ numpy/scipy/soundfile) | pinned | open (ISC/BSD) | Spectral features (centroid, rolloff, band energies) and general DSP. Swap: Essentia. |
| ffmpeg (host, already provisioned) | decode only | open | Same decode seam as STT: temp mono WAV, deleted after each recording. |
| torchcrepe (deferred, optional) | not in v1 | open | Neural f0 cross-check for noisy takes; adds ~2 GB torch dependency. Planned as `--f0-engine crepe` upgrade with its own provisioning line. |

**f0 engine v1 is Praat autocorrelation** (75–1200 Hz, 10 ms hop): well-validated
on monophonic voice and fast enough for the full corpus in a couple of hours.
librosa's pYIN was considered and rejected for v1 (an order of magnitude
slower); CREPE is the designed upgrade path where noise robustness matters.

**Decode target is 22 050 Hz mono** — above STT's 16 kHz for spectral headroom
(singer's-formant band and rolloff measures sit comfortably below Nyquist),
still cheap. The rate is recorded in every manifest row's config fingerprint.

## The extraction seam

`npm run features -- extract-index` follows the local-STT pattern exactly:

- **Dry-run** reads committed manifests only and prints the scope, tool, and
  resume plan (sandbox-safe).
- **Non-dry-run** requires `--approval` + `--corpus-root` (release-gate tier:
  it reads audio bytes through ffmpeg into a temp WAV, deleted per recording).
- **Selection** joins the recording index with the filter-results manifest and
  defaults to `clean_singing` rows (`--buckets` widens it; `--limit` supports
  smoke runs).
- **The worker** (`analysis/extract_features.py`) is spawned once per run and
  fed task lines over stdin, emitting one JSON result line per recording —
  no per-recording interpreter startup. A failed recording records a
  `status: "failed"` row and the run continues (unlike the STT batch, which
  aborted on first error — a recorded lesson).
- **Resume** is keyed on `recordingId`; the manifest is rewritten after each
  completion; failed rows are retried on rerun.

## Storage split and provenance

- `local-artifacts/voice-features/<recordingId>.json` (gitignored): the
  detailed record — decimated f0 contour, per-segment vibrato table, spectral
  summaries. Never committed; the browser or later analysis reads it locally,
  the same way transcript text works today.
- `manifests/local-voice-features.json` (committed): per-recording summary
  aggregates only (~45 numbers across voicing, pitch, tuning, vibrato,
  quality, spectral, dynamics, phrasing), plus `featuresDigest` (sha256 of the
  local JSON, making runs verifiable and cacheable), `configFingerprint`
  (sha256 of the analysis configuration + library versions, the future
  mission-pipeline stage fingerprint), status, and tool provenance.

## Feature contract v1

| Group | Measures |
|---|---|
| voicing | voiced fraction, frame counts |
| pitch | f0 percentiles (p05/p25/p50/p75/p95) in Hz, range in semitones, median absolute deviation |
| tuning | inferred tuning offset (cents vs A440 grid), median absolute cent error, share of voiced time within ±25 cents |
| vibrato | rate (Hz), extent (cents), fraction of sustained time with vibrato, sustained-segment count |
| quality | jitter (local), shimmer (local), mean HNR, CPPS |
| spectral | centroid mean/sd, rolloff, singer's-formant band ratio (2.8–3.4 kHz over full band), on voiced frames |
| dynamics | voiced RMS dB percentiles (p10/p50/p90), dynamic spread |
| phrasing | longest sustained voiced segment, mean voiced-segment duration, pause rate per minute |

Measurement honesty rules: recordings are uncontrolled (rooms, phones, mic
distance vary across five years), so downstream analysis prefers relative and
ratio measures, reports distributions rather than point claims, and treats
absolute dB values as low-reliability. These caveats ride in the manifest's
method notes.

## Verification plan

1. `npm test` — fixture-driven orchestrator tests with injected hooks; no
   audio, no venv, no subprocesses in CI.
2. `analysis/selftest.py` — host-run synthetic-audio check: generates known
   signals (a 440 Hz tone, a tone with 5.5 Hz / ±40 cent vibrato, a glissando,
   noise) inside the venv and asserts the extractor recovers the ground truth
   within tolerance. No corpus audio involved.
3. A `--limit` smoke run over a handful of recordings (release-gate consult
   covers the batch; the smoke run uses the same approval).
4. The gated full run over the clean-singing subset, resumable.

## Host provisioning (release-gate consult, alongside the batch command)

```sh
/opt/homebrew/bin/python3.12 -m venv analysis/.venv
analysis/.venv/bin/pip install -r analysis/requirements.txt
analysis/.venv/bin/python analysis/selftest.py
```

## Smoke-run findings (2026-07-30, operator-approved 5-recording run)

The first live run over five 2019 recordings surfaced a real measurement
hazard: Praat's tracker locked onto upper harmonics (~6×) on a few percent of
frames of three breathy phone-recorded takes, pinning p95 near 960 Hz and
inflating the p05–p95 "range" to ~44 semitones. Two changes landed in the
worker before any full run:

1. **Two-layer f0 outlier suppression.** A local gate rejects frames deviating
   > 600 cents from the running median of voiced neighbors (±0.5 s window) —
   isolated locks and fry-floor frames fail it, while vibrato and glissandi
   track their own median and survive (selftest-proven). A global gate rejects
   sustained lock regions that dominate their own window: frames > 1800 cents
   from the take's global voiced median.
2. **Rejection diagnostics per recording.** `rejectedOutlierShare` (local) and
   `rejectedGlobalOutlierShare` are reported in every summary row. Heavy
   rejection marks a take as lower-reliability rather than silently passing —
   and heavy *global* rejection specifically flags possible genuine
   whistle-register content for listening, since the gate cannot distinguish
   whistling from tracking error on its own.

After suppression the five takes measure 10.8–18.4 semitone ranges with
plausible percentiles throughout. The worker CONFIG (including these
thresholds) now rides in the hello line and is embedded in the manifest's
`configFingerprint`, so any threshold change invalidates downstream caches
honestly. This experience is also the concrete argument for the CREPE
upgrade path on noisy takes.

## Decision asks

1. Approve the pinned stack (parselmouth + librosa, Praat-ac f0 v1, CREPE as
   the designed upgrade).
2. Approve the storage split (detailed features gitignored-local; summary
   aggregates + digests committed).
3. Approve 22 050 Hz mono as the decode target.
4. Approve the default run scope (clean_singing subset, widenable by flag).
