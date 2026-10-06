# References, datasets, and fine-tuning transfer plans

`cole_voice_lab.references` and `cole_voice_lab.datasets` form the derived-data
boundary after capture intake. They never edit accepted raw recordings and have
no network or remote-execution behavior.

For recording-content recommendations, proposed duration budgets, local-model
feasibility caveats, and sources, see [corpus research](CORPUS_RESEARCH.md).
Those notes do not replace the approval and evidence requirements below.

```text
raw/<capture-manifest-sha256>/             immutable accepted source
provenance/consents/*.json                 explicit purpose-bound consent
provenance/headphone-reviews/*.json        one human pass per exact utterance
provenance/pilot-gates/*.json              separate full-corpus gate
provenance/transfer-approvals/*.json       optional hosted-transfer authority
                │
                ├── references/<reference-id>/
                │     ├── reference.wav   byte-for-byte source copy
                │     └── manifest.json   complete evidence chain
                │
                ├── derived/datasets/<dataset-id>/manifest.json
                │                         raw paths only; no copied audio
                │
                └── derived/transfer-plans/<plan-id>/manifest.json
                                          plan only; never executes a transfer
```

All manifests use strict, versioned Pydantic contracts, canonical JSON, SHA-256
bindings, asset-root-relative POSIX paths, and fail-closed extra-field handling.

## Human headphone review evidence

Neither source QC nor an agent may stand in for human listening. Before an
utterance can become a reference or dataset entry, create a canonical
`HeadphoneReviewEvidence` file below `provenance/headphone-reviews/`. It names:

- the exact accepted session, prompt, and raw audio SHA-256;
- the reviewer and timezone-aware review time;
- `headphones_used = true`;
- `transcript_matches = true`; and
- `audio_quality_passed = true`.

These three booleans are literal contract requirements, not defaults that a
workflow may silently weaken. The review must postdate intake-recomputed QC. The publisher
re-hashes and canonical-parses the evidence on every use; links, hard links,
path traversal, noncanonical JSON, and digest mismatches fail closed.

## Publishing a zero-shot reference

Call `publish_reference` with:

- a stable reference ID;
- an accepted raw capture-manifest SHA-256 and exact prompt ID;
- the asset-relative path and SHA-256 of approved consent;
- the asset-relative path and SHA-256 of the headphone review; and
- explicit creator and provenance text.

The consent must be approved, effective, unexpired, unrevoked, match the capture
subject, and allow `private-zero-shot-evaluation`. The source session is
independently revalidated through intake, including its `READY`, manifest,
mini-generated `intake-qc.json`,
audio format, byte count, and every file digest. The selected entry must be an
utterance with a passing per-file metric in a capture whose overall QC status is
`pass`.

Publication copies the exact source bytes into a private staging directory,
re-hashes and re-inspects the staged 48 kHz mono 24-bit PCM WAV, writes its
canonical manifest, fsyncs, and atomically renames the directory. The copied WAV
and manifest are mode `0400`; the published directory is mode `0500`. No trim,
resample, normalization, denoising, or other processing is allowed—the source
and output hash, byte count, and `AudioSpec` must be identical.

The manifest binds the prompt ID, exact transcript, subject, session, prompt
suite, content-addressed `raw/<manifest-sha256>` path, source QC report, consent,
headphone review, copied output, purpose, author, timestamp, and provenance
statement. Repeating the identical publication is idempotent after complete
revalidation. Reusing a reference ID for different evidence fails as a
collision.

Use `validate_reference_publication` before every experiment. It rechecks the
entire chain rather than trusting the previously written manifest.

## Building a no-copy dataset

`build_dataset_manifest` accepts explicit `DatasetSelection` records and the
exact `PromptSuite` objects used for capture. A selection contains only the raw
manifest digest, prompt ID, target split, and headphone-review binding. The
builder obtains the transcript and category from the bound prompt suite and
requires them to match accepted capture evidence byte-for-byte.

Every resulting dataset has three nonempty splits:

1. `train`
2. `validation`
3. `permanent-holdout`

The contract rejects:

- duplicate accepted utterances;
- session IDs appearing in more than one split;
- prompt IDs appearing in more than one split;
- exact transcripts appearing in more than one split;
- held-out prompts in train or validation;
- non-held-out prompts in permanent holdout;
- mixed voice subjects;
- missing, stale, mismatched, or failed headphone review;
- a source capture or per-file QC result that is not `pass`; and
- incorrect or incomplete per-category accounting.

The permanent holdout gate records both utterance count and exact duration. Its
default minimum is **600 seconds (10 minutes)**, matching the full-corpus
runbook. A deliberately smaller positive threshold can be recorded for a pilot
manifest, but it is visible in the immutable contract and must not be treated as
the production gate.

Fine-tuning consent is separately revalidated for
`private-fine-tuning`. `publish_dataset_manifest` atomically writes only:

```text
derived/datasets/<dataset-id>/manifest.json
```

Every audio entry continues to point into the content-addressed raw store.
Publishing or validating a dataset never creates audio beneath `derived/`.

### Dataset CLI input and publication

`voice-lab dataset build` consumes a bounded, versioned selection file. The
top-level object and every selection reject unknown fields; the CLI opens it
without following the final symlink and refuses files larger than 16 MiB.

```json
{
  "schema_version": "cole-voice-lab.dataset-selection/v1",
  "selections": [
    {
      "raw_manifest_sha256": "<64 lowercase hex characters>",
      "prompt_id": "neutral-01",
      "split": "train",
      "headphone_review_path": "provenance/headphone-reviews/review-01.json",
      "headphone_review_sha256": "<64 lowercase hex characters>"
    }
  ]
}
```

