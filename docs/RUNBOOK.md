# Pilot-to-handoff runbook

## 0. Core verification

```bash
cd /Users/cole/Projects/cole-voice-lab
scripts/verify
.venv/bin/voice-lab doctor
```

The mini must retain at least 10 GiB of free space. Candidate setup uses a
stricter per-runtime estimate and never downloads models implicitly.

## 1. Mini private root

```bash
.venv/bin/voice-lab assets init \
  --asset-root /Users/cole/.local/share/cole-voice-lab
```

This installs a **pending**, non-authorizing consent record. Cole completes the
explicit approval command personally before voice cloning; an agent must not
infer or supply the attestation flag on his behalf.

Consent is least-privilege and checksum-rotated. First preview the exact scope:

```bash
.venv/bin/voice-lab consent preview \
  --purpose private-zero-shot-evaluation \
  --execution-boundary capture-host \
  --execution-boundary tailnet-local-host \
  --expires-at 2027-08-17T00:00:00Z
```

Then Cole—not an agent—runs `consent approve-self` with those same scope flags,
the preview's `scope_sha256`, the pending record's SHA-256 from `assets init`,
and `--i-am-the-subject-and-authorize`. Fine-tuning and ChloeSpeech rendering
use later, separate previews/approvals; zero-shot approval does not imply them.
Each consumer validates its actual boundary: any capture-side consumer must
request `capture-host`, while reference publication, datasets, and offline
synthesis on the mini request `tailnet-local-host`. A record lacking the
requested boundary fails closed even when its subject, purpose, digest, and
dates are otherwise valid.

For the zero-shot scope shown above, the personal approval command is:

```bash
.venv/bin/voice-lab consent approve-self \
  --asset-root /Users/cole/.local/share/cole-voice-lab \
  --purpose private-zero-shot-evaluation \
  --execution-boundary capture-host \
  --execution-boundary tailnet-local-host \
  --expires-at 2027-08-17T00:00:00Z \
  --expected-current-sha256 <pending-consent-sha256> \
  --reviewed-scope-sha256 <previewed-scope-sha256> \
  --i-am-the-subject-and-authorize
```

## 2. Capture-kit handoff

Prefer an offline rebuild from an independently reviewed wheelhouse:

```bash
scripts/build-capture-kit \
  --wheelhouse /absolute/reviewed/wheelhouse \
  --replace
```

If no reviewed wheelhouse is available, make the refresh deliberate; network
access remains limited to the hash-pinned build/runtime wheels:

```bash
scripts/build-capture-kit --allow-network --replace
```

The builder requires a clean committed tree and builds from a fresh archive of
that exact `HEAD`; commit or remove any intended source changes before running
it. Record the Git commit alongside the printed bundle digests.

Preserve the printed `BUNDLE_MANIFEST_SHA256`, `APPLICATION_WHEEL`, and
`APPLICATION_WHEEL_SHA256` outside the bundle. On the receiving MacBook Air,
do not execute any transferred bundle file yet. Authenticate the manifest and
all of its entries with trusted macOS tools first:

```bash
CAPTURE_KIT=/absolute/received/capture-kit
APPLICATION_WHEEL='<printed APPLICATION_WHEEL>'
APPLICATION_WHEEL_SHA256='<printed APPLICATION_WHEEL_SHA256>'
BUNDLE_MANIFEST_SHA256='<independently retained BUNDLE_MANIFEST_SHA256>'

/usr/bin/printf '%s  %s\n' \
  "$BUNDLE_MANIFEST_SHA256" "$CAPTURE_KIT/SHA256SUMS" \
  | /usr/bin/shasum -a 256 -c -

(cd "$CAPTURE_KIT" && /usr/bin/shasum -a 256 -c SHA256SUMS)
```

Both commands must report `OK`; otherwise quarantine the transfer. Only after
that independent system-tool verification may the authenticated bootstrap run
its deeper bundle checks before installation:

```bash
"$CAPTURE_KIT/macbook-bootstrap" --verify-bundle \
  "$CAPTURE_KIT/$APPLICATION_WHEEL" \
  "$APPLICATION_WHEEL_SHA256" \
  "$BUNDLE_MANIFEST_SHA256"
```

Install into a fresh sibling root; do not reuse or overwrite the earlier
capture host:

