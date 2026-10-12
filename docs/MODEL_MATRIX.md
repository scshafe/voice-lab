# Model/runtime matrix (locked 2026-08-17)

The authoritative machine-readable records are
[`config/candidates.toml`](../config/candidates.toml) and
[`config/model-sources.lock.json`](../config/model-sources.lock.json). Every
runtime consumes a `worker-request/v1` JSON object on stdin, emits one
`worker-result/v1` object on stdout, and exits. Model downloads and runtime
installation are disabled unless the caller supplies the explicit
`allow_downloads` policy flag. A snapshot starts only when free space covers
the **10 GiB floor + its exact locked bytes + 256 MiB staging margin**. Runtime
installation reserves the floor plus its declared conservative estimate (6
GiB for MLX, 8 GiB each for F5 and Seed), then rechecks the floor after every
command and deletes only its own incomplete environment on failure.

## Candidate matrix

| Candidate | Runtime | Quality profile | Smoke profile | Output | License notes |
|---|---|---|---|---|---|
| Qwen3-TTS 1.7B Base | shared `mlx-audio==0.5.0`, commit `65fb3ccbecbbbb8b32fd625326fb0bc85d2ad03b` | `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16@a6eb4f68e4b056f1215157bb696209bc82a6db48` | `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit@e7dd0585652209fa0d7783659aad4e8a324de11c`, 512-token cap | 24 kHz direct TTS | Apache-2.0 weights; MLX code MIT; upstream code Apache-2.0 |
| Chatterbox Turbo | same MLX runtime | `mlx-community/chatterbox-turbo-fp16@b2d0a13aa7cfff0a06d9acb247ae91c8f19a6d75` | `mlx-community/chatterbox-turbo-8bit@2f2e21a03863f86a1274d1060dcc188e7cde77e1`, 512-token cap | 24 kHz English direct TTS | MIT upstream provenance. Converted-card Apache metadata is not treated as a new grant. |
| Chatterbox multilingual V3 | same MLX runtime | `mlx-community/chatterbox-multilingual-v3@03565773edd72e949572557597af8063bb49a18a` | same weights, 512-token cap | 24 kHz, 23-language direct TTS | MIT; recent MLX port is a canary and does not stream |
| F5-TTS v1 Base | separate official `f5-tts==1.1.22` MPS runtime; commit `9c614e9657089213efc6a7421b30630be138a3f5` | `SWivid/F5-TTS@84e5a410d9cead4de2f847e7c9369a6440bdfaca`, 32 NFE | same weights, 8 NFE | 24 kHz direct TTS | MIT code; **CC-BY-NC-4.0 weights**, so not commercial-use eligible |
| Seed-VC | separate Python 3.12 checkout/runtime at `51383efd921027683c89e5348211d93ff12ac2a8` | `Plachta/Seed-VC@257283f9f41585055e8f858fba4fd044e5caed6e`, 25 diffusion steps | same weights, 4 steps | 22.05 kHz performed-source voice conversion | GPL-3.0 code/model; auxiliary checkpoints retain their licenses; real 3.12/MPS smoke remains mandatory |

Turbo and V3 share the exact curated
`mlx-community/S3TokenizerV2@e0c9886f0e1c35ae85b1f27277416fb19fc72bec`
snapshot. The lock lists every allowed file, expected byte count, and available
upstream LFS SHA-256. Whole upstream repositories are never downloaded.

## Isolation and memory

One Python 3.12 MLX environment serves Qwen, Turbo, and V3 without dependency
conflict. It must not be a persistent multi-model server. On this 16 GiB Apple
Silicon host, the orchestrator takes an asset-root-wide file lock, starts one
fresh candidate subprocess, sends one request, collects evidence, waits for the
process to exit (the reliable Metal-memory release), then starts the next.
Candidate processes are never parallel.

F5 and Seed have separate environments. Seed additionally runs from its pinned
checkout because upstream uses working-directory-relative model caches and is
not distributed as a package. `--compile` is not used on MPS.

