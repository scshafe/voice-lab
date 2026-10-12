# voice-lab

Private, local-first infrastructure for:

1. capturing a checksum-manifested voice corpus on a MacBook;
2. transferring it over the Tailnet into a fail-closed Mac mini intake;
3. building reference clips and versioned datasets without modifying raw audio;
4. running a reproducible, blind comparison of Qwen3-TTS, Chatterbox Turbo,
   Chatterbox multilingual V3, F5-TTS, and Seed-VC; and
5. speaking with the voice release Cole designates, for callers he registers on
   the tailnet (D-044 to D-047; [docs/SPEECH_API.md](docs/SPEECH_API.md), designed,
   not yet built).

```text
MacBook capture -> checksum-bound READY package -> Tailnet landing zone
    -> verified immutable raw store -> references/derived datasets
    -> isolated candidate workers -> normalized outputs + metrics
    -> blind review -> fine-tune -> voice release (Cole) -> speech jobs
```

The next shape of the project, with voice-journey merged in, the backend on the PC,
and a cross-platform desktop app, is in
[docs/design/VOICE-PLATFORM.md](docs/design/VOICE-PLATFORM.md) (accepted on
2026-10-06 and built in phases; not a description of what runs today). Everything it
uses is free and runs on the owner's own hardware: no cloud service, nothing paid.

voice-lab is released under the [MIT licence](LICENSE).

The Git repository contains only code, schemas, prompts, and policy. Recordings,
weights, consent evidence, runtime environments, and generated speech belong in
an explicit private asset root and are rejected by the repository guard.

## Development bootstrap

`scripts/verify` is the one verification command (`dev.toml [verify]`). It uses the
checkout's `.venv` (Python 3.12) and runs on both hosts below.

**macOS** (the capture and recording host; Homebrew Python):

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps \
  -r requirements/development-build.txt
.venv/bin/python -m pip install -e '.[dev]'
scripts/verify
```

On Cole's Mac, an agent does this, then builds and installs the macOS apps from a commit:
`scripts/mac-refresh` and [docs/mac-agent/README.md](docs/mac-agent/README.md) (D-043).

**Linux** (the Arch PC; Python 3.12 from [uv](https://docs.astral.sh/uv/)):

```bash
uv venv --seed --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]' -r requirements/development-build.txt
scripts/verify
```

`--seed` installs pip, which worker evidence records. Off macOS, `scripts/verify`
skips the native recording app's Swift build and tests, type-checks as macOS
(`[tool.mypy] platform`), and the tests that need macOS itself (the real
recording service, Core Audio, the Homebrew capture bootstrap, launchd, the macOS
artifact locks) skip with the reason named; `tests/macos_only.py` lists them and
`pytest -ra` prints them. Everything else, coverage threshold included, is the
same on both hosts.

The optional `vault` extra adds the library vault's single encryption
dependency. The `dev` extra pins the same version, and both `scripts/verify`'s
strict type check and the native application build now require it to be
installed. The reviewed route is `scripts/bootstrap-desktop --with-vault`, which
adds `requirements/vault-macos.txt` to the pinned desktop install and stays a
preview until `--execute --allow-downloads` is given; that lock also installs on
its own with `--require-hashes --no-deps --only-binary=:all:`. The vault itself
is described in [docs/LIBRARY_VAULT.md](docs/LIBRARY_VAULT.md).

## First pilot

The complete operator flow is in [docs/RUNBOOK.md](docs/RUNBOOK.md). The short
version is:

```bash
voice-lab doctor
voice-lab assets init --asset-root ~/.local/share/voice-lab
voice-lab capture devices
voice-lab desktop --asset-root <macbook-private-root> --device UMIK-1
voice-lab sync plan <session-directory> --remote-user <user> \
  --remote-root /Users/<user>/.local/share/cole-voice-lab/incoming
voice-lab intake validate <mini-private-root>/incoming/cole-pilot-001
voice-lab intake accept --asset-root <mini-private-root> \
  --session-id cole-pilot-001
