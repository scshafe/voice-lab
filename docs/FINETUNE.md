# Fine-tuning from the recording library

`voice-lab finetune` turns the recording library into a fine-tuned F5-TTS voice in one
command and can be re-run after every revision of the corpus. The native recording app's
**Fine-tune model** button runs the same pipeline ("From the app" below), and its **Speak**
page speaks typed text with a finished run or the base weights ("Speak page" below). It
reads takes directly from the library; lab packages, intake, dataset manifests and the
pilot gate are not involved. Module: `src/cole_voice_lab/finetune.py` (`@canonical
finetune`). Decisions: D-016, D-018, D-019.

F5-TTS is the first model family (D-048). The pipeline's settings, dataset, runtime, training
and samples are its training adapter's (`src/cole_voice_lab/training.py`, D-049), so another
family can be trained on the same corpus selection: `finetune dataset`, `train` and `run` take
`--family` (default `f5-tts`, the only one registered), and every run records the host it
trained on (`training-host.json`). What a run cost and how it renders are recorded with
`voice-lab families record` and compared with `voice-lab families compare` (D-049,
[MODEL_FAMILIES.md](MODEL_FAMILIES.md)). Everything below is F5-TTS's; `finetune synthesize`
is F5-TTS's only.

## The corpus

The live corpus is **the latest kept take of every spoken prompt** in the configured catalog
for one subject, across native (app) takes and old-app (Qt) takes alike:

- retained means kept and not invalid, the same predicate the Library uses for coverage;
- the newest capture date wins; unknown dates sort oldest; a trim recipe on a native take is
  applied sample-exactly;
- room-tone and procedure prompts never enter; a prompt with no retained take is reported as
  missing, and the command refuses to continue (`FINETUNE.CORPUS_INCOMPLETE`, led by the
  missing and unreadable counts) unless `--allow-missing` is given;
- a take recorded against an earlier revision of a prompt counts when a keep-recordings edit
  accepts that revision (`docs/CORPUS_STORE.md`); the newest take wins across the current and
  every accepted revision;
- the training transcript is always the prompt's current text, even for a take recorded
  against an earlier revision;
- held-out and silent recordings never enter, and takes from held-out groups are skipped.

`--catalog` names a released built-in (`pilot-v1`, `evaluation-v1`, `pilot-v2`,
`physical-test-v1`) or a catalog published in the library's corpus store:
`<catalog_id>` for its latest version, `<catalog_id>@<version>` for a fixed one. An exact
built-in name wins. A held-out-evaluation catalog is refused with
`FINETUNE.CATALOG_HELD_OUT`, because evaluation material never trains a voice. Refusals exit 2;
a refusal with a code prints `voice-lab: <CODE>: <message>`.

```sh
voice-lab finetune corpus --asset-root <library> --subject-id cole-self --catalog pilot-v2 [--json]
voice-lab finetune corpus --asset-root <library> --subject-id cole-self --catalog cole-corpus --allow-missing [--json]
```

A published catalog that includes the standard sentence sets has far more prompts than
recordings, so it needs `--allow-missing`; the corpus is then the prompts that have a take, and
the summary still lists every missing prompt.

Each entry of `dataset/corpus.json` records `prompt` (the current prompt, whose text is the
transcript), `prompt_sha256` (its digest) and `recorded_prompt_sha256` (the digest the audio
was recorded against; equal to `prompt_sha256` unless a keep-recordings alias matched). The
corpus records `catalog_reference`, the published version it was selected from (`null` for a
built-in), and the command summaries print it. A `corpus.json` written before these fields
existed still loads unchanged.

## The revision loop

1. Record or re-record prompts in the app. Every saved take is a candidate; the newest wins.
   Adding or editing a prompt is a `voice-lab catalog` change, published and activated for the
   app (`docs/CORPUS_STORE.md`).
2. `voice-lab finetune run --asset-root <library> --subject-id cole-self --catalog pilot-v2 \
      --runtime-root <f5-runtime>` (about two hours on the M4 24 GB MacBook Air at the defaults),
   or `--catalog cole-corpus --allow-missing` for the published catalog.
3. Listen: `derived/finetune/<run-id>/samples/finetuned/sample-0N.wav` against
   `.../samples/base/sample-0N.wav` (the untouched base model, same reference, same sentences).

`run` is `corpus` → `dataset` → `train` → `synthesize` (fine-tuned, then base), chained by
`finetune.run_finetune_pipeline`, the one function the app's jobs also call. Each step is
also a command on its own. All are keyed by `--run-id` (default `finetune-<UTC time>`, for
example `finetune-20260916t153000z`) and all write under
`<library>/derived/finetune/<run-id>/`:

| Path | What it is |
| --- | --- |
| `dataset/corpus.json` | the selected takes, sources and SHA-256s (`finetune-corpus`) |
| `dataset/wavs/<prompt>.wav`, `dataset/metadata.csv` | 24 kHz mono 16-bit training copies and the F5 `audio_file\|text` list |
| `dataset/dataset.json` | clip digests and the per-clip gain (`finetune-dataset`) |
| `checkpoints/model_last.pt`, `model_<N>.pt` | fine-tuned weights (light: model + EMA, about 2.7 GB each) |
| `train.log` | the trainer's output |
| `run.json` | hyperparameters, runtime identity, checkpoint digests, status (`finetune-run`) |
| `samples/<label>/sample-NN.wav`, `samples.json` | listening samples (`finetune-samples`): 24 kHz mono 16-bit, 0600, each checked from its WAV header; `job.json` beside them is the synthesizer's input, and its temporary `synthesis-tmp/` there is gone once it exits |

