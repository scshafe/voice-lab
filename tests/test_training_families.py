"""Training model families: the adapter port, F5-TTS's adapter and stored-record compatibility
(training.py, finetune.py, D-049).

Nothing trains here: runs are written by ``finetune_speech_runs`` (synthetic clips, a few bytes
of checkpoint), and a second family is a double registered for one test, whose adapter writes
synthetic records only. ``pre_model_families_records`` holds an F5-TTS run, its dataset,
corpus and samples record as the code before model families (c43d6ba) wrote them.
"""

from __future__ import annotations

import json
import re
import wave
from datetime import timedelta
from pathlib import Path
from typing import Any

import pre_model_families_records as golden
import pytest
from pydantic import BaseModel, ValidationError
from speech_fixtures import START, finished_run
from test_pilot_v2_coverage import SUBJECT, released_pilot_v2

from cole_voice_lab import finetune, training
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
    FineTuneRuntimeIdentity,
    FineTuneSampleSet,
    finetune_run_summary,
    finished_finetune_run_ids,
    load_finetune_dataset,
    require_f5_dataset,
    resolve_speech_model,
    run_checkpoints,
    run_finetune_pipeline,
)
from cole_voice_lab.library_classification import (
    PINNED_WEIGHT_FILES,
    classify,
    pinned_weight_rule_name,
)
from cole_voice_lab.models import F5_FAMILY
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import FineTuneRunSummary
from cole_voice_lab.recording_finetune_jobs import FineTuneLogTail
from cole_voice_lab.speech import families as speech_families
from cole_voice_lab.speech.contracts import F5ReleaseModel, F5RenderSettings
from cole_voice_lab.training import (
    F5_TRAINING,
    F5ListeningSettings,
    F5TrainingAdapter,
    FineTuneTrainingProgress,
    ListeningSamples,
    UnknownTrainingFamily,
    adapter_for,
    hyperparameters_family,
    require_trains,
    run_family,
    training_adapter,
    training_family_ids,
)


class NoSettings(BaseModel):
    """A family's listening settings that has none."""


SOURCE = Path(__file__).parents[1] / "src" / "cole_voice_lab"


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
    with pytest.raises(UnknownTrainingFamily, match="cannot train piper"):
        require_trains(F5_TRAINING, other)
    with pytest.raises(UnknownTrainingFamily, match="does not train"):
        require_trains(F5_TRAINING, FineTuneHyperparameters.model_construct(model_name="x"))
    with pytest.raises(FineTuneError, match="newline"):
        F5_TRAINING.normalise_text("two\nlines")
    assert F5_TRAINING.normalise_text("As written, kept.") == "As written, kept."
    progress = F5_TRAINING.parse_progress("Epoch 3/9: [00:10<00:20, loss=0.5, update=42]")
    assert progress is not None and progress.update == 42


@pytest.mark.parametrize(
    ("stored", "model"),
    [
        (golden.RUN, FineTuneRun),
        (golden.DATASET, FineTuneDataset),
        (golden.CORPUS, FineTuneCorpus),
        (golden.SAMPLES, FineTuneSampleSet),
    ],
)
def test_records_written_before_model_families_reserialize_byte_for_byte(
    stored: bytes, model: type[BaseModel]
) -> None:
    # Written by the code before model families (c43d6ba), never by this code.
    assert canonical_json_bytes(model.model_validate_json(stored)) + b"\n" == stored


def test_the_golden_run_binds_its_golden_dataset_and_corpus() -> None:
    run = FineTuneRun.model_validate_json(golden.RUN)
    dataset = FineTuneDataset.model_validate_json(golden.DATASET)
    corpus = FineTuneCorpus.model_validate_json(golden.CORPUS)
    assert canonical_sha256(dataset) == run.dataset_sha256
    assert canonical_sha256(corpus) == run.corpus_sha256 == dataset.corpus_sha256
    assert run_family(run) == "f5-tts" and isinstance(run.runtime, FineTuneRuntimeIdentity)
    summary = FineTuneRunSummary.model_validate(
        {
            "run_id": run.run_id,
            "run_root": run.run_root,
            "status": "finished",
            "started_at": "2026-09-07T12:00:00Z",
            "finished_at": "2026-09-07T12:10:00Z",
            "epochs": 3,
            "prompt_count": 2,
            "retained_seconds": 1.0,
        },
        strict=False,
    )
    assert summary.model_family == "f5-tts"  # a summary without the field reads as before