Only Seed provisions an executable Git checkout. Direct-TTS workers execute the
hash-locked installed distribution and record its exact bytes; their locked Git
revision remains upstream provenance and is not cloned as unused executable
state. This also avoids treating F5-TTS's unneeded BigVGAN gitlink as a runnable
checkout without separately pinning that submodule.

Each runtime has one complete CPython 3.12/macOS-arm64 artifact lock. One
isolated pip transaction uses `--require-hashes --only-binary=:all:` for the
entire graph, including pip itself; no un-hashed transitive artifact or sdist
may be installed. The separately reviewed lab wheel is digest-bound and
installed with `--no-deps --require-hashes`. F5 and Seed packages that upstream
publishes only as sdists must come from an exact local wheelhouse. Its seven
runtime entries (six unique wheels) bind source sdist bytes, deterministic build
recipe/environment, exact CPython executable bytes, hash-locked
packaging/setuptools/wheel tool wheels, and compiler/SDK evidence where native
code is built. Missing, extra, hard-linked, or rebound wheelhouse bytes fail.
F5 and Seed's locked setuptools artifact installs one executable
`distutils-precedence.pth`. The runtime lock binds that single path, its 151-byte
preimage, and SHA-256; preparation removes it immediately after the hash-locked
artifact transaction and before another runtime Python process starts. Missing
or changed preimages fail, the removal is recorded in the sealed receipt, and
any other executable or external `.pth` remains forbidden.

The base interpreter is not a version-only host assumption. Every runtime lock
binds the exact CPython 3.12.12 Homebrew keg and executable, the complete
mpdecimal/OpenSSL/readline/SQLite/xz formula trees and `opt` aliases, the macOS
product/build TCB, and the observed system-library closure. Preparation and
every pre/post worker receipt check rehash that closure. A normal `-I -P` site
probe rejects executable or external `.pth` entries and import roots outside
the sealed venv/interpreter closure; Mach-O extensions may link only inside the
sealed venv, locked formulae, or the exact-build macOS system TCB.

The lab wheel is installed into each environment, so workers and snapshot
provisioning launch in Python isolated mode (`-I -P`) without inherited
`PYTHONPATH`. Installation seals a canonical receipt containing the runtime
lock, lab wheel, complete installed distribution graph, interpreter digest, and
every runtime-tree byte. Only the three `bin/python*` aliases may be symlinks;
each must resolve to the separately hashed interpreter. Every trial validates
that receipt/tree before and after execution, copies the exact receipt into the
experiment, and binds its separately approved digest in the persisted result.
The trial also copies the complete primary/auxiliary local-source manifest
preimages and Git integrity preimage. Resume, status and blind review
validate those immutable experiment copies after space-driven model
or runtime eviction; they intentionally do not reinterpret an unrelated later
install at the same runtime ID. A new trial always requires a currently sealed
resident tree plus its operator-approved receipt digest. Trial evidence also
inventories every installed distribution before/after inference and retains
`pip freeze --all`.

Inference processes force `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and
`HF_DATASETS_OFFLINE=1`. Model APIs receive verified local paths, not mutable Hub
aliases. Chatterbox's internal S3-tokenizer Hub calls are redirected to the
verified local auxiliary snapshot.

## Exact worker APIs

### Qwen Base through mlx-audio 0.5.0

```python
from mlx_audio.tts.utils import load_model

