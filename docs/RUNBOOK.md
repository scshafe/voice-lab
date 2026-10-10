# Pilot-to-handoff runbook

## 0. Core verification

```bash
cd ~/Projects/voice-lab  # your checkout (the Air's is ~/Projects/cole-voice-lab)
scripts/verify
.venv/bin/voice-lab doctor
```

The mini must retain at least 10 GiB of free space. Candidate setup uses a
stricter per-runtime estimate and never downloads models implicitly.

## 1. Mini private root

```bash
.venv/bin/voice-lab assets init \
  --asset-root $HOME/.local/share/voice-lab
```

This installs a **pending**, non-authorizing consent record. Cole completes the
explicit approval command personally before voice cloning; an agent must not
infer or supply the attestation flag on Cole's behalf.

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
  --asset-root $HOME/.local/share/voice-lab \
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
CAPTURE_ROOT=$HOME/.local/share/voice-lab/capture-host-console-01

/usr/bin/install -d -m 700 $HOME/.local/share/voice-lab
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

## 3. Recording pilot

The primary interface is now the standalone Python desktop recording library.
Follow [DESKTOP.md](DESKTOP.md) for setup, the macOS application launcher, the
complete prompt table, recording dates, and retake history. Use
`voice-lab desktop --asset-root <private-assets> --device UMIK-1` from the
environment with desktop dependencies installed. The older authenticated
browser-console workflow below remains available as an explicit alternative.

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
CAPTURE_ROOT=$HOME/.local/share/voice-lab/capture-host-console-01
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
  --identity-file $HOME/.ssh/ABSOLUTE_RESTRICTED_INTAKE_KEY \
  --known-hosts-file $HOME/.ssh/ABSOLUTE_REVIEWED_KNOWN_HOSTS \
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
  --identity-file $HOME/.ssh/ABSOLUTE_RESTRICTED_INTAKE_KEY \
  --known-hosts-file $HOME/.ssh/ABSOLUTE_REVIEWED_KNOWN_HOSTS \
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
  --identity-file $HOME/.ssh/ABSOLUTE_RESTRICTED_INTAKE_KEY \
  --known-hosts-file $HOME/.ssh/ABSOLUTE_REVIEWED_KNOWN_HOSTS \
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

## 9. Library vault

D-020 permits an encrypted library copy on Cole's own approved tailnet host;
D-023 reverses it, so the PC holds the working library and copies it to vaults on
Cole's other machines, one destination each (D-034). This is separate from hosted GPU
transfer and does not authorize synthesis on the vault. The implementation and
templates are built; **nothing is deployed and no real library is bound yet**.
See [LIBRARY_VAULT.md](LIBRARY_VAULT.md) for the data model and
[the host runbook](../deploy/vault/README.md) for container provisioning, policy,
sidecar join, hashes-only server configuration, access-log rotation and removal.

Before Step 15, decide with Cole whether to do the pending combined app build
and install first. A new build is needed for the sidebar status and the frozen
encryption dependency closure; the installed app is not restarted by verification.
Cole approves every remote or tailnet action, including SSH, policy changes,
image transfer, container starts and the first upload. The host data path,
training opt-in, sidecar image pin, direct versus relayed connectivity, and any
real run archive/removal remain deployment choices. No command below has been
run against Cole's real library as part of the synthetic verification.

### The PC (phase 2): the laptop destination

