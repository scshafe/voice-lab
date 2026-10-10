# voice-journey Agent Contract

Voice Journey: longitudinal analysis of ~2,400 Apple Voice Memos (2019 on),
singing vs. non-singing filtering, local STT, voice features, same-song
journeys, and a corpus browser (Node HTTP server `src/corpus-browser.mjs` plus
a React SPA in `web/`). The repo holds code + repo-safe manifests only. Read
`README.md` and `docs/breathing-studio-plan.md` first. scshafe-dev kind:
`service` (`dev.toml`).

## Run and verify

- Node >=22 (`package.json` engines). The repo root has no dependencies;
  `web/` owns the only `node_modules` (`cd web && npm ci && npm run build`;
  its `@scshafe/ui` dependency is the private GitHub Packages package,
  pinned exactly; `web/.npmrc` maps the `@scshafe` scope to
  `npm.pkg.github.com`, so installing `web/` needs GitHub Packages read access:
  `NODE_AUTH_TOKEN` in the environment or a token in `~/.npmrc`, never a token
  in the repo). `npm run typecheck --prefix web` is the SPA's type check.
- Verify (`dev.toml [verify]`, run by `.github/workflows/ci.yml`): `npm test`
  (`node --test`). `test/web-spa.test.mjs` skips its four render checks when
  `web/node_modules` is absent (as in CI); install `web/` to run them.
- Local development: set a temp data root so nothing touches real data, e.g.
  `VOICE_JOURNEY_DATA=$(mktemp -d) npm run browser -- --host 127.0.0.1 --port 8787`.
  Generated manifests are untracked (gitignored): a fresh data root has none,
  and tests build fixtures or temp roots; they must not read tracked copies.
- Every pipeline command has a `--dry-run` that reads only manifests (the data root's, or fixtures)
  (README "Verification"). Anything without `--dry-run` that reads audio
  needs a release-gate approval and the Mac's corpus (`--corpus-root`).

## Server portability (Phase 1 of `docs/move-to-lubuntu.md`)

- `src/paths.mjs` decides every path: `VOICE_JOURNEY_DATA` is the data root
  (`/data` in the image) and unset keeps the local-dev layout. Input manifest:
  `manifests/chains.json` only. Every other manifest is generated and belongs
  under `$VOICE_JOURNEY_DATA/manifests/`. The app never writes into the source
  tree in the container. Tools come from `FFMPEG_BIN`, `WHISPER_CPP_BIN`,
  `WHISPER_CPP_MODEL`, `VOICE_JOURNEY_PYTHON`: no Homebrew paths in code.
- Binding: loopback only, except in the image (`VOICE_JOURNEY_CONTAINER=1`),
  where the door and sidecar front it. `/healthz` is unauthenticated.
- `src/intake.mjs` is the capture client's upload API (README "Intake API"):
  door identity headers required, fail-closed finalize, no content in logs.
- `Dockerfile` needs BuildKit and the package token as a secret
  (`--secret id=node_auth_token,env=NODE_AUTH_TOKEN`); never a build arg.
  `docker build --target test` runs `npm test` in the image. `dev.toml`
  `[identity] docker = true`.

## Production

- The server runs on the laptop (`cole-lubuntu-laptop`), node
  `https://voice-journey.<tailnet>`, data root `/srv/voice-journey`. The stack
  is app-owned: `deploy/stack/` (`compose.yaml`, `serve.json`, `stack.toml`),
  validated by the host before anything runs; infra's
  `stacks/voice-journey/host.conf` grants the data root binds and the
  `vj-mirror` group. `dev.toml [deploy]` is lane `runner`, layout `app`, host
  `laptop`, door `pocket-id`: **a merge to `main` deploys to production**
  (`.github/workflows/deploy.yml`: verify on a GitHub-hosted runner, then the
  `voice-journey-prod` runner builds the image on the laptop and deploys, then a
  health check). Merge one change at a time and watch the run (landing section
  below). The build reads `@scshafe/ui` with the deploy job's own token
  (`stack.toml` `build_secrets`), which needs the package's Actions access grant
  for this repository.
  The Mini no longer serves: its browser, door and node jobs are disabled
  (plists in `~/Library/LaunchAgents/disabled/`); never kickstart them or update
  the Mini's checkout to serve.
- The corpus mirror (`npm run mirror`, `src/mirror.mjs`, phase 3 of
  `docs/move-to-lubuntu.md`) runs on the Mini under launchd
  (`ai.voice-journey.mirror`, every 900 s) through the same launcher in
  `mirror` mode. It reads only via the host-access seam, pushes to the drop
  account `vj-mirror@cole-lubuntu-laptop:` with write-only rsync
  (`/opt/homebrew/bin/rsync`, relative remote path, forced `rrsync -wo`), never
  deletes remotely, and logs counts only. Real runs need `--approval`; `--dry-run`
  reads nothing. The launcher takes a fixed allowlist of modes
  (`browser`, `mirror`); rebuilding it may need Full Disk Access re-granted.
- On the server `corpus/phone/` (the drop) is read-only to the app; the
  watcher and index ignore rsync temp files, `.rsync-partial/` and `_mirror/`.
- The mirror is pinned to the Mac: the corpus is the TCC-protected Voice Memos group
  container (infra `docs/PLACEMENT.md`, `autodeploy/ONBOARDING.md`).
- CI never deploys (SERVICE-02). Bind to 127.0.0.1 only; never `tailscale
  funnel`.

## Rules

- Never commit recordings or audible derivatives (`.gitignore`), transcript
  text or raw lyrics; `local-artifacts/` stays local.
- Never read `~/Library` directly: corpus access goes through the read-only
  seam (`npm run corpus`).
- Secrets are files on the Mini (`~/.mission-control/secrets/voice-journey-oidc.env`);
  never print or commit them.
- Infra context: `~/src/infra` (`docs/PLACEMENT.md`, `docs/network.md`).

<!-- scshafe-dev:begin landing -->
## Verify and landing

Managed by scshafe-dev: `dev adopt` and `dev update` refresh this section from `dev.toml`; change `dev.toml`, not these lines.

Before finishing, both of these must pass:

```sh
npm test
dev check .
```

How a change lands:

1. Work on a branch and open a PR.
2. Run the two commands above. If the repository is private, GitHub Actions does not run for it: verify locally and say in the PR what you ran. If it is public, wait for CI to be green.
3. Merge your own PR with a merge commit, one change at a time: `gh pr merge <N> --merge --subject "Merge #<N>: <title>"`. Never squash or rebase (both are off on the repository), and pass `--subject`: `gh pr merge` does not make the `Merge #N: <title>` subject by itself.

The project's agent may merge its own PR and push `main`; there is no approval gate.

Merging deploys to production ([deploy] lane `runner`: `.github/workflows/deploy.yml` verifies on a GitHub-hosted runner, deploys through the host entrypoint on the `voice-journey-prod` self-hosted runner, then checks health).
Watch the run yourself with `gh run list -w deploy`, `gh run watch <id>` and `gh run view <id> --log` (a public repository's deploy log is a summary only); say in your reply what the run did, naming the merge commit.
Roll back by merging a `git revert`, or by dispatching `deploy.yml` with `sha=<older commit on main>` and `allow_rollback=true` (`gh workflow run deploy.yml -f sha=<sha> -f allow_rollback=true`).
<!-- scshafe-dev:end landing -->