model = load_model(verified_local_snapshot)
results = list(
    model.generate(
        text=request.text,
        ref_audio=request.reference.path,
        ref_text=request.reference.transcript,
        lang_code="English",
        max_tokens=4096,
        temperature=0.9,
        top_k=50,
        top_p=1.0,
        repetition_penalty=1.5,
        stream=False,
        verbose=False,
    )
)
```

Base is a rapid-cloning model, not a stable named voice. Both reference audio
and its exact transcript are mandatory; `voice="Chelsie"` is not used. A
long-text/tail test is required because mlx-audio issue #882 reports premature
silent EOS/tail truncation.

### Chatterbox Turbo through mlx-audio 0.5.0

```python
model = load_model(verified_local_snapshot)
results = list(
    model.generate(
        text=request.text,
        ref_audio=request.reference.path,
        max_tokens=800,
        temperature=0.8,
        top_k=1000,
        top_p=0.95,
        repetition_penalty=1.2,
        stream=False,
    )
)
```

Turbo's MLX API accepts but does not use `min_p`, `exaggeration`, or
`cfg_weight`; the worker does not advertise them.

### Chatterbox V3 through mlx-audio 0.5.0

```python
results = list(
    model.generate(
        text=request.text,
        ref_audio=request.reference.path,
        lang_code=request.language,
        exaggeration=0.5,
        cfg_weight=0.5,
        temperature=0.8,
        min_p=0.05,
        top_p=1.0,
        repetition_penalty=1.2,
        max_tokens=1000,
        stream=False,
        verbose=False,
    )
)
```

The generic mlx-audio CLI flag is named `--cfg_scale`, but V3's API requires
`cfg_weight`; the worker therefore calls Python directly. V3 ignores MLX stream
flags. Upstream PyTorch V3 custom-reference MPS remains excluded from the green
path pending its float64-conditioning fix.

Official Chatterbox states that its PyTorch output embeds a Perth watermark.
The MLX 0.5.0 Turbo/V3 source invokes no Perth/watermark API. Worker evidence
therefore records `watermarked=false`; it never inherits or claims the official
watermark.

### Official F5-TTS 1.1.22 on MPS

```python
from f5_tts.api import F5TTS