Every capture suite used by the selected sources must be supplied explicitly as
`SUITE_ID=PATH`; the declared key must equal the strict suite contract's
embedded ID. A build revalidates all raw, QC, transcript, review, consent, split,
and holdout evidence and then atomically publishes the resulting manifest.

```sh
voice-lab dataset build \
  --asset-root /private/voice-assets \
  --dataset-id cole-pilot-dataset-v1 \
  --corpus-stage pilot \
  --selection-file /private/operator-input/pilot-selection.json \
  --prompt-suite cole-pilot-v1=/private/operator-input/cole-pilot-v1.json \
  --prompt-suite cole-held-out-v1=/private/operator-input/cole-held-out-v1.json \
  --consent-path provenance/consents/cole-self-v1.json \
  --consent-sha256 '<approved-consent-sha256>'

voice-lab dataset validate \
  --asset-root /private/voice-assets \
  --dataset-id cole-pilot-dataset-v1
```

## Pilot gate and full-corpus boundary

This layer does not generate prompts or initiate capture. A `pilot` dataset may
be assembled for analysis without authorizing more recording. A `full-corpus`
dataset requires a separate canonical `PilotGateApproval` below
`provenance/pilot-gates/`. It must explicitly attest that pilot headphone review
is complete and full-corpus capture is allowed.

Do not generate a full-corpus prompt suite, schedule the long capture, or treat a
pilot manifest as production training data before Cole has supplied this gate.
The intended order remains:

1. five-minute capture pilot;
2. source QC plus human headphone review;
3. zero-shot candidate bake-off;
4. explicit pilot-gate decision; then
5. conditional full-corpus capture and dataset assembly.

The CLI authors pilot-gate evidence only when all three affirmative attestations
are present. It revalidates every named accepted capture and its passing QC
before writing the canonical, read-only record.

```sh
voice-lab dataset pilot-gate \
  --asset-root /private/voice-assets \
  --gate-id cole-pilot-approved-v1 \
  --pilot-manifest-sha256 '<accepted-pilot-manifest-sha256>' \
  --statement 'Pilot listening review is complete; full-corpus work is approved.' \
  --approve \
  --headphone-review-completed \
  --allow-full-corpus
```

A later `--corpus-stage full-corpus` build must supply both the emitted
`--pilot-gate-path` and `--pilot-gate-sha256`. The dataset layer revalidates that
gate rather than inferring approval from the requested stage.

## Fine-tuning transfer plans

`build_transfer_plan` enumerates the exact published dataset manifest and every
unique raw WAV needed by it. Each payload member records its asset-relative
path, byte count, media type, and SHA-256. `payload_set_sha256` is a canonical
digest over the ordered payload bindings.

The default plan is:

```json
{
  "hosted_transfer_status": "prohibited",
  "destination": null,
  "approval": null,
  "execution_performed": false
}
```

There is intentionally no transfer executor in this module. Publishing a plan
only writes canonical evidence below `derived/transfer-plans/`.

A hosted plan can become `approved-not-executed` only when all three are
supplied together:

1. an exact destination string;
2. a separate canonical `HostedTransferApproval` below
   `provenance/transfer-approvals/`; and
3. the expected SHA-256 of that approval file.

The approval must name the same destination, exact dataset-manifest SHA-256,
exact payload-set SHA-256, purpose `private-fine-tuning`, approver, approval
time, and future expiry. Missing, mismatched, not-yet-effective, expired,
noncanonical, linked, or tampered evidence fails closed. Even an approved plan
still has `execution_performed = false`; a later transfer tool must obtain
separate authority, revalidate all payload bytes and approval expiry, define
transport encryption and remote retention, and record its own receipt.

No hosted endpoint, SSH host, cloud account, or GPU provider is contacted by
these modules or their tests.

The encrypted library vault is a separate storage boundary (D-020), governed by
`VaultDestinationApproval` under `provenance/vault/destinations/<d>/approvals/`, never by a
`HostedTransferApproval`. It needs no synthesis consent because Cole's host
receives only ciphertext and runs no model. `hosted_transfer_allowed` remains
`Literal[False]`. The vault copies existing transfer-approval records as library
history; that does not authorize a dataset transfer to a hosted model service.
See [LIBRARY_VAULT.md](LIBRARY_VAULT.md).

The CLI preserves the same boundary. Its default command publishes a prohibited
plan and never transfers anything:

```sh
voice-lab dataset transfer-plan \
  --asset-root /private/voice-assets \
  --plan-id cole-pilot-transfer-v1 \
  --dataset-id cole-pilot-dataset-v1
```

It may consume—but cannot author—an existing, separately reviewed hosted
approval by providing the complete destination-bound triple:

```sh
voice-lab dataset transfer-plan \
  --asset-root /private/voice-assets \
  --plan-id cole-pilot-hosted-plan-v1 \
  --dataset-id cole-pilot-dataset-v1 \
  --hosted-destination 'approved-provider:private-project' \
  --hosted-approval-path provenance/transfer-approvals/cole-pilot-hosted.json \
  --hosted-approval-sha256 '<hosted-approval-sha256>'
```

There is no dataset transfer execution flag and no hosted-approval-authoring
subcommand. Both prohibited and approved plans record
`execution_performed = false`; publication only writes local canonical evidence.
