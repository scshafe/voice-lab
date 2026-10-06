# Phase 2 progress log

Phase 2 of [VOICE-PLATFORM.md](VOICE-PLATFORM.md) (§9): the PC backend foundation, with
the intake moved off the Mac mini. Approved by the owner on 2026-10-06. This log is
written as the work goes, so that a successor can resume it. Newest entries last.

Rules that bind this work: recordings and consent evidence never enter Git
(`scripts/guard-private-assets` before every push); only synthetic audio is used; the
real asset roots (the Air's `~/.local/share/cole-voice-lab`, the PC's
`/data/backups/voice-lab`, `/srv/voice-journey`) are never read; the Mini's intake is
not retired until every acceptance item passes; changes to infra's signed `host/` tree
stop at the owner's manifest-hash approval.

## Plan

| Step | What | Where | State |
| --- | --- | --- | --- |
| 1 | Mini constants out of `sync.py` (rsync lane bound to the SSH alias) | voice-lab | done, #5 |
| 2 | `/api/v1` (`meta`, `intake`), upload staging under `incoming/`, intake publisher, token checks | voice-lab | done, #6 |
| 3 | The standing home-host approval (D-021), its ledger, and the HTTPS upload client | voice-lab | done, #6 |
| 4 | `deploy/stack/`, `deploy/host/`, `dev.toml` `kind = "service"` with `[deploy]` | voice-lab | done, #7 (#8: bearer token) |
| 5 | Vault on Linux: credential key store, Linux tailscale binary, per-destination bindings | voice-lab | done, #9 (#10: PC lock, classification) |
| 6 | `pc` as an alias of `arch` | scshafe-dev | done: #36, `v0.10.4`, installed for agents |
| 7 | Tailnet tags and grants, `stacks/voice-lab/host.conf` | infra (unsigned) | done: `ff9144a`, policy applied |
| 8 | Runner on the PC, entrypoint host-unit install, `voice-lab` user, `/data/voice-lab` subvolumes | infra `host/` (signed) | `6df55a38…59fb` installed 2026-10-06 (runners, provisioning, snapshots); the takeover is **blocked on a resolver bug in it**: fix `4e6020d3…761e` awaits the owner's approval |
| 9 | Live: Pocket ID clients, PC layout, laptop vault stack, synthetic sessions, restore drill | hosts | done as a development deployment (below) |
| 10 | One real session from the Air | owner | open |

## Entries

### 2026-10-06

- Design PR #4 merged (`97ffa5b`). Baseline `scripts/verify` on the PC: green except
  three `tests/test_workers.py` tests that need `pip` in the virtual environment (a
  `uv venv` has none; `uv pip install pip` fixes it). The full suite takes about 21
  minutes on the PC.
- Mini check (read-only): `~/.local/lib/cole-voice-lab` and its asset root exist; no
  intake LaunchAgent is loaded. Nothing there is changed by this phase.