voice-lab reference --help
voice-lab models list
voice-lab models verify-registry
voice-lab audition --help
voice-lab blind --help
```

The [native desktop recording library](docs/DESKTOP.md) opens its own window
with all pilot prompts, recording dates, completion, and take history. Select
a batch, record and review each take, and finalize. Refreshing an older prompt
creates a new session and preserves the earlier recording. The desktop uses
buffered native macOS capture with per-buffer continuity checks. Install its optional widgets using the pinned setup
command in the desktop guide; a macOS application launcher is also available.
`capture run` remains the terminal alternative and `capture console` explicitly
opens the older browser controller; these legacy FFmpeg paths are not recommended
for new training recordings while their input-loss issue remains. Finalize and complete a headphone review
before planning transfer.

`voice-lab finetune run` fine-tunes F5-TTS from the latest kept take of every
spoken prompt in the recording library and writes listening samples next to the
untouched base model; re-run it after any revision. See [docs/FINETUNE.md](docs/FINETUNE.md).
The native recording app built from this checkout runs the same pipeline from its
**Fine-tune model** button, with progress in the app, and no longer prepares lab packages
([docs/NATIVE_RECORDING_UX.md](docs/NATIVE_RECORDING_UX.md) §16). Its **Speak** page
speaks typed text with a finished fine-tune or the base model, for local listening
(§17 there, and "Speak page" in docs/FINETUNE.md).

`voice-lab catalog` edits the prompts the native app records against. Prompts and
published catalog versions live in a database inside the library, the app loads the
active version at launch, and adding or editing a prompt is a publish, an activate
and a relaunch, never a rebuild. The Harvard and CMU ARCTIC sentence sets import
with one command each. See [docs/CORPUS_STORE.md](docs/CORPUS_STORE.md).

`sync plan` prints a dry plan. A live transfer requires a separate `--live-plan`
preview, copy/paste approval of its exact `plan_sha256`, and all execution gates:
`--live-plan --execute --allow-network --approved-plan-sha256 <digest>`. The
destination is restricted to the primary mini Tailnet IP or its single reviewed
alias; arbitrary public hosts are not configurable. Model provisioning is
likewise a plan unless both `--execute --allow-downloads` are supplied.
Audition execution is offline, resumable, and requires its own `--execute`;
each selected runtime also requires the exact reviewed
`--runtime-receipt RUNTIME_ID=SHA256` binding printed by preparation. Completed
trials retain their runtime receipt and source-integrity preimages under the
experiment so status and blind review remain verifiable after cache eviction.
candidate snapshots can only be evicted with an exact manifest digest and two
destructive confirmation flags.

The CLI authors intake, reference-review and blind-review evidence. It does
**not** choose a winner. ChloeSpeech, the promotion manifest and its handoff were
retired on 2026-10-11 (D-044): the voice that speaks is the voice release Cole
designates himself (D-045).

`voice-lab vault` approves, binds, copies, verifies and restores an encrypted
library copy under D-020. The Mac keeps its working database; Speak clips stay
local and training assets are a separate opt-in. The Swift app shows copying
status and blocks fine-tuned models whose checkpoints were explicitly archived.
`finetune archive --remove-local-copy` reclaims checkpoints one run at a time
only after verification. The implementation and container templates are built;
no real vault is deployed. See [the library guide](docs/LIBRARY_VAULT.md) and
[operator procedures](docs/RUNBOOK.md#9-library-vault).

## Name, asset roots and stable identifiers

The project is `voice-lab`; the repository was `scshafe/cole-voice-lab` until
2026-10. The conventional locations are now `~/.local/share/voice-lab` for the
asset root and `~/.cache/voice-lab` for the vault build cache.

- **Asset root.** No command has a default (D-002): every command that handles
  private data takes `--asset-root` or `COLE_VOICE_LAB_ASSET_ROOT`. A host set
  up before the rename keeps its `~/.local/share/cole-voice-lab` and passes that
  path; nothing has to move. The repository guard treats both names as private
  asset roots, under a macOS or a Linux home.
- **Vault build cache.** `scripts/vault_container.py` uses `~/.cache/voice-lab`
  and falls back to `~/.cache/cole-voice-lab` while only the old one exists.
- **Kept on purpose.** These name stored or deployed things, so renaming them
  would orphan existing data or installs:
  - the `cole-voice-lab.<record>/vN` schema and protocol identifiers and the
    `cole-voice-lab-common-v1` normalization id, which every stored record
    carries (the vault's sealed blobs also bind one as associated data);
  - the Python distribution `cole-voice-lab` and package `cole_voice_lab`, bound
    in install receipts, runtime locks and executed-code evidence, and the
    `.cole-voice-lab-*` marker files inside runtimes;
  - the `COLE_VOICE_LAB_*` environment variables;
  - the Mac mini deployment's names (the `com.scshafe.cole-voice-lab.intake`
    LaunchAgent, the `cole-voice-lab-mini` alias, its intake path
    `/Users/cole/.local/share/cole-voice-lab/incoming` and the
    `cole-voice-lab-vault` image), which the backend's move off the mini
    replaces.

## Status

See [CURRENT.md](CURRENT.md) and [PROGRESS_CONTRACT.md](PROGRESS_CONTRACT.md).