```bash
CAPTURE_ROOT=/Users/cole/.local/share/cole-voice-lab/capture-host-console-01

/usr/bin/install -d -m 700 /Users/cole/.local/share/cole-voice-lab
test ! -e "$CAPTURE_ROOT" && test ! -L "$CAPTURE_ROOT"

"$CAPTURE_KIT/macbook-bootstrap" \
  "$CAPTURE_ROOT" \
  "$CAPTURE_KIT/$APPLICATION_WHEEL" \
  "$APPLICATION_WHEEL_SHA256" \
  "$BUNDLE_MANIFEST_SHA256"
```

Keep the earlier capture-host root until every session beneath its `assets/tmp`
has been synced and accepted. In a foreground Terminal, run the device probe
from the fresh root so macOS can display microphone permission.

Do not install an always-listening LaunchAgent. The UMIK-1 session is explicitly
started and stopped by Cole.

## 3. Capture-console pilot

- Quiet absorptive room, HVAC/fans/notifications off.
- UMIK-1 about 15–20 cm away and 20–30 degrees off axis, with a pop filter.
- Mono 48 kHz 24-bit PCM WAV.
- No AGC, denoising, voice isolation, echo cancellation, EQ, compression,
  normalization, or calibration baked into the raw signal.
- Start and end room tone; follow exact displayed transcripts.
- Peaks should generally land from -12 to -6 dBFS with no clipping.

Use the freshly bootstrapped root and keep native capture in a foreground
Terminal:

```bash
CAPTURE_ROOT=/Users/cole/.local/share/cole-voice-lab/capture-host-console-01
VOICE_LAB="$CAPTURE_ROOT/runtime/bin/voice-lab"
ASSET_ROOT="$CAPTURE_ROOT/assets"
SESSION_ID=capture-console-acceptance-01
SESSION_DIRECTORY="$ASSET_ROOT/tmp/capture-$SESSION_ID"
UMIK1_DEVICE='<exact UMIK-1 ID or display name>'

"$VOICE_LAB" capture devices --ffmpeg /opt/homebrew/bin/ffmpeg
"$VOICE_LAB" capture console \
  --asset-root "$ASSET_ROOT" \
  --session-id "$SESSION_ID" \
  --suite pilot \
  --device "$UMIK1_DEVICE" \
  --open
```

The server is a per-run controller bound only to `127.0.0.1` on an ephemeral
port. Its URL contains a 256-bit secret in the fragment and is printed exactly
once; do not share it. Every request is protected by exact Host checks, and
every API request also requires the token plus a same-origin browser signal.
The page has no browser recording path: it only tells native FFmpeg when to
start or stop and plays an authenticated, in-memory 16-bit preview of the
canonical 24-bit take.

Use Space to start/stop, P to play, R to retake, and Return to accept. Rejected
takes and interrupted partials are retained as mode-`0600` forensics in the
private sibling rejected directory, never inside the transferable session.
Automatic QC is diagnostic; listen to each take and use the plain-language
findings to decide whether to retake it. Only the Finalize action invokes
`finalize_capture_session` and writes `manifest.json`, `qc.json`, and `READY`.
Review the finished session on headphones before sync. `capture run` remains
the terminal-only alternative.

### Physical acceptance checklist (Cole)

Leave every item unchecked until it has been observed on the MacBook Air:

- [ ] First Start shows the macOS microphone prompt for Terminal.
- [ ] Only the UMIK-1 is selectable; the built-in microphone is rejected.
- [ ] Preview plays in both Safari and Chrome.
- [ ] Ctrl-C during a take leaves a partial and no `READY`.
- [ ] Relaunching with `--resume` continues at the first missing prompt.
- [ ] A full pilot reaches `READY` with per-take and summary QC status shown.
- [ ] `sync plan --live-plan --json` validates the package and the mini intake
      accepts it idempotently.

For the negative device-selection check, copy the built-in microphone's exact
ID or display name from `capture devices`. This command must fail before it
creates a session:

```bash
BUILT_IN_DEVICE='<exact built-in microphone ID or display name>'
BUILT_IN_SESSION_ID=capture-console-built-in-rejection-01

if "$VOICE_LAB" capture console \
  --asset-root "$ASSET_ROOT" \
  --session-id "$BUILT_IN_SESSION_ID" \
  --suite pilot \
  --device "$BUILT_IN_DEVICE"
then
  echo 'ERROR: built-in microphone was accepted' >&2
  exit 1
fi
test ! -e "$ASSET_ROOT/tmp/capture-$BUILT_IN_SESSION_ID"
```

