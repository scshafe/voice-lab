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
/opt/voice-lab/current`, and before each model load, and whenever it is idle without a model,
it checks that its own code is still the release that link points at. Once a deploy has moved
the link it gives back the GPU slot and any job it had claimed (the job is queued again) and
exits with status 75; `Restart=on-failure` starts the new code ten seconds later. A model
that is loaded keeps serving with the code that loaded it until it is unloaded (at most the
idle time, 15 minutes).

The long-term fix is for the runner lane to own the unit, so that a deploy installs and
restarts it like the units in `deploy/host/`. That needs infra's host-unit validator to accept
GPU device access (`DeviceAllow=` for `/dev/kfd` and the DRM nodes, `SupplementaryGroups=render
video`) and the delivery unit's `LoadCredential=`: an infra change, for the owner.

## Rollback

- **Stop new speech:** remove `--speech-config` from the API's command (the API then answers
  `404` under `/api/v1/speech`) and redeploy the stack.
- **Stop the host side:** `sudo systemctl disable --now voice-lab-speech.service
  voice-lab-speech-webhooks.path voice-lab-speech-webhooks.timer`. Stopping the runner
  mid-job leaves that job `running`; the next start fails it `internal_error`. Jobs still
  queued wait, and fail `queue_timeout` if the runner is not back within their queue time.
- **Withdraw consent:** `consent revoke --consent-id cole-self-v2 --expected-sha256 <digest>`.
  The API refuses new jobs at once (`503 consent_unavailable`), queued jobs fail
  `consent_changed` when the runner reaches them, and a render in progress stops before its
  next sentence (the sentence being rendered finishes; what was published stays until
  retention). The releases and the ledger stay; nothing is deleted.
- **A different voice:** designate another run; a render in progress stops before its next
  sentence (`voice_release_changed`), and later jobs use the new release.
