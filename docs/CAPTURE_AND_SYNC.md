# Capture, transfer, and intake

This is the operational and security contract for moving Cole's natural voice
from the UMIK-1 on the MacBook Air to the private asset root on the Mac mini.
It does not authorize deployment, unattended recording, hosted processing, or
deletion of the MacBook copy.

## Boundary at a glance

```text
MacBook Air, foreground only
  exact bundled prompt -> one raw WAV per utterance
  room tone            -> one raw WAV per room-tone prompt
  complete set         -> manifest.json -> qc.json -> READY (last)
                                 |
                                 | dry-run plan by default
                                 v
Tailnet SSH + restricted rsync receiver on the primary mini
  payload except READY -> READY alone
                                 |
                                 v
Mac mini incoming -> same-filesystem staging -> validation
  reject: partial/link/traversal/extra/tamper/collision/low disk
  accept: raw/<manifest-sha256>/, all files read-only
```

Raw means raw: mono 48 kHz, 24-bit little-endian PCM in a WAV container. There
is one file per utterance. FFmpeg receives no audio filter, gain, resampler,
normalizer, denoiser, equalizer, compressor, calibration filter, or automatic
gain control option. Any later trimming, resampling, reference extraction, or
normalization belongs under `derived/`, never inside the accepted raw package.

## 1. MacBook Air and UMIK-1 preparation

1. Connect the UMIK-1 directly or through a known-good powered USB connection.
2. Keep the microphone, seat, and pop filter in a repeatable position. A useful
   starting point is 15–20 cm away and 20–30 degrees off axis.
3. In Audio MIDI Setup, select 48,000 Hz. Capture itself forces mono 24-bit PCM.
4. Disable Voice Isolation and other operating-system or conferencing effects.
5. Close apps that might claim the microphone or play notifications.
6. Use a quiet, absorptive room. Turn off avoidable fans and HVAC.

Run device discovery in a visible Terminal once. This is deliberately a
foreground step so macOS can show its microphone-permission ceremony:

```bash
/opt/homebrew/bin/ffmpeg \
  -hide_banner \
  -f avfoundation \
  -list_devices true \
  -i "" 2>&1
```

The selected AVFoundation audio device must identify `UMIK-1`. Do not silently
fall back to the MacBook Air's built-in microphone. Record the UMIK-1 serial in
the `CaptureDevice` evidence when it is available.

## 2. Foreground capture semantics

Both capture drivers run as foreground Terminal processes and use the same
`cole_voice_lab.capture` library boundary:

- `voice-lab capture run` presents each prompt in the terminal and leaves
  FFmpeg in the foreground until Cole presses `q`;
- `voice-lab capture console` serves a token-protected page on
  `127.0.0.1` and lets the browser start, stop, review, retake, and accept each
  take while FFmpeg remains the only recorder; and
- `voice-lab capture console --resume` re-opens a specifically named
  unfinalized session after re-inspecting every completed canonical take.

The browser never captures audio. Both drivers:

- require an initialized, mode-`0700` `AssetLayout`;
- require the UMIK-1;
- present each bundled prompt text verbatim;
- record each prompt to its own file;
- record every prompt whose category is `room-tone` as room tone;
- stop FFmpeg explicitly after each take;
- store the exact prompt text as that utterance's transcript evidence; and
- refuse to overwrite an existing take.

The effective FFmpeg shape is:

```bash
/opt/homebrew/bin/ffmpeg \
  -hide_banner -loglevel warning \
  -f avfoundation -i ':<UMIK-1-device-id>' \
  -map 0:a:0 -vn -sn -dn \
  -ac 1 -ar 48000 -c:a pcm_s24le \
  -f wav -n '<utterance>.partial.wav'
```

There are intentionally no signal-processing flags.

### Interrupt and recovery behavior

`Ctrl-C` is an abort, not an implicit approval. The recorder forwards `SIGINT`
to FFmpeg, waits for it to close the WAV, fsyncs any partial file, and exits with
a `CaptureInterrupted` error. The partial remains for inspection, but:

- it is not renamed to the canonical utterance filename;
- `manifest.json`, `qc.json`, and `READY` are not published; and
- sync and intake reject the session.

