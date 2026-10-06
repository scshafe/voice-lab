# Host units (the PC)

The voice-lab stack (`deploy/stack/`) is the network-facing half of the home host.
These systemd units are the other half: they run on the PC itself, as the `voice-lab`
system user, because they need what a container in the runner lane may not have (a
private network namespace, the btrfs snapshot tool). Design §3.2, §8.1; D-021, D-032.

| Unit | Runs | Network |
| --- | --- | --- |
| `voice-lab-intake.path` → `voice-lab-intake.service` | `voice-lab intake publish`: validates each finalized package in `incoming/` and publishes it into `raw/` | none (`PrivateNetwork=yes`) |
| `voice-lab-intake.timer` | the same pass every 15 minutes, as a backstop | none |
| `voice-lab-jwks.timer` → `.service` | `voice-lab home-host fetch-jwks`: the identity provider's signing keys into `state/auth/jwks.json` for the API | the identity provider |
| `voice-lab-vault@<destination>.timer` → `voice-lab-vault@<destination>.service` | `voice-lab vault push --destination <destination>`, then `vault verify`: a sealed copy of the lab to that destination's vault, then every file of the new head downloaded, decrypted and re-hashed (design §4.3, D-034); nightly at 02:30 | the tailnet only (`IPAddressAllow=100.64.0.0/10`; the tailscaled socket for the node's identity check) |

Every unit here runs as `voice-lab`; nothing from this repository runs as root. The
root-run parts are infra's, in its signed host tree: the asset layout's provisioning
(`scshafe-voice-lab-provision`) and the read-only snapshots of the lab subvolume
(`scshafe-voice-lab-snapshot`: a `.path` unit on `raw/`, so every accepted session is in a
snapshot right after it is published, and a nightly `.timer`; kept 14 days, never the
newest; D-033).

They expect, as installed by the runner lane's entrypoint (D-032; infra's signed host
tree, pending the owner's approval of its manifest hash):

- the released wheel in a sealed, content-addressed release with `current` pointing at
  it: `/opt/voice-lab/current/bin/voice-lab`;
- `/etc/voice-lab/host.env` with `VOICE_LAB_OIDC_ISSUER=https://id.<tailnet>`;
- the asset layout made by infra's provisioning: `/data/voice-lab` (btrfs subvolume,
  `root:voice-lab 0750`), `/data/voice-lab/lab` (its own subvolume, the asset root, `voice-lab
  0700`, initialised with `voice-lab assets init`), `/data/voice-lab/state/auth`
  (`voice-lab 0700`, the key file), `/data/voice-lab/.snapshots/lab` (root 0700).

The vault copier is a template: installing it enables nothing. A destination is copied to
only once Cole has created the library key and its tokens, encrypted them as systemd
credentials, approved and bound the destination, and enabled its timer
(`systemctl enable --now voice-lab-vault@laptop.timer`); RUNBOOK §9 "The PC (phase 2)" has
the commands. The unit reads the encrypted credentials from `/etc/credstore.encrypted/`
(root-owned, made once with `systemd-creds encrypt`) through two `ImportCredential=` globs:
`private.cole.voice-lab.vault.*` (the library key) and
`private.cole.voice-lab.vault-destination.%i.*` (this destination's tokens, never another's).
The service manager decrypts them into the unit's private, read-only
`$CREDENTIALS_DIRECTORY`; nothing in the unit runs as root or can write a credential. The
timer is generic; a destination that needs another cadence (the Air's hourly probe, phase 3)
gets a drop-in for its instance.

Rollback: `systemctl disable --now voice-lab-intake.path voice-lab-intake.timer` stops
publication; nothing here deletes or rewrites `raw/`. `systemctl disable --now
voice-lab-vault@<destination>.timer` stops a destination's copies; the vault keeps its
generations and the PC its records.