def test_a_stored_f5_run_reads_and_reserializes_exactly_as_before(tmp_path: Path) -> None:
    # A run written by today's F5-TTS code, read again.
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
    # A run's hyperparameters and runtime are one family's.
    mixed = {**record, "runtime": {**record["runtime"], "family": "double"}}
    with pytest.raises(ValueError, match="that family's runtime"):
        FineTuneRun.model_validate_json(json.dumps(mixed))
    f5_runtime = json.loads(golden.RUN)["runtime"]
    with pytest.raises(ValueError, match="that family's runtime"):
        FineTuneRun.model_validate_json(json.dumps({**record, "runtime": f5_runtime}))
    # F5-TTS is never spelled as "another family".
    with pytest.raises(ValueError, match="FineTuneHyperparameters"):
        FamilyHyperparameters(family="f5-tts", model_name="x", epochs=1, learning_rate=0.1)
    with pytest.raises(ValueError, match="FineTuneRuntimeIdentity"):
        FamilyRuntimeIdentity.model_validate({**record["runtime"], "family": "f5-tts"})


class DoubleAdapter:
    """A second family's adapter: every step writes synthetic records and says it ran."""

    family_id = "double"
    model_names = frozenset({"double-base"})
    dataset_sample_rate_hz = 22_050
    pinned_weight_pattern = "pretrained_*.ckpt"

    def __init__(self, layout: AssetLayout) -> None:
        self.layout = layout
        self.steps: list[str] = []

    def hyperparameters(self, options: Any) -> FamilyHyperparameters:
        return FamilyHyperparameters(
            family="double", model_name="double-base", epochs=3, learning_rate=0.001, **options
        )

    def listening_settings(self, options: Any) -> BaseModel:
        if options:
            raise ValidationError.from_exception_data("DoubleListening", [])
        return NoSettings()

    def normalise_text(self, text: str) -> str:
        return text.lower()

    def describe_runtime(self, runtime_root: Path, *, digests: Any) -> Any:
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

    def listening_samples(self, *args: Any, settings: BaseModel, **kwargs: Any) -> ListeningSamples:
        self.steps.append("samples")  # no reference clip: this family chooses none
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
    hyperparameters = double.hyperparameters({})
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
    host = json.loads(
        (layout.root / FINETUNE_OUTPUT_ROOT / "double-a/training-host.json").read_text()
    )
    assert host["schema_version"] == "cole-voice-lab.host-fingerprint/v1"


def test_a_family_mismatch_fails_before_the_corpus_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    double = DoubleAdapter(layout)
    monkeypatch.setattr(training, "_registry", lambda: {"f5-tts": F5_TRAINING, "double": double})

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the corpus was selected")

    monkeypatch.setattr(finetune, "select_finetune_corpus", never)

    def pipeline(hyperparameters: Any, **options: Any) -> Any:
        return run_finetune_pipeline(
            layout,
            released_pilot_v2(),
            SUBJECT,
            tmp_path / "runtime",
            hyperparameters,
            run_id="mixed",
            allow_missing=True,
            **options,
        )

    with pytest.raises(UnknownTrainingFamily, match="cannot train double"):
        pipeline(double.hyperparameters({}), adapter=F5_TRAINING)
    with pytest.raises(UnknownTrainingFamily, match="cannot train f5-tts"):
        pipeline(FineTuneHyperparameters(), adapter=double)
    with pytest.raises(ValidationError):  # F5-TTS's listening options for another family
        pipeline(double.hyperparameters({}), listening={"nfe_steps": 8})
    with pytest.raises(ValidationError):
        pipeline(FineTuneHyperparameters(), listening={"device": "tpu"})
    assert double.steps == [] and not (layout.root / FINETUNE_OUTPUT_ROOT / "mixed").exists()


def test_f5_settings_are_built_by_its_adapter() -> None:
    assert F5_TRAINING.hyperparameters({}) == FineTuneHyperparameters()
    assert F5_TRAINING.hyperparameters({"epochs": 7}).epochs == 7
    with pytest.raises(ValidationError):
        F5_TRAINING.hyperparameters({"no_such_option": 1})
    settings = F5_TRAINING.listening_settings({"device": "cuda", "nfe_steps": 16})
    assert (settings.device, settings.nfe_steps, settings.reference_prompt_id) == ("cuda", 16, None)
    assert F5_TRAINING.listening_settings({}) == F5ListeningSettings()
    with pytest.raises(UnknownTrainingFamily):
        F5_TRAINING.listening_samples(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            "run",
            texts=("x",),
            settings=NoSettings(),
            digests=None,  # type: ignore[arg-type]
        )


