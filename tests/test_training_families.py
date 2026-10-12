"""Training model families: the adapter port, F5-TTS's adapter and stored-record compatibility
(training.py, finetune.py, D-048).

Nothing trains here: runs are written by ``finetune_speech_runs`` (synthetic clips, a few bytes
of checkpoint), and a second family is a double registered for one test, whose adapter writes
synthetic records only.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from speech_fixtures import START, finished_run
from test_pilot_v2_coverage import SUBJECT, released_pilot_v2

from cole_voice_lab import training
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, write_canonical_json
from cole_voice_lab.finetune import (
    FINETUNE_OUTPUT_ROOT,
    FamilyHyperparameters,
    FamilyRuntimeIdentity,
    FineTuneCheckpoint,
    FineTuneCorpus,
    FineTuneDataset,
    FineTuneError,
    FineTuneHyperparameters,
    FineTuneRun,
    FineTuneSampleSet,
    load_finetune_dataset,
    run_checkpoints,
    run_finetune_pipeline,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_finetune_jobs import FineTuneLogTail
from cole_voice_lab.training import (
    F5_TRAINING,
    FineTuneTrainingProgress,
    ListeningSamples,
    UnknownTrainingFamily,
    adapter_for,
    hyperparameters_family,
    run_family,
    training_adapter,
    training_family_ids,
)


def test_f5_tts_is_the_one_trainable_family() -> None:
    assert training_family_ids() == ("f5-tts",)
    assert training_adapter("f5-tts") is F5_TRAINING
    assert adapter_for(FineTuneHyperparameters()) is F5_TRAINING
    assert F5_TRAINING.dataset_sample_rate_hz == 24_000
    assert F5_TRAINING.model_names == frozenset({"F5TTS_v1_Base"})
    with pytest.raises(UnknownTrainingFamily):
        training_adapter("piper")
    other = FamilyHyperparameters(
        family="piper", model_name="vits-medium", epochs=2, learning_rate=1e-4
    )
    assert hyperparameters_family(other) == "piper"
    with pytest.raises(UnknownTrainingFamily):
        adapter_for(other)
    with pytest.raises(FineTuneError, match="newline"):
        F5_TRAINING.normalise_text("two\nlines")
    assert F5_TRAINING.normalise_text("As written, kept.") == "As written, kept."
    progress = F5_TRAINING.parse_progress("Epoch 3/9: [00:10<00:20, loss=0.5, update=42]")
    assert progress is not None and progress.update == 42


def test_a_stored_f5_run_reads_and_reserializes_exactly_as_before(tmp_path: Path) -> None:
    # A run written by today's F5-TTS code: its bytes are what every stored run holds.
    layout = AssetLayout.initialize(tmp_path / "lab")
    run = finished_run(layout)
    run_json = layout.root / FINETUNE_OUTPUT_ROOT / run.run_id / "run.json"
    stored = run_json.read_bytes()
    parsed = FineTuneRun.model_validate_json(stored)
    assert canonical_json_bytes(parsed) + b"\n" == stored
    assert isinstance(parsed.hyperparameters, FineTuneHyperparameters)
    assert run_family(parsed) == "f5-tts"
    dataset, corpus = load_finetune_dataset(layout, f"{FINETUNE_OUTPUT_ROOT}/{run.run_id}")
    assert canonical_sha256(dataset) == parsed.dataset_sha256
    assert canonical_sha256(corpus) == parsed.corpus_sha256
    dataset_json = layout.root / dataset.dataset_root / "dataset.json"
    assert canonical_json_bytes(dataset) + b"\n" == dataset_json.read_bytes()
    assert dataset.sample_rate_hz == 24_000 and all(
        clip.sample_rate_hz == 24_000 for clip in dataset.clips
    )
    assert run_checkpoints(layout, f"{FINETUNE_OUTPUT_ROOT}/{run.run_id}") == run.checkpoints
    assert run_checkpoints(layout, f"{FINETUNE_OUTPUT_ROOT}/nowhere") == ()


def test_another_familys_run_names_its_family_and_runtime() -> None:
    record = {
        "run_id": "piper-a",
        "subject_id": SUBJECT,
        "catalog_sha256": "a" * 64,
        "corpus_sha256": "b" * 64,
        "dataset_sha256": "c" * 64,
        "hyperparameters": {
            "schema_version": "cole-voice-lab.training-hyperparameters/v1",
            "family": "piper",
            "model_name": "vits-medium",
            "epochs": 300,
            "learning_rate": 0.0001,
            "settings": {"batch_size": 16, "quality": "medium"},
        },
        "runtime": {
            "family": "piper",
            "runtime_root": "/data/voice-lab/lab/runtimes/piper",
            "python_sha256": "d" * 64,
            "packages": {"piper-train": "1.0.0", "torch": "2.14.0"},
            "pretrained_path": "/data/voice-lab/lab/runtimes/piper/pretrained/x.ckpt",
            "pretrained_sha256": "e" * 64,
            "device": "RX 6900 XT (gfx1030)",
        },
        "run_root": "derived/finetune/piper-a",
        "log_path": "derived/finetune/piper-a/train.log",
        "checkpoints": [],
        "status": "running",
        "started_at": "2026-10-12T12:00:00Z",
    }
    run = FineTuneRun.model_validate_json(json.dumps(record))
    assert isinstance(run.runtime, FamilyRuntimeIdentity) and run.runtime.device
    assert run_family(run) == "piper"
    # An F5-TTS identity with a family key is neither shape: refused, never guessed.
    f5_like = {**record, "runtime": {**record["runtime"], "f5_tts_version": "1"}}
    with pytest.raises(ValueError):
        FineTuneRun.model_validate_json(json.dumps(f5_like))


class DoubleAdapter:
    """A second family's adapter: every step writes synthetic records and says it ran."""

    family_id = "double"
    model_names = frozenset({"double-base"})
    dataset_sample_rate_hz = 22_050

    def __init__(self, layout: AssetLayout) -> None:
        self.layout = layout
        self.steps: list[str] = []

    def normalise_text(self, text: str) -> str:
        return text.lower()

    def describe_runtime(self, runtime_root: Path, **_: Any) -> Any:
        self.steps.append("runtime")
        return self

    @property
    def pretrained_path(self) -> Path:
        return Path("/nowhere/pretrained.ckpt")

    def runtime_link(self, runtime: Any, run_id: str) -> Path | None:
        return None

    def release_runtime_link(self, link: Path, checkpoints: Path) -> bool:
        return False

    def build_dataset(
        self, layout: AssetLayout, corpus: FineTuneCorpus, destination: str, *, ffmpeg: Path
    ) -> FineTuneDataset:
        self.steps.append("dataset")
        source, _ = load_finetune_dataset(layout, f"{FINETUNE_OUTPUT_ROOT}/finetune-a")
        dataset = source.model_copy(
            update={
                "dataset_root": destination,
                "sample_rate_hz": self.dataset_sample_rate_hz,
                "clips": tuple(
                    clip.model_copy(
                        update={
                            "sample_rate_hz": self.dataset_sample_rate_hz,
                            "text": self.normalise_text(clip.text),
                        }
                    )
                    for clip in source.clips
                ),
            }
        )
        write_canonical_json(layout.root / destination / "dataset.json", dataset)
        return dataset

    def train(
        self,
        layout: AssetLayout,
        dataset: FineTuneDataset,
        runtime: Any,
        hyperparameters: Any,
        run_root: str,
        **fields: Any,
    ) -> FineTuneRun:
        self.steps.append("train")
        return FineTuneRun(
            run_id=fields["run_id"],
            subject_id=fields["subject_id"],
            catalog_sha256=fields["catalog_sha256"],
            corpus_sha256=dataset.corpus_sha256,
            dataset_sha256=canonical_sha256(dataset),
            hyperparameters=hyperparameters,
            runtime=FamilyRuntimeIdentity(
                family="double",
                runtime_root="/nowhere",
                python_sha256="1" * 64,
                packages={"double": "0.1"},
                pretrained_path="/nowhere/pretrained.ckpt",
                pretrained_sha256="2" * 64,
            ),
            run_root=run_root,
            log_path=f"{run_root}/train.log",
            checkpoints=(
                FineTuneCheckpoint(
                    relative_path=f"{run_root}/checkpoints/last.ckpt",
                    sha256="3" * 64,
                    size_bytes=1,
                    update=0,
                    kind="last",
                ),
            ),
            status="finished",
            started_at=START,
            finished_at=START + timedelta(minutes=5),
        )

    def checkpoints(self, layout: AssetLayout, run_root: str) -> tuple[FineTuneCheckpoint, ...]:
        return ()

    def parse_progress(self, text: str) -> FineTuneTrainingProgress | None:
        return None

    def listening_samples(self, *args: Any, **kwargs: Any) -> ListeningSamples:
        self.steps.append("samples")
        empty = FineTuneSampleSet.model_construct(samples=())
        return ListeningSamples(finetuned=empty, base=empty)