For the Ctrl-C and resume checks, inspect the interrupted session before
relaunching the same command with the explicit resume flag:

```bash
test ! -e "$SESSION_DIRECTORY/READY"
find "$SESSION_DIRECTORY/audio" -maxdepth 1 -type f \
  -name '*.partial.wav' -print

"$VOICE_LAB" capture console \
  --asset-root "$ASSET_ROOT" \
  --session-id "$SESSION_ID" \
  --suite pilot \
  --device "$UMIK1_DEVICE" \
  --resume --open
```

After the full pilot has been finalized, verify `READY` and preview the exact
live sync plan. Do not add `--execute` until its digest and all transfer
evidence have been reviewed under section 4:

```bash
test -f "$SESSION_DIRECTORY/READY"
"$VOICE_LAB" sync plan "$SESSION_DIRECTORY" \
  --identity-file /Users/cole/.ssh/ABSOLUTE_RESTRICTED_INTAKE_KEY \
  --known-hosts-file /Users/cole/.ssh/ABSOLUTE_REVIEWED_KNOWN_HOSTS \
  --live-plan --json
```

After reviewing that plan, execute it on the Air with its exact printed
digest. On the mini, validate and accept the received session. Then repeat the
same approved transfer and the same two mini commands; the second accept must
return the original receipt rather than create a second raw package:

```bash
# MacBook Air
APPROVED_PLAN_SHA256='<reviewed plan_sha256 from the preview>'
"$VOICE_LAB" sync plan "$SESSION_DIRECTORY" \
  --identity-file /Users/cole/.ssh/ABSOLUTE_RESTRICTED_INTAKE_KEY \
  --known-hosts-file /Users/cole/.ssh/ABSOLUTE_REVIEWED_KNOWN_HOSTS \
  --live-plan --execute --allow-network \
  --approved-plan-sha256 "$APPROVED_PLAN_SHA256"
```

```bash
# Mac mini
MINI_VOICE_LAB='<absolute verified mini voice-lab executable>'
MINI_ASSET_ROOT='<absolute mini private asset root>'
SESSION_ID=capture-console-acceptance-01

"$MINI_VOICE_LAB" intake validate \
  "$MINI_ASSET_ROOT/incoming/$SESSION_ID"
"$MINI_VOICE_LAB" intake accept \
  --asset-root "$MINI_ASSET_ROOT" \
  --session-id "$SESSION_ID"
```

```bash
# MacBook Air: repeat the exact approved transfer
"$VOICE_LAB" sync plan "$SESSION_DIRECTORY" \
  --identity-file /Users/cole/.ssh/ABSOLUTE_RESTRICTED_INTAKE_KEY \
  --known-hosts-file /Users/cole/.ssh/ABSOLUTE_REVIEWED_KNOWN_HOSTS \
  --live-plan --execute --allow-network \
  --approved-plan-sha256 "$APPROVED_PLAN_SHA256"
```

```bash
# Mac mini: repeat and compare the printed receipt with the first receipt
SESSION_ID=capture-console-acceptance-01
"$MINI_VOICE_LAB" intake validate \
  "$MINI_ASSET_ROOT/incoming/$SESSION_ID"
"$MINI_VOICE_LAB" intake accept \
  --asset-root "$MINI_ASSET_ROOT" \
  --session-id "$SESSION_ID"
```

## 4. Transfer and intake

`voice-lab sync plan` is dry-run-only unless its execution flag is present. The
plan transfers payload first and `READY` second over the dedicated restricted SSH
lane. It never uses `--delete`, `--remove-source-files`, or `--inplace`.

Before first contact, preview the exact live plan with `--live-plan --json`.
Review its fixed mini target, source, destination, package-snapshot digest and
member evidence, and both argv arrays. A later execution must supply the printed
digest with `--approved-plan-sha256` as well as `--live-plan --execute
--allow-network`; any command, package path, identity, membership, or byte change
invalidates that approval. Execution revalidates the package immediately before
the allowlisted payload pass and again before sending the isolated `READY`
marker; mutation after either approval or pass one fails closed. Public hosts
and unreviewed private/Tailnet peers are rejected.

The mini re-hashes, re-inspects, and atomically accepts the package. Source
`qc.json` is advisory: the mini persists its own `intake-qc.json`, and downstream
reference/pilot gates use only those independently recomputed metrics. Retain the
MacBook copy until an idempotent v2 receipt binding both QC digests has been
verified.

