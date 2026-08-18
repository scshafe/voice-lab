# Current status

## Implemented increment

The pilot-to-bake-off code path is implemented and verified with synthetic
fixtures: capture, transfer planning, intake, consent, references, datasets,
candidate/source locks, isolated workers, normalization, blinded review, and
ChloeSpeech handoff validation/export. The CLI now persists and resumes the
all-candidate audition matrix, runs one candidate at a time, deeply validates a
schema-authored promotion manifest, exclusively writes a canonical private
handoff, and provides digest-confirmed model-cache eviction for the constrained
mini. It does not author the human listening summary, verdict, winning-run, or
promotion manifest. Real voice trials begin only after Cole records and
personally approves the pilot scope.

Integrity hardening now binds the held-out prompt suite and equal candidate
matrices on every load, launches installed workers without inherited import
paths, hash-locks every runtime artifact (including transitive distributions),
records before/after executable byte evidence, requires a completely clean
pinned Seed checkout, seals each installed runtime tree and exact Homebrew
CPython/formula/macOS closure, and rehashes the complete
result/evidence/receipt chain before resume or status reports completion.
Every new trial requires an externally retained resident-receipt digest and
copies its source-manifest/Git-integrity preimages so historical validation
remains complete after deliberate cache eviction.

## Candidate matrix

- Qwen3-TTS 1.7B Base through MLX-Audio
- Chatterbox Turbo
- Chatterbox multilingual V3 through MLX-Audio
- F5-TTS v1 Base (non-commercial pretrained-weight caveat)
- Seed-VC (separate voice-conversion track)

## Known host constraint

The Mac mini currently has limited free disk. Runtime planning must reserve
operating-system headroom and may require sequential model caching or a separate
approved runner before all weight sets can coexist.

All five candidates use the installed CPython 3.12 base. Seed-VC's dependency
graph now resolves as a complete macOS-arm64 binary lock on 3.12, but this is a
compatibility hypothesis until its pinned checkout, weights, and MPS path pass
a real cold/warm smoke. No release claim treats lock resolution as that smoke.

## Deployment gates still human/external

- Record the UMIK-1 pilot in a foreground MacBook Terminal.
- Verify the mini host key and Tailnet ACL out of band.
- Install and denial-test the pinned write-only `rrsync` receiver/key lane.
- Install the verified application wheel non-editably under the content-addressed
  mini release root, seal its install receipt, and only then render/load intake.
- Run candidate downloads/renders explicitly and sequentially.
- Author and review the schema-bound promotion records from real blind-review
  evidence, then run `promotion validate` and retain its promotion digest.
- Export the handoff into a private destination and review ChloeSpeech provider
  compatibility before any consumer-side render.
