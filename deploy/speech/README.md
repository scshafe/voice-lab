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

1. **Consent.** Preview, then approve, a consent record whose purposes include
   `private-assistant-speech` and whose boundaries include `home-host` (RUNBOOK §1). The pending
   record that `assets init` installed before 2026-10-11 names the retired
   `private-chloe-speech-rendering`; it is rotated into approval like any pending record.
2. **The runtime.** The F5-TTS runtime for the PC's GPU (design phase 5, spike S-2), with its
   pinned weights, at the `runtime.runtime_root` the configuration names (`device: "cuda"` on
   ROCm). Until it exists every job fails `runtime_unavailable`; leaving `runtime` out of the
   configuration says so explicitly.
3. **A voice release.** On the PC, as `voice-lab`:
   `voice-lab voice release designate --asset-root /data/voice-lab/lab --run <run id>
   --designated-by Cole`, review the preview, then the same with `--i-designate-this-run`.
4. **Pocket ID.** One confidential client per caller with the `client_credentials` grant.
   Fetch one token with the client's credentials and decode its claims locally (never print,
   log or keep the token): its `sub` and `aud` are the caller's `subject` and `audiences`.
5. **The configuration** `/etc/voice-lab/speech.json` (root:voice-lab 0640;
   `cole-voice-lab.speech-config/v1`, example in docs/SPEECH_API.md §4), checked with
   `voice-lab speech check-config --config /etc/voice-lab/speech.json`.
6. **Webhook secrets.** For each caller with webhook URLs,
   `/etc/voice-lab/speech-webhook-secrets/<caller id>` (root:voice-lab 0640) holding one
   `whsec_` and the base64 of 32 random bytes; the caller gets the same secret by a private
   channel. Never in Git or the configuration.
7. **The stack.** Fold `compose.speech.yaml` into `deploy/stack/compose.yaml` in a reviewed
   change, add `VOICE_LAB_SPEECH_AUDIENCE` to the stack's `.env` (`stack env-set`), and have
   infra's `stacks/voice-lab/host.conf` grant the read-only bind of
   `/etc/voice-lab/speech.json`. Then confirm with a caller's real token that the door admits
   it: oauth2-proxy builds a session from a bearer token and a client-credentials token has
   no `email` claim. If the door refuses it, fix the claim in Pocket ID or the door's claim
   mapping; never skip the door for `/api/v1/speech`.
8. **Host units.** Install and enable the speech runner and the webhook delivery
   ([`../host-manual/README.md`](../host-manual/README.md)).
9. **The tailnet policy** (infra `tailnet/policy.hujson`): the caller's node may reach
   `tag:voice-lab:443`; the PC may reach each caller's node on its webhook port; and the
   service user's egress table names those nodes (`HOST_EGRESS_TAILNET`).

Rollback: remove `--speech-config` (the API then answers `404` under `/api/v1/speech`) and
`systemctl disable --now voice-lab-speech.path voice-lab-speech.timer
voice-lab-speech-webhooks.path voice-lab-speech-webhooks.timer`. Revoking the consent record
stops every render at once. The ledger and the releases stay.
