# Deploying the speech API (the owner's steps)

The speech API of D-046 ([docs/SPEECH_API.md](../../docs/SPEECH_API.md)) is in the code, but
nothing here is applied by a merge:

- `deploy/stack/compose.yaml` is unchanged; the stack changes are in
  [`compose.speech.yaml`](compose.speech.yaml) here, for the owner to fold in;
- the host units are in [`../host-manual/`](../host-manual/README.md), which the runner lane
  does not install.

Until these steps are done the API serves no `/api/v1/speech`, and the units do not run.
Every step is Cole's; an agent prepares and reviews, it never approves consent, designates a
release, creates a Pocket ID client or touches the running stack.

1. **Consent.** A consent record cannot be replaced once it is approved or revoked
   (`consent approve-self` refuses), and `cole-self-v1` is already approved for the
   fine-tune runs. Speech for registered callers therefore gets a new record,
   `cole-self-v2`, naming `private-assistant-speech` on `home-host` (RUNBOOK §1). On the PC,
   as `voice-lab`, preview it, then approve it with the preview's `scope_sha256`:

   ```sh
   VL=/opt/voice-lab/current/bin/voice-lab
   $VL consent preview --consent-id cole-self-v2 \
     --purpose private-assistant-speech --execution-boundary home-host \
     --expires-at <date, at most a year away>
   $VL consent approve-self --asset-root /data/voice-lab/lab --consent-id cole-self-v2 \
     --purpose private-assistant-speech --execution-boundary home-host \
     --expires-at <the same date> --reviewed-scope-sha256 <scope_sha256> \
     --i-am-the-subject-and-authorize
   ```

   No `--expected-current-sha256`: there is no current record of that id. The release names
   this record by id and digest (step 3), so renewing it later means a new id and designating
   again.
2. **The runtime.** The F5-TTS runtime for the PC's GPU (design phase 5, spike S-2), with its
   pinned weights, at the `runtime.runtime_root` the configuration names (`model_family:
   "f5-tts"`, `device: "cuda"` on ROCm; torch 2.14.0+rocm7.2 and torchcodec 0.16.0+cpu, as
   spike S-2 ran it; its hash-locked lock is a follow-up). Until it exists every job fails
   `runtime_unavailable`; leaving `runtime` out of the configuration says so explicitly.
   `runtime.nfe_steps` stays 32 until Cole's 16-versus-32 listening check.
3. **A voice release.** On the PC, as `voice-lab`, preview, then designate, naming the consent
   record of step 1 and the configuration (whose `subject_id` is the one subject that may
   speak):

   ```sh
   $VL voice release designate --asset-root /data/voice-lab/lab --run <run id> \
     --consent-id cole-self-v2 --config /etc/voice-lab/speech.json --designated-by Cole
   $VL voice release designate --asset-root /data/voice-lab/lab --run <run id> \
     --consent-id cole-self-v2 --config /etc/voice-lab/speech.json --designated-by Cole \
     --i-designate-this-run
   ```

4. **Pocket ID.** One confidential client per caller with the `client_credentials` grant.
   Fetch one token with the client's credentials and decode its claims locally (never print,
   log or keep the token): its `sub` and `aud` are the caller's `subject` and `audiences`.
5. **The configuration** `/etc/voice-lab/speech.json` (root:voice-lab 0640;
   `cole-voice-lab.speech-config/v1`, example in docs/SPEECH_API.md §4), checked with
   `voice-lab speech check-config --config /etc/voice-lab/speech.json`.
6. **Webhook secrets.** For each caller with webhook URLs,
   `/etc/voice-lab/speech-webhook-secrets/<caller id>` (the directory root:root 0700, each
   file root:root 0600) holding one `whsec_` and the base64 of 32 random bytes; the caller
   gets the same secret by a private channel. Never in Git or the configuration. The
   delivery unit receives the directory as systemd credentials (`LoadCredential=`); no other
   process, the API and the job runner included, can read a secret.
7. **The stack.** Fold `compose.speech.yaml` into `deploy/stack/compose.yaml` in a reviewed
   change, add `VOICE_LAB_SPEECH_AUDIENCE` to the stack's `.env` (`stack env-set`), and have
   infra's `stacks/voice-lab/host.conf` grant the read-only bind of
   `/etc/voice-lab/speech.json`. Then confirm with a caller's real token that the door admits
   it: oauth2-proxy builds a session from a bearer token and a client-credentials token has
   no `email` claim. If the door refuses it, fix the claim in Pocket ID or the door's claim
   mapping; never skip the door for `/api/v1/speech`.
8. **Host units.** The runner writes only the speech state, the GPU slot's lock and the
   render ledger, so the ledger's directory must exist first:
   `sudo -u voice-lab install -d -m 0700 /data/voice-lab/lab/provenance/speech-renders`.
   Then install and enable the speech runner (long-running, with the resident model) and the
   webhook delivery ([`../host-manual/README.md`](../host-manual/README.md)). The resident
   model needs about 1.3–3 GiB of the card's 16 GB beside llama-swap's models.
9. **The tailnet policy** (infra `tailnet/policy.hujson`): the caller's node may reach
   `tag:voice-lab:443`; the PC may reach each caller's node on its webhook port; and the
   service user's egress table names those nodes (`HOST_EGRESS_TAILNET`).

## Deploys and the runner

