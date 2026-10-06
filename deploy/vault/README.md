# Encrypted library vault deployment

These are reviewed deployment templates, **not a deployed service**. The Mac
keeps the working library and encryption key; this server receives opaque sealed
blobs and refs. It has no model runtime, cryptography dependency, audio decoder,
or synthesis job. [Library behavior](../../docs/LIBRARY_VAULT.md) and the
[policy fragment](tailnet-policy.md) define the other sides of this boundary.

Every remote or tailnet action in Step 15 requires Cole's approval, including
SSH (`ssh ubuntu-tailscale`), policy changes, auth-key creation, image transfer,
container starts, and the first push. Cole runs key/token creation in a private
terminal and stores the recovery sheet before continuing. Decide the pending
combined Mac app build/install before starting deployment. Never start this
Tailscale sidecar on the Mac.

## Prerequisites and local proof

The surveyed target is `cole-ubuntu-pc`, Ubuntu 24.04.4 x86_64, Docker Engine
29.6.1, host Tailscale 1.102.2, and SSH user `cole` with Docker/sudo access.
Confirm those facts and the Compose plugin with Cole at deployment. Other host
services already use HTTPS 443/8443/8444; the vault has its own sidecar node and
does not change host Serve handlers. Ask Cole whether to compare these templates
with a current `scshafe/infra` stack before deploying.

The development Mac has Colima, no buildx or Compose plugin, and a VM that mounts
only `$HOME`. The verifier uses the legacy Docker builder and plain `docker run`:

```sh
scripts/verify-vault-container
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_operator_scripts.py
```

The verifier reads local Docker metadata before contacting any daemon. It requires
the current context `colima` to name the owned Unix socket
`~/.colima/default/docker.sock`; host/context/config/TLS environment overrides
are refused. Set the intended local context yourself and unset those overrides
before rerunning. It does not change Docker's global configuration. Every daemon
command, including build and cleanup, pins that validated socket explicitly and
uses a frozen environment, so later context changes cannot redirect it. Staging
and sidecar image-pin validation do not consult Docker at all.

It refuses downloads. It stages current source under `~/.cache/voice-lab/`
(or the pre-rename `~/.cache/cole-voice-lab/` when only that exists), builds a
wheel offline, checks the application wheel
digest in the image, and installs only the hashed Linux Pydantic closure. The
base is the reviewed multi-architecture index in `Dockerfile` (amd64 and arm64).
The resulting server runs as 10001:10001, read-only with no capabilities, a small
tmpfs, resource limits, and one **test-only** loopback published port. The shared
storage conformance suite exercises namespace `test`; damage tests explicitly
skip because a client has no access to backing files. `docker inspect` checks
actual hardening. Named test volumes, containers, tag, and private staging are
removed on success, failure, or handled interruption. The server never starts a
sidecar. No real Keychain, library, or tailnet peer is involved.

Cleanup deletes private staging and the synthetic token before contacting Docker,
attempts each owned removal with a 10-second deadline, and defers SIGINT/SIGTERM
until all removals have been attempted. An interrupted run remains unsuccessful
(exit 130/143), and any failed removals are reported. Ordinary commands have a
60-second deadline; offline builds have 10 minutes and conformance has 3 minutes.

The default wheelhouse is `~/.cache/voice-lab/vault-linux-wheels` (override
with `--wheelhouse`). Populate it **only with separate download approval**; this
one shell command downloads both architectures, because one pip resolution
chooses only one core wheel:

```sh
for vault_arch in x86_64 aarch64; do
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pip download \
    --require-hashes --only-binary=:all: --no-deps --implementation cp \
    --python-version 3.12 --abi cp312 --platform "manylinux2014_$vault_arch" \
    -r requirements/vault-linux.txt \
    --dest "$HOME/.cache/voice-lab/vault-linux-wheels"
done
```

Create that external directory with mode 0700 first. All six wheel SHA-256s
were checked against PyPI JSON when the lock was written. Staging rechecks the
bytes, exact package versions, both architectures, and single-link regular
files. Wheels and build contexts never enter Git. Pull the exact base digest
only with download approval if it is not already cached; the offline verifier
checks for it before attempting a build.

## Stage, transfer, and build

```sh
scripts/stage-vault-image
```

The JSON result names the retained private `stage` and its `build_context`.
Only `context/` is the Docker build input: `Dockerfile`, the six dependency
wheels, application wheel, Linux lock, and `wheel.sha256`. The source snapshot
used to build the wheel is removed afterwards. No repository-root context,
library data, credentials, or model files are transferred. Delete the retained
stage when deployment no longer needs it.