def test_f5_trains_only_a_24_khz_dataset(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    finished_run(layout)
    dataset, _ = load_finetune_dataset(layout, f"{FINETUNE_OUTPUT_ROOT}/finetune-a")
    require_f5_dataset(layout, dataset)  # what F5-TTS's dataset step writes
    with pytest.raises(FineTuneError, match="22050 Hz") as refused:
        require_f5_dataset(layout, dataset.model_copy(update={"sample_rate_hz": 22_050}))
    assert refused.value.code == "FINETUNE.SOURCE_CHANGED"
    one = dataset.clips[0]
    relabelled = dataset.model_copy(
        update={"clips": (one.model_copy(update={"sample_rate_hz": 16_000}), *dataset.clips[1:])}
    )
    with pytest.raises(FineTuneError, match=f"{one.relative_path} is 16000 Hz"):
        require_f5_dataset(layout, relabelled)
    # A clip whose record says 24 kHz but whose header does not.
    path = layout.root / dataset.dataset_root / dataset.clips[1].relative_path
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(b"\0\0" * 10)
    with pytest.raises(FineTuneError, match="is 44100 Hz"):
        require_f5_dataset(layout, dataset)
    path.write_bytes(b"not a wav")
    with pytest.raises(FineTuneError, match=dataset.clips[1].relative_path):
        require_f5_dataset(layout, dataset)
    # The trainer refuses it before it writes anything.
    with pytest.raises(FineTuneError):
        F5_TRAINING.train(
            layout,
            dataset,
            None,  # type: ignore[arg-type]
            FineTuneHyperparameters(),
            f"{FINETUNE_OUTPUT_ROOT}/refused",
            run_id="refused",
            subject_id=SUBJECT,
            catalog_sha256="a" * 64,
        )
    assert not (layout.root / FINETUNE_OUTPUT_ROOT / "refused").exists()


def test_every_training_family_has_a_speech_family_and_one_f5_id() -> None:
    assert set(training_family_ids()) == set(speech_families.family_ids())
    assert F5RenderSettings(nfe_steps=1).family == F5_FAMILY
    assert F5ReleaseModel.model_fields["family"].default == F5_FAMILY
    assert F5_TRAINING.family_id == F5_FAMILY
    defined = [
        str(path.relative_to(SOURCE))
        for path in SOURCE.rglob("*.py")
        if re.search(r"^F5_FAMILY\b.*=", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert defined == ["models.py"]


def test_each_familys_pinned_weights_are_classified_as_it_declares() -> None:
    assert set(PINNED_WEIGHT_FILES) == set(training_family_ids())
    for family in training_family_ids():
        pattern = training_adapter(family).pinned_weight_pattern
        assert PINNED_WEIGHT_FILES[family] == pattern
        example = f"derived/finetune/run-1/checkpoints/{pattern.replace('*', 'model_1')}"
        rule = classify(example).rule
        assert (rule.name, rule.transfer) == (pinned_weight_rule_name(family), "pin")
    # F5-TTS keeps its rule exactly as it was.
    assert pinned_weight_rule_name("f5-tts") == "fine-tune-pretrained-link"
    assert PINNED_WEIGHT_FILES["f5-tts"] == "pretrained_*.safetensors"
    assert classify("derived/finetune/run-1/checkpoints/pretrained_x.ckpt").rule.name == (
        "fine-tune-run"
    )
    assert pinned_weight_rule_name("piper") == "fine-tune-pretrained-link-piper"
    assert F5TrainingAdapter().pretrained_pin is None


def write_family_run(layout: AssetLayout, run_id: str, minutes: int) -> None:
    """A finished run of the double family: an F5-TTS run's files with another family's
    hyperparameters and runtime."""
    run = finished_run(layout, run_id, finished_at=START + timedelta(minutes=minutes))
    other = run.model_copy(
        update={
            "hyperparameters": FamilyHyperparameters(
                family="double", model_name="double-base", epochs=3, learning_rate=0.001
            ),
            "runtime": FamilyRuntimeIdentity(
                family="double",
                runtime_root="/synthetic/double",
                python_sha256="1" * 64,
                packages={"double": "0.1"},
                pretrained_path="/synthetic/double/pretrained.ckpt",
                pretrained_sha256="2" * 64,
            ),
        }
    )
    write_canonical_json(layout.root / FINETUNE_OUTPUT_ROOT / run_id / "run.json", other)


def test_speak_renders_only_f5_runs_and_summaries_name_their_family(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    finished_run(layout, "finetune-a")  # finished at minute 10
    write_family_run(layout, "double-b", 20)  # newer, of a family Speak cannot render
    assert finished_finetune_run_ids(layout) == ("double-b", "finetune-a")
    assert finished_finetune_run_ids(layout, families=frozenset({"f5-tts"})) == ("finetune-a",)
    for model in ("latest", "base"):
        assert resolve_speech_model(layout, model).reference_run.run_id == "finetune-a"
    with pytest.raises(FineTuneError, match="double run; Speak renders F5-TTS") as refused:
        resolve_speech_model(layout, "double-b")
    assert refused.value.code == "FINETUNE.NO_FINISHED_RUN"
    summary = finetune_run_summary(layout, f"{FINETUNE_OUTPUT_ROOT}/double-b")
    assert summary.model_family == "double"
    assert finetune_run_summary(layout, f"{FINETUNE_OUTPUT_ROOT}/finetune-a").model_family == (
        "f5-tts"
    )
    only_other = AssetLayout.initialize(tmp_path / "other")
    write_family_run(only_other, "double-c", 5)
    with pytest.raises(FineTuneError, match="no fine-tuned model has finished"):
        resolve_speech_model(only_other, "latest")


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
