# Progress contract

## Outcome

Produce evidence sufficient to choose a local voice system for Cole and speak
with it, as the newest voice release (D-055), without leaking biometric media,
corrupting source recordings, or confusing runtime/model differences with voice
quality. (Until 2026-10-11 the outcome was a handoff to ChloeSpeech, retired by
D-044.)

## Scope

- A native macOS pilot recorder with exact prompts and lossless mono PCM.
- Deterministic manifests, portable QC, secure Tailnet transfer planning, and a
  fail-closed Mac mini intake.
- Permission-restricted asset layout, consent/provenance records, immutable raw
  storage, reference selection, and derived-dataset boundaries.
- Isolated workers for Qwen3-TTS 1.7B Base, Chatterbox Turbo, Chatterbox
  multilingual V3, F5-TTS, and Seed-VC.
- Reproducible experiment plans, identical direct-TTS trials, separate
  voice-conversion trials, audio normalization, optional ASR/speaker metrics,
  static blind review, and a machine-readable verdict.
- A checksum-bound voice release, released automatically for the newest finished run or
  pinned by Cole, the base voice until one exists, and speech for registered tailnet callers
  (D-045, D-046, D-055; docs/SPEECH_API.md).

## Non-goals for v0.1

- A public, unauthenticated or always-on synthesis service, or audio streamed while it is
  synthesized.
- Uploading biometric media to a hosted model service.
- Automatic acceptance of a full training corpus before the pilot quality gate.
- Pretending direct TTS and performed-source voice conversion are identical
  experimental conditions.
- Fine-tuning before zero-shot cloning has been measured.

## Verification gates

1. Core formatting, lint, strict types, unit/integration tests, branch coverage,
   wheel build, schema generation, and asset-leak guard pass.
2. Pilot fixtures prove 24-bit PCM parsing, QC thresholds, manifest stability,
   traversal/symlink rejection, tamper detection, atomic intake, and idempotency.
3. Transfer remains a dry run by default and addresses an explicit Tailnet host
   and explicit landing path.
4. Every candidate passes a dependency/readiness probe before generation; every
   real render records immutable model and environment identity.
5. A bake-off cannot start without checksum-valid references of the pinned subject
   (`cole-self`; consent records are retired, D-050), held-out prompts, and enough
   free disk for the selected candidate.
6. Blind ordering is deterministic but hides candidate identity; reveal and
   aggregation are reproducible from saved scoring evidence.
7. A render uses only the current voice, the newest voice release of the pinned subject
   or the base voice while there is none, and refuses when its checkpoint or reference
   digest no longer matches; every render is in the ledger.
8. An independent review reports no unresolved high-severity finding.

## Progress rule

Do not record the full corpus until the five-minute pilot passes both automated
QC and a headphone review. Do not fine-tune until the zero-shot bake-off shows a
material, repeatable identity or stability gap.
