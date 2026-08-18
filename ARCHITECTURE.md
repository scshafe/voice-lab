# Architecture

## Boundaries

```text
code repository (Git-safe)
  schemas + CLI + workers + prompts + policy + tests

private asset root (mode 0700)
  incoming/       resumable Tailnet landing zones
  raw/            verified, write-protected capture sessions
  derived/        trims, resamples, references, datasets
  provenance/     consent and transfer approvals
  experiments/    plans, raw renders, normalized renders, run evidence
  evaluations/    blinded indices, listening scores, reveals, verdicts
  models/         shared model cache and immutable lock evidence
  runtimes/       isolated Python environments/source checkouts
  tmp/            same-filesystem atomic staging
```

## Data flow

```text
PromptSuite
    │
    ▼
CaptureSession ── finalize/QC ──> CaptureManifest + manifest digest
    │                                  │
    └──── rsync staging over Tailnet ──┘
                                       ▼
                                  Intake validator
                                       │
                                       ▼
                              content-addressed raw session
                                       │
                         reference/dataset derivations
                                       │
                                       ▼
ExperimentPlan ──> isolated WorkerRequest/WorkerResult ──> normalization
                                       │
                                       ├── automatic diagnostics
                                       └── blinded listening package
                                                  │
                                                  ▼
                                           Verdict + promotion
```

## Trust model

- Capture hosts are not trusted to declare correctness; the mini re-hashes and
  re-inspects every accepted file.
- Transport encryption does not replace the content manifest.
- Worker processes are dependency isolation boundaries, not security sandboxes.
- The audition orchestrator persists a prompt-suite-bound immutable matrix,
  runs one isolated installed worker subprocess at a time, and resumes only
  after rehashing the full result/evidence chain.
- A model cache is untrusted until its resolved revision and relevant artifact
  hashes have been captured.
- Seed-VC source performances are path-, byte-count-, and SHA-256-bound in the
  worker request and rechecked before and after inference.
- Model eviction is an explicit, digest-confirmed deletion of one verified
  source-ID directory and is serialized against active workers.
- Static review pages receive blinded audio indices, never the reveal mapping.

## Reusable-library lens

The canonical JSON, secure manifest intake, content-addressed evidence, isolated
worker protocol, and blind-evaluation planner are intentionally provider-neutral.
After the pilot, extract them only if a second project demonstrates the same
boundary; avoid premature packaging around voice-specific policy.