Resume only with `voice-lab capture console --resume`. The resume path
re-inspects every completed canonical take and moves each partial to the private
rejected-takes directory beside the session with reason `interrupted`. Rejected
takes are forensic evidence: they are never synced, never deleted
automatically, and never promoted to canonical takes. Do not rename a partial
by hand and do not construct `READY` manually.

## 3. Finalization and portable QC

`finalize_capture_session` first requires the exact expected audio set. A
missing utterance, unexpected file, symlink, hardlink, non-regular file, wrong
format, or truncated WAV prevents finalization.

QC is implemented with the Python standard library—no NumPy and no optional
model runtime. It computes:

- duration;
- peak and RMS level in decibels relative to full scale (dBFS);
- direct-current offset;
- count of samples at either integer clipping limit;
- longest run of exact digital zeroes; and
- utterance signal-to-noise estimate relative to captured room tone.

Quality findings make the source report `review`; structural and integrity
failures stop finalization. A headphone review remains mandatory. Typical voice
peaks should land around -12 to -6 dBFS with zero clipped samples.

Finalization order is fixed:

1. Hash and inspect every WAV through a no-follow file descriptor.
2. Write canonical `manifest.json` with exact path, byte count, SHA-256, format,
   prompt ID, and exact transcript bindings.
3. Write canonical `qc.json`, bound to the exact manifest-file SHA-256.
4. Write canonical `READY`, binding both evidence-file digests, **last**.

The final package is:

```text
<session>/
  audio/
    000-room-tone.wav
    001-<prompt-id>.wav
    ...
  manifest.json
  qc.json
  READY
```

Only a package with a valid final `READY` is transferable.

## 4. Dry-run transfer planning

`cole_voice_lab.sync.build_sync_plan(session_directory)` performs a complete
local package validation and returns two argv arrays. It does not invoke SSH or
rsync. The default plan also includes rsync's `--dry-run` flag. Its immutable
package snapshot binds the validated manifest, source QC, and `READY` digests;
the complete member path, byte count, SHA-256, and filesystem identity set; and
the directory identity set. The JSON preview exposes both that evidence and its
own snapshot digest.

The default target is the Mac mini's primary Tailnet node:

```text
100.123.230.91
```

The single configured alias `cole-voice-lab-mini` is also allowed. Every other
IP address or hostname—including public hosts, other private/Tailnet peers, and
the dedicated OpenClaw gateway node (`100.91.94.21`)—is rejected. The remote
account and landing root are likewise fixed to Cole's private mini intake lane.

Pass one transfers an explicit allowlist of approved payload paths while
excluding `/READY`; its final exclude rule prevents a newly added source file
from being selected. Pass two includes only `/READY`. Both use checksums, a partial directory, delayed updates, private
destination modes, a timeout, and explicit hardened SSH options:

- batch and public-key authentication only;
- one explicit intake identity;
- one explicit known-hosts file;
- strict host-key checking;
- no agent, port, or other forwarding;
- no TTY;
- connection and keepalive bounds.

The planner never emits `--delete`, `--remove-source-files`, or `--inplace`. It
also avoids archive mode so symlinks, devices, ownership, and groups are not
copied. FLAC-style compression is irrelevant to PCM WAV and is not enabled.

Even an rsync `--dry-run` would contact SSH if executed. Therefore
`execute_sync_plan` refuses a dry-run plan. A caller must separately:

1. rebuild with `dry_run=False`;
2. review the complete live plan and record its `approval_sha256()` digest;
3. pass that exact digest as `approved_plan_sha256`; and
4. pass `allow_network=True` to execution.

The v2 digest covers the source path, private destination, SSH identity and
known-hosts paths, dry/live state, validated package snapshot, and every
argument in both rsync passes. Execution reconstructs both commands from those
fields and requires exact full-argument equality; injected prefix filters,
remote commands, or other rsync options are rejected even if a caller computes
a new digest for the modified object. Execution
revalidates the complete package against that snapshot immediately before the
payload pass and again immediately before the isolated `READY` pass; a final
post-transfer audit also detects mutation during the marker pass. Added,
changed, replaced, or newly hard-linked members fail closed. If mutation occurs
during pass one, `READY` is never sent. Execution also revalidates the fixed
destination, member filters, pass order, and nondestructive option policy. The
CLI flow is:

