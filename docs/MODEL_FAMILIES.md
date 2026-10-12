# Model families

A model family is one kind of fine-tunable TTS model (D-048). F5-TTS is the only family
today. Cole asked for the code to admit others, so that once the corpus is complete (about 800
recordings) different models can be trained on it and compared for training time, inference
time and quality. Which family speaks is never a user setting: Cole designates a release of
one (D-045).

The corpus does not belong to any family. Its takes are 48 kHz 24-bit mono WAV with their
prompt text, and each family derives its own training format from them (sample rate,
loudness normalisation, transcript form), as the F5-TTS fine-tune does today with its 24 kHz
dataset.

## What a family provides to the speech path

`src/cole_voice_lab/speech/families.py` holds the registry and the `ModelFamily` port.
F5-TTS's entry is `speech/f5.py`. An entry gives:

| Member | What it is | F5-TTS |
| --- | --- | --- |
| `family_id` | a stable id, recorded in every release and ledger entry | `f5-tts` |
| `designate` | the files a release of a finished run binds, by role, and the family's section of the release record | `checkpoint` (the run's `model_last.pt`) and `reference` (a dataset clip); the reference prompt, its transcript's digest, the training runtime |
| `verify` | the release checked against the run's records again; the segment limit; what the backend needs | upstream's per-pass limit from the reference clip |
| `backend` | the family's `SpeechBackend`, from the configuration's runtime for it | the resident worker, `f5_resident.py` in the runtime's interpreter |
| `native_sample_rate_hz` | what the model renders at | 24,000 |
| `seeded` | whether a seed makes a render reproducible | yes |

The release (`VoiceRelease`), the render fingerprint and the ledger entry are common to every
family. The family-specific parts are two unions tagged by `family`: `ReleaseModel` (the
release's `model` section) and `RenderSettings` (what a render was asked for; their digest
seeds the job). The configuration's `runtime` is a third, tagged by `model_family`.

Whatever the model renders, the backend delivers 24 kHz mono 16-bit WAV, the API's one format:
a family that renders at another rate resamples inside its own runtime, and its fingerprint
records the native rate. A family that cannot seed says so (`seeded: false`), and its reruns
are not promised to be byte-identical.

## What a family provides to training

`src/cole_voice_lab/training.py` holds the `TrainingAdapter` port and its registry (D-049);
F5-TTS's adapter (`F5_TRAINING`) runs the existing F5-TTS functions of `finetune.py`. The
fine-tune pipeline (`finetune.run_finetune_pipeline`, which the command line and the Mac app's
job worker both call) first holds the hyperparameters, the adapter and the listening options
to one family (`training.require_trains`; a mismatch fails before the corpus is read), then
does the selection itself and every later step through the adapter:

| Member | What it does | F5-TTS |
| --- | --- | --- |
| `family_id`, `model_names` | the family, and the trainer model names it trains | `f5-tts`; `F5TTS_v1_Base` |
| `hyperparameters`, `listening_settings` | the family's settings from options by name (its defaults, then the options given; an option it does not have is refused) | `FineTuneHyperparameters`; `F5ListeningSettings` (reference prompt, EMA, flow steps, device `mps`, `cpu` or `cuda`) |
| `dataset_sample_rate_hz`, `normalise_text`, `build_dataset` | the family's training copies of a corpus selection: rate, loudness, transcript form, bound to the corpus by digest | 24 kHz mono 16-bit, level-normalized, `metadata.csv`; text as written (no `\|` or line break) |
| `describe_runtime` | bind the isolated runtime: interpreter, packages, pinned weights | `describe_finetune_runtime` (the adapter holds the pins' test seam) |
| `train` | launch in that runtime, block, write `run.json` with the runtime's identity | `run_f5_finetune` and `f5_finetune_launcher.py`; it refuses a dataset that is not 24 kHz in its record, each clip's record or each clip's header |
| `runtime_link`, `release_runtime_link` | a pointer the runtime keeps into the run, released when training fails or is stopped; `None` for a runtime that keeps none | the runtime's `ckpts/<run id>` link |
| `checkpoints` | the checkpoints a run holds, hashed | `checkpoints/model_*.pt` |
| `pinned_weight_pattern` | the name of the link to the pinned pretrained weights it leaves in `checkpoints/`, which the vault records by its pin and never uploads | `pretrained_*.safetensors` |
| `parse_progress` | the latest progress in the training log | tqdm's `Epoch E/N … update=U` |
| `listening_samples` | the fine-tuned and base samples, choosing the reference clip itself when the family has one | `synthesize_finetune_samples`; the settings' prompt, else the dataset's first clip |

A run names its family through its hyperparameters. F5-TTS's runs keep their own schema
(`FineTuneHyperparameters`, `cole-voice-lab.finetune-hyperparameters/v1`) and runtime
identity (`FineTuneRuntimeIdentity`), so every stored run reads, and re-serializes, byte for
byte as before (`tests/fixtures/pre-model-families/`, written by the code before families).
Another family's run carries `FamilyHyperparameters` (its family, model name, epochs, learning
rate and its own settings) and `FamilyRuntimeIdentity` (its packages, pinned weights and
training device); a run whose hyperparameters and runtime name different families is refused,
and neither may name `f5-tts`. A dataset's sample rate is the family's (8–192 kHz). A run's
summary for the app (`FineTuneRunSummary`) names its `model_family` (default `f5-tts`, so a
summary without it reads as before).

