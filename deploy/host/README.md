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

Rollback: `systemctl disable --now voice-lab-intake.path voice-lab-intake.timer` stops
publication; nothing here deletes or rewrites `raw/`.