Nothing is written under `tmp/` or `raw/`, and no evidence is written under `provenance/`. The
app may keep running; the command takes no library locks and only reads takes. A published
`--catalog` name is the one exception: it opens the corpus store under `provenance/corpus/`
only to resolve the version and closes it before any take is read. That open takes the
store's lease and lock and may change the store's own files there: it may create the two
lock files and an empty `snapshots/` directory, change the SQLite WAL files, and remove a
crashed publish's scratch files ([CORPUS_STORE.md](CORPUS_STORE.md), "Where it lives").

## Level normalization is required

Raw UMIK-1 takes sit near -50 dBFS RMS. F5-TTS assumes speech near RMS 0.1 (-20 dBFS): its
inference raises the reference clip to that level and trims "silence" at -42 dBFS, which on a
raw take discards nearly the whole clip. The training copies therefore get a **linear gain
only**: RMS raised to -20 dBFS when below, capped so the peak stays at or below -0.5 dBFS.
Every gain is recorded in `dataset.json`. Originals are never modified; derived copies may be
normalized (`docs/CAPTURE_AND_SYNC.md`, "Raw means raw").

## The runtime

Training and synthesis run out of process in a pinned F5-TTS environment, one per host
(`--target`, this host's by default):

| Target | Lock | Interpreter (default `--python`) |
| --- | --- | --- |
| `macos-mps` (the Mac) | `requirements/runtimes/f5-tts-finetune-1.1.22-mps.txt`: torch 2.14 with MPS, f5-tts 1.1.22; versions pinned, not yet hashed | Homebrew `python@3.12` |
| `linux-rocm` (the PC, RX 6900 XT) | `requirements/runtimes/f5-tts-finetune-1.1.22-rocm7.2.txt`: spike S-2's stack (D-046), torch 2.14.0+rocm7.2, torchaudio 2.11.0+rocm7.2, torchcodec 0.16.0+cpu, f5-tts 1.1.22, every other package at its constraints' version; every file hash-bound | infra's host Python, `/opt/voice-lab/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12` |

The ROCm lock is compiled by uv from
`requirements/runtimes/f5-tts-finetune-1.1.22-rocm7.2/pyproject.toml` (which routes only the
torch family to PyTorch's `rocm7.2` and `cpu` indexes) with the versions of every other package
as constraints (`requirements/runtimes/f5-tts-finetune-1.1.22-rocm7.2/constraints.txt`: spike
S-2's, which were the Mac lock's until 2026-10-19, with multidict at 6.9.1 and aiohttp at
3.14.4 since); its header has the exact command, and its body is that command's unchanged
output. It installs in two steps, both `--require-hashes`: the hash-locked build tools
(`requirements/development-build.txt`, `--build-requirements`), then the lock without build
isolation and wheels only, except the three packages that publish none
(`antlr4-python3-runtime`, `encodec`, `transformers-stream-generator`), which build from their
hash-bound sdists with those tools (their setuptools is the lock's own 84.0.0, a dependency of
torch, so the lock's run keeps it). Both runs are `--isolated` (no `PIP_*`
variables, no `pip.conf`) and `--no-cache-dir`. The lock names its own indexes in its
header, PyPI first, then PyTorch's two: pip replaces the index list of its command line with a
requirements file's `--index-url`, so indexes given on the command line would be dropped (and
an `--extra-index-url` written before the `--index-url` too). pip takes from PyTorch's indexes
only files whose hashes the lock names.

The default interpreter names infra's `HOST_PYTHON` patch release, and a venv links to the
interpreter it was made with. When infra installs another 3.12 and prunes the old one, the
runtime's `bin/python` dangles and every job fails `runtime_unavailable`. Provision again with
the new interpreter (`--python`, until the default follows `HOST_PYTHON`): the plan lists the
dangling links (`stale_interpreter_links`), execution removes them before it runs `venv`
(which would keep them), and pip finds the 3.12 packages already installed; the weights are
kept. Another minor version needs a new lock.

The runtime root must hold `bin/python` and these pinned files under `pretrained/`:

| Path under `<runtime>/pretrained/` | Source (Hugging Face, immutable revision) | Bytes | SHA-256 |
| --- | --- | --- | --- |
| `F5TTS_v1_Base/model_1250000.safetensors` | `SWivid/F5-TTS` @ `84e5a410d9cead4de2f847e7c9369a6440bdfaca` | 1,348,435,761 | `670900fd14e6c458b95da6e9ed317cdb20dbaf7a1c02ac06a05475a9d32b6a38` |
| `F5TTS_v1_Base/vocab.txt` | `SWivid/F5-TTS` @ `84e5a410d9cead4de2f847e7c9369a6440bdfaca` | 13,800 | `2a05f992e00af9b0bd3800a8d23e78d520dbd705284ed2eedb5f4bd29398fa3c` |
| `vocos-mel-24khz/config.yaml` | `charactr/vocos-mel-24khz` @ `0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21` | 461 | `da9033922f969a47f0c160010226919e59f27761fd5066f3828d46de6650b0fc` |
| `vocos-mel-24khz/pytorch_model.bin` | `charactr/vocos-mel-24khz` @ `0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21` | 54,365,991 | `97ec976ad1fd67a33ab2682d29c0ac7df85234fae875aefcc5fb215681a91b2a` |

`finetune train`, `finetune synthesize`, `finetune run` and every app job describe the
runtime first (`finetune.describe_finetune_runtime`). That check runs before any dataset is
written. It refuses a missing Vocos file with `FINETUNE.RUNTIME_UNAVAILABLE`, naming the file
and the fetch command. It refuses missing base weights, or any of the three checked files
(the weights and both Vocos files) with the wrong size or SHA-256, with
`FINETUNE.PRETRAINED_MISMATCH`, as it refuses a checked file that is a link rather than the
file itself (a Hugging Face cache snapshot holds links to blobs, so copy the files with links
followed). The big weights are hashed last. A runtime without the vocoder therefore fails in
seconds, not after two hours of training. The first runtime root is
`~/.local/share/voice-lab/finetune-f5-01/venv`. Its `pretrained/` is a link to the
sibling `pretrained/` directory the manual run downloaded, so its Vocos files belong in
`~/.local/share/voice-lab/finetune-f5-01/pretrained/vocos-mel-24khz/`.

```sh
voice-lab finetune provision-runtime --runtime-root <dir> [--target …]         # prints the plan
voice-lab finetune provision-runtime --runtime-root <dir> --execute --allow-downloads
voice-lab finetune fetch-pretrained  --runtime-root <dir> [--allow-downloads]  # verify or fetch
```

The installed F5-TTS 1.1.22 package also carries upstream's public English reference clip,
`lib/python3.12/site-packages/f5_tts/infer/examples/basic/basic_ref_en.wav` (256,018 bytes,
SHA-256 `b0e22048e72414fcc1e6b6342e47a774d748a195ed34e4a5b3fcf416707f2b71`, from the
hash-locked wheel). The speech API's base voice is that clip on the pinned weights (D-055), so
a provisioned runtime holds every file it needs; nothing else is fetched for it.

The plan names the target, the interpreter, the lock (and, for `linux-rocm`, the build tools)
and every command it would run. From an installed release rather than a checkout, pass
`--requirements` (and `--build-requirements`) explicitly: the wheel carries no lock.

`fetch-pretrained` and provisioning fetch all four files from their pinned revisions
(`finetune.pretrained_downloads`), and only with `--allow-downloads`. A file that already
matches its pin is kept. Provisioning needs about 3 GB for packages on the Mac (on the PC the ROCm
torch wheel alone is a 6.2 GB download), 1.35 GB for the weights and 54 MB for the vocoder. Each download is verified against its pinned size and SHA-256
before it is published.

Every runtime subprocess (the probe, dataset packing, training and synthesis, from the
command line and from the app alike) runs with this process's environment plus
`PYTHONDONTWRITEBYTECODE=1` and `HF_HUB_OFFLINE=1`, so the runtime's Hugging Face libraries
download nothing. When the environment has no `HOME` (the app's frozen service has none), `HOME`
comes from the password database, because the runtime's libraries cache under it.
Synthesis (`resources/f5_synthesize.py`) loads the Vocos vocoder from the runtime's pinned
`pretrained/vocos-mel-24khz/` directory (`load_vocoder(is_local=True, local_path=…)`, passed
as the synthesis job's `vocoder_path`), never from the Hugging Face cache. Training loads no
vocoder: F5-TTS 1.1.22's `finetune_cli` loads one only for `--log_samples`, which the lab never
passes and the launcher refuses.

Synthesis keeps its temporary files inside the library. F5-TTS 1.1.22's
`preprocess_ref_audio_text` writes a converted copy of the reference clip, a recording of
Cole, to the temporary directory and never removes it. The synthesizer therefore creates a
private (0700) work directory, `synthesis-tmp/`, in its output directory (the job's
`work_directory`), replacing one an interrupted run left there. It makes that directory the
temporary directory (`tempfile.tempdir` and `TMPDIR`) before F5-TTS is imported, and removes
it when synthesis ends, whether it succeeded or failed. Listening samples and Speak clips
alike fail `FINETUNE.RUNTIME_FAILED` when the directory is still there after the
synthesizer exits. A synthesizer that is killed (a cancelled or interrupted job, a stopped
service) cannot remove it. In a samples directory it then stays until the next synthesis
into that directory; a Speak request's directory is removed with it ("Speak page" below).
Synthesis runs before 2026-09-16 wrote these copies to the user's temporary directory
(`$TMPDIR`, files named `tmp….wav`) and left them there. The lab does not remove them;
delete them by hand if they are unwanted.

Training also writes inside the runtime tree. The packed dataset goes to
`data/<run-id>_pinyin`, and `ckpts/<run-id>` is a symlink to the run's `checkpoints/`.
`finetune run` and the app remove that link when training fails, is interrupted (Ctrl-C) or
is cancelled. A finished run keeps it, unless its app job is cancelled or fails while writing
samples. Neither removes the packed dataset.

The launcher (`resources/f5_finetune_launcher.py`) wraps F5's own `finetune_cli` and only
changes three things for a 24 GB Mac: two dataloader workers instead of sixteen, activation
checkpointing (without it the run reached a 20 GB footprint and swapped, 88 s per update
instead of 6), and light checkpoints without optimizer state. It refuses `--log_samples`
(exit 2), which would load an unpinned vocoder.

## Defaults and what to change

| Setting | Default | When to change |
| --- | --- | --- |
| `--epochs` | 120 (about 1,080 updates on 34 clips) | halve it if the fine-tuned samples sound worse than base |
| `--learning-rate` | 1e-5 | rarely |
| `--batch-frames` | 3200 | 1600 if `footprint <pid>` exceeds about 12 GB |
| `--nfe-steps` (samples) | 32 | 16 for a quick listen |
| `--device` (samples) | `mps` | `cpu`, or `cuda` on the PC (ROCm answers to torch's `cuda`) |
| `--reference-prompt-id` | first corpus clip | any clip in the corpus |

Run one training job at a time and no other heavy work on the machine while it runs.
`finetune run`, `finetune train` and `finetune synthesize` refuse with `FINETUNE.JOB_ACTIVE`
while the recording app's service holds a queued or running job
(`provenance/finetune-jobs/head.json`) or runs a Speak request
(`provenance/finetune-jobs/speech-active.json` names a service process that is still alive).
Both files are read without a lock and without writing; a damaged one is refused by name.
The app does not see work started from the command line, so do not press Fine-tune model or
Speak while it runs.

## Archiving a finished run

A finished run is the largest thing in the library: 3–4 GB, of which about 2.7 GB is
checkpoints. `voice-lab finetune archive` copies a run to the library vault, proves the copy
is readable there, and can then remove the run's checkpoints from the Mac;
`voice-lab finetune restore-run` brings them back. The vault itself — the encrypted,
content-addressed copy of the whole library — is described in
[LIBRARY_VAULT.md](LIBRARY_VAULT.md).

```sh
voice-lab finetune archive     --asset-root <library> --run <run-id> [--remove-local-copy]
voice-lab finetune restore-run --asset-root <library> --run <run-id> [--runtime-root <f5-runtime>]
```

**Runs are an opt-in.** A fine-tuned checkpoint and the dataset's clips are enough to clone
the voice, so they are their own file class, `training`. Archiving needs a library bound to a
vault whose binding copies that class (`voice-lab vault bind --classes … training`) and, for
a tailnet vault, a destination approval that covers it too. Anything else is
`STORAGE.NOT_APPROVED`, naming the opt-in; an unbound library is `STORAGE.NOT_BOUND`. No
vault is deployed yet. D-020 authorizes the standing destination approval and ledger
model for encrypted storage; deployment and Cole's training opt-in remain Step 15.
The container templates are in [the host runbook](../deploy/vault/README.md), and
[RUNBOOK.md](RUNBOOK.md#9-library-vault) covers Mac approval, binding and restoration.

**What `archive` does**, in order. It refuses a run that is not `finished`, and refuses while
the app's service holds a fine-tune job or a Speak request (`FINETUNE.JOB_ACTIVE`). It then
pushes the whole library once, which is how a run reaches the vault; a push whose pass did
not carry this Mac's fine-tune runs (a pass that meets a job or a Speak request keeps the
previous generation's run entries) archives nothing and says so. It verifies the run in the
generation that push left: every chunk of every entry under `derived/finetune/<run-id>/` is
downloaded, decrypted and compared with the digest the manifest names, so nothing rests on
the vault's word. Only then does it write `archived.json`, and push once more so that record
is in the vault too. The command prints a JSON document with the generation, the manifest
digest, the number of verified files and what was removed.

| Path under `derived/finetune/<run-id>/` | What it is |
| --- | --- |
| `archived.json` | the vault holds a verified copy: library id, destination digest, generation, manifest digest, and every file of the run with its digest, size and kind (`finetune-run-archive`) |
| `local-removal.json` | the checkpoints were removed from this Mac, and the generation whose manifest holds them (`finetune-run-removal`) |
| `local-removal-restored-<n>.json` | the removal record of a finished restore, kept as history |

Each is written once, 0600, beside `run.json`, and is never replaced.

**`--remove-local-copy`** removes the run's checkpoints, and only those: `checkpoints/**` and
nothing else. `run.json`, the dataset, the corpus record and both sample sets stay, because
every status reply reads them and Speak reads the dataset clips. The removal verifies the run
in the vault again, asks the app's job and Speak questions again (the first check is minutes
and gigabytes old by then), writes `local-removal.json` before the first unlink, and hashes
every file it is about to unlink first: a file that is not the one the record names is
refused (`FINETUNE.SOURCE_CHANGED`), because a run retrained after its archive has newer
checkpoints the vault never took. A crash in the middle leaves the record, and either
`restore-run` or the same command again finishes the job. The directories stay, so the
runtime's `ckpts/<run-id>` symlink still points at `checkpoints/`.

**The pinned base weights.** `checkpoints/pretrained_<name>.safetensors` is a hard link to
the runtime's pinned F5 base weights (1.35 GB). The vault records it by that pin and never
holds its bytes, and unlinking it frees nothing, because the runtime holds the same file.
Removing a run therefore frees about 2.7 GB of checkpoints, not the run's full size.

**Local or archived.** Each run summary in the service's `fine-tune-status` reply carries
`location` (`local` or `archived`) and `in_vault` (`archived.json` is there); they are
independent facts. A run counts as archived only while `local-removal.json` is beside its
`run.json` **and** a file that record names is missing. Both halves matter: a whole-library
restore writes the checkpoints back beside the record it also carries, so a library restored
from the vault reports its runs as `local`, and a crash between the record and the first
unlink leaves a run that can still speak. A removal record that cannot be read at all counts
as archived, so a damaged record never offers Speak a run whose checkpoints are gone.
Speak marks Latest and named archived runs with ` · Archived` and disables generation
with `Archived — restore with voice-lab finetune restore-run --run <id>`. The sidebar's
last line reports vault copying status independently of that run's availability.

**An archived run is refused.** `finetune dataset`, `finetune train`, `finetune run` and
`finetune synthesize` refuse it with `FINETUNE.RUN_ARCHIVED` and one wording: run `<run-id>`
is archived, restore it with `voice-lab finetune restore-run --run <run-id>`. Speak refuses
the same way, for a named run and for `latest`. `base` does not: it speaks with the pinned
base weights and only borrows the newest finished run's dataset clip as its reference, and a
removal never takes the dataset.

**`restore-run`** needs `local-removal.json`; a run that was never removed has nothing to
restore. It refuses while the app holds a job or a Speak request, checks free space (the
files' bytes, a tenth more, and a 256 MiB margin), then downloads and decrypts each file,
checks it against the manifest entry, and renames it into place from a private temporary
without ever replacing anything. A file already there with the recorded bytes is kept. With
`--runtime-root` it re-links the pinned base weights and refuses a runtime that does not hold
the pinned bytes (`FINETUNE.PRETRAINED_MISMATCH`, naming it); without one the pin is reported
missing and everything else is still restored, because only F5's trainer reads that link and
a Mac rebuilt from the vault often has no runtime yet. Last, `local-removal.json` is renamed
to `local-removal-restored-<n>.json`, and that is what makes the run local again. When every
recorded file is back already — a whole-library restore, or a crash before the first unlink —
no vault is opened at all and only the record is kept as history.

**The removed files stay in the vault.** The removal record names the generation whose
manifest holds them, and this Mac keeps a copy of every manifest under
`provenance/vault/destinations/default/manifests/`. Each later generation carries those entries forward from that
copy, so the vault's newest generation still names a removed run's checkpoints and a
whole-library restore brings them back. While the copy is missing (`voice-lab vault bind
--reset-state` moves the copies aside), `vault push` reports the removed files as waiting
instead of carrying them, and both `--remove-local-copy` and `restore-run` refuse by name.

## From the app

The native recording app (a build whose settings name `finetune_runtime_root`,
[NATIVE_RECORDING_APPLICATION.md](NATIVE_RECORDING_APPLICATION.md)) has a **Fine-tune
model** button on the Record page. What the operator sees (availability, the progress
line, cancelling) is specified in
[NATIVE_RECORDING_UX.md](NATIVE_RECORDING_UX.md) §16. This section covers what the button
runs. Module: `src/cole_voice_lab/recording_finetune_jobs.py` (`@canonical finetune_jobs`).

**What runs.** The button sends one `fine-tune` request with `allow_missing: true` and no
epoch override. The recording service accepts it in metadata time. It checks only that a
runtime is configured and `<runtime>/bin/python` exists (`FINETUNE.RUNTIME_UNAVAILABLE`), that the library's disk has
at least 8 GiB free (`RECORDING.DISK_SPACE`), and that no other job is queued or running
(`FINETUNE.JOB_ACTIVE`). It records, in the job's request, the catalog version the service
pinned at launch, which is the version the app shows (a pin that cannot be resolved refuses
the submit under the corpus store's code). A worker process (`python -m
cole_voice_lab.recording_finetune_jobs --worker`) then does the rest:

1. It reads that pinned snapshot as a file; the corpus store is not opened. A later
   `voice-lab catalog activate` therefore takes effect for jobs only after the app is
   relaunched, together with the Library. (A group-scoped service, which pins nothing, reads
   the subject's active version; the store is open only for that.)
2. It calls `run_finetune_pipeline`, the chain behind `finetune run`, with that version:
   the default hyperparameters (120 epochs), the default sample sentences, the first
   corpus clip as the reference, EMA weights, 32 NFE steps on MPS, and the output root
   `derived/finetune`.

With `allow_missing` the corpus is the spoken prompts that have a kept take. The active
`cole-corpus` catalog lists the Harvard and CMU ARCTIC sentences, which mostly have none.
As on the command line, `allow_missing` also accepts records that could not be read. They
stay listed in `dataset/corpus.json` under `unknown_record_ids`. The pipeline reports each
stage (`selecting`, `dataset`, `training`, `sampling`) to the service. During training the
service also reads the tail of `train.log` for the pass, update, loss and time left. The
app gets at most one progress event every five seconds.

**Where results go.** Exactly where `finetune run` writes: `derived/finetune/<run-id>/`,
laid out as in the table above. The run id is `finetune-<UTC time>`, with `-2`, `-3`, … when
that directory already exists. The job itself is recorded under
`provenance/finetune-jobs/`:

| Path | What it is |
| --- | --- |
| `.owner.lock` | held by the one recording service that owns the queue |
| `head.json` | the one queued or running job, or none (`finetune-job-head`) |
| `requests/<operation>.json` | the accepted request and the pinned catalog version, written once (`finetune-job-intent`) |
| `terminal/<operation>.json` | the job's end: finished, failed with its error, or cancelled (`finetune-job`) |
| `speech-active.json` | while a Speak request's worker runs: its operation id, the service's process id and start time (`finetune-speech-active`, 0600) |

Unlike the command line, the app's service therefore writes under `provenance/`. The
service creates this directory whenever it starts, even in a build without a runtime.

**The finished runs.** `finetune.finished_finetune_run_ids` is the one ordering. It scans
`derived/finetune/` for real directories whose name is a stable id and that hold a
`run.json`, keeps those with status `finished`, and lists them by newest `finished_at`. On
equal times the greater run id comes first. It reads only the `run.json` files. Runs from
`finetune run` with the default `--output-root` count too; a `finetune dataset` directory
without `run.json` does not. `finetune.finished_finetune_runs` summarizes the newest 32
(`finetune_run_summary`, which reads each run's `run.json`, dataset, corpus and samples
records), and `finetune.latest_finished_finetune_run` is the first of them. A published
catalog's `corpus.json` is about 1.5 MB, so the service keeps each listed run's summary
(`finetune.FineTuneSummaryCache`) and reads a run again only when one of its seven records
(`run.json`, `dataset/dataset.json`, `dataset/corpus.json`, both `samples.json` and the two
archive records of "Archiving a finished run" above) has changed identity (device, inode,
mode, size, modification or change time) or appeared or disappeared, or when the run is no
longer where the summary says it is (`finetune.finetune_run_location`, which records alone
would miss: a removal resumed from an interrupted one's record writes no record, it only
unlinks). A repeated status reply therefore reads only the `run.json` files. With 32 such
runs the tests measured 1.26 s for the first reply and 6 ms for a repeat. A record that
cannot be read is never kept, so it fails every reply until it is repaired. The status reply
carries these as `finished_runs` (each with its Speak `reference_prompt_ids`) and
`latest_run`, which is always the first. A `run.json` that is unreadable, invalid, or names
another run or directory, or a listed run whose dataset, corpus or samples record cannot be
read or does not bind its run, leaves them unresolved: the status reply still names the
active job and whether a runtime is configured, and carries `latest_run_error`
(`FINETUNE.SOURCE_CHANGED`) instead of either list. Fine-tune model stays usable and its help
names the problem, while Speak is unavailable with that message; repair the record or move
it out of `derived/finetune/`.

**Cancellation.** A queued job is cancelled at once. For a running job the service sends
SIGTERM to the worker's process group, which holds the worker, the F5 runtime processes
and whatever they started. It sends SIGKILL after 10 s and repeats it until the group is
gone; a group still alive 10 s after that first SIGKILL stops supervision (below). Then it
rewrites `run.json` as
`failed` with "cancelled by the operator" while it still says `running`, removes the
runtime's `ckpts/<run-id>` link when it points at this run, and records the job as
`cancelled`. The link is released even when the `run.json` rewrite fails; the job then ends
failed, naming the first failure. Checkpoints, training copies and `train.log` already
written stay. A job cancelled or failing during `sampling` is different: training already
wrote a `finished` `run.json`, which stays finished. That run is a usable model (synthesis
reads `<run>/checkpoints/model_last.pt`, never the link) and counts as the latest finished
run, although its link is removed and its samples are missing or incomplete. The job itself
still ends cancelled or failed and says so: a cancelled job's `message` is "Fine-tune
cancelled after training: the model <run id> was trained, but its listening samples are
missing or incomplete", and a failed job's error message starts with that clause. The app
shows `Model ready · <run id> · no samples` (or `· samples incomplete`).

**Interruption.** The worker leads its own process group and runs under umask 077, so
everything it and the runtime create is private. It kills that group when its stdin
closes and after any failure of its own (it writes one JSON line naming the code first), so
training never outlives the service or the worker. When the app quits (it asks first while a
job is queued or running), the service kills the group and leaves the job recorded as
running. The next service start marks such a job `failed` with `FINETUNE.INTERRUPTED`. It
rewrites its `run.json` the same way and releases the link. It kills a group that is still
alive only when the group's leader has this job's worker command line for this library. If
the leader is gone, a remaining member must name the job's run id. A job is never resumed or
retried. While this recovery runs (rewriting `run.json` hashes the checkpoints), a status
request is answered at once and reports the job as running with the message "Checking the
last fine-tune". Recovery then publishes the job's run summary (when it has a `run.json`) and
its final state, keyed by its operation id. The service keeps these, and any job outcome or
supervision failure published before the app's handshake, and sends them right after the
handshake. The app shows the interruption as `Fine-tune interrupted: <message>`. A job still
queued when the service stopped starts when the service starts again.
`FINETUNE.INTERRUPTED` also ends a job whose worker cannot start, does not take its request
within 2 s, exits without a result, or runs longer than 24 hours. `terminal/<operation>.json`
and `run.json` record it.

**Supervision failure.** If the supervisor itself fails (damaged job evidence; a worker
group of a job or Speak request still alive 10 s after the SIGKILL of a cancel or deadline,
or 5 s after the SIGKILL that ends every worker), it reports `RECORDING.WRITE_FAILED`
"fine-tune supervision stopped; restart the app to recover the job" under request id
`finetune-supervisor`. When it cannot start because of a damaged Speak marker ("Speak page"
below), the same report carries
`RECORDING.SOURCE_CHANGED` and a message naming the entry and what to do. From then on every `fine-tune`, `cancel-fine-tune` and
`fine-tune-status` request is refused `RECORDING.GATE_NOT_READY` "fine-tune supervision
stopped; restart the app", so no second job starts while an old group may live. The next
service start recovers the job.

**Refusals and failures** reach the app under their own codes: every `FINETUNE.*` code
(`finetune.FineTuneErrorCode`, which the wire `ErrorCode` lists exactly), the corpus
store's `CORPUS.STORE_MISSING`, `CORPUS.NO_ACTIVE_CATALOG` and `CORPUS.SNAPSHOT_MISMATCH`
(any other store failure as `RECORDING.GATE_NOT_READY`), changed or unsafe evidence as
`RECORDING.SOURCE_CHANGED`, and file-system failures as `RECORDING.WRITE_FAILED`. The app
shows the message verbatim.

**The runtime** is the build setting `finetune_runtime_root`, passed to the service as
`--finetune-runtime-root`. Each job checks the runtime's weights and Vocos vocoder against
the pins above, and nothing in the shipped service can change those pins. The native
`finetune` verification suite substitutes a stub runtime's pins through a test-only wrapper
(`tests/native_finetune_service_fixture.py`) that is never part of a build. The runtime
environment is the same as on the command line: `HF_HUB_OFFLINE=1`, and `HOME` from the
password database, because the frozen service's environment has no `HOME`.

**Recording while training.** The worker takes no library lock, never opens the recording
index, and reads takes read-only. It retries its selection for up to 10 s while a Save
commits. A job trains on the takes that were kept when it selected them.

## Speak page

The native app's **Speak** page (D-019) speaks typed text with a finished fine-tuned run or
the pinned base weights. What the operator sees is specified in
[NATIVE_RECORDING_UX.md](NATIVE_RECORDING_UX.md) §17. This section covers what the service
runs. Modules: `recording_finetune_jobs.py` (the supervisor's second job kind) and
`finetune.py` (`resolve_speech_model`, `synthesize_speech`, `discard_speech_output`,
`SpeechClip`).

**Requests.** `synthesize` (`SynthesizeCommand`) carries an operation id and the text: 1 to
2,000 code points, not only whitespace, and no format or control character except a
newline. It also carries the model (`latest`, `base` or a run id), an optional
`reference_prompt_id`, and `nfe_steps` (4 to 64, default 32; the app always sends 32).
`cancel-synthesis` (`CancelSynthesisCommand`) names the request to stop. The fine-tune
supervisor answers both, before and without an open recording context, in a workspace
service and a group-scoped service alike. Every state is a `SynthesisEvent`.

**Accepting a request** takes metadata time only. The service checks, in order:

1. supervision is running (else `RECORDING.GATE_NOT_READY`);
2. the operation id is new, or names the identical request, whose current state is then
   returned (a different request under a used id is `RECORDING.INVALID_MESSAGE`);
3. no other Speak request and no fine-tune job is queued or running (`FINETUNE.JOB_ACTIVE`,
   retryable);
4. a runtime is configured and has `bin/python` (`FINETUNE.RUNTIME_UNAVAILABLE`);
5. some run has finished (`FINETUNE.NO_FINISHED_RUN`), judged from the `run.json` files
   alone; records that cannot be read are left to the worker, which names the failure;
6. `derived/speech/<operation id>/` does not exist (`FINETUNE.RUN_EXISTS`).

Nothing is probed, hashed or written for a refusal. The reply is `queued`. The service also
publishes `queued`, then `running` when the worker starts, then exactly one of `finished`,
`failed` or `cancelled`, each under the operation id as request id (the app sends the submit
under another request id). There is no progress in between. Requests are kept in memory
only; the one file the service writes under `provenance/` for them is the running request's
marker ("One GPU slot" below). A service stop ends a request without a final event. The
service remembers its last 64 ended requests for resends and cancels.

**Model and reference.** The worker resolves the request (`finetune.resolve_speech_model`):

- `latest` is the first of `finished_finetune_run_ids` ("The finished runs" above), resolved
  when the worker runs.
- `base` is the runtime's pinned F5-TTS v1 Base weights. Its reference comes from that same
  newest finished run, because a reference needs recorded audio. With no finished run,
  every choice is refused `FINETUNE.NO_FINISHED_RUN`. (The speech API's base voice, D-055,
  is the same pinned weights with F5-TTS's public English reference clip instead, so it needs
  no finished run: docs/SPEECH_API.md §3.)
- Any other value must name a finished run (`FINETUNE.NO_FINISHED_RUN` names the missing or
  unfinished run). A run named `latest` or `base` cannot be chosen by its id.
- The run that would speak must still have its checkpoints, else `FINETUNE.RUN_ARCHIVED`
  names the restore command ("Archiving a finished run" above). `base` is never refused for
  that: it borrows the newest run's dataset clip, which a removal never takes.
- The run's `run.json`, `dataset/dataset.json` and `dataset/corpus.json` must bind each
  other and the directory (`finetune.load_finetune_run_inputs`), else
  `FINETUNE.SOURCE_CHANGED`.
- The reference is `reference_prompt_id`'s clip in the reference run's dataset
  (`FINETUNE.REFERENCE_MISSING` when that run has none). Without one, it is the first prompt
  in the run's `corpus.json` marked `reference_candidate` that has a clip in `dataset.json`
  (`finetune.speech_reference_candidates`, catalog order), else the run's first clip. Each
  run summary in the status reply lists these candidates as `reference_prompt_ids` (at most
  64), and the page's reference menu shows them.

Then `finetune.synthesize_speech` checks the text again (`FINETUNE.UNSUPPORTED_TEXT`) and
creates `derived/speech/<operation id>/` (0700; `FINETUNE.RUN_EXISTS` if it exists). It
describes the runtime with every pin, as training does. It checks the checkpoint: for a
fine-tuned run, the `checkpoints/model_last.pt` that `run.json` records, which must still
hash to its recorded digest (`FINETUNE.CHECKPOINT_MISSING`, `FINETUNE.SOURCE_CHANGED`); for
`base`, the pinned weights. The reference clip must still hash to its digest in
`dataset.json` (`FINETUNE.SOURCE_CHANGED`). The synthesizer (`resources/f5_synthesize.py`,
`file_prefix` `speech`) then runs once in the runtime, with EMA weights on MPS and the
reference clip's exact text. The worker never opens the take store or the corpus store.
Finished runs are never written.

**Output.** One directory per request:

| Path | What it is |
| --- | --- |
| `derived/speech/<operation id>/speech-01.wav` | the clip: 24 kHz mono 16-bit PCM, 0600 |
| `derived/speech/<operation id>/speech.json` | its record (`finetune-speech`, `SpeechClip`), 0600, written last |

`speech.json` holds relative paths only. It records:

- `operation_id` and `created_at`;
- `model`: `base`, or the id of the run that spoke (a `latest` request records the run it
  resolved to);
- `reference_run_id`, `reference_prompt_id` and `reference_audio_sha256`;
- `checkpoint_sha256`, `use_ema` and `nfe_steps`;
- `text`;
- the clip's `relative_path`, `sha256`, `frames` (from the WAV header), `sample_rate_hz`
  (24000) and `seconds` (frames / 24000).

The synthesizer's `job.json`, which holds absolute paths and the text, and its
`synthesis-tmp/` work directory, which holds F5-TTS's copy of the reference clip, are
removed when the synthesizer exits. Before anything is reported, the clip is made 0600 and read back from its header. A
file that is not PCM WAV, is not 24 kHz 16-bit mono, or is empty or truncated fails
`FINETUNE.RUNTIME_FAILED`, naming what it is. So does a synthesizer that reports another
file or another count. The finished event repeats the clip's path, digest, frames, rate and
seconds. The supervisor refuses a result whose text or step count differs from the request
(`RECORDING.INVALID_MESSAGE`).

A request that does not finish (failed, cancelled or past its deadline) has its directory
removed (`finetune.discard_speech_output`), unless it failed `FINETUNE.RUN_EXISTS`, when the
directory was not its own. If that removal fails, a failed request keeps its own error and a
cancelled one fails `RECORDING.WRITE_FAILED` ("the Speak request stopped but its partial
output was not removed: …").

A request ended by a service stop or crash cannot clean up. Its directory has no
`speech.json` and may hold `job.json`, a partial clip and `synthesis-tmp/`. The next time
fine-tune supervision starts, before any request runs, the service removes every such
directory (`finetune.discard_unfinished_speech`). A directory with `speech.json` is a finished
request and is never touched. A directory named like an operation id without it is removed
only when it holds nothing but `job.json`, `speech-01.wav`, the JSON writer's temporary files
(`.job.json.tmp-…`, `.speech.json.tmp-…`), a Finder `.DS_Store` and `synthesis-tmp/` (regular
files and directories only). Anything else under `derived/speech/` is left in place untouched
and stops nothing: another file or a link in such a directory, a file directly in
`derived/speech/` (a Finder `.DS_Store` too) and a directory with another name. Nothing removes finished clips; they accumulate under
`derived/speech/`. They also never leave the Mac: the library vault does not copy
`derived/speech/**` (D-019).

**Deadline and cancel.** A worker may run 120 seconds (`SPEECH_DEADLINE_SECONDS`), counted
from its start, so loading the model is included. Past that the service kills its process
group with SIGKILL and fails the request `FINETUNE.SYNTHESIS_DEADLINE` (retryable). Cancel
kills the group with SIGKILL at once; unlike training, there is no SIGTERM grace. Either way
the request ends only once the group is gone. Until then it stays running, SIGKILL is
repeated, and nothing else can start. A group still alive 10 s after the first SIGKILL stops
supervision ("Supervision failure" above): the request then never ends, the app fails it
itself, and its marker stays. A worker that cannot start, does not take
its request within 2 s, or exits without a result fails the request `FINETUNE.INTERRUPTED`.
An invalid frame fails it `RECORDING.INVALID_MESSAGE`. The shipped service cannot change the
deadline. The native `speak` suite shortens it through the test-only wrapper
(`tests/native_finetune_service_fixture.py --test-synthesis-deadline`), which is never part
of a build.

**One GPU slot.** The supervisor runs one worker at a time, and Speak and fine-tuning share
that slot. A Speak request is refused while a fine-tune job is queued or running, including
a job the service is still recovering at launch, or while another request is queued or
running. A `fine-tune` submit is refused `FINETUNE.JOB_ACTIVE` ("Speak request <operation
id> is queued|running; fine-tune once it has finished or been cancelled") while a request
is queued or running. The slot is freed only after the worker's whole process group is
gone.

The command line sees the slot too. Just before a request's worker starts, the service
writes `provenance/finetune-jobs/speech-active.json` (`SpeechActiveMarker`: the operation
id, the service's process id and the start time; 0600). It removes the marker once the
worker's group is gone: after the request finished, failed or was cancelled, and when the
service stops. `voice-lab finetune run`, `train` and `synthesize` refuse
`FINETUNE.JOB_ACTIVE` ("the recording app's Speak request <operation id> is running")
while the marker names a live process, as they do while `head.json` holds a job
(`recording_finetune_jobs.active_speech_request`). A marker whose service is gone, left by
a crash, is ignored by the command line and removed when supervision next starts; one whose
process id still exists then is left for the service's next request to replace. A marker
that cannot be written fails the request `RECORDING.WRITE_FAILED` ("Speak could not record
that it is running: …") before any worker starts. One that cannot be removed stops
supervision. A damaged marker stops supervision at start (`RECORDING.SOURCE_CHANGED`,
"… speech-active.json is damaged …; remove it, then restart the app") and makes the
commands fail by name. The app does not see command-line work, so do not press Speak while
a `voice-lab finetune` command runs.

**The digest cache.** Describing the runtime hashes the 1.35 GB base weights, and checking a
checkpoint hashes about 2.7 GB. For the life of the service, the supervisor keeps what Speak
workers verified (`finetune.FineTuneDigestCache`): up to 16 file digests, the newest kept,
and the runtime probe (the F5-TTS and torch versions and the package path). It passes them
with each request (`FineTuneTrustedDigests`). A worker reuses a digest only while the file's
identity (device, inode, size, modification time and change time) equals the recorded one.
It reuses the probe only while the runtime's python and its site-packages directory are
unchanged. Anything else is hashed or probed again, and a pinned file must still match its
pin, so a changed file is still noticed while a repeat request skips the hashing. Fine-tune
jobs neither use nor fill this cache. With the stub runtime the tests show a repeat request
skipping the probe; real timings are left to the physical check.

**The worker.** A Speak request uses the same worker command as a fine-tune job (`python -m
cole_voice_lab.recording_finetune_jobs --worker --asset-root ROOT --operation-id OP`). It
reads one `SpeechWorkerRequest` frame and writes one `SpeechWorkerReply`
(`finetune-speech-worker-request`, `finetune-speech-worker-reply`). The reply carries the
clip or the error, the resolution when the request got that far, and what the worker
verified. The worker runs under umask 077 and kills its own group when its input closes or
it fails, as for jobs. Refusals and failures reach the app under their own codes, as listed
for jobs above, and the page shows the message verbatim.

**Command-line changes.** `finetune train` and `finetune synthesize` read a run's dataset
through `finetune.load_finetune_dataset`, which needs no `run.json`. A missing, invalid or
unbound `dataset.json` or `corpus.json` is therefore `voice-lab:
FINETUNE.SOURCE_CHANGED: …` (exit 2). `finetune.load_finetune_run_inputs` adds the run
record, and `finetune.finished_finetune_runs` lists finished runs as above. Listening
samples from `finetune synthesize` and `finetune run` are now private and header-checked
like Speak clips. The synthesizer writes 16-bit PCM explicitly.

## Not covered

Samples and Speak clips are for listening. They are not accepted raw, not a reference
publication, not a dataset manifest and not a voice release. The lab's transfer rules are
unchanged for any other use; speech for callers uses only the newest voice release, which
the speech job runner writes by itself for each newer finished run of `cole-self` that passes
its checks (or Cole pins), and the base voice while there is none (D-045, D-046, D-055). Neither `voice-lab finetune` nor the app (Fine-tune model or Speak) needs or
reads a consent record: Cole's standing decision (D-050) permits his own voice for
fine-tuning and listening on his own hardware, and consent records are retired. The app
does not play the listening samples, offers no epoch override or other training option, and
offers no step count, streaming or long-form synthesis on the Speak page.