model = F5TTS(
    model="F5TTS_v1_Base",
    ckpt_file=verified_checkpoint,
    vocab_file=verified_vocabulary,
    vocoder_local_path=verified_vocos_snapshot,
    device="mps",
)
wav, sample_rate, spectrogram = model.infer(
    ref_file=request.reference.path,
    ref_text=request.reference.transcript,
    gen_text=request.text,
    nfe_step=32,
    seed=request.seed,
    file_wave=request.output_path,
)
```

Reference text is always supplied. Leaving it blank would cause an implicit
Whisper-large-v3-turbo load/download and violates the curated offline boundary.

### Seed-VC on MPS

The worker imports the pinned checkout's `inference` module and calls its Python
`main(argparse.Namespace(...))` API with exact local `--checkpoint` and
`--config` equivalents. Its derived offline config replaces the mutable
`openai/whisper-small` and NVIDIA BigVGAN names with verified local snapshot
paths; CAMPPlus resolution is similarly redirected locally. Initial smoke
settings are:

The checkout must be detached at the pinned commit. Verification rejects any
worktree/index change, ignored or untracked file, symlink, submodule, non-zero
index stage, or tracked byte that differs from the commit object. Both parent
and isolated worker verify it before use, and the parent verifies it again
after inference; bytecode writes are disabled.

```text
diffusion_steps=4, length_adjust=1.0, inference_cfg_rate=0.7,
f0_condition=false, auto_f0_adjust=false, semi_tone_shift=0, fp16=true
```

Seed receives a separately recorded/performed source WAV and a target reference
WAV. Its prompt text documents the performed source but is not a TTS input, and
Seed results are never pooled into the direct-TTS ranking.

## Fair timing and outputs

Every direct-TTS candidate receives identical held-out prompt text, reference
digest/transcript, language, seed, and timing cells. The persisted plan embeds
the exact prompt suite, canonical suite SHA-256, selected IDs, and an explicit
held-out assertion; strict validation recomputes the digest and candidate
matrices whenever the plan is loaded or run.

- **Cold:** a fresh process; the measured interval begins before verified model
  load and ends after synchronized synthesis.
- **Warm:** still a fresh one-request process; it loads once, performs one
  unmeasured synthesis of that exact request, resets the requested seed, then
  records one synchronized synthesis and exits.
- **Seed-VC:** cold-only, separately reported, because its pinned upstream API
  owns model load inside each conversion call.

Workers retain the engine-native WAV. A separate common normalization copy uses
mono 24 kHz signed 16-bit PCM and one-pass FFmpeg loudness normalization at
-18 LUFS / -1.5 dBTP / LRA 11. Native input is hashed before and after and is
never overwritten.

Each successful result binds:

- reference SHA-256 (checked again after inference) and the reference's subject,
  which must be the pinned `cole-self` (no consent record is read, D-050; a request
  written before names a consent file's digest, which is carried, never read);
- request-bound performed-source SHA-256 and byte count for Seed;
- model ID/revision, code revision, local snapshot-manifest hashes, and settings;
- exact `pip freeze --all` bytes and dependency fingerprint;
- an exact executed-code manifest/digest for the primary distribution and lab
  worker package; Seed also binds its verified checkout integrity digest;
- cold/warm breakdown, output SHA-256/size/PCM inspection, and realized seed.

## Automated smoke gate

Dry tests never import optional runtimes or download weights. An explicitly
enabled real smoke run should assert:

1. the runtime/package and MPS/MLX device probe succeeds;
2. every local artifact matches the curated allow-list, byte count, manifest,
   any supplied upstream hash, and its locally sealed observed hash;
3. output decodes as mono WAV at 24 kHz (direct TTS) or 22,050 Hz (Seed), has
   finite/non-zero samples, non-zero duration/RMS, and no clipping;
4. cold/warm latency, RTF, process RSS, and MLX memory evidence are retained;
5. Qwen passes a long-text tail regression;
6. ASR WER and speaker-embedding cosine are retained as diagnostics only; and
7. a blinded human A/B/X listening pass remains the decision signal.

Only checksum-bound reference recordings of Cole's pinned subject may enter a worker
(D-050).

## Offline audition orchestration

`cole_voice_lab.audition` exposes the CLI-facing, side-effect-bounded workflow:

- `build_audition_plan(...)` creates the fair direct-TTS/Seed-VC matrix;
  `persist_audition_plan(...)` atomically seals its canonical JSON at
  `experiments/<experiment_id>/plan.json` and refuses a different plan under
  the same ID.
- `build_candidate_preparation_plan(...)` and `build_preparation_plans(...)`
  return argv-only provisioning plans. They never install, clone, download, or
  delete. Standard destinations are `runtimes/<runtime_id>`,
  `models/sources/<source_id>`, and, for Seed only,
  `runtimes/source-checkouts/<source_id>`.
- `run_offline_audition(...)` accepts direct-TTS and voice-conversion
  selections independently, invokes one prepared worker subprocess at a time,
  normalizes successful native outputs, and performs no provisioning or
  network access. An asset-root orchestration lock also serializes separate
  audition callers.

Each trial result is atomically written to
`experiments/<experiment_id>/results/<trial_id>.json`; the evolving run state
is atomically written to `run-manifest.json`. Resume accepts an existing result
only when its exact canonical request digest, worker identity, native path, and
normalized path remain bound to the plan. Successes and structured worker or
normalization failures are all durable, so rerunning the same selection is
idempotent rather than destructive.

Resume and status also rehash the result JSON, native/normalized WAVs,
dependency freeze, model evidence, executed-code evidence, and the copied
primary/auxiliary source-manifest and Seed Git-integrity preimages before a
result can be skipped or counted complete. They intentionally do not consult a
later resident cache or checkout at the same ID. Each run pointer binds the
exact result JSON SHA-256.
Persisted worker/model and executed-code records are parsed through strict v1
contracts: the latter requires non-empty lab-worker and installed-distribution
byte inventories plus its internal integrity digest. Validation cross-binds the
candidate/model/code revisions, model and auxiliary manifests, executed-code
path/digest, reference/performed-source hashes (and a consent digest, only in
evidence written before D-050), settings, runtime
package/version, and applicable watermark flag across the request, worker
result, and evidence files. Evidence parsing and its outer SHA-256 check use the
same bounded file read.

Before persistence or execution, path confinement requires exact native and
normalized destinations under that experiment, references under `references`,
consent paths (only in a plan written before D-050) under `provenance/consents`, and
performed sources within the private
asset root. Existing symlink components are rejected.

Seed-VC plans additionally hash the performed source at plan construction and
place both `source_audio_sha256` and `source_audio_size_bytes` in the worker
request. The worker compares both values before and after conversion. This
keeps the separately ranked VC track reproducible without treating its source
performance as direct-TTS prompt input.
