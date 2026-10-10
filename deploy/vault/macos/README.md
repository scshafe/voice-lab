# The Air vault (design §4.3, phase 3)

Reviewed templates, **not an installed service**. The MacBook Air keeps the offsite copy
of the PC's library: ciphertext only, refreshed weekly whenever the Air is online, wherever
it is (D-023). It runs the same standard-library vault server as the laptop
(`cole_voice_lab.storage_server`), from a sealed install, as a LaunchDaemon under a
dedicated non-admin account that owns the vault directory; a second, userspace `tailscaled`
with its own state gives it its own tailnet node, `voice-lab-vault-air`, tagged
`tag:voice-lab-vault-air`, serving HTTPS with the node's certificate to the server's
loopback port. The Air's own Tailscale app and Cole's account are not touched. No key ever
reaches the Air: the PC seals every blob before it leaves (D-020).

| File | What it is |
| --- | --- |
| `com.scshafe.voice-lab-vault-air.server.plist.in` | the vault server's LaunchDaemon: the durable launcher below, as the vault account, `127.0.0.1:<port>` only |
| `com.scshafe.voice-lab-vault-air.tailscaled.plist.in` | the second `tailscaled`: userspace networking, its own `--statedir` and `--socket`, as the vault account |
| `run-installed-vault.py` | the durable launcher: runs the mini intake's launcher (`../../macos/run-installed-intake.py`) in `--verify-only` mode over the sealed release, then executes exactly `<release>/bin/python -I -B -m cole_voice_lab.storage_server serve …` with a fixed environment |

Every placeholder (`@VAULT_HOME@` and the like) is filled in step 8. Values written `<…>` are reviewed with Cole and never
committed: the tailnet's name (`<tailnet>.ts.net`), node ids, addresses and hashes.

## Layout on the Air

```text
/opt/voice-lab-vault-air/                         root:wheel 0755
├── launcher/                                      root:wheel 0755, files 0444 (the account cannot change them)
│   ├── run-installed-intake.py                    deploy/macos/run-installed-intake.py
│   └── run-installed-vault.py                     deploy/vault/macos/run-installed-vault.py
├── releases/<application-wheel-sha256>/           _voicelabvault 0755, sealed (deploy/README.md, "Mini intake")
├── config/                                        _voicelabvault 0700
│   └── vault-server-config.json                   0600, token hashes only
├── vault/                                         _voicelabvault 0700 (the vault directory)
│   ├── data/                                      0700, the namespaces' blob and ref stores
│   └── logs/                                      0700, access.jsonl and the daemon's output
└── tailscale/                                     _voicelabvault 0700, the node's state and socket
```

`/opt` is a real directory on the data volume, so FileVault covers all of it, and none of
these paths has a symbolic link component (the launcher refuses one; `/var` and `/tmp` are
links on macOS). Check FileVault first: `fdesetup status` must say it is on.

## Install, step by step

Cole runs these in a private terminal; nothing below prints a token. On the Air, as Cole
(an admin), unless a step says otherwise. Placeholders: `ACCOUNT=_voicelabvault`,
`HOME_DIR=/opt/voice-lab-vault-air`, `PORT=8787`, `<uid>` an unused id below 500.

1. **The account** (non-admin, hidden, no shell, no password, no home):

   ```sh
   ACCOUNT=_voicelabvault HOME_DIR=/opt/voice-lab-vault-air PORT=8787
   dscl . -list /Users UniqueID | sort -nk2 | tail -3   # pick an unused <uid> below 500
   dscl . -list /Groups PrimaryGroupID | sort -nk2 | tail -3
   sudo dscl . -create /Groups/$ACCOUNT PrimaryGroupID '<uid>'
   sudo dscl . -create /Users/$ACCOUNT UniqueID '<uid>'
   sudo dscl . -create /Users/$ACCOUNT PrimaryGroupID '<uid>'
   sudo dscl . -create /Users/$ACCOUNT UserShell /usr/bin/false
   sudo dscl . -create /Users/$ACCOUNT NFSHomeDirectory /var/empty
   sudo dscl . -create /Users/$ACCOUNT RealName 'voice-lab vault (Air)'
   sudo dscl . -create /Users/$ACCOUNT IsHidden 1
   sudo dscl . -create /Users/$ACCOUNT Password '*'
   id $ACCOUNT        # not in admin, staff only by default membership
   ```