For the first package, keep intake in the foreground after the restricted sync
has delivered `READY`; do not run `intake scan` concurrently:

```bash
voice-lab intake validate \
  <mini-private-root>/incoming/cole-pilot-001
voice-lab intake accept --asset-root <mini-private-root> \
  --session-id cole-pilot-001
```

Retain the accepted manifest digest and the printed intake receipt. An
installed scanner is an alternative execution mode, not an additional step for
the same package.

## 5. Pilot gate

Stop before a full corpus if any of these remain unresolved:

- clipping, dropouts, near-silence, or unstable level;
- audible room flutter/reverberation;
- microphone/seat geometry cannot be repeated;
- transcript mismatch;
- reference candidates contain noise, strong emotion, or unstable pacing.

Room treatment or a directional vocal microphone is a higher-value change than
recording more compromised audio.

## 6. Zero-shot bake-off

1. Complete subject consent as shown in section 1.
2. Review and publish three checksum-bound reference candidates. Use a distinct
   reviewed prompt and IDs for each candidate; copy every printed digest into
   the next command without retyping it:

   ```bash
   voice-lab reference review --asset-root <mini-private-root> \
     --review-id cole-reference-01-review \
     --raw-manifest-sha256 <accepted-manifest-sha256> \
     --prompt-id <reviewed-prompt-id> \
     --headphones-used --transcript-matches --audio-quality-passed

   voice-lab reference publish --asset-root <mini-private-root> \
     --reference-id cole-reference-01 \
     --raw-manifest-sha256 <accepted-manifest-sha256> \
     --prompt-id <reviewed-prompt-id> \
     --consent-path provenance/consents/cole-self-v1.json \
     --consent-sha256 <approved-consent-sha256> \
     --headphone-review-path \
       provenance/headphone-reviews/cole-reference-01-review.json \
     --headphone-review-sha256 <review-sha256> \
     --provenance-statement 'Byte-identical reviewed pilot reference.'

   voice-lab reference validate --asset-root <mini-private-root> \
     --reference-id cole-reference-01
   ```
3. Inspect `voice-lab models list` and run `voice-lab models verify-registry`.
4. Preview one candidate at a time with:

   ```bash
   voice-lab models prepare --asset-root <mini-private-root> \
     --profile smoke <candidate-id>
   ```

   Build the application wheel once and retain its SHA-256. For F5 or Seed,
   first populate a private toolchain directory with the exact
   `packaging==26.0`, `setuptools==80.9.0`, and `wheel==0.46.3` wheels. The
   builder independently checks every byte against the registry before use.
   It is preview/offline by default; `--execute` performs a fresh deterministic
   build, and `--allow-network` permits only the individually hash-bound source
   sdist downloads:

   ```bash
   scripts/build-reviewed-wheelhouse f5-tts-1.1.22-mps \
     --output <private-build-root>/f5-reviewed \
     --sdist-dir <private-build-root>/sdists \
     --build-python /opt/homebrew/bin/python3.12 \
     --toolchain-wheelhouse <private-build-root>/build-tools

   scripts/build-reviewed-wheelhouse f5-tts-1.1.22-mps \
     --output <private-build-root>/f5-reviewed \
     --sdist-dir <private-build-root>/sdists \
     --build-python /opt/homebrew/bin/python3.12 \
     --toolchain-wheelhouse <private-build-root>/build-tools \
     --execute --allow-network
   ```

   Repeat with runtime ID `seed-vc-51383efd-mps`. Retain each printed
   `manifest_sha256`; `--verify-existing` replays the byte/toolchain checks.
   Provision only after reviewing the argv, adding the reviewed lab wheel and
   digest, the runtime wheelhouse for F5/Seed, and both execution flags:

   ```bash
   voice-lab models prepare --asset-root <mini-private-root> \
     --profile smoke <candidate-id> \
     --lab-wheel <cole-voice-lab.whl> --lab-wheel-sha256 <sha256> \
     --reviewed-wheelhouse <reviewed-output>/artifacts \
     --execute --allow-downloads
   ```

   The successful command prints the sealed runtime receipt path and SHA-256;
   retain both. Reusing the shared MLX runtime requires
   `--expected-runtime-receipt-sha256 <sha256>` and rehashes the full tree.
   Do not provision candidates concurrently. All candidates use Python 3.12;
   Seed-VC remains a no-go until a real pinned-checkout MPS smoke validates its
   3.12 compatibility.
   F5/Seed preparation also verifies and removes only the exact lock-declared
   executable setuptools `.pth` preimage before any subsequent runtime import;
   a missing/rebound preimage or any additional executable/external `.pth`
   aborts preparation and removes the incomplete environment.
