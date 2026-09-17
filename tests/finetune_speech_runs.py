"""Finished fine-tune runs written directly, for Speak tests and the native ``speak`` suite.

``write_speech_run`` binds a run directory the way a trained run is bound (dataset clips,
``corpus.json``, ``dataset.json``, a checkpoint and ``run.json``) without training anything,
so Speak can resolve models and reference clips against it. The clips are 24 kHz 16-bit mono
WAV files with a fixed nonzero pattern; the checkpoint is a few bytes, which the stub runtime
(``finetune_stub_runtime.py``) accepts. ``sized_catalog`` builds a catalog as large as the
published corpus (1,886 prompts), so status tests can write runs of realistic size.
"""

from __future__ import annotations

import wave
from datetime import datetime
from pathlib import Path

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.finetune import (
    FINETUNE_DEFAULT_SAMPLE_TEXTS,
    FINETUNE_OUTPUT_ROOT,
    FineTuneCheckpoint,
    FineTuneCorpus,
    FineTuneCorpusEntry,
    FineTuneDataset,
    FineTuneDatasetClip,
    FineTuneHyperparameters,
    FineTuneRun,
    FineTuneRuntimeIdentity,
    FineTuneSample,
    FineTuneSampleSet,
)
from cole_voice_lab.models import Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference

CLIP_FRAMES = 2_400
# The published cole-corpus catalog: pilot prompts plus the Harvard and CMU ARCTIC sentences.
PUBLISHED_CORPUS_PROMPTS = 1_886


def sized_catalog(prompt_count: int = PUBLISHED_CORPUS_PROMPTS) -> PromptSuite:
    """A capture catalog of ``prompt_count`` spoken sentences of ordinary length.

    The first two prompts are reference candidates (``sentence-0000``, ``sentence-0001``).
    """

    return PromptSuite(
        suite_id="sized-corpus",
        purpose="capture-corpus",
        prompts=tuple(
            Prompt(
                prompt_id=f"sentence-{index:04d}",
                text=(
                    f"Sentence {index} is read at an easy pace, the way the published corpus "
                    "lists its Harvard and ARCTIC lines."
                ),
                category="reference" if index < 2 else "neutral",
                style="plain reading",
                reference_candidate=index < 2,
                expected_seconds=4.0,
            )
            for index in range(prompt_count)
        ),
    )