A merge deploys a new release to `/opt/voice-lab/current`, but the runner lane neither
installs nor restarts `voice-lab-speech.service`, because the unit lives in
`deploy/host-manual/`. The runner looks after itself: it runs with `--release-link
/opt/voice-lab/current`, and at the start of every pass (four a second) and before it claims
each job, with a model loaded or not, it checks that its own code is still the release that
link points at. Once a deploy has moved the link, the job it is rendering (if any) finishes
first, never cut mid-sentence; then it unloads the model, gives back the GPU slot and any job
it had claimed but not started (the job is queued again) and exits with status 75;
`Restart=on-failure` starts the new code ten seconds later. So a busy runner picks up a deploy
after its current job, not after its idle time.

A link that is missing, dangling or unreadable is not a deploy, and restarting cannot fix it:
the runner gives back what it holds, logs "stopping, not restarting: … does not resolve" and
exits 78, and the unit's `RestartPreventExitStatus=78` leaves it stopped rather than restarting
it every ten seconds. Fix the link (the runner lane's activation, or infra's restore of the
host), then `sudo systemctl start voice-lab-speech.service`.

The long-term fix is for the runner lane to own the unit, so that a deploy installs and
restarts it like the units in `deploy/host/`. That needs infra's host-unit validator to accept
GPU device access (`DeviceAllow=` for `/dev/kfd` and the DRM nodes, `SupplementaryGroups=render
video`) and the delivery unit's `LoadCredential=`: an infra change, for the owner.

## One pass by hand (`--once`)

`voice-lab speech run --config /etc/voice-lab/speech.json --once` makes one pass (recover,
render what is queued, sweep), unloads the model and prints the job ids and outcomes. It is for
when the unit is stopped (a check after an install, or draining the queue while the unit is
disabled), as `voice-lab`:

```sh
sudo systemctl stop voice-lab-speech.service
sudo -u voice-lab /opt/voice-lab/current/bin/voice-lab speech run \
  --config /etc/voice-lab/speech.json --once
sudo systemctl start voice-lab-speech.service
```

Only one runner process runs at a time: while the unit runs, `--once` is refused at once (exit
2, "another speech runner is running") instead of waiting for the GPU slot that the unit's
resident model holds, which would stall the queue. A unit started during a pass by hand waits
for the pass to end.

## The text key

`/data/voice-lab/state/speech/text-digest.key` (32 random bytes, `voice-lab`, 0600) is the key
of the keyed digests that name each job's text and request and that the render ledger keeps
for ever. It is made only on a new install, when there is no job record and no ledger entry
yet, and never made again or replaced automatically after that: a lost key is a loss to be
decided on, not hidden. Problems are logged once per change, never with the key
(`journalctl -u voice-lab-speech`, the API container's log), and the API checks the file on
every request, so it never keeps using a key that vanished and uses a replaced one from then
on (logging "the text key changed on disk").

- **Unusable** (empty, the wrong size, open to group or others, a link or not a regular
  file): submits answer `500 internal_error` and the logs say why ("the text key cannot be
  used: … is 0 bytes, not 32 bytes"). A wrong mode is repaired with `chmod 0600` (as
  `voice-lab`). Anything else is a lost key: move the file aside
  (`mv text-digest.key text-digest.key.broken-<date>`), then as below. Keep the old file if it
  may still be the real key (a mode or link problem); it is needed to check a text against the
  earlier ledger entries.
- **Missing once jobs or ledger entries exist** (deleted, or the state restored without it):
  submits answer `500 internal_error` and the logs say "… is missing and is not made again (N
  job record(s) and M ledger entr(ies) were named by it)". Restore it from a backup if there
  is one. Otherwise, as `voice-lab`, make a new one and accept the loss:

  ```sh
  sudo -u voice-lab /opt/voice-lab/current/bin/voice-lab speech new-text-key \
    --config /etc/voice-lab/speech.json --i-accept-the-lost-key
  ```

  It refuses while a key file is there, prints how many records and entries the lost key
  named, and the API picks the new key up at its next request. Jobs and their audio are
  unaffected, but the earlier digests were made under the old key: a caller that replays an
  earlier idempotency key gets `409 idempotency_key_unverifiable` and must use a new one, and
  the earlier ledger entries can be checked against a text only with the old key.

## Rollback

- **Stop new speech:** remove `--speech-config` from the API's command (the API then answers
  `404` under `/api/v1/speech`) and redeploy the stack.
- **Stop the host side:** `sudo systemctl disable --now voice-lab-speech.service
  voice-lab-speech-webhooks.path voice-lab-speech-webhooks.timer`. Stopping the runner
  mid-job ends that job `failed internal_error` after the sentence being rendered, with its
  ledger entry (only a kill leaves it `running`, and the next start then fails it and
  withdraws its sentences). Jobs still queued wait, and fail `queue_timeout` if the runner is
  not back within their queue time.
- **Withdraw consent:** `consent revoke --consent-id cole-self-v2 --expected-sha256 <digest>`.
  The API refuses new jobs at once (`503 consent_unavailable`), queued jobs fail
  `consent_changed` when the runner reaches them, and a render in progress stops before its
  next sentence (the sentence being rendered finishes; what was published stays until
  retention). The releases and the ledger stay; nothing is deleted.
- **A different voice:** designate another run; a render in progress stops before its next
  sentence (`voice_release_changed`), and later jobs use the new release.