2. **The directories:**

   ```sh
   sudo install -d -o root -g wheel -m 0755 $HOME_DIR $HOME_DIR/launcher
   sudo install -d -o $ACCOUNT -g $ACCOUNT -m 0755 $HOME_DIR/releases
   sudo install -d -o $ACCOUNT -g $ACCOUNT -m 0700 $HOME_DIR/config $HOME_DIR/vault \
     $HOME_DIR/vault/data $HOME_DIR/vault/logs $HOME_DIR/tailscale
   ```

3. **Python and tailscaled.** Homebrew's `python@3.12` and the open-source `tailscale`
   formula (it brings `tailscaled`; it is not started as a Homebrew service, and the
   Tailscale app keeps running as it is). Each install is an approved download:

   ```sh
   brew install python@3.12 tailscale
   PY=/opt/homebrew/opt/python@3.12/bin/python3.12
   TAILSCALED=/opt/homebrew/opt/tailscale/bin/tailscaled
   TS=/opt/homebrew/opt/tailscale/bin/tailscale
   ```

   A Homebrew upgrade of `python@3.12` changes the interpreter the release is sealed to, and
   the launcher then refuses to start the server (fail closed): reseal (step 5) after one.

4. **Bring the release over.** On the PC, from a checkout at the reviewed commit, build the
   application wheel (`uv build --wheel`, or the wheel of the PC's current release), and
   collect the pydantic closure for CPython 3.12 on macOS arm64 that
   `requirements/capture-kit-runtime.artifacts.txt` locks (one approved download, as in
   `deploy/vault/README.md`). Copy the wheel, that lock, the wheelhouse,
   `scripts/seal-mini-install`, `deploy/macos/run-installed-intake.py` and the files of this
   directory to a staging directory on the Air that `$ACCOUNT` can read (for example
   `/opt/voice-lab-vault-air-stage`, 0755), and compare the wheel's SHA-256 on both sides.

5. **The sealed release**, as the account (deploy/README.md, "Mini intake", steps 3-4):

   ```sh
   cd /    # every sudo -u below needs a working directory the account can enter
   STAGE=/opt/voice-lab-vault-air-stage
   WHEEL_SHA=$(shasum -a 256 $STAGE/cole_voice_lab-*.whl | cut -d' ' -f1)
   REL=$HOME_DIR/releases/$WHEEL_SHA
   sudo -u $ACCOUNT $PY -m venv $REL
   sudo -u $ACCOUNT $REL/bin/python -m pip install --no-cache-dir --no-index --find-links $STAGE/wheelhouse \
     --require-hashes --only-binary=:all: --no-deps \
     -r $STAGE/capture-kit-runtime.artifacts.txt
   sudo -u $ACCOUNT $REL/bin/python -m pip install --no-cache-dir --no-index --no-deps $STAGE/cole_voice_lab-*.whl
   sudo -u $ACCOUNT /usr/bin/python3 $STAGE/seal-mini-install --release $REL \
     --wheel $STAGE/cole_voice_lab-*.whl --expected-wheel-sha256 $WHEEL_SHA
   ```

   Keep both printed lines (the receipt path and its SHA-256) as deployment evidence; the
   receipt's digest is `<install-receipt-sha256>` below.

6. **The tokens and the server configuration.** On the PC, as in RUNBOOK §9 "The PC (phase
   2)" (a) and (b), but for the Air: into a fresh private staging directory as `voice-lab`,
   then encrypted into `/etc/credstore.encrypted/` and the staging directory shredded. The
   key already exists there; only the Air's two tokens are new:

   ```sh
   vl_stage vault token create --asset-root "$LAB" --destination air --namespace air-lab
   ```

   On the Air, write the two printed hashes (never a token) into
   `$HOME_DIR/config/vault-server-config.json`, mode 0600, owned by `$ACCOUNT`:

   ```json
   {
     "schema_version": "cole-voice-lab.vault-server-config/v1",
     "namespaces": [{"name": "air-lab", "read_token_sha256": "<64hex read hash>",
                     "write_token_sha256": "<64hex write hash>",
                     "quota_bytes": "<reviewed quota, bytes>", "allowed_user_logins": []}],
     "max_workers": 4,
     "request_timeout_seconds": 600
   }
   ```

   (`quota_bytes` is a number; 600 s gives a 16 MiB blob room on a slow DERP path away from
   home.) Then provision the namespace once, as the account, and close without serving:

   ```sh
   sudo -u $ACCOUNT $REL/bin/python -I -B -c "from pathlib import Path; from cole_voice_lab.storage_server import VaultServer, load_server_configuration; s=VaultServer(('127.0.0.1',0), config=load_server_configuration(Path('$HOME_DIR/config/vault-server-config.json')), data_root=Path('$HOME_DIR/vault/data'), access_log=Path('$HOME_DIR/vault/logs/access.jsonl'), provision=True); s.server_close()"
   ```

