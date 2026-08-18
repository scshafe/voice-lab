# ChloeSpeech handoff

Cole Voice Lab promotes a winner by producing evidence, not by copying audio or
editing ChloeSpeech. The lab-side contract is implemented in
`cole_voice_lab.promotion` and has one direction:

```text
private asset root
  reference WAV ───────────────┐
  approved consent JSON ───────┤
  reference provenance JSON ──┤
  model lock + source manifests ──┼─> PromotionManifest
  worker model evidence + files ──┼─> PromotionManifest
  winning run + output ────────┤       │
  sealed blind package ────────┤       │
  bound score sheet + summary ─┤       │
  winner reveal + verdict ─────┘       │ re-hash + re-parse + cross-check
                                       ▼
                              ChloeSpeechHandoff (in memory)
                                ├─ public-contract-shaped VoiceManifest
                                ├─ actual required disclosure Cue
                                └─ evidence index
```

The validator opens only existing files beneath an explicit asset root. It does
not create a destination, copy biometric media, install a provider, edit a
consumer catalog, or authorize publication.

## Promotion contract

`PromotionManifest` is strict, frozen, versioned, and rejects unknown fields. It
names exactly one subject, candidate, experiment, and winning trial. Its evidence
chain includes:

- `HumanReferenceBinding`: asset-relative WAV path, SHA-256, byte count, human
  classification, and private stable subject ID.
- `BoundConsentEvidence`: exact consent JSON file plus the expected strict
  `ConsentRecord` contents.
- `BoundProvenanceEvidence`: exact human-reference provenance JSON file plus its
  expected strict contents. The record binds the reference path/digest, subject,
  purpose, consent ID, and consent-file digest.
- `BoundModelLockEvidence`: exact model-lock JSON, the winning worker's exact
  `WorkerModelEvidence`, the primary and auxiliary immutable source manifests,
  and every selected model artifact. The lock's file map must equal the complete
  union of the source-manifest file maps. Candidate, model ID, immutable model
  revision, immutable code revision, manifest digests, and artifact bytes are
  cross-checked against the persisted winning result rather than trusted from a
  second operator-authored record.
- `BoundWinningRunEvidence`: exact run JSON plus experiment/trial/candidate,
  exact audition plan/run/result/request digests, provider/model/code/reference/
  consent identity, audition- and provider-settings digests, environment
  fingerprint, normalized output binding, and realized watermark state.
- `BoundBlindPackageEvidence`: the canonical sealed blind-package manifest. Its
  public index, reviewer-safe manifest, private reveal, copied WAVs, and original
  audition evidence are all revalidated through `verify_review_package()`.
- `BoundScoreSheetEvidence`: the exact v2 score sheet, including headphones
  attestation and commitments to the sealed review index and public package.
- `BoundListeningSummaryEvidence`: a strict promotion summary that names the
  package and score-sheet digests and embeds the complete recomputed aggregate.
- `BoundVerdictEvidence`: exact verdict JSON naming the winning candidate and
  one reviewed winning-candidate trial. It binds the run, model lock, blind
  package, score sheet, summary, and full reveal entry by digest and identity.

The expected JSON record is embedded in the promotion manifest as well as stored
in its bound file. Validation reads each file through the no-follow boundary,
checks its exact byte count and digest, parses it through the declared strict
model, and requires the parsed record to equal the embedded record. A digest
cannot silently bless different semantic fields.

Model artifacts and the winning output are re-hashed too. Every referenced WAV
and generated WAV is also parsed as nonempty, uncompressed PCM rather than
trusted by filename or declared media type. All bound evidence, audio, and model
files must have exactly one filesystem link; symlinks and hardlinks both fail
closed. This may be expensive; that cost is intentional at a production
promotion boundary.

Provider fields are not operator assertions. Promotion derives them from the
verified audition trial and worker model evidence through the versioned
`cole-voice-lab.worker-provider-projection/v1` crosswalk, then requires the
winning-run record and promotion manifest to match that projection exactly. The
provider voice is always the checksum-bound human reference; provider settings
come from the realized request settings after removing lab-only timing/profile
controls. A candidate without a reviewed projection fails closed.

