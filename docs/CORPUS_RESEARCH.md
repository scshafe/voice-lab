# Voice-recording corpus research

Research date: 2026-09-07. These are evidence-backed recommendations and open
questions, not an approved full-corpus suite or a verified training recipe.

## Current decision and scope

Cole will record the existing first pilot batch now and return to interface
review next. Do not expand the recording workload, change prompts, download
models, initiate training, or transfer audio based on this note.

The working end-use assumption is English in Cole's normal accent, emphasizing
conversational responses, explanations, and narration. This has not been
confirmed as the final application mix. Confirm it before allocating the full
corpus. The existing pilot/headphone-review/zero-shot-bake-off/full-corpus-gate
sequence remains authoritative; see [dataset boundaries](REFERENCES_AND_DATASETS.md#pilot-gate-and-full-corpus-boundary).

## Research conclusions

### Quality and quantity

Preserve clean, model-independent recordings that capture Cole's natural voice.
No reviewed source establishes a universal optimum number of minutes. A
FastPitch/HiFiGAN adaptation study compared 1, 5, 30, and 60 minutes for two target
speakers; 30 minutes could approach a much larger scratch-trained baseline.
That is evidence for incremental experiments, not a guarantee for modern
models or Cole. [Neekhara et al., 2022](https://arxiv.org/abs/2110.05798).

Use about 90 minutes of accepted training speech as an initial collection
budget, excluding silence, rejected takes, and evaluation material. Compare
nested subsets, such as 30, 60, and 90 minutes, under a documented training
budget and common validation protocol. Expand toward multiple hours only when
listening identifies persistent coverage or quality gaps. Do not assume that
more recordings or more training steps necessarily improve the voice.

### Proposed initial training mix

These allocations are a design hypothesis, not research-proven optimal quotas.
Content, phonetic coverage, and delivery style are overlapping dimensions;
every category should contribute useful sound coverage.

| Material | Accepted speech | Purpose |
| --- | ---: | --- |
| Natural conversation and explanations | 45 minutes | Default cadence, contractions, questions, answers, transitions |
| Narrative and connected passages | 15 minutes | Sentence-to-sentence flow, pacing, sustained delivery |
| Names, numbers, and domain vocabulary | 10 minutes | Dates, amounts, acronyms, technical terms, actual application vocabulary |
| Controlled expressive variation | 15 minutes | Warmth, reassurance, curiosity, restrained excitement, firm emphasis |
| Targeted sound-coverage gaps | 5 minutes | Natural sentences selected to fill remaining pronunciation gaps |
| Total training speech | 90 minutes | Initial research budget |

Separately budget roughly 10 minutes of validation and at least 10 minutes of
permanent held-out speech. Reserve several pristine short reference candidates
with explicitly assigned roles. Reference audio used as training conditioning
is training input, even if it is outside the target-utterance set; it must not
be represented as unseen test data. Room tone is quality evidence, not ordinary
paired speech-training material.

### Prompt selection and performance

- Measure phonemes and their neighboring sounds, word positions, vowel stress,
  consonant clusters, and word boundaries. Select readable sentences from a
  larger pool to fill gaps while maintaining realistic language frequencies.
  Pangrams cover letters, not necessarily the relevant sound contexts. The
  corpus-selection research supports this method, but does not prove an
  optimal allocation for a particular adapted model.
  [LREC-COLING corpus-selection paper](https://aclanthology.org/2024.lrec-main.93/).
- Keep the default voice natural rather than monotone. Capture useful questions,
  corrections, reassurance, and explanations. Prefer communicative scenarios
  over exaggerated acting instructions. Keep deliberate accent changes,
  character voices, whispering, and shouting in separately selectable material
  if they are later wanted. Consistent identity and natural prosodic variation
  are different objectives. [Coqui dataset guidance](https://docs.coqui.ai/en/latest/what_makes_a_good_dataset.html).
- As a portable starting point, use mostly 5–15-second utterances, some brief
  replies, and a smaller collection of longer multi-sentence passages. These
  are recording heuristics, not universal model limits. Preserve longer raw
  takes and paragraph context; derive model-specific clips at natural
  boundaries rather than cutting words to hit a duration. Sentence/context
  preservation is a documented TTS dataset concern.
  [LibriTTS paper](https://arxiv.org/pdf/1904.02882).
- Use original prompts and actual application vocabulary. Include names,
  acronyms, dates, decimals, currency, versions, and context-dependent
  pronunciations. Preserve displayed text and the exact intended spoken form.
  Fine-tuning is not a substitute for correct text normalization.
  [Coqui formatting guidance](https://docs.coqui.ai/en/latest/formatting_your_dataset.html).
- Every training transcript must match the audio. Retake a scripted substitution
  or preserve an accurate, provenance-bound transcript through an explicitly
  designed correction workflow; never silently change immutable capture
  evidence. Optional spontaneous responses require careful transcription.
  Removing spoken fillers or restarts from text can create alignment problems;
  ASR suggestions are not ground truth.
  [DisfluencySpeech study](https://arxiv.org/html/2406.08820v1).
- Preserve delivery labels separately from spoken text. A catalog label does
  not make expression controllable unless the selected training interface uses
  it. The basic Qwen single-speaker format is audio, transcript, and reference
  audio, not a general emotion-control contract.
  [Qwen training guide](https://github.com/QwenLM/Qwen3-TTS/blob/main/finetuning/README.md).

## Capture, references, and voice history

The existing master format is suitable: mono 48 kHz, 24-bit PCM WAV, with no
baked-in enhancement. Keep originals immutable; model-specific resampling,
trimming, normalization, or any later augmentation belongs in separately
tracked derivatives. Repeatable microphone placement, gain, room conditions,
posture, breaks, and headphone review matter. Preserve complete word boundaries
and short lead/tail silence in raw recordings.
[Recording guidance](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/record-custom-voice-samples).

The UMIK-1 is an omnidirectional measurement microphone with a 24-bit/48 kHz
interface. Assess reflections and noise in the pilot before considering an
equipment change; its specifications alone neither establish nor rule out
suitable speech quality. [miniDSP specifications](https://www.minidsp.com/products/acoustic-measurement/umik-1?showall=1).

Human review must check transcript match, natural delivery, clipping, plosives,
mouth clicks, noise, and reverberation. Automatic QC is a floor, not a listening
verdict. In particular, current QC computes an SNR estimate but does not enforce
a minimum SNR threshold. See [audio QC implementation](../src/cole_voice_lab/audio.py).

Retain dated takes without pooling them indiscriminately into training. Record
voice-period and session-condition notes when relevant, and deliberately choose
whether a dataset represents the current voice or a broader period. Recurring
anchor sentences can reveal session drift, but should not inflate apparent
breadth or leak into unseen-text evaluation.

## Evaluation and split discipline

Assign prompt groups and recording sessions to roles before full-corpus capture:

- Training updates model parameters.
- Validation selects settings/checkpoints and guides iterative recording.
- Permanent holdout is reserved for the final blind comparison; do not repeatedly
  optimize prompts or checkpoints against it and still call it untouched.
- Reference clips condition generation or training and have separately recorded
  roles and consent requirements.

All retakes, intentional style variants, and near-duplicate prompt groups should
stay in one partition. Existing code enforces session, prompt-ID, and exact-text
separation; semantic near-duplicate grouping is an additional curation need,
not an implemented guarantee. All three dataset splits must be nonempty, and
the default permanent-holdout minimum is 600 seconds.
[Dataset contract](REFERENCES_AND_DATASETS.md#building-a-no-copy-dataset).

Blind listening is the primary outcome: assess identity, naturalness, prosody,
intelligibility, missing/repeated words, and long-form stability separately.
Keep model revision, dataset digest, reference, text, seed where supported,
training budget, and output evidence fixed or explicitly accounted for.

## Model feasibility findings as of the research date

The documented hardware is a 24 GB M4 MacBook Air and a separate 16 GiB Apple
Silicon Mac mini inference target. Local inference support does not establish
training feasibility, memory fit, speed, or quality. The repository has an
inference/bake-off shortlist and dataset contracts, not a validated Mac
fine-tuning runner. [Current matrix](MODEL_MATRIX.md).

- Qwen3-TTS Base 1.7B, with 0.6B as a resource fallback, is a promising candidate
  to investigate. Official single-speaker SFT exists and recommends a consistent
  reference. The official script uses BF16/FlashAttention2 and is CUDA-oriented.
  Community Apple training implementations require a separate pinned audit and
  local smoke test; no memory or quality guarantee is established here.
  [Official guide](https://github.com/QwenLM/Qwen3-TTS/blob/main/finetuning/README.md),
  [trainer](https://github.com/QwenLM/Qwen3-TTS/blob/main/finetuning/sft_12hz.py).
- F5-TTS has an official paired-audio/text fine-tuning path, but this research
  did not establish a measured M4/24 GB training configuration. Its pretrained
  weights have a noncommercial license caveat relevant to later promotion.
  [Training guide](https://github.com/SWivid/F5-TTS/blob/main/src/f5_tts/train/README.md),
  [license statement](https://github.com/SWivid/F5-TTS#license).
- Chatterbox is a useful zero-shot comparator. No official user-speaker training
  recipe was established in this review. [Official repository](https://github.com/resemble-ai/chatterbox).
- Fish S2's released weights and inference claims do not establish Apple
  fine-tuning readiness. The reviewed fine-tuning documentation warns about
  quality regression when adapting RL-trained models and gives an S1-mini
  recipe, not a validated S2-on-Mac workflow.
  [Installation](https://speech.fish.audio/install/),
  [fine-tuning guidance](https://speech.fish.audio/finetune/).
- The July 2026 Qwen-Audio-3.0-TTS report is distinct from Qwen3-TTS. No local
  weights/training release was established by this research; do not treat the
  newer paper as an immediately installable successor.
  [Technical report](https://arxiv.org/html/2607.23938v1).

These are feasibility candidates, not a declared quality winner. Source URLs
may track mutable branches: recheck them and pin exact code, weight, and
environment revisions before any implementation. No hosted evaluation or
training service may receive audio without separate destination/payload-bound
approval; third-party training examples must be audited for hidden uploads.

## Current catalog gaps and deferred work

The [pilot](../src/cole_voice_lab/resources/pilot-v1.json) has 36 rows: 34 speech
prompts totaling 282 expected seconds (4m42s), plus two 15-second room tones.
These are expected durations, not evidence of completed recordings. Its category
breadth is illustrative; phonetic coverage has not been measured. The bundled
[evaluation suite](../src/cole_voice_lab/resources/evaluation-v1.json) has only
12 prompts/107 expected seconds, not the full 10-minute permanent holdout.

Before bulk capture, separately approve work to confirm end-use priorities,
measure prompt coverage, prepare distinct splits/sessions, and support their
capture workflow. The desktop currently rejects held-out-evaluation capture and
requires the approved pilot gate for full-corpus suites. Proposed pronunciation,
structured delivery, voice-period, and training-eligibility metadata are not
all implemented. Preserved take history alone is not a curated training set.

At the close of this research, the next conversation was interface review using
Cole's screenshot and feedback. Subsequent interface implementation has separate
user authorization; this research does not authorize expanded corpus work.