def test_the_pipeline_trains_another_family_through_its_adapter_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    finished_run(layout)
    double = DoubleAdapter(layout)
    monkeypatch.setattr(training, "_registry", lambda: {"f5-tts": F5_TRAINING, "double": double})
    stages: list[str] = []
    hyperparameters = FamilyHyperparameters(
        family="double", model_name="double-base", epochs=3, learning_rate=0.001
    )
    result = run_finetune_pipeline(
        layout,
        released_pilot_v2(),
        SUBJECT,
        tmp_path / "no-f5-runtime",  # never described: the double's runtime is its own
        hyperparameters,
        run_id="double-a",
        allow_missing=True,
        progress=lambda step: stages.append(step.stage),
    )
    assert double.steps == ["runtime", "dataset", "train", "samples"]
    assert stages == ["selecting", "dataset", "training", "sampling"]
    assert run_family(result.run) == "double" and result.dataset.sample_rate_hz == 22_050
    assert all(clip.text == clip.text.lower() for clip in result.dataset.clips)


def test_the_log_tail_reads_progress_with_the_familys_parser(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    log = layout.root / FINETUNE_OUTPUT_ROOT / "run-a" / "train.log"
    log.parent.mkdir(parents=True)
    log.write_text("step 7 of 9\n")
    seen: list[str] = []

    def parse(text: str) -> FineTuneTrainingProgress | None:
        seen.append(text)
        return FineTuneTrainingProgress(epoch=1, epochs=1, update=7, loss=None, eta_seconds=None)

    progress = FineTuneLogTail(layout, "run-a", parse=parse).read()
    assert progress is not None and progress.update == 7 and seen == ["step 7 of 9\n"]