The branch is not pushed. Cole chooses and approves how this context or a built
image and the reviewed `deploy/vault/` templates reach the host; do not improvise
a Git push. Keep the templates separate from the Docker build context. An arm64
image built on the Mac is not the x86_64 host image. The portable context includes both dependency
architectures so the host can build natively, using the same index pin:

```sh
DOCKER_BUILDKIT=0 docker build --network none --pull=false --force-rm \
  -t cole-voice-lab-vault:reviewed '<transferred-context>'
docker image inspect --format '{{.Id}}' cole-voice-lab-vault:reviewed
```

Record that immutable image ID as `VAULT_IMAGE=sha256:<64hex>` in a private
external interpolation file copied from `.env.example`. The base must already
be cached on the host; any required pull is a separately approved action.
Normal Compose uses `pull_policy: never` for the vault image.

## Host directories and token hashes

Cole supplies `VAULT_DATA_DIR` on the large disk; its mount path is not known
yet. Verify the mounted filesystem first so a missing disk cannot silently turn
into root-disk storage. The directory and its `data` and `logs` children must
be absolute, symlink-free, mode 0700, owned by UID/GID 10001. Never substitute
the Mac asset root. Have Cole authorize creating these exact paths, for example:

```sh
sudo install -d -m 0700 -o 10001 -g 10001 '<VAULT_DATA_DIR>' \
  '<VAULT_DATA_DIR>/data' '<VAULT_DATA_DIR>/logs'
```

Choose a separate root-owned 0700 `VAULT_TS_STATE_DIR`. It is the persistent
Tailscale node identity: preserve it across ordinary upgrades and restarts.
`VAULT_SERVE_CONFIG_DIR` holds the supplied `tailscale/serve.json`; mount the
**directory** read-only so containerboot sees atomic replacements. All bind
mounts use `create_host_path: false`. No Docker socket or node-state directory
is visible inside the vault application.

After the library identity exists, Cole runs on the Mac:

```sh
voice-lab vault token create --asset-root '<Mac asset root>' --namespace '<namespace>'
```

This stores distinct read/write tokens in the login Keychain and prints only
their SHA-256 hashes. Copy only those hashes into an external server configuration:

```json
{
  "schema_version": "cole-voice-lab.vault-server-config/v1",
  "namespaces": [{
    "name": "<namespace>",
    "read_token_sha256": "<64hex read hash>",
    "write_token_sha256": "<64hex write hash>",
    "quota_bytes": 1099511627776,
    "allowed_user_logins": []
  }],
  "max_workers": 8,
  "request_timeout_seconds": 120
}
```

Choose the quota with Cole. The optional allowed-login list is defence in
depth when the source Mac is user-owned; an empty list leaves token checking
authoritative. DERP links slower than about 140 KB/s per 16 MiB chunk may require
a higher request deadline (maximum 600 seconds). The config is a read-only
Compose secret, containing **hashes only**, mounted at `/run/secrets/vault_config`.
Set the host file to mode 0600 and owner 10001:10001; ordinary Compose file
secrets do not reliably apply requested `uid`/`mode` for you. Set its absolute
path as `VAULT_CONFIG_FILE`. Never put raw tokens in JSON, `.env`, shell argv,
Git, the library, or logs. The server's `hash-token` helper reads one token
from stdin without echoing it; ordinary setup needs only the hashes already
printed by `vault token create`. Do not run `new-token` through an agent.

Provision once after reviewing the hashes and paths. `VAULT_ENV` below names
the private external interpolation file; the normal image command deliberately
has no `--provision`. This one-off constructor validates config and creates
missing namespace stores, then closes without accepting a request:

```sh
docker compose --env-file "$VAULT_ENV" -f deploy/vault/compose.yaml \
  run --rm --no-deps --entrypoint python vault -I -B -c \
  "from pathlib import Path; from cole_voice_lab.storage_server import VaultServer, load_server_configuration; s=VaultServer(('127.0.0.1',0), config=load_server_configuration(Path('/run/secrets/vault_config')), data_root=Path('/var/lib/voice-lab-vault/data'), access_log=Path('/var/lib/voice-lab-vault/logs/access.jsonl'), provision=True); s.server_close()"
```

## Dedicated sidecar join and first use

With Cole, review [tailnet-policy.md](tailnet-policy.md), then select and inspect
the multi-architecture digest of `tailscale/tailscale:v1.102.2`. No sidecar image
was pulled or run by the Mac verifier. Record the full pin as `TAILSCALE_IMAGE`
and validate it before any Compose start:

```sh
scripts/check-vault-deployment --image 'tailscale/tailscale:v1.102.2@sha256:<64hex>'
```

Run this check from the Mac checkout; it needs no network and does not start or
inspect the sidecar. Use the same validated pin in the host interpolation file.