From phase 2 the PC holds the working library and copies it, sealed, to the laptop's vault
(D-023); the Air follows in phase 3 with the same steps and `--destination air`. Every
destination has its own binding, approval, state, ledger and token pair (D-034); the
library key is one per library. On the PC the key and tokens are encrypted systemd
credentials, not Keychain items ([LIBRARY_VAULT.md](LIBRARY_VAULT.md#the-pc-destinations-systemd-credentials-and-the-copier-unit)).
The subsections after this one are D-020's procedure for a Mac with one vault (destination
`default`); their review points (the approval ceremony, the offline anchor, what a restore
does not bring back) hold here too.

Cole runs these in a private terminal on the PC that no agent or log is capturing: the
recovery sheet holds the key. Nothing below prints a token. Prerequisites: the release at
`/opt/voice-lab/current`, the lab at `/data/voice-lab/lab` (`voice-lab assets init`
done), the laptop vault stack joined as its own tailnet node (`deploy/vault/README.md`), and
its endpoint, StableID and namespace reviewed with Cole.

```bash
VL=/opt/voice-lab/current/bin/voice-lab
LAB=/data/voice-lab/lab
STAGE=/run/voice-lab-credstage    # a tmpfs: decrypted items never reach a disk
VAULT_NAMESPACE='<reviewed namespace on the laptop vault>'
# voice-lab as voice-lab (cd / so its cwd is one it can enter):
vl() { (cd / && sudo -u voice-lab "$VL" "$@"); }
# ... with the staging directory as its keychain, to create items once:
vl_stage() { (cd / && sudo -u voice-lab env CREDENTIALS_DIRECTORY="$STAGE" "$VL" "$@"); }
# ... with the encrypted credentials loaded, exactly as the copier unit gets them:
vl_creds() {
  sudo systemd-run --pipe --wait --collect --quiet \
    -p User=voice-lab -p Group=voice-lab -p UMask=0077 \
    -p 'ImportCredential=private.cole.voice-lab.vault.*' \
    -p 'ImportCredential=private.cole.voice-lab.vault-destination.laptop.*' \
    "$VL" "$@"
}
```

**(a) The library identity, the key with its recovery sheet, and the laptop's tokens**, as
`voice-lab`, into a private staging directory:

```bash
sudo install -d -o voice-lab -g voice-lab -m 0700 "$STAGE"
vl vault init --asset-root "$LAB"            # once; keep the printed library_id with the sheet
vl_stage vault key create --asset-root "$LAB" --sheet-out "$STAGE/recovery-sheet.txt"
vl_stage vault key show --asset-root "$LAB"  # the fingerprint, to compare with the sheet
vl_stage vault token create --asset-root "$LAB" --destination laptop \
  --namespace "$VAULT_NAMESPACE"             # prints the two SHA-256 hashes and item names
sudo cat "$STAGE/recovery-sheet.txt"         # into the offline store; one copy away from home
```

Put the two printed hashes (never a token) into the laptop vault's server configuration
(`deploy/vault/README.md`). Pause until Cole confirms the recovery sheet is stored offline,
with one copy kept away from the house (D-023): it is the only way back to the key.

**(b) Encrypt each item as a root-owned credential, then remove the staging directory:**

```bash
sudo install -d -m 0700 /etc/credstore.encrypted
for item in $(sudo ls "$STAGE" | grep '^private\.cole\.voice-lab\.vault'); do
  sudo systemd-creds encrypt --name="$item" "$STAGE/$item" "/etc/credstore.encrypted/$item"
done
sudo ls -l /etc/credstore.encrypted          # three items: the key and the laptop's two tokens
sudo sh -c 'shred -u "$1"/* && rmdir "$1"' sh "$STAGE"
```

`systemd-creds encrypt` binds each item to the PC's host key (and its TPM2 when there is
one), so the files are useless anywhere else. If the PC is rebuilt, the key comes back from
the recovery sheet (`vault key import` into a fresh staging directory, then the same
encryption) and the tokens are created again with new hashes on the vault.

**(c) Approve and bind the laptop destination** (no credential is read). Fill the reviewed
values as in "Preview, approve and bind the destination" below:

```bash
VAULT_ENDPOINT='<laptop vault node>.<tailnet-suffix>'
VAULT_NODE_ID='<reviewed StableID>'
VAULT_SUFFIX='<reviewed MagicDNS suffix>'
VAULT_APPROVAL_ID='laptop-<date>'
VAULT_EXPIRES_AT='<timezone-aware expiry, at most 366 days on>'
VAULT_STATEMENT='I approve encrypted storage of this library and the listed classes on the laptop vault.'
VAULT_CLASSES=(records prompt-database recordings derived-edits)
approval=(--destination laptop --approval-id "$VAULT_APPROVAL_ID" --endpoint "$VAULT_ENDPOINT"
  --node-stable-id "$VAULT_NODE_ID" --tailnet-suffix "$VAULT_SUFFIX"
  --namespace "$VAULT_NAMESPACE" --classes "${VAULT_CLASSES[@]}"
  --expires-at "$VAULT_EXPIRES_AT" --approved-by Cole --statement "$VAULT_STATEMENT")

vl vault approve --asset-root "$LAB" --preview "${approval[@]}"
VAULT_APPROVED_AT='<exact approval.approved_at from the preview>'
VAULT_APPROVAL_SHA256='<exact approval_sha256 from the preview>'
vl vault approve --asset-root "$LAB" "${approval[@]}" --approved-at "$VAULT_APPROVED_AT" \
  --reviewed-approval-sha256 "$VAULT_APPROVAL_SHA256" --i-approve-this-destination
vl vault bind --asset-root "$LAB" --destination laptop --backend tailnet-vault \
  --endpoint "$VAULT_ENDPOINT" --namespace "$VAULT_NAMESPACE" --classes "${VAULT_CLASSES[@]}" \
  --approval-id "$VAULT_APPROVAL_ID" --approval-sha256 "$VAULT_APPROVAL_SHA256"
```