def write_wav(path: Path, frames: int = CLIP_FRAMES) -> None:
    """A private 24 kHz 16-bit mono clip of ``frames`` frames."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(bytes(index % 251 for index in range(frames * 2)))
    path.chmod(0o600)


def corpus_entry(prompt: Prompt) -> FineTuneCorpusEntry:
    """A corpus entry for ``prompt`` as if a native take of it had been selected."""

    return FineTuneCorpusEntry(
        prompt_id=prompt.prompt_id,
        prompt_sha256=canonical_sha256(prompt),
        recorded_prompt_sha256=canonical_sha256(prompt),
        prompt=prompt,
        take_id=f"take-{prompt.prompt_id}",
        source_kind="native",
        source_path=f"tmp/recording-takes/take-{prompt.prompt_id}/original.wav",
        source_sha256="0" * 64,
        source_frames=48_000,
        source_sample_rate_hz=48_000,
        recorded_at=None,
        date_precision="unknown",
    )


def write_speech_run(
    layout: AssetLayout,
    run_id: str,
    *,
    catalog: PromptSuite,
    subject_id: str,
    started_at: datetime,
    finished_at: datetime | None,
    entries: tuple[str, ...],
    clips: tuple[str, ...],
    status: str = "finished",
    checkpoint: bool = True,
    catalog_reference: CatalogReference | None = None,
    samples: bool = False,
) -> FineTuneRun:
    """Write ``derived/finetune/<run_id>`` as a trained run of ``catalog``'s prompts.

    Params: ``entries`` are the corpus's prompt ids (catalog order) and ``clips`` those with a
    dataset clip; ``finished_at`` is ``None`` only for a running run; ``checkpoint=False``
    leaves the run without weights; ``samples`` also writes the fine-tuned and base
    listening-sample records (``samples/<label>/samples.json``, three samples each; the
    sample audio itself is not written).
    Returns: the run record written.
    Raises: ``StopIteration`` when an entry names no prompt of ``catalog``;
    ``pydantic.ValidationError`` for an inconsistent run.
    """

    run_root = f"{FINETUNE_OUTPUT_ROOT}/{run_id}"
    directory = layout.root / run_root
    prompts = {prompt.prompt_id: prompt for prompt in catalog.prompts}
    corpus = FineTuneCorpus(
        subject_id=subject_id,
        catalog_suite_id=catalog.suite_id,
        catalog_sha256=canonical_sha256(catalog),
        catalog_reference=catalog_reference,
        entries=tuple(corpus_entry(prompts[prompt_id]) for prompt_id in entries),
        missing_prompt_ids=(),
        unknown_record_ids=(),
        selected_at=started_at,
    )
    texts = {entry.prompt_id: entry.text for entry in corpus.entries}
    dataset_clips = []
    for prompt_id in clips:
        path = directory / "dataset" / "wavs" / f"{prompt_id}.wav"
        write_wav(path)
        dataset_clips.append(
            FineTuneDatasetClip(
                prompt_id=prompt_id,
                take_id=f"take-{prompt_id}",
                relative_path=f"wavs/{prompt_id}.wav",
                sha256=sha256_file(path)[0],
                frames=CLIP_FRAMES,
                gain_db=0.0,
                text=texts[prompt_id],
            )
        )
    dataset = FineTuneDataset(
        corpus_sha256=canonical_sha256(corpus),
        dataset_root=f"{run_root}/dataset",
        normalization_rule="synthetic",
        clips=tuple(dataset_clips),
        metadata_csv_sha256="4" * 64,
        materialized_at=started_at,
    )
    write_canonical_json(directory / "dataset" / "corpus.json", corpus)
    write_canonical_json(directory / "dataset" / "dataset.json", dataset)
    checkpoints: tuple[FineTuneCheckpoint, ...] = ()
    if checkpoint:
        weights = directory / "checkpoints" / "model_last.pt"
        weights.parent.mkdir(mode=0o700)
        weights.write_bytes(f"fine-tuned weights of {run_id}".encode())
        digest, size = sha256_file(weights)
        checkpoints = (
            FineTuneCheckpoint(
                relative_path=f"{run_root}/checkpoints/model_last.pt",
                sha256=digest,
                size_bytes=size,
                update=0,
                kind="last",
            ),
        )
    run = FineTuneRun(
        run_id=run_id,
        subject_id=subject_id,
        catalog_sha256=corpus.catalog_sha256,
        corpus_sha256=canonical_sha256(corpus),
        dataset_sha256=canonical_sha256(dataset),
        hyperparameters=FineTuneHyperparameters(epochs=3),
        runtime=FineTuneRuntimeIdentity(
            runtime_root="/synthetic/runtime",
            python_sha256="5" * 64,
            f5_tts_version="stub",
            torch_version="stub",
            pretrained_path="/synthetic/runtime/pretrained/model.safetensors",
            pretrained_sha256="6" * 64,
        ),
        run_root=run_root,
        log_path=f"{run_root}/train.log",
        checkpoints=checkpoints,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        error="stopped" if status == "failed" else None,
    )
    if samples:
        for label in ("finetuned", "base"):
            write_canonical_json(
                directory / "samples" / label / "samples.json",
                FineTuneSampleSet(
                    label=label,
                    checkpoint_path=f"/synthetic/{label}/model.pt",
                    checkpoint_sha256="7" * 64,
                    use_ema=True,
                    nfe_steps=32,
                    device="mps",
                    reference_prompt_id=dataset.clips[0].prompt_id,
                    reference_audio_sha256=dataset.clips[0].sha256,
                    samples=tuple(
                        FineTuneSample(
                            index=index,
                            relative_path=f"{run_root}/samples/{label}/sample-{index:02d}.wav",
                            sha256="8" * 64,
                            seconds=4.5,
                            text=text,
                        )
                        for index, text in enumerate(FINETUNE_DEFAULT_SAMPLE_TEXTS, start=1)
                    ),
                    created_at=finished_at or started_at,
                ),
            )
    write_canonical_json(directory / "run.json", run)
    return run
