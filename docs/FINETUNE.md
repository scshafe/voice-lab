# Fine-tuning from the recording library

`voice-lab finetune` turns the recording library into a fine-tuned F5-TTS voice in one
command and can be re-run after every revision of the corpus. The native recording app's
**Fine-tune model** button runs the same pipeline ("From the app" below). It reads takes
directly from the library; lab packages, intake, dataset manifests and the pilot gate are
not involved. Module: `src/cole_voice_lab/finetune.py` (`@canonical finetune`). Decisions:
D-016, D-018.

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
| `samples/<label>/sample-NN.wav`, `samples.json` | listening samples (`finetune-samples`) |

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

Training and synthesis run out of process in a pinned F5-TTS environment
(`requirements/runtimes/f5-tts-finetune-1.1.22-mps.txt`: torch 2.14 with MPS, f5-tts 1.1.22).
The runtime root must hold `bin/python` and the pinned pretrained weights at
`pretrained/F5TTS_v1_Base/model_1250000.safetensors` (Hugging Face `SWivid/F5-TTS` at revision
`84e5a410d9cead4de2f847e7c9369a6440bdfaca`, SHA-256
`670900fd14e6c458b95da6e9ed317cdb20dbaf7a1c02ac06a05475a9d32b6a38`) plus
`pretrained/F5TTS_v1_Base/vocab.txt`. `finetune train` refuses a runtime whose weights hash
differently. The first runtime root is `~/.local/share/cole-voice-lab/finetune-f5-01/venv`
(its `pretrained/` is a link to the sibling `pretrained/` directory the manual run downloaded).

```sh
voice-lab finetune provision-runtime --runtime-root <dir>                      # prints the plan
voice-lab finetune provision-runtime --runtime-root <dir> --execute --allow-downloads
voice-lab finetune fetch-pretrained  --runtime-root <dir> [--allow-downloads]  # verify or fetch
```

Provisioning needs about 3 GB for packages and 1.35 GB for the weights; each download is
verified against its pinned size and SHA-256 before it is published.