```bash
voice-lab sync plan /absolute/session --live-plan --json

# After reviewing the JSON, rerun the same arguments with the printed digest.
voice-lab sync plan /absolute/session \
  --live-plan --execute --allow-network \
  --approved-plan-sha256 '<reviewed-plan-sha256>'
```

Those are code safety gates, not deployment authorization. Verify the mini host
key out of band and obtain Cole's approval before first contact.

The checks assume the source is quiescent while rsync runs. A hostile process
running as the same user can race after the pre-pass snapshot and before rsync
opens a pathname. The marker-last gate and independent mini intake validation
prevent such a raced payload from being accepted, but bytes may reach the
private incoming staging directory before the mutation is detected. Do not run
live sync while another same-UID process can write the capture package; use an
immutable staging/snapshot mechanism if that actor is in the threat model.

Sync-plan v1 approval digests are intentionally incompatible. Rebuild and
review a v2 plan; no earlier digest authorizes a transfer. Also rebuild any v2
plan created before identity/known-hosts fields and exact full-command
validation were added: its digest is stale even though the schema label remains
v2.

## 5. Restricted SSH receiver

Use a dedicated intake key, never Cole's broad administrative SSH identity. The
mini-side authorized-key policy should have this shape:

```text
from="<macbook-air-tailnet-ip>",restrict,command="<pinned-write-only-rsync-gate>" ssh-ed25519 ...
```

The forced command must confine writes to:

```text
/Users/cole/.local/share/cole-voice-lab/incoming
```

Use a pinned, audited `rrsync`-style receiver or an equivalently tested closed
protocol boundary. Do not `eval` `SSH_ORIGINAL_COMMAND`, permit a general shell,
or grant read/delete access through this lane. Tailnet access-control policy
should independently allow only the MacBook Air to the mini's TCP port 22.

No receiver deployment was performed as part of this code increment.

## 6. Mini intake

The uploader targets `incoming/<session-id>/`. Intake only accepts direct
children of the private `incoming/` directory and requires at least 10 GiB free
before claiming a package.

`accept_capture_session` then:

1. verifies `incoming/`, `tmp/`, and `raw/` share one filesystem;
2. atomically renames the incoming directory into private staging;
3. rejects symlinks, hardlinks, sockets/devices, traversal, noncanonical JSON,
   missing or extra paths, excessive sizes, digest/byte-count changes, wrong
   WAV format, incomplete QC coverage, and evidence-binding changes;
4. recomputes every WAV hash and metric independently, treating capture-side
   `qc.json` as advisory;
5. copies the validated package to fresh private inodes, rehashes every package
   member and directory identity, and thereby severs pre-existing uploader file
   descriptors before publication;
6. persists `intake-qc.json` and a v2 receipt binding the manifest, source-QC,
   and intake-QC digests;
7. detects any prior package claiming the same session ID;
8. publishes to `raw/<manifest-sha256>/` by same-filesystem rename;
9. makes every raw file mode `0400` and every raw directory mode `0500`; and
10. writes nothing under `derived/`.

An identical manifest returns the original receipt only when the immutable
source QC and READY evidence also match; it then discards only the newly verified
transport duplicate. The acceptance timestamp and raw location remain stable.
The same session ID with a different manifest digest is a hard collision; the
package is restored to `incoming/` for investigation.

Failures before publication restore the claimed package to its incoming path.
No prior accepted package is replaced.

## 7. Retention and verification

- Keep the MacBook package until the exact mini receipt and raw manifest digest
  have been verified and the chosen retention window has elapsed.
- Never use transfer flags that remove source files.
- Treat raw recordings as biometric assets; do not put their paths, content, or
  transcripts in general-purpose logs.
- Do not send human audio to a hosted model or GPU host without a separate,
  destination- and digest-specific approval.
- Run `scripts/verify` after code changes. Tests use tiny synthetic 24-bit PCM
  fixtures and require no microphone, peer, model, or network.
