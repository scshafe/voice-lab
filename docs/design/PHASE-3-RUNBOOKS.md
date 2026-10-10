# Phase 3 runbooks: the moves, the cutover, the rollback

Design [VOICE-PLATFORM.md](VOICE-PLATFORM.md) §4.3, §5.5, §5.6, §9 phase 3. Progress and
results: [PHASE-3-LOG.md](PHASE-3-LOG.md). Everything here is **prepared, not done**: each
step that changes production, reads the real corpus, or needs the Air, the iCloud sync or a
real recording is the owner's. voice-journey keeps running on the laptop, untouched, as the
rollback until the owner says to cut over.

Set once in every shell below:

```sh
TN=<tailnet>.ts.net          # the fleet's tailnet suffix
VL=/opt/voice-lab/current/bin/voice-lab
```

Order (each section says what it needs first):

| # | Runbook | Changes production? | Who |
| --- | --- | --- | --- |
| A | Host preparation on the PC | the PC's layout and packages (no service changes) | owner (hash approval), then any session |
| B | The mirror drop on the PC, fed in parallel | adds a second mirror target; the laptop's is untouched | owner (Mini plist) |
| C | Corpus and data move | freezes the laptop's watcher; copies its manifests | owner (go-ahead, transfer approval) |
| D | Real-corpus parity | no (reads the moved copy read-only) | owner |
| E | Serving journey from the PC | the PC's stack (journey routes and `/view/`) | any session after A–C |
| F | Cutover | the laptop's stack becomes the redirect; the PC's job runner starts | owner (go-ahead) |
| G | Rollback | restores the laptop | owner |
| H | The Air: the mirror sender, then the vault | the Air | owner |
| I | After the cutover (14 and 90 days) | removals | owner |

## A. Host preparation on the PC

Needs: the owner's approval of infra's re-signed host tree (infra PR #2, branch
`voice-lab-phase3`, `platform/M0.sha256` =
`83464eccd610362c48ffd2c9c65796bf85eba2e77d32c5ebd764698da3c700d7`).

1. Stage and check the approved tree exactly as phase 2 did (infra
   `docs/platform/agent-deploy.md`, "Install on the PC"): the staged `M0.sha256` must hash to
   the approved value.
2. Provision the new layout (idempotent; nothing existing changes):

   ```sh
   sudo /root/scshafe-m0/bin/scshafe-voice-lab-provision --dry-run layout
   sudo /root/scshafe-m0/bin/scshafe-voice-lab-provision layout
   stat -c '%U:%G %a %n' /data/voice-lab/journey /data/voice-lab/journey/* /data/voice-lab/memos
   # voice-lab:voice-lab 700 (journey and its four), root:voice-lab 750 (memos)
   ```
