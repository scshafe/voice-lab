# Host units (the PC)

The voice-lab stack (`deploy/stack/`) is the network-facing half of the home host.
These systemd units are the other half: they run on the PC itself, as the `voice-lab`
system user, because they need what a container in the runner lane may not have (a
private network namespace, the btrfs snapshot tool). Design §3.2, §8.1; D-021, D-032.

| Unit | Runs | Network |
| --- | --- | --- |
| `voice-lab-intake.path` → `voice-lab-intake.service` | `voice-lab intake publish`: validates each finalized package in `incoming/` and publishes it into `raw/`, applies the decisions and trims the API queued in `incoming/` (`provenance/corpus-takes/`, `provenance/corpus-requests/`), then refreshes the corpus read model (`derived/corpus/`, D-052) | none (`PrivateNetwork=yes`) |
| `voice-lab-intake.timer` | the same pass every 15 minutes, as a backstop | none |
| `voice-lab-jobs.timer` → `.service` | `voice-lab journey watch --once`: one tick of the journey job runner (scan the Voice Memos drop; when two scans agree and differ from the index, run the pipeline). Inert until `/data/voice-lab/journey/manifests` and the owner's `/etc/voice-lab/journey.env` exist (phase 3 cutover) | none (`PrivateNetwork=yes`) |
| `voice-lab-jwks.timer` → `.service` | `voice-lab home-host fetch-jwks`: the identity provider's signing keys into `state/auth/jwks.json` for the API | the tailnet only (`IPAddressDeny=any`, `IPAddressAllow=` the tailnet ranges, granted by infra's `host.conf` `HOST_NETWORK`); the service user's egress table narrows it to the issuer |

Every unit here runs as `voice-lab`; nothing from this repository runs as root. The
root-run parts are infra's, in its signed host tree: the asset layout's provisioning
(`scshafe-voice-lab-provision`) and the read-only snapshots of the lab subvolume
(`scshafe-voice-lab-snapshot`: a `.path` unit on `raw/`, so every accepted session is in a
snapshot right after it is published, and a nightly `.timer`; kept 14 days, never the
newest; D-033).

They expect, as installed by the runner lane's entrypoint (D-032; infra's signed host
tree, installed on the PC 2026-10-06), which validates every unit here before it installs
it (infra `docs/platform/agent-deploy.md`, "Host units"):

- the released wheel in a sealed, content-addressed release with `current` pointing at
  it: `/opt/voice-lab/current/bin/voice-lab`;
- `/etc/voice-lab/host.env` with `VOICE_LAB_OIDC_ISSUER=https://id.<tailnet>`;
- the asset layout made by infra's provisioning: `/data/voice-lab` (btrfs subvolume,
  `root:voice-lab 0750`), `/data/voice-lab/lab` (its own subvolume, the asset root, `voice-lab
  0700`, initialised with `voice-lab assets init`), `/data/voice-lab/state/auth`
  (`voice-lab 0700`, the key file), `/data/voice-lab/.snapshots/lab` (root 0700).

The vault copier has one fixed-name pair per destination: infra's host-unit validator
refuses template units for now, so there is no `voice-lab-vault@.service`. It also refuses
`ImportCredential=` until it has a rule for credentials, so the laptop's pair is not here
but in `../host-manual/`, installed by hand (its README); the Air destination (phase 3) adds its own, `voice-lab-vault-air.service` and
`.timer` (an hourly probe rather than a nightly copy). Installing a pair enables nothing. A
destination is copied to only once Cole has created the library key and its tokens,
encrypted them as systemd credentials, approved and bound the destination, and enabled its
timer (`systemctl enable --now voice-lab-vault-laptop.timer`); RUNBOOK §9 "The PC (phase 2)"
has the commands. The unit reads the encrypted credentials from `/etc/credstore.encrypted/`
(root-owned, made once with `systemd-creds encrypt`) through two `ImportCredential=` globs:
`private.cole.voice-lab.vault.*` (the library key) and
`private.cole.voice-lab.vault-destination.laptop.*` (the laptop's tokens, never another
destination's). The service manager decrypts them into the unit's private, read-only
`$CREDENTIALS_DIRECTORY`; nothing in the unit runs as root or can write a credential.

Rollback: `systemctl disable --now voice-lab-intake.path voice-lab-intake.timer` stops
publication; nothing here deletes or rewrites `raw/`. `systemctl disable --now
voice-lab-vault-laptop.timer` stops the laptop's copies; the vault keeps its
generations and the PC its records.