7. **The launchers**, root-owned and read-only, then checked as the account:

   ```sh
   sudo install -o root -g wheel -m 0444 $STAGE/run-installed-intake.py \
     $STAGE/run-installed-vault.py $HOME_DIR/launcher/
   RECEIPT=$REL/INSTALL_RECEIPT.json
   RECEIPT_SHA='<install-receipt-sha256 from step 5>'
   sudo -u $ACCOUNT /usr/bin/python3 $HOME_DIR/launcher/run-installed-vault.py \
     --receipt $RECEIPT --expected-receipt-sha256 $RECEIPT_SHA \
     --config $HOME_DIR/config/vault-server-config.json --data-root $HOME_DIR/vault/data \
     --access-log $HOME_DIR/vault/logs/access.jsonl --port $PORT --verify-only
   ```

   It prints `"verified": true` and the exact server command; nothing is started.

8. **The two LaunchDaemons**, rendered, linted and loaded:

   ```sh
   render() {
     sed -e "s|@VAULT_USER@|$ACCOUNT|g" -e "s|@VAULT_HOME@|$HOME_DIR|g" \
         -e "s|@VAULT_PORT@|$PORT|g" -e "s|@TAILSCALED@|$TAILSCALED|g" \
         -e "s|@INSTALL_RECEIPT@|$RECEIPT|g" -e "s|@INSTALL_RECEIPT_SHA256@|$RECEIPT_SHA|g" "$1"
   }
   for name in server tailscaled; do
     render $STAGE/com.scshafe.voice-lab-vault-air.$name.plist.in > /tmp/vault-air-$name.plist
     plutil -lint /tmp/vault-air-$name.plist
     ! grep -n '@[A-Z_]*@' /tmp/vault-air-$name.plist
     sudo install -o root -g wheel -m 0644 /tmp/vault-air-$name.plist \
       /Library/LaunchDaemons/com.scshafe.voice-lab-vault-air.$name.plist
     rm /tmp/vault-air-$name.plist
   done
   sudo launchctl bootstrap system /Library/LaunchDaemons/com.scshafe.voice-lab-vault-air.server.plist
   sudo launchctl bootstrap system /Library/LaunchDaemons/com.scshafe.voice-lab-vault-air.tailscaled.plist
   curl -fsS http://127.0.0.1:$PORT/healthz; echo     # ok
   sudo lsof -nP -iTCP:$PORT -sTCP:LISTEN               # 127.0.0.1 only
   ```

   Whether `tailscaled` runs as a non-admin account in userspace networking on this macOS is
   checked here, before the join: `sudo -u $ACCOUNT $TS --socket=$HOME_DIR/tailscale/tailscaled.sock
   status` must answer (logged out). If it does not, stop and decide with Cole; do not run
   it as root without a reviewed change to this file.