Speak, and the speech path's F5-TTS family which resolves through the same seam
(`finetune.resolve_speech_model`), render F5-TTS runs only (`SPEAK_FAMILIES`): `latest` and
`base` are the newest finished F5-TTS run, never a newer run of another family, and naming
another family's run is refused.

## Comparing families

`voice-lab families record --run <id>` writes one record per measurement,
`provenance/model-comparisons/<run id>/<comparison id>.json` (0400, write-once, copied to the
vault like the ledger; D-049):

- **training**: the family and trainer model, the corpus and dataset (digests, clips,
  seconds, rate), the epochs and the last update in the run's log, the wall time from its run
  record, the device (`--training-device` when the run's runtime did not record it), the
  training host (`training-host.json`, written into the run directory before it trains: the
  CPU model and count, the memory, each GPU's PCI ids, name, driver and VRAM, the ROCm
  release, or MPS; `null` for a run trained before hosts were recorded), and the runtime
  identity;
- **inference**, with `--benchmark --config /etc/voice-lab/speech.json`: the
  fixed, public, held-out text set `benchmark-v1` (six sentences; refused if any is a text the
  run trained on), rendered sentence by sentence through the same `SpeechBackend` port as the
  speech API, holding the GPU slot (kind `benchmark`). The seeds derive from the run, its
  files' digests, the text set and the render settings only, never from the preview's date,
  so a rerun renders the same segments. It records the load time, the time to the first
  segment and to the first audio, the real-time factor, the peak accelerator memory with how
  the backend measured it (F5-TTS's worker: `torch.cuda.max_memory_allocated`, its own
  tensors since it started, not the allocator's reserve or other processes), whether a rerun
  of the first segment is byte-identical, the host's fingerprint, whether it ran in the
  sandbox, and, as advisory fields, the GPU's other users before the load and after the
  model is unloaded (every card's VRAM in use and the names of the other processes holding
  the ROCm device, never the benchmark itself or its resident worker, as far as an
  unprivileged process can see: the PC's llama-swap shares the card). The run is rendered as
  an unwritten release preview, so only a finished run of the pinned subject (`cole-self`)
  renders, and no consent record is read (D-050); the audio is hashed in the benchmark's own
  scratch under the speech state (`state/speech/benchmark/`, never the lab, whose `tmp/`
  holds raw take originals) and deleted (scratch a killed benchmark left is swept by the next
  one, and a file that cannot be removed is logged), never kept or served. It works for the
  designated run and for any candidate run. **On the PC it runs only in its unit**, the
  oneshot `voice-lab-speech-benchmark.service` (deploy/host-manual/; the run in
  `/etc/voice-lab/speech-benchmark.env`): the speech runner's fences, so no network and
  only the GPU's devices. The command refuses to render unless the unit's marker
  (`VOICE_LAB_SANDBOX=voice-lab-speech-benchmark`) is set and no network interface but
  loopback is visible, or `--i-render-outside-the-sandbox` is given (the record then says
  `sandboxed: false`). `sandboxed: true` records exactly those two observations, not the
  unit's other fences: a shell under `unshare -rn` with the marker set would pass;
- **quality**, with `--blind-review <id> --score-sheet <path> --score-sheet-sha256 <digest>`:
  a link to the blind review that judged the run, made from the score sheet itself: its
  digest must be the one given, it must be that review's, and it must verify against the
  sealed review package (`blind.aggregate_scores`), and the review must have scored the run
  (candidate id: the run id, or `--review-candidate`); a review that did not is never linked.
  The link carries the run's sample count, mean per dimension and overall mean. Blind listening stays the decision signal; the numbers are
  diagnostics.

`voice-lab families compare [--run <id> …]` prints `rows`, one per run composed from its
records: the newest training summary, the newest benchmark and the newest review, each with
the record it came from and when that was recorded (a later record without a benchmark never
hides an earlier one's), and `warnings`: a run trained on another corpus than the others
(`corpus_differs`), benchmarks or trainings on different hosts, a benchmark that shared the
GPU, and one run outside the sandbox. Measure every family on the same corpus selection, text
set, host and render settings, with nothing else on the GPU.

## Adding a family

Every step below is needed; the tests named fail until each is done.

1. **A runtime lock.** The family's isolated environment, hash-locked under
   `requirements/runtimes/`, with its pinned weights (immutable revisions, D-004). It never
   shares an interpreter with voice-lab or another family.
2. **A training adapter.** A `TrainingAdapter` in `training.py`'s `_registry`: its settings
   (`hyperparameters` building `FamilyHyperparameters` with its `family`, and its own
   `listening_settings` model), its dataset from the corpus selection, its runtime check, its
   launcher in its runtime recording `FamilyRuntimeIdentity`, its progress parser, its
   checkpoints, its listening samples (choosing its reference, if it has one), and its
   `pinned_weight_pattern`. The link to the pinned weights it leaves in `checkpoints/` must be
   named `pretrained_<the weights' file name>`, which is what the vault's pin check compares.
3. **Its pinned weights in the vault's table.** The same pattern under its family in
   `library_classification.PINNED_WEIGHT_FILES` (`test_training_families.py` holds the two
   equal); the rule `fine-tune-pretrained-link-<family>` follows from it.
4. **A speech backend.** A `SpeechBackend` for the family: load a verified release, render one
   segment with a seed into a 24 kHz mono 16-bit WAV within a deadline, report its peak memory
   and how it measured it (`peak_memory`) if it can, unload. A resident worker in the family's
   runtime, speaking JSON lines as `f5_resident.py` does, keeps the network out and the text
   off disk.
5. **The speech registry and contracts.** A `ModelFamily` in `speech/families.py`'s
   `_registry` (every training family must have one, and the reverse:
   `test_training_families.py`), a member in `ReleaseModel` and `RenderSettings`
   (`speech/contracts.py`) and in the configuration's `SpeechRuntime` (`speech/config.py`).
6. **Its id once.** A `<FAMILY>_FAMILY` constant beside `F5_FAMILY` in `models.py`, imported
   by the adapter, the speech family and the contracts.
7. **The command line.** Nothing to add for `finetune dataset|train|run --family <id>` (the
   choices come from the registry); options the family does not have are refused by its
   `hyperparameters`. `finetune synthesize` and Speak stay F5-TTS's (below).
8. **Regenerate the schemas** (`python -m cole_voice_lab.schema --output schemas`).

Tests use synthetic audio only: a stand-in worker that writes a generated tone, as the F5-TTS
backend's tests do, and adapter doubles that write synthetic records.

## What is still F5-TTS's shape

Common code that still assumes F5-TTS, kept so because changing it would change stored
records or the app's contracts before a second family exists:

- **The listening-sample record** (`FineTuneSampleSet`, `samples/<label>/samples.json`) has a
  reference clip, EMA and flow steps, and its device is one of `mps`, `cpu`, `cuda`. A family
  without a reference clip, or with other settings, needs that record generalised (its
  reference fields optional, its settings a union tagged by family) when it is added.
- **The app's fine-tune** (`recording_finetune_jobs`) trains `APP_TRAINING_FAMILY`, F5-TTS: its
  intent names no family, its progress parser and pin seam are F5-TTS's, and the runs its
  status lists (the latest and Speak's choices) and Speak's "no finished run" check are
  F5-TTS's runs only (`SPEAK_FAMILIES`).
- **Speak** (`synthesize_speech`, `resolve_speech_model`) and **`finetune synthesize`** render
  F5-TTS only.
- **`run_f5_finetune`'s 24 kHz dataset**, `FINETUNE_SAMPLE_RATE_HZ` and the sample clip check
  are F5-TTS's own, reached only through its adapter.
- Training adds no consent check: consent records are retired (D-050).

## Candidates (unverified)

Open, fine-tunable families that could be compared once the corpus is complete. Written from
memory on 2026-10-12: **every licence, revision and capability here is unverified** and must be
checked at the source, with the weights' licence read separately from the code's, before a
family is adopted. Nothing paid or hosted (D-028).

| Family | Code licence | Weights licence | Native rate | Note |
| --- | --- | --- | --- | --- |
| F5-TTS (registered) | MIT | CC-BY-NC-4.0 | 24 kHz | reference clip; flow steps |
| StyleTTS 2 | MIT | own terms (disclose synthetic speech) | 24 kHz | style diffusion; no reference clip at render time after fine-tuning |
| GPT-SoVITS | MIT | MIT | 32 kHz | two models (GPT and SoVITS) and a reference clip |
| Piper (VITS) | MIT | per voice | 22.05 kHz | small, fast, no sampling steps; a speaker per model |
| XTTS-v2 (Coqui) | MPL-2.0 | Coqui Public Model Licence (non-commercial) | 24 kHz | speaker embedding from reference clips |
| CosyVoice 2 | Apache-2.0 | Apache-2.0 | 24 kHz (25 Hz tokens) | LLM and flow matching |
| Fish-Speech / OpenAudio S1-mini | Apache-2.0 | CC-BY-NC-SA-4.0 | 44.1 kHz | LLM with a codec |
| Chatterbox | MIT | MIT | 24 kHz | fine-tuning by community scripts |
| Orpheus TTS | Apache-2.0 | Llama 3.2 licence | 24 kHz (SNAC) | LLM-based; heavy for 16 GB |
