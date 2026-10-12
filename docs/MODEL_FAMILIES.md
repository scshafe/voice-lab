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

`src/cole_voice_lab/training.py` holds the `TrainingAdapter` port and its registry; F5-TTS's
adapter (`F5_TRAINING`) runs the existing F5-TTS functions of `finetune.py`. The fine-tune
pipeline (`finetune.run_finetune_pipeline`, which the command line and the Mac app's job
worker both call) does the selection itself and every later step through the adapter of the
hyperparameters' family (`training.adapter_for`):

| Member | What it does | F5-TTS |
| --- | --- | --- |
| `model_names` | the trainer model names it trains | `F5TTS_v1_Base` |
| `dataset_sample_rate_hz`, `normalise_text`, `build_dataset` | the family's training copies of a corpus selection: rate, loudness, transcript form, bound to the corpus by digest | 24 kHz mono 16-bit, level-normalized, `metadata.csv`; text as written (no `\|` or line break) |
| `describe_runtime` | bind the isolated runtime: interpreter, packages, pinned weights | `describe_finetune_runtime` |
| `train` | launch in that runtime, block, write `run.json` with the runtime's identity | `run_f5_finetune` and `f5_finetune_launcher.py` |
| `runtime_link`, `release_runtime_link` | a pointer the runtime keeps into the run, released when training fails | the runtime's `ckpts/<run id>` link |
| `checkpoints` | the checkpoints a run holds, hashed | `checkpoints/model_*.pt` |
| `parse_progress` | the latest progress in the training log | tqdm's `Epoch E/N … update=U` |
| `listening_samples` | the fine-tuned and base samples | `synthesize_finetune_samples` |

A run names its family through its hyperparameters. F5-TTS's runs keep their own schema
(`FineTuneHyperparameters`, `cole-voice-lab.finetune-hyperparameters/v1`) and runtime
identity (`FineTuneRuntimeIdentity`), so every stored run reads, and re-serializes, byte for
byte as before. Another family's run carries `FamilyHyperparameters` (its family, model name,
epochs, learning rate and its own settings) and `FamilyRuntimeIdentity` (its packages, pinned
weights and training device). A dataset's sample rate is the family's (8–192 kHz).

## Comparing families

`voice-lab families record --run <id>` writes one record per measurement,
`provenance/model-comparisons/<run id>/<comparison id>.json` (0400, write-once, copied to the
vault like the ledger; D-049):

- **training**: the family and trainer model, the dataset (digest, clips, seconds, rate), the
  epochs and the last update in the run's log, the wall time from its run record, the device
  (`--training-device` when the run's runtime did not record it), and the runtime identity;
- **inference**, with `--benchmark --config /etc/voice-lab/speech.json --consent-id <id>`: the
  fixed, public, held-out text set `benchmark-v1` (six sentences; refused if any is a text the
  run trained on), rendered sentence by sentence with derived seeds through the same
  `SpeechBackend` port as the speech API, holding the GPU slot (kind `benchmark`). It records
  the load time, the time to the first segment and to the first audio, the real-time factor,
  the peak accelerator memory the backend reports (F5-TTS's worker reports torch's peak), and
  whether a rerun of the first segment is byte-identical. The run is rendered as an unwritten
  release preview, under an approved consent record for `private-home-host-synthesis` on
  `home-host`; the audio is hashed in the asset root's `tmp/` and deleted, never kept or
  served. It works for the designated run and for any candidate run;
- **quality**, with `--blind-review <id> --score-sheet-sha256 <digest>`: a link to the blind
  review that judged the run. Blind listening stays the decision signal; the numbers are
  diagnostics.

`voice-lab families compare [--run <id> …]` prints the newest record of each run side by side.
Measure every family on the same corpus selection, text set, device and render settings.

## Adding a family

1. **A runtime lock.** The family's isolated environment, hash-locked under
   `requirements/runtimes/`, with its pinned weights (immutable revisions, D-004). It never
   shares an interpreter with voice-lab or another family.
2. **A training adapter.** A `TrainingAdapter` in `training.py`'s `_registry`: its dataset from
   the corpus selection, its launcher in its runtime, its progress parser, its checkpoints, its
   listening samples, recording `FamilyHyperparameters` and `FamilyRuntimeIdentity`.
3. **A speech backend.** A `SpeechBackend` for the family: load a verified release, render one
   segment with a seed into a 24 kHz mono 16-bit WAV within a deadline, report its peak memory
   if it can, unload. A resident worker in the family's runtime, speaking JSON lines as
   `f5_resident.py` does, keeps the network out and the text off disk.
4. **One registry entry.** A `ModelFamily` in `speech/families.py`'s `_registry`, a member in
   `ReleaseModel` and `RenderSettings` (`speech/contracts.py`) and in the configuration's
   `SpeechRuntime` (`speech/config.py`), then regenerate the schemas
   (`python -m cole_voice_lab.schema --output schemas`).

Tests use synthetic audio only: a stand-in worker that writes a generated tone, as the F5-TTS
backend's tests do, and adapter doubles that write synthetic records.

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
