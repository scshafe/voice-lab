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

## Adding a family

1. **A runtime lock.** The family's isolated environment, hash-locked under
   `requirements/runtimes/`, with its pinned weights (immutable revisions, D-004). It never
   shares an interpreter with voice-lab or another family.
2. **A training adapter.** How the family's training data is built from a corpus selection
   and how a run is launched, reported and recorded (the follow-up of D-048).
3. **A speech backend.** A `SpeechBackend` for the family: load a verified release, render one
   segment with a seed into a 24 kHz mono 16-bit WAV within a deadline, unload. A resident
   worker in the family's runtime, speaking JSON lines as `f5_resident.py` does, keeps the
   network out and the text off disk.
4. **One registry entry.** A `ModelFamily` in `speech/families.py`'s `_registry`, a member in
   `ReleaseModel` and `RenderSettings` (`speech/contracts.py`) and in the configuration's
   `SpeechRuntime` (`speech/config.py`), then regenerate the schemas
   (`python -m cole_voice_lab.schema --output schemas`).

Tests use synthetic audio only: a stand-in worker that writes a generated tone, as the F5-TTS
backend's tests do.