5. Put one performed source WAV per selected held-out prompt beneath the private
   asset root. Seed-VC consumes these checksum-bound source performances while
   the same reviewed target reference is used by every candidate.
6. Persist the all-candidate smoke matrix. For example, one held-out prompt,
   one reference, and one seed produces cold/warm cells for all four direct-TTS
   candidates plus one separately labeled Seed-VC cell:

   ```bash
   voice-lab audition plan --asset-root <mini-private-root> \
     --experiment-id zero-shot-smoke-001 \
     --prompt-id eval-short-neutral \
     --reference-id cole-reference-01 \
     --seed 17 \
     --source-audio \
       eval-short-neutral=<mini-private-root>/derived/performed/eval-short-neutral.wav \
     --profile smoke
   ```

7. Preview, then explicitly run **one candidate at a time**. The run path is
   offline and never provisions missing dependencies:

   ```bash
   voice-lab audition run --asset-root <mini-private-root> \
     --experiment-id zero-shot-smoke-001 --candidate qwen3-tts-base

   voice-lab audition run --asset-root <mini-private-root> \
     --experiment-id zero-shot-smoke-001 --candidate qwen3-tts-base \
     --runtime-receipt mlx-audio-0.5.0=<approved-receipt-sha256> --execute
   ```

   Repeat for `chatterbox-turbo`, `chatterbox-v3`, `f5-tts`, and `seed-vc`.
   Use the receipt digest printed and retained by that runtime's successful
   `models prepare`; execution rejects a missing, extra, or rebound runtime-ID
   binding. Rebuilt runtimes require review of their new receipt digest. Status
   and resume validate copied receipt and source-manifest/integrity preimages,
   so evicting a completed trial's caches—or later installing different bytes
   at the same runtime ID—does not invalidate its historical evidence.
   Read the resumable run manifest with:

   ```bash
   voice-lab audition status --asset-root <mini-private-root> \
     --experiment-id zero-shot-smoke-001
   ```
8. If the 10 GiB disk floor prevents the next snapshot from being prepared,
   preview an exact source eviction. Only the digest copied from that preview,
   plus both destructive flags, can delete it:

   ```bash
   voice-lab models evict --asset-root <mini-private-root> <source-id>
   voice-lab models evict --asset-root <mini-private-root> <source-id> \
     --expected-manifest-sha256 <previewed-sha256> --execute --confirm-evict
   ```

   Eviction is irreversible and serialized against audition workers. Retain
   run/model evidence before removing any snapshot.

   Runtime environments may also be rotated without touching model snapshots,
   results, or copied trial receipts. Preview the exact sealed tree first, then
   supply its receipt digest and both destructive flags:

   ```bash
   voice-lab models evict-runtime --asset-root <mini-private-root> <runtime-id>
   voice-lab models evict-runtime --asset-root <mini-private-root> <runtime-id> \
     --expected-receipt-sha256 <previewed-sha256> --execute --confirm-evict
   ```
9. Run the quality matrix with repeated seeds after smoke results are healthy.
   Normalization retains engine-native audio and writes a common 24 kHz,
   PCM-16, -18 LUFS/-1.5 dBTP comparison copy.
10. Compute diagnostic ASR/speaker/format metrics.
11. Build the candidate-blind package only from the exact persisted audition
    plan/run/result digests. Package construction derives every candidate,
    prompt, seed, track, and normalized-audio label from that evidence, requires
    every planned candidate and a balanced failure-free matrix, and rejects
    caller-supplied labels. Serve **only** its `public/` directory on loopback,
    for example `python -m http.server --bind 127.0.0.1 --directory public`.
    The page verifies the reviewer-safe public manifest and every WAV before it
    enables scoring. Score every dimension deliberately on headphones and save
    the public-manifest-bound score JSON.

    ```bash
    voice-lab blind build --asset-root <mini-private-root> \
      --experiment-id zero-shot-smoke-001 --review-id zero-shot-smoke-review-001

    voice-lab blind verify --asset-root <mini-private-root> \
      --review-id zero-shot-smoke-review-001

    cd <mini-private-root>/evaluations/blind-reviews/zero-shot-smoke-review-001/public
    python -m http.server --bind 127.0.0.1 8000
    ```

    Open `http://127.0.0.1:8000/`, complete every score deliberately, and save
    the exported JSON outside the served directory. Stop the loopback server,
    then verify and aggregate that exact score sheet:

    ```bash
    voice-lab blind aggregate --asset-root <mini-private-root> \
      --score-sheet /private/path/to/listening-scores.json
    ```