The validator does not trust `blind_review_completed=true` or a manually typed
winner. It reruns package verification and score aggregation, requires at least
two direct-TTS candidates, selects the sole highest direct-TTS `overall_mean`,
and fails closed on a tie. The verdict's complete `RevealEntry` must occur
exactly once in the verified package and must name that winning candidate. The
entry then reopens the exact audition plan, run manifest, persisted result, and
request; their trial, candidate, reference, consent, model/code revisions,
settings, environment fingerprint, normalized output, and watermark evidence
must reproduce the promotion run record.

Promotion v2 is intentionally incompatible with v1's assertion-only verdict.
Earlier v2 drafts that lack source-manifest, worker-model-evidence, or provider-
projection bindings are also rejected. Promotion, winning-run, and verdict
records must be regenerated from a currently valid audition run, blind package,
and score sheet; they are not auto-upgraded.

## Human-reference requirements

Promotion accepts only:

- `identity_kind = "human_authorized"`;
- `recognizable_human = true`;
- the same `subject_id` in promotion, reference, consent, and provenance;
- approved, effective, unexpired, unrevoked consent containing
  `private-chloe-speech-rendering` and the `tailnet-local-host` boundary;
- timestamp-aware consent whose effective time precedes both the provenance
  record and winning run, and which remains valid through promotion creation;
- a human reference and provenance document beneath the caller's asset root;
- both a nonblank spoken disclosure and nonblank persistent disclosure; and
- a direct-TTS winning run. Voice-conversion results remain a separate track and
  are not promoted as text-to-speech providers.

The subject export deliberately omits a display name. A stable private ID is
enough for the ChloeSpeech identity binding and avoids copying unnecessary
personal information into render manifests.

## Chatterbox requirements

The retained Chatterbox audition candidates currently execute through
`mlx-audio`, so their reviewed projection exports `provider_id = "mlx-audio"`.
They cannot be relabeled as ChloeSpeech's official `chatterbox` adapter after
listening. An official adapter reproduction must be added as a new immutable
candidate and pass the same blind-review chain.

If a future reviewed worker projection emits `provider_id = "chatterbox"`, the
additional adapter-specific rules below apply:

For `provider_id = "chatterbox"`, promotion additionally requires:

```text
provider_voice = "reference-sha256:" + reference.sha256
```

The provider setting `variant` must be explicit:

| Variant | Required model ID |
|---|---|
| `turbo` | `ResembleAI/chatterbox-turbo` |
| `v3` | `ResembleAI/chatterbox` |
| `original` | `ResembleAI/chatterbox` |

The winning run, model lock, and promotion must agree on the exact model and code
commits. Declared and realized watermark states must agree, and Chatterbox must
remain watermarked. Exaggeration and CFG settings are rejected for Turbo and V3
because the current ChloeSpeech adapter only realizes them for the original
variant.

The friendly reusable identifier belongs in `voice_id`. Never replace the
checksum-derived `provider_voice` with a display name.

## Actual disclosure cue

A disclosure string in provenance is not audible by itself. ChloeSpeech validates
that a disclosure exists but does not insert script content. Consequently,
`build_chloespeech_handoff()` always emits at least one actual public `Cue`-shaped
record in `required_disclosure_cues`:

```json
{
  "schema_version": 1,
  "id": "synthetic-voice-disclosure",
  "speaker_id": "narrator",
  "text": "This narration uses an authorized synthetic voice.",
  "voice_id": "cole-chatterbox-turbo",
  "delivery": {},
  "pause_after_ms": 500
}
```

The handoff model verifies that at least one emitted cue speaks the exact declared
text with the promoted voice. A consumer must place that cue in its `CueSheet`;
receiving the handoff is not completion evidence. The consumer must also retain
the persistent disclosure wherever its policy requires it.