- Step 1 landed: voice-lab #5 (`56b3858`).
- Steps 2 and 3, built together (one PR): `cole_voice_lab.api` (contracts, staging, auth,
  server), `intake_publisher`, `home_host` (approval, binding, ledger, sign-in settings),
  `oidc` (PKCE sign-in, keychain stores, `fetch-jwks`), `intake_client`, `tailnet` (the
  identity check, moved out of `storage_http_client` so the intake client imports no
  storage adapter), `synthetic_capture`, the CLI (`home-host approve|show|sign-in|
  fetch-jwks`, `intake upload|publish|synthetic`), schemas and `docs/HOME_HOST_INTAKE.md`.
  Choices made inside the design, recorded here:
  - The upload staging lives in `incoming/.uploads/<id>/` and a finalized package is
    renamed to `incoming/<id>`, so the API's only writable mount is `incoming/` (design
    §8.1 `HOST_BINDS`) and the publisher is the existing `intake scan` rule (dot entries
    skipped) around the unchanged `accept_capture_session`. The design's "FINALIZE file"
    trigger (§3.2) is this rename, which the path unit sees.
  - A package the publisher refuses is moved untouched to `incoming/.refused/` with a
    record in `provenance/intake/refusals/`; the API reports it as `refused`.
  - The client sends a Pocket ID token for `voice-lab-desktop`: the access token when it
    is a JWT for that client (as voice-journey-capture does), else the ID token (changed
    in #8 from "the ID token", so a refresh without an ID token still works); only the
    refresh token is kept, in the OS keychain (tested on the Air's login Keychain with a
    throwaway item).
  - JSON Schemas are `schemas/api-v1-*.schema.json` (flat, because
    `cole_voice_lab.schema --check` treats a subdirectory as drift), not
    `schemas/api/v1/`.
- Step 4: `Dockerfile` (the API image: wheel + hash-locked pydantic closure),
  `deploy/stack/` (sidecar `voice-lab`, the Pocket ID door, the API on `10.253.91.0/29`
  and `.8/29`), `deploy/host/` (intake path/service/timer, snapshot, JWKS refresh),
  `dev.toml` `kind = "service"` with the runner-lane `[deploy]` (`host = "pc"`,
  `verify_on = "pc"`), and `dev adopt --add-missing`'s `deploy.yml` with a uv toolchain
  block. Found: infra's validator (V-06) refuses `TS_EXTRA_ARGS`, so the node's
  `tag:voice-lab` comes from its auth key, not `--advertise-tags`.
- **Deviations, recorded as D-033:** the lab (whole asset root) is the btrfs subvolume
  that is snapshotted, because the validator's atomic publish needs `incoming/`, `tmp/`
  and `raw/` on one `st_dev`; raw files stay `0400`/`0500` (read-only, owner-only), not
  `0444`.
- Steps 2 and 3 landed: voice-lab #6 (`64d8b72`). scshafe-dev #36 (`d1d6544`, tag
  `v0.10.4`): `host = "pc"` is the registry key `arch`; installed for the project agents
  with `scshafe-agent-setup dev v0.10.4`.
