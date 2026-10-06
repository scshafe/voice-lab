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
| 4 | `deploy/stack/`, `deploy/host/`, `dev.toml` `kind = "service"` with `[deploy]` | voice-lab | in review |
| 5 | Vault on Linux: credential key store, Linux tailscale binary, per-destination bindings | voice-lab | in review |
| 6 | `pc` as an alias of `arch` | scshafe-dev | done: #36, `v0.10.4`, installed for agents |
| 7 | Tailnet tags and grants, `stacks/voice-lab/host.conf` | infra (unsigned) | done: `ff9144a`, policy applied |
| 8 | Runner on the PC, entrypoint host-unit install, `voice-lab` user, `/data/voice-lab` subvolumes | infra `host/` (signed) | todo, stops at the hash |
| 9 | Live: Pocket ID clients, PC layout, laptop vault stack, synthetic sessions, restore drill | hosts | todo |
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
  - The client sends the Pocket ID **ID token** (audience `voice-lab-desktop`), as
    voice-journey-capture does when the access token is not a JWT; only the refresh token
    is kept, in the OS keychain.
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
