# Progress contract

## Outcome

Produce evidence sufficient to choose a local voice system for Cole and hand it
to ChloeSpeech without leaking biometric media, corrupting source recordings,
or confusing runtime/model differences with voice quality.

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
- A one-way, hash-pinned ChloeSpeech promotion manifest.

## Non-goals for v0.1

- A public or unauthenticated synthesis service.
- Uploading biometric media to a hosted model service.
- Automatic acceptance of a full training corpus before the pilot quality gate.
- Pretending direct TTS and performed-source voice conversion are identical
  experimental conditions.
- Fine-tuning before zero-shot cloning has been measured.
- Editing ChloeSpeech as a hidden side effect.

## Verification gates

1. Core formatting, lint, strict types, unit/integration tests, branch coverage,
   wheel build, schema generation, and asset-leak guard pass.
2. Pilot fixtures prove 24-bit PCM parsing, QC thresholds, manifest stability,
   traversal/symlink rejection, tamper detection, atomic intake, and idempotency.
3. Transfer remains a dry run by default and addresses an explicit Tailnet host
   and explicit landing path.
4. Every candidate passes a dependency/readiness probe before generation; every
   real render records immutable model and environment identity.
5. A bake-off cannot start without approved consent, checksum-valid references,
   held-out prompts, and enough free disk for the selected candidate.
6. Blind ordering is deterministic but hides candidate identity; reveal and
   aggregation are reproducible from saved scoring evidence.
7. The ChloeSpeech export validates its reference/model/provenance digests and
   performs no copy or consumer mutation without a separate command.
8. An independent review reports no unresolved high-severity finding.

## Progress rule

Do not record the full corpus until the five-minute pilot passes both automated
QC and a headphone review. Do not fine-tune until the zero-shot bake-off shows a
material, repeatable identity or stability gap.