Every runtime subprocess (the probe, dataset packing, training and synthesis, from the
command line and from the app alike) runs with this process's environment plus
`PYTHONDONTWRITEBYTECODE=1` and `HF_HUB_OFFLINE=1`, so the runtime's Hugging Face libraries
download nothing. When the environment has no `HOME` (the app's frozen service has none), `HOME`
comes from the password database, because the runtime's libraries cache under it.
Synthesis loads F5's Vocos vocoder (`charactr/vocos-mel-24khz`) through Hugging Face, and
no revision or digest pins it. The runtime is offline, so the vocoder must already be in
the Hugging Face cache under that `HOME`. Otherwise sampling fails after training has
finished.

Training also writes inside the runtime tree. The packed dataset goes to
`data/<run-id>_pinyin`, and `ckpts/<run-id>` is a symlink to the run's `checkpoints/`.
`finetune run` and the app remove that link when training fails, is interrupted (Ctrl-C) or
is cancelled. A finished run keeps it, unless its app job is cancelled or fails while writing
samples. Neither removes the packed dataset.

The launcher (`resources/f5_finetune_launcher.py`) wraps F5's own `finetune_cli` and only
changes three things for a 24 GB Mac: two dataloader workers instead of sixteen, activation
checkpointing (without it the run reached a 20 GB footprint and swapped, 88 s per update
instead of 6), and light checkpoints without optimizer state.

## Defaults and what to change

| Setting | Default | When to change |
| --- | --- | --- |
| `--epochs` | 120 (about 1,080 updates on 34 clips) | halve it if the fine-tuned samples sound worse than base |
| `--learning-rate` | 1e-5 | rarely |
| `--batch-frames` | 3200 | 1600 if `footprint <pid>` exceeds about 12 GB |
| `--nfe-steps` (samples) | 32 | 16 for a quick listen |
| `--reference-prompt-id` | first corpus clip | any clip in the corpus |

Run one training job at a time and no other heavy work on the machine while it runs. The
app enforces one job at a time for its own jobs only. It does not see a `finetune run`
started from the command line, and the command line does not see the app's job, so do not
start both.

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
at least 8 GB free (`RECORDING.DISK_SPACE`), and that no other job is queued or running
(`FINETUNE.JOB_ACTIVE`). A worker process (`python -m
cole_voice_lab.recording_finetune_jobs --worker`) then does the rest:

1. It reads the subject's active catalog version from the corpus store. The store is open
   only for that, and the snapshot is read as a file.
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
| `requests/<operation>.json` | the accepted request, written once (`finetune-job-intent`) |
| `terminal/<operation>.json` | the job's end: finished, failed with its error, or cancelled (`finetune-job`) |

Unlike the command line, the app's service therefore writes under `provenance/`. The
service creates this directory whenever it starts, even in a build without a runtime.

**The latest finished run.** `finetune.latest_finished_finetune_run` chooses it. It scans
`derived/finetune/` for directories whose name is a stable id and that hold a `run.json`,
and picks the one with status `finished` and the newest `finished_at`. On equal times the
greater run id wins. Runs from `finetune run` with the default `--output-root` count too;
a `finetune dataset` directory without `run.json` does not. A `run.json` that is
unreadable, invalid, or names another run or directory makes the app's status request fail
with `FINETUNE.SOURCE_CHANGED`. Fine-tune model then stays unavailable until that record
is repaired or moved out of `derived/finetune/`. The app's status reply and the Speak
page plan both use this rule.

**Cancellation.** A queued job is cancelled at once. For a running job the service sends
SIGTERM to the worker's process group, which holds the worker, the F5 runtime processes
and whatever they started. It sends SIGKILL after 10 s. Then it rewrites `run.json` as
`failed` with "cancelled by the operator" while it still says `running`, removes the
runtime's `ckpts/<run-id>` link when it points at this run, and records the job as
`cancelled`. Checkpoints, training copies and `train.log` already written stay. A job
cancelled or failing during `sampling` is different: training already wrote a `finished`
`run.json`, which stays finished, so that run still counts as the latest finished run,
although its link is removed and it may have no samples.

**Interruption.** The worker leads its own process group and kills that group when its
stdin closes, so training never outlives the service. When the app quits, the service
kills the group and leaves the job recorded as running. The next service start marks such
a job `failed` with `FINETUNE.INTERRUPTED`. It rewrites its `run.json` the same way and
releases the link. It kills a group that is still alive only when the group's leader has
this job's worker command line for this library. If the leader is gone, a remaining member
must name the job's run id. A job is never resumed or retried. A job still queued when the
service stopped starts when the service starts again. `FINETUNE.INTERRUPTED` also ends a
job whose worker cannot start, does not take its request within 2 s, exits without a
result, or runs longer than 24 hours. The app shows no notice of an interruption at its
next launch. `terminal/<operation>.json` and `run.json` record it.

**Refusals and failures** reach the app under their own codes: every `FINETUNE.*` code
(`finetune.FineTuneErrorCode`, which the wire `ErrorCode` lists exactly), the corpus
store's `CORPUS.STORE_MISSING`, `CORPUS.NO_ACTIVE_CATALOG` and `CORPUS.SNAPSHOT_MISMATCH`
(any other store failure as `RECORDING.GATE_NOT_READY`), changed or unsafe evidence as
`RECORDING.SOURCE_CHANGED`, and file-system failures as `RECORDING.WRITE_FAILED`. The app
shows the message verbatim.

**The runtime** is the build setting `finetune_runtime_root`, passed to the service as
`--finetune-runtime-root`. Each job checks the runtime's weights against the F5-TTS v1 Base
pin above, and nothing in the shipped service can change that pin. The native `finetune`
verification suite substitutes a stub runtime's pin through a test-only wrapper
(`tests/native_finetune_service_fixture.py`) that is never part of a build. The runtime
environment is the same as on the command line: `HF_HUB_OFFLINE=1`, and `HOME` from the
password database, because the frozen service's environment has no `HOME`.

**Recording while training.** The worker takes no library lock, never opens the recording
index, and reads takes read-only. It retries its selection for up to 10 s while a Save
commits. A job trains on the takes that were kept when it selected them.

## Not covered

Samples are for listening. They are not accepted raw, not a reference publication, and not a
dataset manifest; the lab's transfer, consent-for-rendering and promotion rules are unchanged.
The app does not play the samples (the Speak page is a separate plan), and it offers no
epoch override or other training option.