**(d) The first copy, verify and the restore drill, by hand** (Cole approves the first
upload). Keep the push's `generation` and `manifest_sha256` offline with the sheet:

```bash
vl_creds vault show --asset-root "$LAB" --destination laptop --json   # key and tokens present
vl_creds vault push --asset-root "$LAB" --destination laptop --dry-run
vl_creds vault push --asset-root "$LAB" --destination laptop
vl_creds vault verify --asset-root "$LAB" --destination laptop
sudo install -d -o voice-lab -g voice-lab -m 0700 /data/voice-lab/drill
vl_creds vault drill --asset-root "$LAB" --destination laptop \
  --target "/data/voice-lab/drill/$(date +%F)" \
  --generation '<generation from the push>' --manifest-sha256 '<manifest_sha256 from the push>'
```

The drill restores the generation into the empty target, then reads every file back and
re-hashes it against the manifest; `"ok": true` with `rehashed_files` equal to
`restored_files` is a passed drill (phase 2's acceptance: it covers every file of the pilot
session). The target is a plaintext copy of the library: once the result is recorded, remove
it (`sudo rm -rf /data/voice-lab/drill/<date>`).

**Then the timer.** Install `deploy/host-manual/voice-lab-vault-laptop.service` and `.timer`
by hand (`deploy/host-manual/README.md`: the runner lane's validator refuses
`ImportCredential=`, so the lane does not install them); then `sudo systemctl enable --now
voice-lab-vault-laptop.timer` copies nightly (02:30 plus up to 30 minutes, caught up after a
night the PC was off); `sudo systemctl start voice-lab-vault-laptop.service` runs one copy
now, and `journalctl -u voice-lab-vault-laptop.service` shows the push's and the verify's
JSON. A failed push or verify fails the unit. Stopping the copies is `systemctl disable --now
voice-lab-vault-laptop.timer`; nothing on the PC or the vault is removed by it.

### The Air (phase 3): the offsite destination

The Air's server, node and install are in
[deploy/vault/macos/README.md](../deploy/vault/macos/README.md); on the PC its steps are the
laptop's above with `--destination air`: its own tokens (`vault token create --destination
air`), its approval naming `--tag tag:voice-lab-vault-air` and binding with the `memos`
class, a first push by hand at home (`--memos-root /data/voice-lab/memos/phone`; `vl_creds`
importing `vault-destination.air.*`), the verify and a drill with `--memos-target`, then
`deploy/host-manual/voice-lab-vault-air.{service,timer}`. `vault staleness` reports every
destination's age and flags the Air after 14 days ([LIBRARY_VAULT.md](LIBRARY_VAULT.md#the-air-resume-cadence-and-staleness-phase-3)).

### Prepare the Mac and protect the recovery material

Use the reviewed application environment with the exact vault closure from
`requirements/vault-macos.txt`. If it is missing, the reviewed bootstrap is
`scripts/bootstrap-desktop --with-vault`; its execution/download flags are a
separate approved action. An already downloaded wheelhouse can install the
lock offline with `--no-index --find-links <wheelhouse> --require-hashes
--no-deps --only-binary=:all:`. The client uses the actual Tailscale binary at
`/Applications/Tailscale.app/Contents/MacOS/Tailscale`, not the shell wrapper.

The examples below use `voice-lab` from that environment and Bash variables
containing reviewed, non-secret values. Set `LAB` to the existing private Mac
asset root. Initialize its identity once; retain the printed `library_id` with
the recovery information:

```bash
LAB='<existing private Mac asset root>'
VAULT_NAMESPACE='<reviewed namespace>'
voice-lab vault init --asset-root "$LAB"
```

Cole, in a private terminal that no agent or log is capturing, creates the key
and stores its recovery sheet before proceeding:

```bash
voice-lab vault key create --asset-root "$LAB" \
  --sheet-out '<new private recovery-sheet path outside repository and library>'