12. Reveal identities only after score evidence is saved.

The directory named `public/` inside a blind-review package means “safe for the
reviewer without candidate labels.” It is independently self-contained and does
not need access to the sibling `private/reveal.json`. Never serve the package
root: doing so would expose the reveal. `public/` does **not** authorize Internet
sharing.

## 7. Promotion

Promotion v2 requires the canonical sealed blind-package manifest, the exact v2
score sheet, and a promotion listening-summary record that binds both file
digests. Validation reruns blind-package verification and score aggregation,
rejects direct-TTS ties, and requires the promoted candidate to be the sole
highest-overall-mean direct-TTS candidate. The verdict binds one full winning
reveal entry; that entry must occur exactly once and reproduce the exact
plan/run/result/request/trial/candidate/reference chain. The winning-run record
also binds the same consent, model/code revisions, environment evidence,
normalized output, and realized watermark evidence. A handwritten
`blind_review_completed` assertion is never promotion evidence.

The promotion model lock must also bind the exact winning
`WorkerModelEvidence`, every primary/auxiliary immutable source manifest, and
the complete re-hashed artifact inventory. Provider ID, checksum-derived voice,
and provider settings are deterministically projected from that verified trial;
do not type or relabel them after review. Unsupported candidate/provider
projections fail closed. Regenerate earlier v2 draft records that lack these
bindings.

Promotion enforces the full evidence chronology from plan and completed result
through run manifest, blind package, score sheet, verdict, and promotion. All
three blind-package creation timestamps must agree. Directly and indirectly
bound evidence must remain regular, non-symlink, single-link files. Both public
promotion APIs take a detached, fully revalidated snapshot, so mutating nested
Pydantic containers or using unvalidated `model_copy(update=...)` is rejected.

The experiment retains both engine-native and common-normalized audio; the
promotion names exactly one checksum-bound reviewed normalized output.
Export a ChloeSpeech handoff without copying files or changing a consumer. The
eventual render must author an actual spoken synthetic disclosure cue and
persistent disclosure metadata.

An MLX Chatterbox win is not automatically compatible with ChloeSpeech's
official Chatterbox adapter: the reviewed provider projection is `mlx-audio`,
and MLX evidence says `watermarked=false`. Add/review a dedicated MLX provider,
or add an official pinned-runtime reproduction as a new candidate and repeat
the blind review. Promotion v2 cannot substitute an unreviewed post-hoc render;
never relabel MLX output as an official watermarked render.

The CLI deliberately does not author or infer the listening summary, verdict,
winning-run, or `PromotionManifest`. Create and review those strict records from
the saved evidence using the repository schemas
(`schemas/promotion-listening-summary.schema.json`,
`schemas/lab-verdict.schema.json`, `schemas/winning-run.schema.json`, and
`schemas/promotion-manifest.schema.json`). Then validate the completed manifest
and export exactly one handoff:

```bash
voice-lab promotion validate --asset-root <mini-private-root> \
  --manifest <private-path>/promotion.json

install -d -m 0700 <private-export-directory>
voice-lab promotion handoff --asset-root <mini-private-root> \
  --manifest <private-path>/promotion.json \
  --output <private-export-directory>/chloespeech-handoff.json
```

Use `--as-of <aware-timestamp>` only for a deliberately time-pinned replay;
normal operation validates at current UTC. The manifest is read through a
bounded no-follow path. The handoff destination must not exist, its existing
parent directory must be owned by the current user with no group/world mode
bits, and the new canonical JSON is mode `0600`. Retain the printed
`output_sha256` and `promotion_sha256`. This export does not copy audio, choose
a winner, alter provider evidence, or mutate a ChloeSpeech installation.

## 8. Conditional full corpus/fine-tuning

Only proceed if the pilot passes and zero-shot results show a meaningful gap.
Target roughly 90 usable training minutes plus a permanently untouched 10-minute
holdout. Hosted GPU transfer remains separately prohibited until Cole approves a
destination- and payload-specific transfer record.
