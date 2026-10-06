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
| 1 | Mini constants out of `sync.py` (rsync lane bound to the SSH alias) | voice-lab | in progress |
| 2 | `/api/v1` (`meta`, `intake`), upload staging under `incoming/`, intake publisher, token checks | voice-lab | todo |
| 3 | The standing home-host approval (D-021), its ledger, and the HTTPS upload client | voice-lab | todo |
| 4 | `deploy/stack/`, `deploy/host/`, `dev.toml` `kind = "service"` with `[deploy]` | voice-lab | todo |
| 5 | Vault on Linux: credential key store, Linux tailscale binary, per-destination bindings | voice-lab | todo |
| 6 | `pc` as an alias of `arch` | scshafe-dev | todo |
| 7 | Tailnet tags and grants, `stacks/voice-lab*/host.conf`, docs | infra (unsigned) | todo |
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