voice-lab vault key show --asset-root "$LAB"
```

The sheet contains the key. Keep it offline, never in the repository, library,
shell arguments, chat or logs. `key show` prints only the fingerprint; compare
it with the sheet. Pause until Cole confirms the sheet is safely stored. Key
creation refuses an existing item. If sheet writing fails after Keychain
creation, follow the command's explicit recovery instruction before uploading;
there is no command to regenerate a sheet from the stored key. Losing both
the key and sheet makes the vault unreadable.

Cole also creates distinct read/write tokens privately, using the namespace
selected for the server:

```bash
voice-lab vault token create --asset-root "$LAB" --namespace "$VAULT_NAMESPACE"
```

Only hashes are printed; the raw tokens stay in the login Keychain. Put only
those hashes in the host configuration described in the deployment runbook.
The library recovery sheet does not contain the tokens. A new Mac needs a
surviving read token transferred privately, or a separately approved server
token replacement. Never run a raw-token creation/export helper through an agent.

### Preview, approve and bind the destination

After the dedicated host node is joined and its identity checked with Cole,
record its exact endpoint FQDN, StableID, MagicDNS suffix, namespace and
tag (`tag:voice-lab-vault`). The endpoint must be the dedicated sidecar, not
the Ubuntu host's unrelated Serve endpoint. Apply the restricted access policy
first; identity fields are not a substitute for that policy.
The endpoint field takes a bare lowercase DNS name, without `https://`, a port
or a path. The client supplies HTTPS and verifies TLS for that name automatically.

Choose a fresh approval id and a timezone-aware expiry 12 months from approval
(the contract permits no more than 366 days). Start with the four classes below.
Add `training` to the array only if Cole opts in to datasets and fine-tune runs;
Speak clips are always excluded. The statement records what Cole is approving.
These values are not secrets:

```bash
VAULT_ENDPOINT='<dedicated-node>.<tailnet-suffix>'
VAULT_NODE_ID='<reviewed StableID>'
VAULT_SUFFIX='<reviewed MagicDNS suffix>'
VAULT_NAMESPACE='<reviewed namespace>'
VAULT_APPROVAL_ID='<new approval id>'
VAULT_EXPIRES_AT='<timezone-aware expiry>'
VAULT_STATEMENT='I approve encrypted storage of this library and the listed classes on this destination.'
VAULT_CLASSES=(records prompt-database recordings derived-edits)

voice-lab vault approve --asset-root "$LAB" --preview \
  --approval-id "$VAULT_APPROVAL_ID" --endpoint "$VAULT_ENDPOINT" \
  --node-stable-id "$VAULT_NODE_ID" --tailnet-suffix "$VAULT_SUFFIX" \
  --namespace "$VAULT_NAMESPACE" --classes "${VAULT_CLASSES[@]}" \
  --expires-at "$VAULT_EXPIRES_AT" --approved-by Cole \
  --statement "$VAULT_STATEMENT"
```

Preview writes nothing and does not contact the host. Review every field,
including `library_id`, classes and expiry. Because this preview omitted
`--approved-at`, it generated that timestamp. Copy **both** the printed
`approval.approved_at` and `approval_sha256` into the variables below. Confirmation
must pass the same fields and the exact timestamp. Omitting the timestamp again
would generate a different record and the digest check would refuse it. It is
also valid to supply a chosen `--approved-at` to both preview and confirmation.