- infra `ff9144a` (main): `tag:voice-lab` and `tag:voice-lab-vault` (owner
  `autogroup:admin`), grants `autogroup:member → tag:voice-lab:443` and
  `fleet-watch-arch → tag:voice-lab-vault:443`, with policy tests; applied to the live
  policy as only these additions (the pending `fleet-watch-laptop` rename, which lands at
  the laptop's reinstall, was left unapplied). The allow-all grant still applies until the
  fleet narrows it. `stacks/voice-lab/host.conf` validated in deploy mode.
- Pocket ID: public client `voice-lab-desktop` (PKCE, `http://127.0.0.1:*/callback`,
  group `cole_default`, tokens 60 min / refresh 30 days), as `voice-journey-capture`.
  The confidential door client `voice-lab` is made when the production stack starts (its
  secret is streamed straight into the stack's `.env`).
- **Development deployment on the PC (not the final one).** Until infra's host tree is
  approved (D-032), by hand and recorded here:
  - `scshafe-voice-lab-provision` (draft, to be signed in infra) made the `voice-lab`
    user (uid 951, gid 950), the subvolumes `/data/voice-lab` and `/data/voice-lab/lab`,
    and an **acceptance** copy `/data/voice-lab-acceptance` that only synthetic sessions
    ever enter (so the real raw store holds no test audio);
  - a dev release venv `/opt/voice-lab/releases/<wheel-sha256>` (uv-managed Python 3.12
    in `/opt/voice-lab/python`), `current` → it;
  - the stack in `/srv/voice-lab-dev` (cole, 0700): `deploy/stack/` plus
    `compose.acceptance.yaml`, which serves the acceptance root, trusts only an
    acceptance test issuer (a throwaway key in the session scratchpad; subject
    `synthetic-acceptance`) and bypasses the door (the door admits only Pocket ID
    tokens). Node `voice-lab`, StableID `nMDiVAZnG111CNTRL`, tags `tag:voice-lab`,
    `tag:container`, from a one-off 1 h key (id `kdBtpxm4Vd11CNTRL`), blanked after the
    join. **The runner lane's takeover must keep this node identity** (copy
    `state/tailscale` into `/srv/stacks/voice-lab/state/tailscale` after stopping the dev
    stack), or every approval that binds the StableID must be renewed;
  - the acceptance publisher as a transient path unit
    (`systemd-run --unit voice-lab-acceptance-intake --path-property=PathChanged=…`,
    `User=voice-lab`, `PrivateNetwork=yes`, `ProtectSystem=strict`).
- **Acceptance, Linux (the PC as the capturing host, over the tailnet, through the
  node's TLS name and the identity check):**
  - `linux-synthetic-001` (4 takes) accepted; receipt manifest
    `d6f985f5…f12b7b8` = the client's; raw files `-r--------` in `dr-x------` dirs owned
    by `voice-lab`; a read-only snapshot of the acceptance lab taken after it
    (`touch` in it: "Read-only file system");
  - resume: `linux-synthetic-003` (20 files, 220 MB) killed (`SIGKILL`) with 38.9 MB
    held; the rerun resumed and was accepted (`1e5e2b0c…ede0ecb` = the client's);
  - refused at the client before any byte: a tampered take ("recording hash or byte
    count was tampered"), an undeclared file ("undeclared=['audio/extra.wav']"), a
    symlinked take ("package member is not a regular file"); nothing in the ledger;
  - refused by the server: bytes that do not hash to their declaration (422
    `checksum_mismatch`, discarded), an undeclared path (404 `file_not_declared`),
    finalize while incomplete (409), a forged token (401);
  - refused by the publisher, moved to `incoming/.refused/` with a record: a package with
    an extra file, a tampered take, and a symlink in place of the package directory.
- **Acceptance, macOS (the Air as the capturing host, a scratch client root and runtime in
  `~/.local/share/voice-lab-phase2/` on the Air, never its asset folder):**
  - found and fixed: the macOS Tailscale app binary answers as the CLI only when `SHLVL`
    is set; the resolver's fixed environment (`PATH`, `HOME`) made it try to start the GUI
    ("The Tailscale GUI failed to start") from a terminal, SSH or launchd alike. D-020's
    vault client had the same latent fault (never deployed). `tailnet._tailscale_environment`
    now sets `SHLVL=1`;
  - `macos-synthetic-001` accepted (receipt `2b8fb879…a66c43` = the client's); raw files
    `-r--------` owned by `voice-lab`;
  - resume: `macos-synthetic-002` (12 takes × 60 s) killed with 8.4 MB held; the rerun
    was accepted (`25163811…6f1584` = the client's); ledger: 5 entries, chained;
  - refused at the client: tampered, undeclared and symlinked takes, as on Linux.
  - A second read-only snapshot of the acceptance lab holds all five accepted sessions.
- Step 4 landed: voice-lab #7 (`b4965ec`). A push to `main` queues `deploy.yml`, which
  waits for the PC's runners that do not exist yet; such runs are cancelled by hand until
  the host tree is installed (the first deploy is the takeover dispatch anyway).
- Step 5, the vault side of phase 2 (D-034), built on the step-4 branch and verified with
  synthetic data only; nothing deployed, no key or token created:
  - **Per-destination records.** `provenance/vault/destinations/<destination>/` holds each
    destination's binding, approvals, state, manifest copies, ledger, request log and
    `.push.lock`; `library.json` stays per library. `StorageBinding` and
    `VaultDestinationApproval` are `v2` and name their destination. Every vault command
    but `init` and `key` takes `--destination` (default `default`, which the Mac's
    recording service, its native fixtures and the existing tests use unchanged).
  - **Tokens per destination**, named
    `private.cole.voice-lab.vault-destination.<destination>.<library_id>.(token|read-token)`;
    the key stays `private.cole.voice-lab.vault.<library_id>.key`. The destination comes
    first because `ImportCredential=` takes only a trailing `*` (checked on the PC's
    systemd 262 with a throwaway user unit: only the globbed items arrive, `%i` expands in a
    glob, and the directory is read-only).
  - **The Linux keychain**, `SystemdCredentialKeychain` over `$CREDENTIALS_DIRECTORY`,
    chosen by `keychain_client()` off macOS; it was also run under a real transient user
    unit (reads the imported item; an add is refused, "Permission denied").
  - **`deploy/host/voice-lab-vault-laptop.service` and `.timer`** (fixed names: infra's
    host-unit validator refuses templates for now; the Air gets its own pair): push then verify as
    `voice-lab`, tailnet-only network, the intake publisher's hardening;
    `systemd-analyze verify` clean, `systemd-analyze security` 2.9 ("OK").
  - **`vault drill`**: restore into an empty target, then re-hash every restored file
    from disk against the manifest. `tests/test_vault_destinations.py` runs the whole
    Linux flow (credentials created in a staging directory, made read-only, two
    destinations on a loopback vault, push, verify, drill, restore by endpoint with the
    read token).
  - The owner's steps (RUNBOOK §9 "The PC (phase 2)"): create the library identity, key
    with recovery sheet and the laptop's tokens; encrypt them; approve and bind `laptop`;
    the first push, verify and drill by hand; enable `voice-lab-vault-laptop.timer` once
    the runner lane installs `deploy/host/`.
- Step 5 landed: voice-lab #9 (`ae9c82f`, D-034). Found while deploying it: the PC's
  release needs the vault extra's encryption closure, which had only a macOS lock
  (`ModuleNotFoundError: cryptography`); #10 adds `requirements/home-host-linux.txt`
  (pydantic + cryptography closures, CPython 3.12, Linux x86_64; every digest matched
  PyPI) for the home host's release, and classification rules for `provenance/intake/**`
  and `provenance/home-host/**` (both stay local, like `provenance/vault`), without which a
  push refuses the PC's lab ("no classification rule covers provenance/intake/…").
  infra's branch `voice-lab-pc-host` now names the new lock in `stacks/voice-lab/host.conf`
  (`b5a512c`; the signed manifest is unchanged).
- **infra host tree (step 8)**: branch `voice-lab-pc-host`, head `d2a4d01`, re-signed;
  `platform/M0.sha256` = `a9fd456da44ae5ccfc4aaecdf3acb8029e92117de218dd3d731c4e82d961d614`
  (previous `9c5ba124…afe2a`). It adds `scshafe-host-release` (sealed wheel releases and
  validated app units: user `voice-lab`, no root-escalating exec prefixes, binaries only
  under `/opt/voice-lab/current/`, no templates yet), `scshafe-deploy` reading
  `HOST_UNITS` from host.conf, `scshafe-runner-install --platform arch` (prod runner
  `pc-voice-lab-prod`, verify runner `ci-voice-lab` with uv), the Arch runner egress table
  and sudoers line, `scshafe-voice-lab-provision` and `scshafe-voice-lab-snapshot` with its
  `.path` (on `lab/raw`) and nightly `.timer`. **Not installed; it waits for the owner's
  approval of that hash.** Install steps: infra `docs/platform/agent-deploy.md`, "The PC as
  a second production host".
- **The laptop vault (development deployment, by hand per `deploy/vault/README.md`):**
  `/srv/voice-lab-vault/` on the laptop's `/srv` (xfs, 76 GB free): the vault image built
  on the PC from `ae9c82f`'s wheel (`sha256:e794a439…e01e`, loaded with `docker save |
  docker load`), the hashed server config (namespaces `pc-lab` 50 GiB, `acceptance`
  2 GiB), node `voice-lab-vault`, StableID `nyFbE4WXss11CNTRL`, `tag:voice-lab-vault`
  only, from a single-use 1 h key (id `kFEa7bzXv821CNTRL`); the key file was shredded and
  the sidecar recreated without the join override. Nothing else on the laptop changed.
- **The PC's library (production, `/data/voice-lab/lab`):** library
  `lib-20261006-df017dd15db9`, key fingerprint `a4970dc9408adf64`; the key and the
  laptop's tokens created into a `/run` staging directory as `voice-lab`, encrypted with
  `systemd-creds` into `/etc/credstore.encrypted/`, the staging directory shredded. The
  **recovery sheet** is at `/root/voice-lab-library-recovery-sheet-lib-20261006-df017dd15db9.txt`
  (root, 0400): the owner stores it offline (one copy away from the house) and then
  shreds that file. `systemd-creds` warned that the host's credential secret is not on
  encrypted media (the PC's root disk is not encrypted): the credentials are as safe as
  root on the PC. Destination `laptop` approved (until 2027-10-06) and bound (classes
  records, prompt-database, recordings, derived-edits; not training). Generation 1
  pushed and verified (empty library: 0 files). `voice-lab-vault-laptop.service` and
  `.timer` installed by hand (development) and run once successfully with
  `ImportCredential=`; nightly at 02:30.
- **Restore drill (synthetic, the acceptance library `lib-20261006-df005eb8831d`,
  namespace `acceptance`):** generation 1 (68 files, 373 MB, all five synthetic sessions)
  pushed in 15 s, verified (72 present, 0 missing, 0 mismatch), then `vault drill` restored
  it into an empty target and re-hashed every file from disk: 72/72, 375,031,930 bytes,
  `ok: true`; each restored `raw/<manifest>/` also re-validated as an accepted package
  (receipt and intake QC). The drill copy was removed and the acceptance credentials
  (plaintext, `/run`) shredded. The drill of the **real** session waits for that session.
- **The approved host tree.** The owner approved infra's manifest
  `6df55a3849d321f74eb36ace3a1d351c24f529eab1615b77c418c9ae61dd59fb` (infra `main` at
  `5410973`, after two security reviews), staged root-owned in `/root/scshafe-m0` with that
  hash. Its tightened validator needs two changes here (this PR):
  - `voice-lab-jwks.service` adds `IPAddressDeny=any` and
    `IPAddressAllow=100.64.0.0/10 fd7a:115c:a1e0::/48` (infra `host.conf` grants
    `HOST_NETWORK=voice-lab-jwks.service:tailnet`); the file is now byte-identical to infra's
    test fixture.
  - `voice-lab-vault-laptop.{service,timer}` moved to `deploy/host-manual/`: the validator
    refuses `ImportCredential=`. The pair stays installed by hand, as it has run since the
    vault went live (same unit text), and the nightly copy keeps running; infra's
    `host.conf` names `voice-lab-vault.colobus-stargazer.ts.net:443` in
    `HOST_EGRESS_TAILNET` so the service user's egress table still lets it reach the vault.
    **Follow-up:** a credentials rule in infra's validator (an `ImportCredential=` glob
    allowlist per app, read from `host.conf`), then the pair moves back to `deploy/host/`.
- **Install and the first takeover attempt (2026-10-06, afternoon).** From infra `main`
  5410973, staged root-owned in `/root/scshafe-m0`; the staged `M0.sha256` hashed to the
  approved `6df55a38…59fb`. Installed per infra's "The PC as a second production host":
  - packages `lttng-ust` and `uv` (pacman; nothing else pulled in); the production infra
    checkout `~/infra`; `stack-backup` and its units; `/srv/scshafe-backup/stacks`;
  - `scshafe-voice-lab-provision layout` (nothing to change: the development layout, uid 951
    and gid 950, was accepted) and `install`: `scshafe-voice-lab-snapshot`, its `@.service`,
    `.path` and `.timer` (enabled, active), `/etc/voice-lab/host.env` (the template; same
    issuer value). The nightly instance left `/data/voice-lab/.snapshots/lab/20261006T193206Z`,
    `ro=true`; the publish instance runs and correctly takes none while `raw/` is unchanged;
  - `scshafe-host-release runtime`: 3.12.14 already there (sha256 `5ebcd799…f779`);
  - `/srv/stacks/voice-lab` (`stack init`); every `.env` value moved from
    `/srv/voice-lab-dev/.env` with `stack env-set` (values never shown; `TS_AUTHKEY` left
    unset: the node identity is joined); the release `stack.toml` (0444);
  - runners (2.337.0, sha256 `70920811…6613`; dry runs read first, then one registration
    token on stdin, shredded after): `pc-voice-lab-prod` (`run-voice-lab`, uid 62000, in
    `scshafe-runners`) and `pc-voice-lab-verify` (`ci-voice-lab`, uid 62001, no sudo), both
    online. Each install's gate passed: loopback and the PC's tailnet address refused,
    api.github.com reachable; the `.env`/`.path` guard and the hook drop-in in place. The
    queued push run of #11 was picked up by the verify runner (so the pre-job hook admits
    `deploy.yml` on `main`) and cancelled before the takeover.
  - **Takeover, refused at 5b/8, nothing deployed.** The dev stack was stopped,
    `state/tailscale` copied into `/srv/stacks/voice-lab/state/tailscale` (identical bytes,
    owners and modes; the profile's NodeID is `nMDiVAZnG111CNTRL`), the dev stack started
    again (the pre-deploy backup reads the identity through `docker exec ts-voice-lab`),
    the hand-installed intake and JWKS units removed, and the read-only
    `scshafe-host-release check` at `2f69633` passed. The dispatch (run 37528565746,
    `allow_rollback=true`) stopped in `prepare`: `HOST_EGRESS_TAILNET
    id.colobus-stargazer.ts.net resolves to ::ffff:100.124.169.100, outside the tailnet`.
    MagicDNS on the PC answers no AAAA for tailnet names, and `getent ahostsv6` then
    returns the IPv4 address v4-mapped. The development deployment was put back at once:
    the hand units reinstalled and active, `current` unchanged, the dev stack serving
    (`/healthz` 200, `/api/v1/meta` 401 without a Pocket ID session).
  - The approved tool's `prepare` was then run once with the egress hosts left out (a scratch
    `host.conf`, the hand units out for the minute it ran): build, offline install and seal
    all passed (release `628d1948…f24f6a`, 853 entries); its staged `pending` was removed.
  - **The fix**: infra branch `host-release-v4mapped` (`df0a2d4`), where a v4-mapped answer
    counts as its IPv4 address, still checked against 100.64.0.0/10. Re-signed: `M0.sha256`
    `4e6020d30b146d0fc01af11a916bc68bacbee7503917b42f2228c50df00c761e`, not installed.
  - The vault copier pair was reinstalled by hand from `deploy/host-manual/` (unit body
    unchanged; the nightly timer stays enabled). Until the takeover, a push to `main` runs
    verify on `pc-voice-lab-verify` and then the deploy job is refused at stage 4 ("cannot
    determine the live commit"), as expected before a takeover.

## Open items and the owner's steps

1. **Approve the fixed host tree**, `M0.sha256` `4e6020d3…761e` (infra branch
   `host-release-v4mapped`; it differs from the installed `6df55a38…59fb` only in
   `bin/scshafe-host-release`). Then a session stages it and re-runs the prod runner's
   installer with `--apply` (it installs the new `scshafe-host-release`; the account and the
   registration stay), and redoes the takeover: stop the dev stack, **copy
   `state/tailscale` again** (the copy made on 2026-10-06 may be stale by then), start the
   dev stack, remove the hand intake and JWKS units, `check`, dispatch with
   `allow_rollback=true`. After it is green, remove the development pieces:
   `/srv/voice-lab-dev`, the hand-installed `voice-lab-intake.*` and `voice-lab-jwks.*`
   (not `voice-lab-vault-laptop.*`, which stay installed by hand; `deploy/host-manual/`),
   `/usr/local/sbin/scshafe-voice-lab-provision-dev` and the hand-made releases in
   `/opt/voice-lab/releases/` (`aa459aa6…`, `e3cbdec1…`). The transient
   `voice-lab-acceptance-intake.path` is already gone.
2. **Store the recovery sheet** offline, one copy away from the house; then
   `sudo shred -u /root/voice-lab-library-recovery-sheet-lib-20261006-df017dd15db9.txt`.

### Owner: the one real pilot session from the Air (acceptance item, open)

Prepared: a phase-2 client runtime on the Air at `~/.local/share/voice-lab-phase2/runtime`
(voice-lab from `main`, pydantic from PyPI; nothing else on the Air changed; its scratch
`acceptance-client/` and `macos-synthetic-*` hold synthetic data only and may be deleted).
The PC serves `https://voice-lab.colobus-stargazer.ts.net` (node StableID
`nMDiVAZnG111CNTRL`) in production mode: Pocket ID door, the real asset root
`/data/voice-lab/lab`, the intake publisher unit.

In a Terminal on the Air (not over SSH), with `ASSET_ROOT` the private asset root that holds
the session (the one the recording app or `capture console` writes to):

1. Record a short pilot session as usual and finalize it, so that a package with `READY`
   exists, e.g. `$ASSET_ROOT/tmp/capture-<session-id>` (the app's package step, or
   `capture console` → Finalize). Check it: `VL=~/.local/share/voice-lab-phase2/runtime/bin/voice-lab;
   $VL intake validate "$ASSET_ROOT/tmp/capture-<session-id>"`.
2. Approve the home host once (D-021). Run with `--preview`, read the record (endpoint,
   StableID, tag, classes, expiry), then run the same command without `--preview` plus
   `--reviewed-approval-sha256 <printed approval_sha256> --i-approve-this-destination`.
   Confirm the StableID yourself first: `tailscale status --json | grep -A3 '"voice-lab.colobus'`
   or in the admin console (machine `voice-lab`, tags `tag:voice-lab`, `tag:container`).

   ```sh
   $VL home-host approve --asset-root "$ASSET_ROOT" --approval-id pc-home-host-2026 \
     --endpoint voice-lab.colobus-stargazer.ts.net --tailnet-suffix colobus-stargazer.ts.net \
     --node-stable-id nMDiVAZnG111CNTRL --classes capture \
     --approved-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --expires-at 2027-10-05T00:00:00Z \
     --approved-by Cole --statement "My own captures, to my own PC (D-021)." --preview
   ```
   (Use the same `--approved-at` value in both runs: copy it from the preview.)
3. Sign in once: `$VL home-host sign-in --asset-root "$ASSET_ROOT" --issuer https://id.colobus-stargazer.ts.net`
   (a browser opens Pocket ID; use your passkey; the refresh token goes to the login Keychain).
4. Upload: `$VL intake upload "$ASSET_ROOT/tmp/capture-<session-id>" --asset-root "$ASSET_ROOT"`.
   It prints the `accepted` status; its `receipt.manifest_sha256` equals the local
   `intake validate` digest. If interrupted, run it again; it resumes. The take stays on
   the Air.
5. Tell the next session the session id and the receipt's `manifest_sha256`, so it can run
   the first sealed vault generation and the restore drill of that session (the last
   acceptance item).

6. **After the session (any later session can do it):** push and drill the session from the
   laptop vault, as `voice-lab` with the credentials loaded:

   ```sh
   vl_creds() { sudo systemd-run --pipe --wait --collect --quiet -p User=voice-lab -p Group=voice-lab \
     -p UMask=0077 -p WorkingDirectory=/ -p 'ImportCredential=private.cole.voice-lab.vault.*' \
     -p 'ImportCredential=private.cole.voice-lab.vault-destination.laptop.*' /opt/voice-lab/current/bin/voice-lab "$@"; }
   vl_creds vault push --asset-root /data/voice-lab/lab --destination laptop     # note generation + manifest_sha256
   vl_creds vault verify --asset-root /data/voice-lab/lab --destination laptop
   sudo install -d -o voice-lab -g voice-lab -m 0700 /data/voice-lab/drill
   vl_creds vault drill --asset-root /data/voice-lab/lab --destination laptop \
     --target /data/voice-lab/drill/$(date +%F) --generation <N> --manifest-sha256 <D>
   # ok: true and rehashed_files == restored_files, and raw/<the session's manifest>/ among them;
   sudo rm -rf /data/voice-lab/drill/$(date +%F)
   ```

The Mini's intake is untouched (no LaunchAgent was loaded there before this phase either);
the rsync lane to it stays usable through the `cole-voice-lab-mini` SSH alias with
`--remote-user`/`--remote-root`. Retire it only after every item above passes.