9. **The tailnet.** In infra's `tailnet/policy.hujson` (reviewed and applied as infra does it):
   `tag:voice-lab-vault-air` owned by Cole, one grant from the PC's node to
   `tag:voice-lab-vault-air` on `tcp:443`, nothing with the tag as its source; the cases of
   `../tailnet-policy.md`, for the Air's tag. In infra's `stacks/voice-lab/host.conf`, add
   `voice-lab-vault-air.<tailnet>.ts.net:443` to `HOST_EGRESS_TAILNET`. Then Cole creates a
   **single-use, non-ephemeral** auth key tagged `tag:voice-lab-vault-air`, with the shortest
   expiry, into a file only the account can read, and the node joins and serves:

   ```sh
   TSOCK=$HOME_DIR/tailscale/tailscaled.sock
   sudo install -o $ACCOUNT -g $ACCOUNT -m 0600 /dev/null $HOME_DIR/tailscale/join.key
   # paste the key into that file with an editor run as root; never on a command line
   sudo -u $ACCOUNT $TS --socket=$TSOCK up --hostname=voice-lab-vault-air \
     --advertise-tags=tag:voice-lab-vault-air --auth-key=file:$HOME_DIR/tailscale/join.key \
     --accept-dns=false --accept-routes=false
   sudo rm -P $HOME_DIR/tailscale/join.key
   sudo -u $ACCOUNT $TS --socket=$TSOCK serve --bg --https=443 http://127.0.0.1:$PORT
   sudo -u $ACCOUNT $TS --socket=$TSOCK status --json | \
     /usr/bin/python3 -c 'import json,sys; s=json.load(sys.stdin)["Self"]; print(s["ID"], s["DNSName"], s.get("Tags"))'
   ```

   The node must be `voice-lab-vault-air.<tailnet>.ts.net.` with exactly
   `tag:voice-lab-vault-air` (a `-1` suffix means a stale registration: resolve it first).
   Record its StableID; the first HTTPS request may take about 40 s for the certificate.

10. **Approve and bind the Air on the PC** (RUNBOOK §9 (c), with the Air's values; the tag is
    named, and `memos` is opted in):

    ```sh
    approval=(--destination air --approval-id "air-<date>"
      --endpoint "voice-lab-vault-air.<tailnet>.ts.net" --tailnet-suffix "<tailnet>.ts.net"
      --node-stable-id "<StableID from step 9>" --tag tag:voice-lab-vault-air
      --namespace air-lab --classes memos prompt-database recordings records
      --expires-at "<timezone-aware expiry, at most 366 days on>" --approved-by Cole
      --statement "I approve encrypted storage of this library and its memos on the Air.")
    vl vault approve --asset-root "$LAB" --preview "${approval[@]}"
    vl vault approve --asset-root "$LAB" "${approval[@]}" --approved-at '<from the preview>' \
      --reviewed-approval-sha256 '<from the preview>' --i-approve-this-destination
    vl vault bind --asset-root "$LAB" --destination air --backend tailnet-vault \
      --endpoint "voice-lab-vault-air.<tailnet>.ts.net" --namespace air-lab \
      --classes memos prompt-database recordings records \
      --approval-id "air-<date>" --approval-sha256 '<from the preview>'
    ```

11. **The first, full seeding, at home** with the Air awake (`caffeinate -dis` in a terminal
    on the Air), by hand on the PC with the Air's credentials loaded (`vl_creds` of RUNBOOK
    §9 with `vault-destination.air.*` in place of `laptop`), then the verify and a drill:

    ```sh
    vl_creds vault push --asset-root "$LAB" --destination air --memos-root /data/voice-lab/memos/phone
    vl_creds vault verify --asset-root "$LAB" --destination air
    vl_creds vault drill --asset-root "$LAB" --destination air --accept-latest \
      --target "/data/voice-lab/drill/air-$(date +%F)" \
      --memos-target "/data/voice-lab/drill/air-memos-$(date +%F)"
    ```

    If the Air sleeps part-way, run the push again: it resumes from the journal. Remove the
    drill's plaintext copies once the result is recorded.

12. **The copier on the PC:** install `deploy/host-manual/voice-lab-vault-air.{service,timer}`
    by hand (`deploy/host-manual/README.md`). The acceptance drill of phase 3 is step 11's
    drill run again while the Air is away from home.

## Operation and removal

- The server and `tailscaled` start at boot and stay up; while the Air sleeps the PC's
  hourly run sees it offline and does nothing. `vault staleness` on the PC flags the copy
  after 14 days.
- Logs: `$HOME_DIR/vault/logs/access.jsonl` (rotate by copy-truncate, as
  `../README.md` says), the daemons' output beside it and in `$HOME_DIR/tailscale/`.
- Upgrading the server: a new sealed release (steps 4-5), its receipt into the server plist
  (step 8, `launchctl bootout` then `bootstrap`), the old release removed afterwards.
- Unbinding the Air leaves the laptop's copy untouched: on the PC, `systemctl disable --now
  voice-lab-vault-air.timer`; on the Air, `launchctl bootout` both daemons, `tailscale logout`
  through the account's socket, and the node and its grant removed from the policy. The
  ciphertext and the account are deleted only when Cole decides to.
- Losing the Air costs a copy, not privacy: it holds ciphertext and no key (D-023).