```bash
VAULT_APPROVED_AT='<exact approval.approved_at from preview>'
VAULT_APPROVAL_SHA256='<exact approval_sha256 from preview>'

voice-lab vault approve --asset-root "$LAB" \
  --approval-id "$VAULT_APPROVAL_ID" --endpoint "$VAULT_ENDPOINT" \
  --node-stable-id "$VAULT_NODE_ID" --tailnet-suffix "$VAULT_SUFFIX" \
  --namespace "$VAULT_NAMESPACE" --classes "${VAULT_CLASSES[@]}" \
  --approved-at "$VAULT_APPROVED_AT" --expires-at "$VAULT_EXPIRES_AT" \
  --approved-by Cole --statement "$VAULT_STATEMENT" \
  --reviewed-approval-sha256 "$VAULT_APPROVAL_SHA256" --i-approve-this-destination

voice-lab vault bind --asset-root "$LAB" --backend tailnet-vault \
  --endpoint "$VAULT_ENDPOINT" --namespace "$VAULT_NAMESPACE" \
  --classes "${VAULT_CLASSES[@]}" --approval-id "$VAULT_APPROVAL_ID" \
  --approval-sha256 "$VAULT_APPROVAL_SHA256"
voice-lab vault show --asset-root "$LAB" --json
```

The binding pins the immutable approval's digest and names Keychain items,
never secrets. Approval and binding explicitly enable future copying. The
service reads its binding at startup; coordinate its next launch with Cole
after the first-copy gate, since that launch enables background passes. An
expired or mismatched approval refuses new vault opens. Renewal uses a new
approval id and preview/digest confirmation, then `bind --replace`; changing
destinations after copying also requires the deliberate `--reset-state`, which
moves local state and manifest copies aside. That reset is not a routine fix
for a failed upload, especially when archived checkpoints rely on those copies.

### First copy, verify, and restore drill

Cole approves the first tailnet access and upload. A dry run still contacts the
vault to check its head, but uploads and writes no records. A real push seals
bytes locally and writes its ledger entry before the first new upload:

```bash
voice-lab vault push --asset-root "$LAB" --dry-run
voice-lab vault push --asset-root "$LAB"
voice-lab vault status --asset-root "$LAB" --json
voice-lab vault verify --asset-root "$LAB"
```

Retain the successful push's `generation` and `manifest_sha256` offline with
the recovery information. They are an independent rollback anchor, but a new
Mac can use the pair only while the current remote head still names that
generation. Once the head advances, restoring an older generation also needs
that generation's local manifest copy and the intervening manifest copies.
This version does not retrieve that historical chain automatically on a new
Mac. Keeping only the first push's pair therefore does not guarantee later
pinned recovery. Refreshing the retained pair after a verified push helps only
while it still matches the head; the next background generation can make it
historical again. There is no automatic offline-anchor update. Verification
downloads, decrypts and hashes every chunk; a nonzero exit
or waiting/refused paths is not a successful complete copy. If verification
finds a missing blob, investigate before using `push --full` to re-upload the
current generation's files and verifying again. Never remove local files to
work around a verification failure.

Restore into a new or empty, private, symlink-free directory outside the
repository and working library. The existing parent must be private and owned
by the current user. Do not point this command at the working library:

```bash
voice-lab vault restore --asset-root "$LAB" --target "$VAULT_RESTORE_TARGET" \
  --generation "$VAULT_GENERATION" --manifest-sha256 "$VAULT_MANIFEST_SHA256"
```

Set the two anchor variables to the retained push result. On the original Mac,
older selections can use its retained manifest chain. On a new Mac without
those copies, the pinned generation must be the current head or the command
refuses `STORAGE.NOT_FOUND`; the digest alone cannot locate historical ciphertext.
`--accept-latest`
is the alternative to the generation/digest pair, not an additional flag. On
a new Mac with no local state it trusts the host's selected head and cannot
detect a valid older generation; use the pinned pair when available. Optional
`--runtime-root <pinned F5 runtime>` relinks the base-weight pin; the vault never
holds those pretrained bytes. A missing runtime/pin is reported, not downloaded.

Compare `prompts coverage-report` and `finetune corpus` between source and
restored roots, using the same subject and catalog selection (their `--help`
lists the selection flags). Verify selected original audio digests and the
restored prompt database before calling the drill complete. The restore
rebuilds machine-bound indexes when first opened; it never restores
`provenance/vault/**` or Speak clips. Test the server-unreachable/recovery status
with Cole, and confirm recording still works. Archiving the smoke-test run is
a separate decision, not part of the default drill.

### Restore on a new Mac

Use an empty private control root for the restore credentials and a separate
empty target for restored data. Initialize the control root with the **same**
library id from the recovery sheet. Cole imports the key and read token in a
private terminal; inputs stay outside the repository and both roots:

