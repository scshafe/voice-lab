# Host units installed by hand (the PC)

The units here run on the PC as the `voice-lab` user, like those of `../host/`, but the
runner lane does not install them: infra's host-unit validator (`scshafe-host-release`,
infra `docs/platform/agent-deploy.md`, "Host units") refuses `ImportCredential=`, and the
vault copier needs it for the library key and the destination's tokens (D-034). They stay
here, outside `deploy/host/`, until infra has a rule for credentials; then they move back
and the lane installs them like the others.

| Unit | Runs | Network |
| --- | --- | --- |
| `voice-lab-vault-laptop.timer` → `voice-lab-vault-laptop.service` | `voice-lab vault push --destination laptop`, then `vault verify`: a sealed copy of the lab to the laptop's vault, then every file of the new head downloaded, decrypted and re-hashed (design §4.3, D-034); nightly at 02:30 | the tailnet only (`IPAddressAllow=100.64.0.0/10`; the tailscaled socket for the node's identity check) |
| `voice-lab-vault-air.timer` → `voice-lab-vault-air.service` | `voice-lab vault push --destination air --memos-root /data/voice-lab/memos/phone --if-older-than 7d --skip-unreachable`, then `vault verify --if-unverified --skip-unreachable`: the offsite copy on the MacBook Air (design §4.3, phase 3). Hourly; a run does nothing while the copy is under a week old, and succeeds without sending anything when the Air's node is offline or does not answer (after the same node identity check every copy makes); an interrupted push resumes from the destination's journal; each generation is verified once | the tailnet only, as the laptop's |

| `voice-lab-speech.path`, `.timer` → `voice-lab-speech.service` | `voice-lab speech run`: the speech job runner (docs/SPEECH_API.md §7, D-046). One pass over the queue: re-checks the voice release and its consent record, holds the GPU slot, renders, writes the ledger and the webhook event; then the retention sweep. Started when the API queues a job, and every 5 minutes | none (`PrivateNetwork=yes`); the GPU's `/dev/kfd` and DRM nodes only |
| `voice-lab-speech-webhooks.path`, `.timer` → `voice-lab-speech-webhooks.service` | `voice-lab speech deliver`: signs and delivers ended jobs' webhooks to the callers' tailnet URLs, with retries (§8). Started when an event is queued, and every 30 seconds | the tailnet only, as the JWKS unit |

The speech pair is here because infra's validator has no rule for GPU device access yet and
because it is inert until the owner's steps in [`../speech/README.md`](../speech/README.md)
are done; both services are conditioned on `/etc/voice-lab/speech.json`. Install them with:

```sh
sudo install -o root -g root -m 0644 deploy/host-manual/voice-lab-speech.service \
  deploy/host-manual/voice-lab-speech.path deploy/host-manual/voice-lab-speech.timer \
  deploy/host-manual/voice-lab-speech-webhooks.service \
  deploy/host-manual/voice-lab-speech-webhooks.path \
  deploy/host-manual/voice-lab-speech-webhooks.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-lab-speech.path voice-lab-speech.timer \
  voice-lab-speech-webhooks.path voice-lab-speech-webhooks.timer
```

The service user's egress table (written by the lane from infra's
`stacks/voice-lab/host.conf`) applies to these units too: the vault's name,
`voice-lab-vault.colobus-stargazer.ts.net:443`, is in its `HOST_EGRESS_TAILNET`, so the
copier still reaches the laptop's vault and nothing else on the tailnet.

Install (or update) by hand, as root, from a checkout at `main`:

```sh
sudo install -o root -g root -m 0644 deploy/host-manual/voice-lab-vault-laptop.service \
  deploy/host-manual/voice-lab-vault-laptop.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-lab-vault-laptop.timer
```

The Air's pair is installed the same way, once the Air's vault answers (`deploy/vault/macos/
README.md`), its destination is approved and bound, infra's `stacks/voice-lab/host.conf`
names `voice-lab-vault-air.<tailnet>.ts.net:443` in `HOST_EGRESS_TAILNET` beside the laptop's
vault, and the first full seeding was run by hand at home (docs/LIBRARY_VAULT.md, "The Air"):

```sh
sudo install -o root -g root -m 0644 deploy/host-manual/voice-lab-vault-air.service \
  deploy/host-manual/voice-lab-vault-air.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-lab-vault-air.timer
```

The unit runs `/opt/voice-lab/current/bin/voice-lab`, the release the lane activates, so
a deploy updates the code it runs; only the unit text is installed by hand. Rollback and
the credentials: `../host/README.md` and RUNBOOK §9 "The PC (phase 2)".