## Validation and export

The Python boundary is explicit:

```python
from datetime import UTC, datetime
from pathlib import Path

from cole_voice_lab.promotion import (
    PromotionManifest,
    build_chloespeech_handoff,
    validate_promotion,
)

asset_root = Path("/Users/cole/.local/share/cole-voice-lab")
manifest = PromotionManifest.model_validate_json(promotion_json)

validation = validate_promotion(
    manifest,
    asset_root=asset_root,
    as_of=datetime.now(UTC),
)
handoff = build_chloespeech_handoff(
    manifest,
    asset_root=asset_root,
    as_of=validation.evaluated_at,
)
payload = handoff.model_dump(mode="json")
```

`validate_promotion()` returns only the promotion digest, evaluation time, and
verified relative paths. `build_chloespeech_handoff()` validates again, then
returns a strict in-memory object. Both public functions serialize and fully
revalidate a detached snapshot before reading any field. Pydantic's frozen
models do not make nested dictionaries immutable, and `model_copy(update=...)`
does not validate updates; callers cannot use either behavior to change exported
provider or evidence values. Persisting or transporting `payload` is a separate
operator decision.

Consent is timestamp-granular while ChloeSpeech's public `review_by` clearance
field is date-granular. When consent expires, the handoff conservatively exports
the preceding UTC date. This can shorten authorization, but cannot extend it.
The consumer must still reauthorize the exact consent timestamp before use.

The generated voice manifest mirrors ChloeSpeech's public `VoiceManifest` shape:

- one `human_authorized` voice with a private subject ID;
- immutable model revision;
- purpose-bound approved clearance;
- checksum-bound consent, provenance, model lock, run, blind package, score
  sheet, recomputed listening summary, and verdict evidence;
- checksum-bound primary and auxiliary source manifests plus the exact winning
  worker model evidence;
- checksum-bound human reference;
- provider settings that match the winning run; and
- spoken and persistent disclosures.

No ChloeSpeech private or underscored type is imported. ChloeSpeech remains free
to validate the JSON through its own public model before rendering.

## Asset-root semantics

Every path in the promotion and handoff is a canonical POSIX relative path. The
validator requires a complete, group/world-inaccessible Cole Voice Lab asset
layout and rejects absolute paths, `..`, alternate separators, control
characters, missing files, and symlink traversal. All paths are interpreted
relative to the single `asset_root` supplied by the caller.

Choose stable relative locations before promotion. Moving a byte-identical
reference changes the promotion manifest and ChloeSpeech plan binding because the
portable path is part of the voice manifest. Editing or preprocessing the
reference changes its SHA-256, which also changes Chatterbox `provider_voice` and
requires new provenance and run evidence.

Promotion re-hashes the complete source-manifest inventory and rejects extra
non-cache files. Hugging Face's internal `.cache/` metadata is tolerated because
it is not executable model content, but links and non-regular entries are still
rejected there. All other indirectly bound dependency, model-evidence, and
executed-code files must be regular, single-link files with the exact winning
result digests.

The evidence chronology must be monotonic: audition plan creation, winning
result completion, run-manifest update, the three identical blind-package
creation timestamps, score time, verdict time, promotion time, and validation
time. Re-signing a verdict cannot make a later artifact appear earlier in the
review chain.

## Consumer obligations

After receiving a handoff, the ChloeSpeech consumer still must:

1. validate the exported voice manifest through the public ChloeSpeech contract;
2. reauthorize current consent, purpose, source text, privacy, and publication;
3. use the same explicit asset root when resolving evidence and reference paths;
4. include the emitted disclosure cue in the actual cue sheet;
5. configure the isolated, immutable provider runtime separately;
6. retain the persistent disclosure and watermark policy; and
7. publish or update a catalog only after a successful, verified render.

Promotion is one-way evidence. It is never permission to weaken runtime policy,
send human audio to a hosted system, mutate a consumer, or delete the lab record.