The explicit-endpoint command below supports only the current head; it does not
read local manifest copies. An older offline pair is insufficient after background
copying has advanced the head. Historical selection requires the bound-library
restore route with the needed trusted local manifest chain, a separate recovery
procedure if those records survived. There is no command here to reconstruct them.
If that condition cannot be met, stop and make a separate recovery decision;
do not silently replace the pin with `--accept-latest`, which accepts the host's
head without an independent freshness anchor.

```bash
voice-lab assets init --asset-root "$VAULT_CONTROL_ROOT"
voice-lab vault init --asset-root "$VAULT_CONTROL_ROOT" --library-id "$VAULT_LIBRARY_ID"
voice-lab vault key import --asset-root "$VAULT_CONTROL_ROOT" \
  --sheet '<offline recovery-sheet path>'
voice-lab vault token import --asset-root "$VAULT_CONTROL_ROOT" --scope read \
  < '<private read-token input file>'
voice-lab vault restore --asset-root "$VAULT_CONTROL_ROOT" \
  --target "$VAULT_RESTORE_TARGET" --endpoint "$VAULT_ENDPOINT" \
  --namespace "$VAULT_NAMESPACE" --node-stable-id "$VAULT_NODE_ID" \
  --tailnet-suffix "$VAULT_SUFFIX" --generation "$VAULT_GENERATION" \
  --manifest-sha256 "$VAULT_MANIFEST_SHA256"
```

This explicit endpoint route reads with the read token and checked node identity;
it needs no write approval or binding. Recover the endpoint identity and pinned
generation from trusted retained records, not solely from the server being
restored. If the read token is lost, the host operator must replace its hash
with a newly created credential before a read can succeed; the library key is
still required and cannot be recovered from the host. After checking the
restored library, run `vault init --library-id` on that root. The old Mac's
upload state and ledger were not restored: binding alone cannot resume copying
to the populated namespace, whose head would be refused as a fork. Do not use
`--reset-state` to try to adopt it. Resume only through a separately approved
fresh namespace, new destination approval and binding, while retaining the old
namespace for recovery. Provision the new namespace with a distinct token pair:
coordinate replacing this library's Mac token items as described below, retain the
old read credential privately for recovery, and leave the old namespace's hashes
unchanged. The encryption key and library id stay the same. Trusted original
Mac-only vault records, if separately available, require their own reviewed
recovery procedure; there is no automatic state/ledger recovery helper. The
Keychain items are keyed by
library id; importing the same key again when its item already exists is refused.

### Explicit run archiving and credential maintenance

Only with Cole's decision for the named run and a binding/approval that covers
`training`, archive it while no app training or Speak request is active:

```bash
voice-lab finetune archive --asset-root "$LAB" --run "$VAULT_RUN_ID"
```

This copies the library, downloads/decrypts/checks the entire run, writes its
archive receipt and copies that receipt too. It removes nothing. To reclaim
checkpoint space, Cole explicitly chooses the additional flag for that run:

```bash
voice-lab finetune archive --asset-root "$LAB" --run "$VAULT_RUN_ID" --remove-local-copy
voice-lab finetune restore-run --asset-root "$LAB" --run "$VAULT_RUN_ID" \
  --runtime-root '<pinned F5 runtime>'
```

Removal performs a fresh second download verification of the whole run, then
rehashes the local files and removes only `checkpoints/**` after writing the
removal record. Dataset clips, run metadata and samples remain local. There is
no automatic eviction. Missing checkpoints mark Latest/named models Archived
in Speak; Base still uses its untuned weights and remaining reference clips.
Restoration rechecks the copied bytes and keeps the removal history.

For audit, copy the host's access log privately with a separately approved
transfer and run `voice-lab vault audit --asset-root "$LAB" --access-log <file>`.
It compares the host's answered requests with this Mac's request records;
unexplained accesses fail. Host log rotation must use copy-truncate, never
rename, as the server retains the opened inode.

Token rotation is separate from key recovery. `token create` and `token import`
refuse existing items; there is no automatic overwrite or grace period. Follow
the host runbook's coordinated stop, explicit Keychain token removal, new pair,
hash replacement, container recreation and read/write verification. Never
delete the library encryption-key item while replacing tokens. This plan adds
no second copy and no scheduled deletion; host data removal requires its own
explicit approval.