3. Merge infra PR #2 and fast-forward the production checkout: `git -C ~/infra pull --ff-only`.
4. Tools for the job runner (whisper.cpp from Arch's repositories; ffmpeg is installed):

   ```sh
   sudo pacman -S --needed whisper-cpp
   command -v whisper-cli && whisper-cli --help | head -1
   ```
5. The whisper model, once, sha256-pinned (the laptop's pin; an explicit download step):

   ```sh
   sudo -u voice-lab sh -c 'cd /data/voice-lab/journey/models &&
     curl -fL --retry 3 -o ggml-medium.bin.part \
       https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin &&
     echo "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208  ggml-medium.bin.part" | sha256sum -c - &&
     mv ggml-medium.bin.part ggml-medium.bin'
   ```
6. The isolated analysis runtime (design §5.3; `requirements/runtimes/journey-analysis.txt`,
   exact pins, not hash-locked yet), as `voice-lab`, with the release's pinned Python:

   ```sh
   sudo -u voice-lab sh -c 'cd /data/voice-lab/journey/runtimes &&
     /opt/voice-lab/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12 -m venv analysis &&
     analysis/bin/python -m pip install -r /home/cole/src/voice-lab/requirements/runtimes/journey-analysis.txt &&
     analysis/bin/python -I /opt/voice-lab/current/lib/python3.12/site-packages/cole_voice_lab/resources/journey_extract_selftest.py'
   ```

   (`/home/cole/src/voice-lab` must be readable by `voice-lab`; otherwise copy the one
   requirements file to `/tmp` first.)

## B. The mirror drop on the PC, fed in parallel (sender: the Mac mini)

Needs: A. The Mini keeps pushing to the laptop throughout; this adds the PC as a second
target with the Mini's existing launcher (its Full Disk Access grant is reused; no rebuild).

1. The receiver, with the Mini's existing mirror key (public):

   ```sh
   ssh mac-mini-tailscale cat .ssh/vj-mirror.pub > /tmp/vj-mirror.pub
   cd ~/infra
   sudo VJ_LAYOUT=pc replication/voice-journey-mirror/vj-mirror-drop install --dry-run --from-host elrics-mac-mini /tmp/vj-mirror.pub
   sudo VJ_LAYOUT=pc replication/voice-journey-mirror/vj-mirror-drop install --from-host elrics-mac-mini /tmp/vj-mirror.pub
   VJ_LAYOUT=pc replication/voice-journey-mirror/vj-mirror-drop check
   ```
2. On the Mini, pin the PC's host key (the push runs with `StrictHostKeyChecking=yes`):
   `ssh-keyscan -t ed25519 cole-arch-pc >> ~/.ssh/known_hosts`, then compare the
   fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the PC.
3. On the Mini, a second launchd job for the PC: a copy of
   `~/Library/LaunchAgents/ai.voice-journey.mirror.plist` named
   `ai.voice-journey.mirror-pc.plist` with `Label` `ai.voice-journey.mirror-pc`, its own log
   path, and two more environment variables (the Node mirror reads them):
   `VOICE_JOURNEY_MIRROR_DEST=vj-mirror@cole-arch-pc:` and
   `VOICE_JOURNEY_MIRROR_STATE=/Users/cole/.local/state/voice-journey-mirror-pc` (its own
   state: the hash cache and lock are per target). Same launcher, same `mirror` argument,
   same key. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.voice-journey.mirror-pc.plist`.
4. Check after one period (15 minutes; the first full mirror is about 1.2 GB):
   `ls /data/voice-lab/memos/phone | wc -l` as root (count only) and
   `sudo -u voice-lab $VL journey index --dry-run` with the journey environment of F.2
   (prints counts and scope only). The drop must hold the same number of recordings as the
   laptop's (`ssh laptop-tailscale 'ls /srv/voice-journey/corpus/phone | wc -l'`).

Rollback: `launchctl bootout gui/$(id -u)/ai.voice-journey.mirror-pc` on the Mini; the
laptop's feed was never touched.

## C. Corpus and data move (design §5.6 steps 1–3)

Needs: B (a full mirror on the PC), the owner's go-ahead. As of 2026-10-09 the laptop has
**no measurement sessions** (`corpus/measurement` is empty), 19 MB of manifests and 90 MB of
artifacts; `models/` (1.5 GB) is not moved (A.5 fetched it), nor is `corpus/phone` (B rebuilt
it).

1. **Freeze** the laptop's watcher: merge scshafe/voice-journey PR #5 (draft,
   `VOICE_JOURNEY_WATCH: "0"`), watch its deploy run, `/healthz` 200 after. The server keeps
   serving; the mirror keeps arriving.
2. **The transfer manifest and its approval** (one transfer approval naming the PC and the
   manifest's digest, AGENTS.md's rule for moves between Cole's hosts):

   ```sh
   ssh laptop-tailscale 'cd /srv/voice-journey && find manifests artifacts -type f -print0 | sort -z | xargs -0 sha256sum' > /tmp/vj-transfer.sha256
   sha256sum /tmp/vj-transfer.sha256        # the digest the owner approves
   wc -l < /tmp/vj-transfer.sha256          # file count
   ```

   The owner records the approval (destination `cole-arch-pc`, the digest, the date) in
   `/data/voice-lab/lab/provenance/transfers/voice-journey-<date>.txt` (root-written, never
   in Git).
3. **Copy** into a private staging directory, then verify every file against the approved
   manifest before anything moves into place:

   ```sh
   sudo install -d -o voice-lab -g voice-lab -m 0700 /data/voice-lab/journey/import
   sudo rsync -a --chown=voice-lab:voice-lab --chmod=D0700,F0600 -e 'ssh -F /home/cole/.ssh/config' \
     laptop-tailscale:/srv/voice-journey/manifests laptop-tailscale:/srv/voice-journey/artifacts \
     /data/voice-lab/journey/import/
   cd /data/voice-lab/journey/import && sudo sha256sum -c --quiet /tmp/vj-transfer.sha256 && echo verified
   ```
4. **Into place:** generated manifests into `journey/manifests/`, artifacts into
   `journey/artifacts/`; the browser's state files (`corpus-browser-state.json`,
   `goals-state.json`, `referee-state.json` from `artifacts/`) into
   `/data/voice-lab/state/journey/` (the API's writable state); then remove `import/`.

   ```sh
   sudo -u voice-lab sh -c 'set -e; J=/data/voice-lab/journey; S=/data/voice-lab/state/journey
     mkdir -p -m 0700 "$S"
     cp -a "$J"/import/manifests/. "$J"/manifests/
     cp -a "$J"/import/artifacts/. "$J"/artifacts/
     for f in corpus-browser-state.json goals-state.json referee-state.json; do
       [ -f "$J/artifacts/$f" ] && mv "$J/artifacts/$f" "$S/$f"; done
     rm -rf "$J"/import'
   ```
5. **Measurement sessions** (none today): a session recorded before the cutover lands on
   the laptop; re-upload it from the capture client to the PC after E (the compatibility
   path publishes it into `raw/`, design §5.4) rather than copying it.

## D. Real-corpus parity (design §5.3; owner-run)

Needs: A, B, C. Reads the moved copy read-only; prints only digests and counts.

```sh
cd ~/src/voice-lab && git fetch && git switch --detach origin/main
env -u TMUX -u TMUX_PANE VOICE_JOURNEY_CORPUS_ROOT=/data/voice-lab/memos/phone \
  FFMPEG_BIN=/usr/bin/ffmpeg WHISPER_CPP_BIN=/usr/bin/whisper-cli \
  sudo -E -u voice-lab .venv/bin/python -I -B scripts/journey-parity \
    --data-root /data/voice-lab/journey --approval "<the owner's approval text>" --out /tmp/parity-real.json
```

Without `--fresh` the resumable stages (filter, stt, features) carry the moved rows over and
compare the new rows and the full recomputations (lyric, reclassify, journeys); `--fresh`
re-runs whisper over the whole corpus twice (hours). Record in the log: the report's
`summary` (stage results, file counts, difference counts) and the report's sha256; never its
values. A non-zero difference count stops the cutover until it is explained.

## E. Serving journey from the PC

Needs: A, C. A voice-lab PR (prepared as a draft, "deploy: serve journey and /view/ from
the PC") adds to `deploy/stack/compose.yaml`'s `api` service:

- read-only binds `/data/voice-lab/journey` and `/data/voice-lab/memos` (infra's
  `host.conf` grants them since infra PR #2; `create_host_path: false`);
- `serve` arguments `--journey --playback-approval "<the playback approval text>"`;
- environment `VOICE_JOURNEY_CONTAINER=1`, `VOICE_JOURNEY_DATA=/data/voice-lab/journey`,
  `VOICE_JOURNEY_CORPUS_ROOT=/data/voice-lab/memos/phone`,
  `VOICE_JOURNEY_STATE_DIR=/data/voice-lab/state/journey`,
  `VOICE_JOURNEY_MEASUREMENT_DIR=/data/voice-lab/lab/raw`.

Merging it deploys; then, from the PC: `/healthz` 200; `/api/v1/summary` 401 without a
token; `/view/` 302 to Pocket ID without a session; with the owner's browser session
`https://voice-lab.$TN/view/` shows the summary, recordings, journeys, trends, goals and the
report (no audio, no transcript, no lyric). `scshafe-deploy --verify` as `run-voice-lab`.

The capture client (still `voice-journey-capture`) reaches the PC through the
compatibility path once reconfigured: `VOICE_JOURNEY_CAPTURE_SERVER=https://voice-lab.$TN`,
`VOICE_JOURNEY_CAPTURE_CLIENT_ID=voice-lab-desktop` (Pocket ID: the client's loopback
redirect, including its fallback port 53683, on `voice-lab-desktop`), sign in again. The
acceptance item: one real UMIK-1 session lands in `raw/` with a v2 receipt (owner, real
recording). Known gap until phase 4: this client writes no D-021 ledger entry.

## F. Cutover (owner's go-ahead)

Needs: A–E, D clean.

1. The PC's job runner: write `/etc/voice-lab/journey.env` (root:root 0644):

   ```sh
   sudo install -m 0644 /dev/stdin /etc/voice-lab/journey.env <<'EOF'
   VOICE_JOURNEY_DATA=/data/voice-lab/journey
   VOICE_JOURNEY_CORPUS_ROOT=/data/voice-lab/memos/phone
   VOICE_JOURNEY_STATE_DIR=/data/voice-lab/state/journey
   VOICE_JOURNEY_MEASUREMENT_DIR=/data/voice-lab/lab/raw
   FFMPEG_BIN=/usr/bin/ffmpeg
   WHISPER_CPP_BIN=/usr/bin/whisper-cli
   VOICE_JOURNEY_APPROVAL=<the owner's standing approval for the audio-reading stages on the PC>
   EOF
   systemctl start voice-lab-jobs.service; journalctl -u voice-lab-jobs.service -n 20
   ```

   `voice-lab-jobs.timer` (installed by the lane) then ticks every 5 minutes; the API's
   `GET /api/v1/watch` shows the state.
2. Stop the laptop's mirror target on the Mini (`launchctl bootout
   gui/$(id -u)/ai.voice-journey.mirror`); the PC's job (B.3) continues. The laptop's drop
   stays read-only and intact.
3. Merge scshafe/voice-journey PR #4 (draft: the stack becomes a `308` responder to
   `https://voice-lab.$TN`, same path; the node identity is kept). Watch its deploy run, then
   `curl -sI https://voice-journey.$TN/view/` → `308` with `location: https://voice-lab.$TN/view/`.
4. Acceptance (§9 phase 3): a new Voice Memo recorded on the phone appears in the PC's corpus
   and passes the whole pipeline within one mirror period plus the pipeline's runtime
   (`GET /api/v1/watch`, then the row in `/view/recordings`).

## G. Rollback

Restart the laptop's stack (its data untouched): revert PR #4 (and #5) in voice-journey
(`git revert`, merge; or dispatch `deploy.yml` with the previous `sha` and
`allow_rollback=true`). Point the sender back: `launchctl bootstrap gui/$(id -u)
~/Library/LaunchAgents/ai.voice-journey.mirror.plist` on the Mini. Stop the PC's job
runner (`sudo rm /etc/voice-lab/journey.env`; the unit's condition turns it off). Nothing
on the PC is deleted. The Air vault can be unbound without touching the laptop's.

## H. The Air

### H.1 The mirror sender moves to the Air (after the owner's iCloud Voice Memos sync)

The sender is `apps/mirror/` (Python, stdlib only), installed per
[apps/mirror/README.md](../../apps/mirror/README.md) "Install on a Mac": its own venv, the
launcher compiled with that interpreter and ad-hoc signed, Full Disk Access for the
launcher, a new key `~/.ssh/vj-mirror` (comment `vj-mirror@coles-macbook-air`), the plist
with `VOICE_LAB_MIRROR_DEST=vj-mirror@cole-arch-pc:`. Then on the PC (one key at a time:
this replaces the Mini's):

```sh
scp cole-macbook-air:.ssh/vj-mirror.pub /tmp/vj-mirror-air.pub
sudo VJ_LAYOUT=pc ~/infra/replication/voice-journey-mirror/vj-mirror-drop install --from-host cole-macbook-air /tmp/vj-mirror-air.pub
```

Check the Air's first run (`--dry-run`, then the launchd job), then retire the Mini's jobs
(`launchctl bootout` both). The Air sleeps; the drop tolerates catching up on wake (exit 3
= locked, retry next period).

### H.2 The Air vault (design §4.3; the offsite copy)

Server side on the Air: [deploy/vault/macos/README.md](../../deploy/vault/macos/README.md)
(the non-admin account, the sealed release, the userspace `tailscaled` node
`voice-lab-vault-air`, `tag:voice-lab-vault-air`). Infra: apply the tailnet policy of infra
PR #2 (the tag and the PC-only grant); after the node exists, add
`voice-lab-vault-air.$TN:443` to `stacks/voice-lab/host.conf` `HOST_EGRESS_TAILNET` (not
before: the entrypoint refuses a name that does not resolve). On the PC:

```sh
vl_creds_air() { sudo systemd-run --pipe --wait --collect --quiet -p User=voice-lab -p Group=voice-lab \
  -p UMask=0077 -p WorkingDirectory=/ -p 'ImportCredential=private.cole.voice-lab.vault.*' \
  -p 'ImportCredential=private.cole.voice-lab.vault-destination.air.*' $VL "$@"; }
# tokens for the destination `air` (RUNBOOK §9), encrypted into /etc/credstore.encrypted/
$VL vault approve --destination air --tag tag:voice-lab-vault-air ... --preview   # read it, then approve
$VL vault bind --destination air ... --classes memos prompt-database recordings records
vl_creds_air vault push --asset-root /data/voice-lab/lab --destination air --memos-root /data/voice-lab/memos/phone   # at home, Air awake
vl_creds_air vault verify --asset-root /data/voice-lab/lab --destination air
sudo install -m 0644 deploy/host-manual/voice-lab-vault-air.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now voice-lab-vault-air.timer
```

(The exact `approve`/`bind` arguments are the laptop destination's, phase 2, with
`--destination air` and the tag; see `docs/LIBRARY_VAULT.md`.) Acceptance: put the Air to
sleep during a push; on wake the hourly run resumes without re-sending stored chunks
(`resumed_blobs` in its result); then, with the Air away from home, `vl_creds_air vault
drill --asset-root /data/voice-lab/lab --destination air --target <empty dir> --memos-target
<empty dir> --generation <N> --manifest-sha256 <D>` over the tailnet re-hashes every file of
a generation. `vault staleness --check` flags the Air copy after 14 days.

## I. After the cutover

- 14 days: remove the laptop's voice-journey data (`/srv/voice-journey`), keeping its last
  stack backup in `/data/backups/` until the laptop and Air vault copies have each passed a
  restore drill (§5.6 step 6).
- 90 days: remove the redirect stack, its runner and infra's `stacks/voice-journey/`;
  archive scshafe/voice-journey and scshafe/voice-journey-capture (§5.7, after phases 4 and
  6).