Confirm that the pinned image provides containerboot and `wget`, accepts the
read-only root with `/tmp` tmpfs and persistent state mount, and supports the
configured local health endpoint. This template deliberately uses userspace
networking, root with all capabilities dropped, no devices, and no privilege
gain; changing its user is a separate reviewed deployment change. Node health
binds only `127.0.0.1:9002`. Tailscale's socket is `/tmp/tailscaled.sock`; use
`tailscale --socket=/tmp/tailscaled.sock` for approved container CLI inspection.

Cole creates a **single-use, non-ephemeral**, tagged `tag:voice-lab-vault` auth
key with the shortest expiry the console offers. Store it in an external
root-owned 0600 file, set `TS_AUTHKEY_FILE` to its path, and never place the key
itself in `.env` or a command argument. This intentionally differs from older
stacks' reusable shared key. The join override references the file:

```sh
docker compose --env-file "$VAULT_ENV" -f deploy/vault/compose.yaml \
  -f deploy/vault/compose.join.yaml up -d
```

Each start is approved with Cole. Verify the node joins exactly as
`voice-lab-vault`, with the dedicated tag; a `-1` name means a stale registration
must be resolved before approval. If authorization fails, stop the sidecar
promptly with Cole instead of letting it crash-loop or spend certificate
issuance attempts. Avoid sharing logs containing login URLs. After successful
join, delete the auth-key file and its interpolation entry, then **recreate the
sidecar using only the base Compose file** so it no longer mounts the join secret:

```sh
docker compose --env-file "$VAULT_ENV" -f deploy/vault/compose.yaml \
  up -d --force-recreate tailscale
```

`TS_AUTH_ONCE=true` uses persisted identity on restarts. Serve expands
`${TS_CERT_DOMAIN}` to the node's actual FQDN and proxies HTTPS 443 to
`http://vault:8080` over the internal network. Funnel is absent. The app has no
egress network, and neither service publishes a host port. Certificates may
take about 40 seconds on first use; probe once and back off.

Record the endpoint, node StableID, tag, current tailnet suffix, owner and port
where Cole keeps endpoint inventory. No Mission Control reservation API exists
in this checkout. Bookkeeping does not grant access. Verify the live policy's
positive and negative cases, then follow the library runbook for the binding,
standing approval and first verified upload, all with Cole. The `training`
class is opt-in at binding time; Speak clips stay on the Mac. Archive or remove
any run only after a separate explicit decision for that run.

No UDP host port is published. This may force DERP relay and make large uploads
slow. With Cole, check direct versus relay connectivity at deployment. Publishing
a distinct fixed tailscaled UDP port conflicts with the current no-host-port
template and requires Cole's explicit choice and a reviewed Compose amendment.

## Operation, rotation, and removal

- Keep raw library keys and token recovery outside the vault. A vault backup
  alone cannot decrypt the library. This plan creates **no second data copy**.
- If Cole later authorizes a backup, stop the vault for a consistent copy of
  the complete data tree and hashes-only config, retaining ownership/modes.
  For a live backup, take a stable ref snapshot first, copy every immutable blob
  it names, then install that snapshot last; copying live refs last without
  ensuring their targets were included can produce an incomplete backup.
  Preserve node state separately only if intentionally preserving that identity.
- The access log is opened once. Rotate with **copy-truncate, never rename**:
  keep rotated copies private, copy `logs/access.jsonl` and then truncate the
  same inode. Renaming would leave the server writing into the old file. Stopping
  the server for rotation avoids the small copy/truncate race; live copy-truncate
  can lose concurrent log lines. Docker stdout uses bounded json-file rotation.
- Token replacement is explicit; `vault token create` refuses existing items.
  With Cole, stop copying and stop the server, retain a private recovery path for
  old credentials, remove only the two matching library token entries in Keychain
  Access (never the encryption key), and create a new pair on the Mac. Replace
  both hashes in the host config, validate/provision without changing namespace,
  recreate the vault container to mount the replacement file, and verify a read
  and write with the new pair before resuming. The config supports one read and
  one write hash per namespace, so this procedure has a maintenance window; do
  not claim an overlap/grace period. Revoke obsolete recovery copies afterwards.
- Image updates retain data and node-state paths and use a reviewed immutable
  `VAULT_IMAGE`. A new namespace needs a one-time provision; routine startup
  refuses missing stores. Never add `--provision` to normal CMD to hide a lost disk.
- Removal starts with Cole stopping copying/unbinding the Mac, then stopping the
  Compose project, removing the node and dedicated policy grant with approval,
  and revoking auth material. Preserve encrypted data, node state and the offline
  recovery sheet until Cole explicitly authorizes their deletion. `compose down`
  is not authorization to remove host data; do not use broad prune commands.
