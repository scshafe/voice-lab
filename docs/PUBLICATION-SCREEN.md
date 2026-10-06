# Publication screen: scshafe/voice-lab, 2026-10-05

This is a read-only screen of the private repository `scshafe/voice-lab`
(renamed on 2026-10-05 from `scshafe/cole-voice-lab`), done before the owner
decides whether to make it public with its full history. It follows the method
of infra's `docs/platform/repo-publication-screen-2026-10-05.md`. **Nothing was
changed:** no visibility, history, branch, setting, issue or file.

Every value here is redacted to type, location and at most the first and last
two characters. The screen found **no real credential** and **no private asset**
(recording, weight, consent record) anywhere in history.

## Status (2026-10-05, after the tree-only fixes)

The screen below describes `main` at 77ca43e. Two PRs have since changed the
tree; history is as screened.

- **Fixed in the tree.**
  - The rename PR (#2) replaced the `/Users/cole` asset-root paths in the docs.
  - The publication PR (#3, which adds this document) made these fixes:
    - the Mini's tailnet IP in `config/site.example.toml` and
      `deploy/ssh/capture-host-config.example` is now a placeholder;
    - `docs/CAPTURE_AND_SYNC.md` names the `sync.py` constants instead of the
      IPs;
    - the real tailnet name in the `storage_contracts.py` docstring and the
      tests is now `example-*.ts.net`;
    - the full-name home path in `.claude/plans/` is now `~`;
    - home paths in the SSH examples of `docs/RUNBOOK.md` and
      `docs/CAPTURE_KIT.md` are now `$HOME`;
    - `.env` and `deploy/vault/.env` are ignored.
- **Still open, blocking:** the licence and the copyright holder (§3.7, §4b.1).
- **Still open, recommended before publishing:** the tailnet IPs in code
  (`PRIMARY_MINI_TARGET`, `OPENCLAW_GATEWAY_TARGET` in
  `src/cole_voice_lab/sync.py`, mirrored in `tests/test_sync.py` and
  `tests/test_cli.py`). The sync default `DEFAULT_REMOTE_ROOT` also names the
  Mini's `/Users/cole` intake path, and so do `config/site.example.toml`,
  `deploy/ssh/authorized_keys.example`, `deploy/README.md`,
  `docs/CAPTURE_AND_SYNC.md` and the README. All of these name the live Mac
  mini intake, so changing them is a behaviour change: do it when the backend
  moves off the Mini.
- **Still open, optional:**
  - the `coles-macbook-pro` and `coles-macbook-air` hostnames in tests and one
    example;
  - the repository settings to turn on when publishing (§4a.8).

## 1. Scope and method

- **Mirror clone.** A fresh `git clone --mirror` into the session scratchpad.
  It holds one branch (`main` @ 77ca43e), no tags, and no `refs/pull/*` (the
  repository never had a PR). 58 commits from 2026-08-17 to 2026-10-05, 624
  paths ever committed, 609 in the current tree.
- **Secret scan.** gitleaks 8.30.1 over `--all` history. The release binary's
  sha256 matched the release's checksums file.
- **History grep.** Every added line in history (245,393 lines) was grepped for
  `ghp_`, `github_pat_`, `gh[osu]_`, `tskey-`, `sk-`, `sk-ant-`, `xox?-`,
  `AKIA`, private-key headers, JWTs, passwords in URLs, Slack and Discord
  webhooks, age keys, and 64-hex strings that could be a library key.
- **Path and blob review.** Every path ever committed was checked by name and
  extension. Every blob in history was sniffed by magic bytes. The current
  `scripts/guard-private-assets` was also run against the tree of **each of
  the 58 commits**.
- **Personal data.** The `main` tree, every added line and every commit message
  were checked for emails, phone numbers, names, consent text, biometric
  description, home paths, hostnames, tailnet names and IPs, Tailscale node
  IDs, SSH and GPG keys. Tailnet IPs were matched against the infra tailnet
  policy and the local `tailscale status` (read only).
- **Identities.** Author and committer per commit, signatures and trailers.
- **GitHub side.** Repository settings, branches, tags, releases, issues, PRs,
  Actions permissions, workflows and runs, secret and variable names,
  environments, deploy keys, webhooks, runners, Dependabot, collaborators.

The mirror, extracted trees and the scanner were deleted afterwards.

## 2. Verdict

**Needs owner decision.** No history rewrite is needed. Once the owner settles
the licence and copyright line, the rest is a tree-only PR.

| Item | Kind | Blocking? |
|---|---|---|
| `LICENSE` says "private and proprietary", copyright "Cole and Chloe"; `pyproject.toml` `LicenseRef-Proprietary` (§3.7) | Owner decision | **Yes** |
| Real tailnet IPs of three live nodes in code, tests, docs and examples (§3.3) | Tree-only | No, recommended |
| Real tailnet name in tests and one docstring (§3.3) | Tree-only | No, recommended |
| Home paths, including one that spells the owner's full name, in docs, examples and `.claude/plans/` (§3.3) | Tree-only | No |
| Hostnames `coles-macbook-pro` / `coles-macbook-air` in tests and an example (§3.3) | Tree-only | No |
| `.env` not in `.gitignore` (§3.2) | Tree-only | No |
| University email as author of 57 of 58 commits (§3.5) | Owner decision, already public | No |
| `Claude-Session:` links in 48 commit messages (§3.4) | Accept | No |
| One Dependabot graph run log becomes public (§3.6) | Accept | No |

## 3. Findings

### 3.1 Credentials

- **gitleaks: 2 hits, both false positives.** `generic-api-key` on the
  `auxiliary_revisions` value in `src/cole_voice_lab/workers/chatterbox_turbo.py`
  and `chatterbox_v3.py` (commit e41a089): a Hugging Face model revision hash
  (`e0..ec`), not a key.
- **History grep: no hit** for any token prefix, private-key header, JWT,
  password in a URL, webhook or age key. `sk-` matched only words such as
  `disk-` and `task-`.
- **Key-shaped strings.** The 486 64-hex strings in history are sha256 digests
  in runtime lock files (`requirements/runtimes/*.artifacts.txt`,
  `config/model-sources.lock.json`), docs and tests. The library-key tests use
  synthetic keys (`tests/test_storage_sealing.py:75-76`, `bytes(range(32))`).
  The vault stores only hashes of its bearer tokens; the test tokens are
  literals such as `sy..al`.
- **Env examples hold placeholders only:** `.env.example`,
  `deploy/vault/.env.example`. `deploy/vault/tailscale/serve.json` uses
  `${TS_CERT_DOMAIN}`.

### 3.2 Private assets in history

Nothing slipped in.

- **Paths.** No path ever committed has an audio, model, dataset, database,
  dump, key, PDF, spreadsheet, archive or `.env` extension. The SQL files are
  schema migrations (`migrations/*.sql`). The 15 paths deleted from history are
  all code and schemas (the removed package-preparation feature).
- **Blobs.** Only two binary blobs exist in all history:
  `src/cole_voice_lab/resources/desktop-icon.png` and `desktop-icon.icns`, the
  app icon (a cartoon waveform and mirror, no person). The PNG's EXIF holds
  only its dimensions. The plan calls it a "supplied file"; the owner should
  confirm its source can be published.
- **Guard.** `scripts/guard-private-assets` exists since the initial commit
  and checks the worktree and index only, not history. Run against all 58
  historical trees, it passes every one.
- **Consent.** The only consent JSON is the template
  `src/cole_voice_lab/resources/consent.pending.json` (`approved: false`,
  `pending-subject-attestation`, no dates). It is not a signed record. The
  fixture consent files in `tests/vault_*_fixtures.py` are
  `{"consent":"synthetic"}`.
- **Prompt catalogs** (`src/cole_voice_lab/resources/*-v1.json`,
  `pilot-v2.json`) are neutral reading sentences, nothing personal.
- **Gap, optional.** `.gitignore` does not list `.env` or `deploy/vault/.env`,
  though `deploy/vault/.env.example:1` says to copy it outside Git.

### 3.3 Personal data in the tree

All of it is about the owner and his own infrastructure. Nothing is about a
third party. The repository describes the voice lab's purpose ("recording
Cole's voice", `AGENTS.md:5`) and its biometric policy (`SECURITY.md`), but no
voice characteristics.

- **Names.** "Cole" is the product and package name (`cole-voice-lab`,
  `cole_voice_lab`, "Cole Voice Lab") and the consent subject id
  (`cole-self`), hundreds of times. "Chloe" is not a person here: it is the
  downstream ChloeSpeech consumer (`docs/CHLOESPEECH_HANDOFF.md`,
  `src/cole_voice_lab/promotion.py`, the purpose
  `private-chloe-speech-rendering`), the agent persona noted in the infra
  screen §4.1. It also appears as a co-author in `pyproject.toml:11`
  (`"Cole and Chloe"`) and as a copyright holder in `LICENSE:1` (§3.7). No
  commit is authored by "Chloe".
- **Emails.** Only `example.com` / `example.invalid` test values
  (`tests/test_storage_server.py:1116,1128`, `tests/test_runtime.py:382`). No
  phone numbers or street addresses.
- **Tailnet IPs of live nodes.** All three are online in the tailnet today:
  - the Mac Mini (`10..91`): `src/cole_voice_lab/sync.py:19`
    (`PRIMARY_MINI_TARGET`, the sync allow-list default, also used by
    `src/cole_voice_lab/cli.py:4329`), `config/site.example.toml:17`,
    `deploy/ssh/capture-host-config.example:2`,
    `docs/CAPTURE_AND_SYNC.md:168`, `tests/test_cli.py:1126`,
    `tests/test_sync.py:129`;
  - the OpenClaw gateway node (`10..21`): `src/cole_voice_lab/sync.py:21`
    (`OPENCLAW_GATEWAY_TARGET`, a deny-list entry),
    `docs/CAPTURE_AND_SYNC.md:173`, `tests/test_sync.py:130`;
  - the MacBook Air (`10..11`): `tests/test_sync.py:156`.

  The Mini address is the one infra's screen §3.2 called "not a fleet address"
  in jobtrack; it is the Mini's. The other `100.64.x.x` and `100.96.1.7` values
  in tests are synthetic.
- **Tailnet name** (`co..er.ts.net`): `src/cole_voice_lab/storage_contracts.py:171`,
  `tests/test_storage_contracts.py:91,92,807`,
  `tests/test_storage_binding.py:725`, `tests/vault_binding_fixtures.py:49`,
  `tests/vault_library_fixtures.py:52`. A test variant that keeps the first
  word of the name (`co..st.ts.net`) is in
  `tests/test_storage_http_client.py:71,463,464,506-512`.
- **Tailscale node ID.** `nQ..RL` in `tests/test_storage_contracts.py:654,718,1918`,
  `tests/vault_binding_fixtures.py:51`, `tests/vault_library_fixtures.py:54`
  matches no current tailnet node. Treat as synthetic.
- **Home paths.** `/Users/cole` in 12 files: `README.md:48`,
  `config/site.example.toml:7,20`, `deploy/README.md`,
  `deploy/ssh/authorized_keys.example:3`,
  `deploy/ssh/capture-host-config.example:4,8`, `docs/CAPTURE_AND_SYNC.md:253`,
  `docs/CAPTURE_CONSOLE_PLAN.md:292`, `docs/CAPTURE_KIT.md`,
  `docs/CHLOESPEECH_HANDOFF.md:193`, `docs/RUNBOOK.md`,
  `src/cole_voice_lab/sync.py:22` (`DEFAULT_REMOTE_ROOT`),
  `tests/test_sync.py:174`. A second macOS home, `/Users/co..er`, spells the
  owner's full name: `.claude/plans/2026-09-07-desktop-install-icon.md:57`,
  `.claude/plans/2026-09-11-native-recording-ux.md:365,1606`. No `/home/cole`.
- **Hostnames.** `coles-macbook-pro` as a test `capture_host` in
  `tests/test_capture.py`, `test_sync.py:72`, `test_references.py:114`,
  `test_intake.py:85`, `test_datasets.py:128`; `coles-macbook-air` in
  `deploy/ssh/authorized_keys.example:3`. `cole-ubuntu-pc` / `ubuntu-pc` in
  `.claude/plans/2026-09-17-library-vault-on-ubuntu-pc.md`, `CURRENT.md`,
  `DECISIONS.md`, `deploy/vault/README.md`, `tests/test_storage_contracts.py`.
- **SSH and GPG keys.** None. `deploy/ssh/authorized_keys.example:3` and
  `docs/CAPTURE_AND_SYNC.md:247` use `<DEDICATED_PUBLIC_KEY>` / `...`.
- **`.claude/plans/`** (18 tracked plans). These are agent implementation
  plans: steps, verification notes and design decisions. Apart from the three
  `/Users/co..er` lines they hold no personal data, IP or tailnet name; they
  name the operator's verification directories under Application Support.
  Keeping them is fine once those three lines are fixed; untracking them is
  the owner's choice.

### 3.4 Personal data in history

History adds nothing beyond the tree. Every email, home path, hostname, tailnet
name, tailnet IP and node ID found in added lines also appears in `main`.
Commit messages name `cole-ubuntu-pc` once and "MacBook Air" once.
48 commit messages carry a `Claude-Session:` claude.ai link, private to the
account (same as infra screen §3.3).

### 3.5 Commit identities

| Identity | Commits | Dates |
|---|---|---|
| `cole <sc..@umich.edu>` | 9 | 2026-08-17 to 09-02 |
| `scshafe <sc..@umich.edu>` | 48 | 2026-09-15 to 09-19 |
| `scshafe <17..@users.noreply.github.com>` | 1 | 2026-10-05 |

Author equals committer on every commit; no commit is signed. Trailers name
Claude models as co-authors (`no..@anthropic.com`). This is the cross-cutting
finding of infra screen §3.1: the university address is already public, so the
recommendation is not to rewrite. The newest commit already uses the repo-local
noreply identity.

### 3.6 GitHub side

- Private, default branch `main`, **only branch `main`**. No tags, releases,
  issues, PRs, milestones, forks, deployments, environments, Pages or wiki.
  Projects and issues are enabled. GitHub's licence detection reports
  `NOASSERTION`.
- Actions enabled, all actions allowed, default workflow token `read`. No
  `.github/` directory, no workflows of its own, no self-hosted runner, no
  artifacts or caches. One run exists: the dynamic Dependabot "Graph Update"
  (pip) of 2026-09-02, which becomes visible when public.
- No Actions, Dependabot or Codespaces secrets; no variables; no deploy keys;
  no webhooks. Collaborators: the owner only.
- Dependabot alerts and security updates are off. Branch protection and
  rulesets are unavailable while private on the free plan.
- The old name `scshafe/cole-voice-lab` redirects to the new one.

### 3.7 Licence

`LICENSE` is three lines: copyright "Cole and Chloe", "All rights reserved",
"private and proprietary unless a file explicitly states otherwise".
`pyproject.toml:11-13` has `authors = [{ name = "Cole and Chloe" }]`,
`license = "LicenseRef-Proprietary"`, `license-files = ["LICENSE"]`.

Publishing as is would make the code visible but not usable by anyone, and the
`LICENSE` text ("private") would be false. "Chloe" is an agent persona, not a
legal person, so as a copyright holder it is misleading. The owner needs to
choose: an open licence (MIT matches mailroom-desktop and voice-journey-capture)
or a public "all rights reserved" notice, and a copyright holder.

Third-party text in the tree is attributed: the CMU ARCTIC prompts carry their
COPYING notice (`tests/fixtures/standard_sets/arctic-COPYING.txt`,
`src/cole_voice_lab/standard_prompt_sets.py`), and the Harvard sentences
(IEEE 1969) are cited with their source URL in
`tests/fixtures/standard_sets/harvard-excerpt.html` and
`standard_prompt_sets.py:71`.

## 4. Fixes

### 4a. Tree-only fixes (one normal PR)

1. **Licence** (after the owner decides, §4b.1): replace `LICENSE`; update
   `pyproject.toml:11` (authors) and `:12` (licence expression, e.g. `MIT`).
2. **Tailnet IPs in docs, examples and tests**, replaced with placeholders such
   as `<mini-tailnet-ip>` or synthetic `100.64.0.x` values:
   `config/site.example.toml:17`, `deploy/ssh/capture-host-config.example:2`,
   `docs/CAPTURE_AND_SYNC.md:168,173`, `tests/test_cli.py:1126`,
   `tests/test_sync.py:129,130,156`.
3. **Tailnet IPs in code**: `src/cole_voice_lab/sync.py:19,21`
   (`PRIMARY_MINI_TARGET`, `OPENCLAW_GATEWAY_TARGET`). These are a security
   allow-list and deny-list and the CLI default (`cli.py:4329`), so this is a
   behaviour change: move them to explicit configuration or synthetic values,
   and keep `tests/test_sync.py` consistent. The Mini is being retired, so the
   default may be obsolete anyway.
4. **Tailnet name**, replaced with `example.ts.net` (or `<tailnet>.ts.net` in
   prose): `src/cole_voice_lab/storage_contracts.py:171`,
   `tests/test_storage_contracts.py:91,92,807`,
   `tests/test_storage_binding.py:725`, `tests/vault_binding_fixtures.py:49`,
   `tests/vault_library_fixtures.py:52`; optionally the `co..st.ts.net` variant in
   `tests/test_storage_http_client.py:71,463,464,506-512`.
5. **Full-name home path**: replace `/Users/co..er` with `~` in
   `.claude/plans/2026-09-07-desktop-install-icon.md:57` and
   `.claude/plans/2026-09-11-native-recording-ux.md:365,1606`.
6. **Optional, cosmetic**: `/Users/cole` to `~` or `$HOME` in the docs and
   examples of §3.3 (`src/cole_voice_lab/sync.py:22` is a runtime default;
   leave it or make it configurable); `coles-macbook-pro` to `capture-host-01`
   in the five test files; `coles-macbook-air` in
   `deploy/ssh/authorized_keys.example:3` to `<capture-host>`.
7. **Optional hardening**: add `.env` and `deploy/vault/.env` to `.gitignore`.
8. **Before going public**: enable Dependabot alerts, secret scanning and push
   protection (free for public repositories) and protect `main`.

### 4b. Owner decisions (no history rewrite needed)

1. **Licence and copyright holder** (blocking): open licence or public
   all-rights-reserved; drop "Chloe" as a legal holder.
2. **University email in 57 commits**: accept, as in infra screen §3.1 (already
   public; a rewrite would only help as a fresh repository). If wanted anyway:
   `git filter-repo --mailmap` to the noreply address, then push as a fresh
   repository. There are no `refs/pull/*`, so this repository could also be
   force-pushed in place, but every SHA and clone changes.
3. **Icon provenance**: confirm `desktop-icon.png` / `.icns` may be published.
4. **Accept** the tailnet name and IPs in history (reachable only by tailnet
   members), the `Claude-Session:` links and the one Dependabot run log. Removing
   them from history would need `git filter-repo --replace-text`, which this
   screen does not recommend.
