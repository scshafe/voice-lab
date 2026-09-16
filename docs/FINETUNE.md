# Fine-tuning from the recording library

`voice-lab finetune` turns the recording library into a fine-tuned F5-TTS voice in one
command and can be re-run after every revision of the corpus. It reads takes directly from
the library; lab packages, intake, dataset manifests and the pilot gate are not involved.
Module: `src/cole_voice_lab/finetune.py` (`@canonical finetune`). Decision: D-016.

## The corpus

The live corpus is **the latest kept take of every spoken prompt** in the configured catalog
for one subject, across native (app) takes and old-app (Qt) takes alike:

- retained means kept and not invalid, the same predicate the Library uses for coverage;
- the newest capture date wins; unknown dates sort oldest; a trim recipe on a native take is
  applied sample-exactly;
- room-tone and procedure prompts never enter; a prompt with no retained take is reported as
  missing, and the command refuses to continue unless `--allow-missing` is given;
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

`run` is `dataset` → `train` → `synthesize`; each is also a command on its own, all keyed by
`--run-id` (default `finetune-<UTC time>`), all writing under `<library>/derived/finetune/<run-id>/`:

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

Run one training job at a time and no other heavy work on the machine while it runs.

## Not covered

Samples are for listening. They are not accepted raw, not a reference publication, and not a
dataset manifest; the lab's transfer, consent-for-rendering and promotion rules are unchanged.
A button in the native app that calls `finetune run` is a separate change.
